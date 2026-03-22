# -*- coding: utf-8 -*-
"""
交易模拟引擎 — 借鉴 self-backtest-system 的成交模拟 + 风控出场设计

核心理念:
  1. Stop-Entry 成交模型: 信号 T 日触发，T+1 确认执行
  2. 无未来函数: 入场仅使用 T-1 及更早数据，T 日仅确认执行
  3. 不可成交过滤: 零成交量 / 一字板
  4. 风控出场: 止损 / 移动止盈 / 时间止损
  5. 交易成本: 佣金 + 印花税 + 滑点
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


# ─────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────

@dataclass
class SimConfig:
    """交易模拟参数（全部可由前端传入覆盖）"""
    commission: float = 0.00025       # 佣金 0.025% (双边)
    tax: float = 0.0005               # 印花税 0.05% (卖出)
    slippage: float = 0.001           # 滑点 0.1%
    stop_loss_pct: float = 5.0        # 止损 5%
    trail_stop_pct: float = 50.0      # 移动止盈: 最高浮盈回撤 50% 触发
    max_hold_days: int = 20           # 最大持仓天数
    initial_capital: float = 100000   # 初始资金
    # Phase 3: 高级出场
    take_profit_pct: float = 0.0      # 固定止盈% (0=禁用)
    ma_exit_period: int = 0           # 均线离场周期 (0=禁用)
    profit_drawdown_pct: float = 0.0  # 利润回撤% (0=禁用)
    batch_exit_enabled: bool = False  # 分批出场
    batch_exit_ratios: list = field(default_factory=lambda: [0.5, 0.5])
    batch_exit_targets: list = field(default_factory=lambda: [5.0, 10.0])


@dataclass
class TradeRecord:
    """单笔交易记录"""
    symbol: str
    signal_type: str
    signal_group: str                  # "macd" / "czsc"
    signal_date: str                   # 信号触发日 (T)
    signal_confidence: float
    entry_date: str | None = None      # 实际入场日 (T+1)
    entry_price: float | None = None
    fill_type: str | None = None       # "open_fill" / "trigger_fill" / "unfilled"
    exit_date: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None     # "stop_loss" / "trail_stop" / "time_exit" / "signal_exit"
    return_pct: float | None = None
    net_return_pct: float | None = None
    holding_days: int | None = None
    cost_pct: float = 0.0             # 总交易成本占比
    mfe_pct: float = 0.0              # 最大有利波动
    mae_pct: float = 0.0              # 最大不利波动
    skip_reason: str | None = None     # "unfilled" / "zero_volume" / "locked_bar" / "insufficient_data"


@dataclass
class SimResult:
    """模拟总结果"""
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)  # [{time, value}]
    kpi: dict = field(default_factory=dict)
    skip_reasons: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────
# 成交模拟 (Fill Engine)
# ─────────────────────────────────────────────────────

def _is_locked_bar(bar: pd.Series) -> bool:
    """一字板检测: open == high == low == close"""
    return (
        float(bar["open"]) == float(bar["high"]) ==
        float(bar["low"]) == float(bar["close"])
    )


def _is_zero_volume(bar: pd.Series) -> bool:
    """零成交量检测"""
    vol = bar.get("vol", bar.get("volume", 0))
    if pd.isna(vol):
        return True
    return float(vol) <= 0


def fill_entry(
    df: pd.DataFrame,
    sig_idx: int,
    signal_price: float,
    config: SimConfig,
) -> tuple[float | None, str | None, str | None]:
    """
    T+1 日成交模拟 (借鉴 self-backtest-system 的 stop-entry 模型)

    Returns:
        (entry_price, fill_type, skip_reason)
        - entry_price: 实际成交价 (含滑点), None 表示未成交
        - fill_type: "open_fill" / "trigger_fill"
        - skip_reason: "insufficient_data" / "zero_volume" / "locked_bar" / "unfilled"
    """
    # T+1 bar 是否存在
    next_idx = sig_idx + 1
    if next_idx >= len(df):
        return None, None, "insufficient_data"

    bar = df.iloc[next_idx]

    # 不可成交过滤
    if _is_zero_volume(bar):
        return None, None, "zero_volume"
    if _is_locked_bar(bar):
        return None, None, "locked_bar"

    day_open = float(bar["open"])
    day_high = float(bar["high"])
    trigger_price = signal_price

    # Stop-Entry 模型 (做多方向)
    if day_open >= trigger_price:
        # 开盘价已经 >= 触发价，以开盘价成交
        entry_price = day_open * (1.0 + config.slippage)
        return entry_price, "open_fill", None
    elif trigger_price <= day_high:
        # 盘中触及触发价，以触发价成交
        entry_price = trigger_price * (1.0 + config.slippage)
        return entry_price, "trigger_fill", None
    else:
        # T+1 日价格未触及触发价，未成交
        return None, None, "unfilled"


# ─────────────────────────────────────────────────────
# 风控出场 (Exit Rules)
# ─────────────────────────────────────────────────────

def check_exit(
    df: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    current_idx: int,
    max_high: float,
    config: SimConfig,
) -> tuple[float | None, str | None]:
    """
    逐日出场检查（优先级从高到低）

    Returns:
        (exit_price, exit_reason) or (None, None) 继续持仓
    """
    bar = df.iloc[current_idx]
    day_close = float(bar["close"])
    day_low = float(bar["low"])
    day_high = float(bar["high"])
    holding_days = current_idx - entry_idx

    # 1. 固定止损 (最高优先级)
    stop_price = entry_price * (1.0 - config.stop_loss_pct / 100.0)
    if day_low <= stop_price:
        actual_exit = max(stop_price, float(bar["open"]))
        actual_exit *= (1.0 - config.slippage)
        return actual_exit, "stop_loss"

    # 2. 固定止盈 (Phase 3)
    if config.take_profit_pct > 0:
        tp_price = entry_price * (1.0 + config.take_profit_pct / 100.0)
        if day_high >= tp_price:
            actual_exit = min(tp_price, day_high)
            actual_exit *= (1.0 - config.slippage)
            return actual_exit, "take_profit"

    # 3. 利润回撤 (Phase 3: 绝对百分比)
    if config.profit_drawdown_pct > 0 and max_high > entry_price:
        peak_profit_pct = (max_high - entry_price) / entry_price * 100.0
        current_profit_pct = (day_close - entry_price) / entry_price * 100.0
        if peak_profit_pct > config.profit_drawdown_pct:
            profit_loss = peak_profit_pct - current_profit_pct
            if profit_loss >= config.profit_drawdown_pct:
                exit_price = day_close * (1.0 - config.slippage)
                return exit_price, "profit_drawdown"

    # 4. 均线离场 (Phase 3)
    if config.ma_exit_period > 0 and holding_days >= 3:
        ma_start = max(0, current_idx - config.ma_exit_period + 1)
        ma_slice = df.iloc[ma_start:current_idx + 1]["close"]
        if len(ma_slice) >= config.ma_exit_period:
            ma_val = float(ma_slice.mean())
            if day_close < ma_val:
                exit_price = day_close * (1.0 - config.slippage)
                return exit_price, "ma_exit"

    # 5. 移动止盈 (最高浮盈回撤 N%)
    if max_high > entry_price:
        max_profit = (max_high - entry_price) / entry_price * 100.0
        current_profit = (day_close - entry_price) / entry_price * 100.0
        if max_profit > 0:
            drawdown_from_peak = (max_profit - current_profit) / max_profit * 100.0
            if drawdown_from_peak >= config.trail_stop_pct:
                exit_price = day_close * (1.0 - config.slippage)
                return exit_price, "trail_stop"

    # 6. 时间止损
    if holding_days >= config.max_hold_days:
        exit_price = day_close * (1.0 - config.slippage)
        return exit_price, "time_exit"

    return None, None


# ─────────────────────────────────────────────────────
# 交易成本计算
# ─────────────────────────────────────────────────────

def compute_costs(entry_price: float, exit_price: float, config: SimConfig) -> float:
    """计算交易成本占比 (%)"""
    buy_cost = config.commission + config.slippage
    sell_cost = config.commission + config.tax + config.slippage
    total_cost_ratio = buy_cost + sell_cost
    return total_cost_ratio * 100.0


# ─────────────────────────────────────────────────────
# 主入口: 交易模拟
# ─────────────────────────────────────────────────────

def simulate_trades(
    df: pd.DataFrame,
    signals: list[dict],
    config: SimConfig,
) -> SimResult:
    """
    主入口: 接收 K线 DataFrame + 信号列表，执行交易模拟。

    signals 格式: [{dt, date_str, type, group, price, confidence, eval, ...}]
    df: OHLCV DataFrame, index = DatetimeIndex

    流程:
    1. 按时间排序信号
    2. 对每个买入信号，在 T+1 日尝试成交 (fill_entry)
    3. 成交后逐日检查出场条件 (check_exit)
    4. 计算资金曲线和统计指标
    """
    result = SimResult(config={
        "stop_loss_pct": config.stop_loss_pct,
        "trail_stop_pct": config.trail_stop_pct,
        "max_hold_days": config.max_hold_days,
        "slippage_pct": config.slippage * 100,
        "commission_pct": config.commission * 100,
        "tax_pct": config.tax * 100,
    })

    skip_counts: dict[str, int] = {}
    trade_records: list[TradeRecord] = []

    # 过滤买入信号
    buy_signals = []
    for s in signals:
        sig_type = s.get("type", "")
        sig_group = s.get("group", "")
        # MACD 信号都是买入信号
        # 缠论: 含"买"或"背驰"为买入，含"卖"为卖出
        is_buy = True
        if sig_group == "czsc":
            if "卖" in sig_type and "买" not in sig_type:
                is_buy = False
        if is_buy:
            buy_signals.append(s)

    # 按时间排序
    buy_signals.sort(key=lambda s: s.get("dt", 0))

    # 持仓状态: 同一时间只持有一个仓位
    in_position = False
    position_exit_idx = -1

    for sig in buy_signals:
        sig_dt = sig.get("dt", 0)
        sig_date_str = sig.get("date_str", "")
        sig_type = sig.get("type", "")
        sig_group = sig.get("group", "")
        sig_price = sig.get("price", 0)
        sig_confidence = sig.get("confidence", 0)

        # 找到信号在 df 中的位置
        sig_idx = None
        for i, (dt_idx, row) in enumerate(df.iterrows()):
            row_ts = int(pd.Timestamp(dt_idx).timestamp())
            if row_ts == sig_dt:
                sig_idx = i
                break
        if sig_idx is None:
            # 尝试最近匹配
            try:
                sig_idx = df.index.get_loc(
                    pd.Timestamp(sig_date_str), method="nearest"
                )
            except Exception:
                skip_counts["date_not_found"] = skip_counts.get("date_not_found", 0) + 1
                continue

        # 检查是否还在持仓中
        if in_position and sig_idx <= position_exit_idx:
            skip_counts["overlapping_position"] = skip_counts.get("overlapping_position", 0) + 1
            continue

        # 尝试 T+1 成交
        entry_price, fill_type, skip_reason = fill_entry(df, sig_idx, sig_price, config)

        if entry_price is None:
            skip_counts[skip_reason or "unknown"] = skip_counts.get(skip_reason or "unknown", 0) + 1
            trade_records.append(TradeRecord(
                symbol="", signal_type=sig_type, signal_group=sig_group,
                signal_date=sig_date_str, signal_confidence=sig_confidence,
                fill_type="unfilled", skip_reason=skip_reason,
            ))
            continue

        entry_idx = sig_idx + 1
        entry_date_str = str(df.index[entry_idx].date()) if hasattr(df.index[entry_idx], 'date') else str(df.index[entry_idx])[:10]

        # 逐日检查出场
        max_high = entry_price
        exit_price = None
        exit_reason = None
        exit_idx = None
        mfe = 0.0
        mae = 0.0

        for day_idx in range(entry_idx + 1, len(df)):
            bar = df.iloc[day_idx]
            day_high = float(bar["high"])
            day_low = float(bar["low"])

            # 更新 MFE/MAE
            high_ret = (day_high - entry_price) / entry_price * 100.0
            low_ret = (day_low - entry_price) / entry_price * 100.0
            if high_ret > mfe:
                mfe = high_ret
            if low_ret < mae:
                mae = low_ret

            # 更新最高价
            if day_high > max_high:
                max_high = day_high

            # 检查出场
            exit_price, exit_reason = check_exit(
                df, entry_idx, entry_price, day_idx, max_high, config
            )
            if exit_price is not None:
                exit_idx = day_idx
                break

        # 如果到数据末尾还没出场，强制以最后一根 bar 收盘价出场
        if exit_price is None and entry_idx + 1 < len(df):
            last_idx = len(df) - 1
            exit_price = float(df.iloc[last_idx]["close"]) * (1.0 - config.slippage)
            exit_reason = "data_end"
            exit_idx = last_idx

        if exit_price is None:
            skip_counts["no_exit_data"] = skip_counts.get("no_exit_data", 0) + 1
            continue

        # 计算收益
        holding_days = exit_idx - entry_idx
        gross_return = (exit_price - entry_price) / entry_price * 100.0
        cost_pct = compute_costs(entry_price, exit_price, config)
        net_return = gross_return - cost_pct

        exit_date_str = str(df.index[exit_idx].date()) if hasattr(df.index[exit_idx], 'date') else str(df.index[exit_idx])[:10]

        trade_records.append(TradeRecord(
            symbol="", signal_type=sig_type, signal_group=sig_group,
            signal_date=sig_date_str, signal_confidence=sig_confidence,
            entry_date=entry_date_str, entry_price=round(entry_price, 4),
            fill_type=fill_type,
            exit_date=exit_date_str, exit_price=round(exit_price, 4),
            exit_reason=exit_reason,
            return_pct=round(gross_return, 2),
            net_return_pct=round(net_return, 2),
            holding_days=holding_days,
            cost_pct=round(cost_pct, 2),
            mfe_pct=round(mfe, 2),
            mae_pct=round(mae, 2),
        ))

        in_position = True
        position_exit_idx = exit_idx

    # 构建 trades 列表
    result.trades = [_trade_to_dict(t) for t in trade_records]
    result.skip_reasons = skip_counts

    # 构建资金曲线
    filled_trades = [t for t in trade_records if t.entry_price is not None and t.net_return_pct is not None]
    result.equity_curve = _build_equity_curve(df, filled_trades, config)

    # 计算 KPI
    result.kpi = _compute_sim_kpi(filled_trades, result.equity_curve, config)

    return result


def _trade_to_dict(t: TradeRecord) -> dict:
    """TradeRecord → dict (for JSON serialization)"""
    return {
        "signal_type": t.signal_type,
        "signal_group": t.signal_group,
        "signal_date": t.signal_date,
        "signal_confidence": t.signal_confidence,
        "entry_date": t.entry_date,
        "entry_price": t.entry_price,
        "fill_type": t.fill_type,
        "exit_date": t.exit_date,
        "exit_price": t.exit_price,
        "exit_reason": t.exit_reason,
        "return_pct": t.return_pct,
        "net_return_pct": t.net_return_pct,
        "holding_days": t.holding_days,
        "cost_pct": t.cost_pct,
        "mfe_pct": t.mfe_pct,
        "mae_pct": t.mae_pct,
        "skip_reason": t.skip_reason,
    }


# ─────────────────────────────────────────────────────
# 资金曲线
# ─────────────────────────────────────────────────────

def _build_equity_curve(
    df: pd.DataFrame,
    trades: list[TradeRecord],
    config: SimConfig,
) -> list[dict]:
    """构建资金曲线 [{time, value}]"""
    if not trades:
        return []

    capital = config.initial_capital
    curve = []

    # 按入场日排序
    sorted_trades = sorted(trades, key=lambda t: t.entry_date or "")

    trade_idx = 0
    active_trade: TradeRecord | None = None
    nav = capital

    for i, (dt_idx, row) in enumerate(df.iterrows()):
        ts = int(pd.Timestamp(dt_idx).timestamp())
        date_str = str(dt_idx.date()) if hasattr(dt_idx, 'date') else str(dt_idx)[:10]

        # 检查是否有新交易入场
        if active_trade is None and trade_idx < len(sorted_trades):
            t = sorted_trades[trade_idx]
            if t.entry_date and date_str >= t.entry_date:
                active_trade = t

        # 如果有持仓中的交易
        if active_trade is not None and active_trade.entry_price:
            if active_trade.exit_date and date_str >= active_trade.exit_date:
                # 交易结束，更新资金
                ret = (active_trade.net_return_pct or 0) / 100.0
                nav = nav * (1.0 + ret)
                trade_idx += 1
                active_trade = None
            elif date_str >= (active_trade.entry_date or ""):
                # 持仓中，按当日收盘价估值
                day_close = float(row["close"])
                unrealized = (day_close - active_trade.entry_price) / active_trade.entry_price
                nav_mark = capital * (1.0 + unrealized)
                # 使用 mark-to-market 估值
                # 但保持 capital 不变直到交易结束

        curve.append({"time": ts, "value": round(nav, 2)})

    return curve


# ─────────────────────────────────────────────────────
# KPI 计算
# ─────────────────────────────────────────────────────

def _compute_sim_kpi(
    trades: list[TradeRecord],
    equity_curve: list[dict],
    config: SimConfig,
) -> dict:
    """计算模拟 KPI"""
    total = len(trades)
    if total == 0:
        return {
            "total_trades": 0,
            "filled_trades": 0,
            "win_rate": 0,
            "total_return_pct": 0,
            "sharpe": 0,
            "sortino": 0,
            "max_drawdown_pct": 0,
            "max_drawdown_days": 0,
            "profit_factor": 0,
            "avg_return": 0,
            "avg_hold_days": 0,
            "avg_cost_pct": 0,
            "avg_mfe": 0,
            "avg_mae": 0,
        }

    returns = [t.net_return_pct or 0 for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    hold_days = [t.holding_days or 0 for t in trades]

    win_count = len(wins)
    win_rate = round(win_count / total * 100, 1)
    avg_return = round(sum(returns) / total, 2)
    avg_win = round(sum(wins) / len(wins), 2) if wins else 0
    avg_loss = round(sum(losses) / len(losses), 2) if losses else 0
    avg_hold = round(sum(hold_days) / total, 1)
    avg_cost = round(sum(t.cost_pct for t in trades) / total, 2)
    avg_mfe = round(sum(t.mfe_pct for t in trades) / total, 2)
    avg_mae = round(sum(t.mae_pct for t in trades) / total, 2)

    # Profit Factor
    total_wins = sum(wins)
    total_losses = abs(sum(losses))
    profit_factor = round(total_wins / total_losses, 2) if total_losses > 0 else 999

    # Total return from equity curve
    total_return = 0
    if equity_curve:
        initial = equity_curve[0]["value"]
        final = equity_curve[-1]["value"]
        if initial > 0:
            total_return = round((final - initial) / initial * 100, 2)

    # Max drawdown from equity curve
    max_dd = 0
    max_dd_days = 0
    if equity_curve:
        peak = equity_curve[0]["value"]
        peak_idx = 0
        for i, point in enumerate(equity_curve):
            val = point["value"]
            if val > peak:
                peak = val
                peak_idx = i
            dd = (peak - val) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
                max_dd_days = i - peak_idx

    # Sharpe Ratio (年化, 假设 252 交易日)
    sharpe = 0
    if len(returns) >= 2:
        import statistics
        mean_r = statistics.mean(returns)
        std_r = statistics.stdev(returns)
        if std_r > 0:
            # 简化: 使用交易级别 Sharpe
            trades_per_year = 252 / max(avg_hold, 1)
            sharpe = round(mean_r / std_r * math.sqrt(trades_per_year), 2)

    # Sortino Ratio
    sortino = 0
    if len(returns) >= 2:
        import statistics
        mean_r = statistics.mean(returns)
        downside = [r for r in returns if r < 0]
        if downside:
            down_std = statistics.stdev(downside) if len(downside) >= 2 else abs(downside[0])
            if down_std > 0:
                trades_per_year = 252 / max(avg_hold, 1)
                sortino = round(mean_r / down_std * math.sqrt(trades_per_year), 2)

    # Expectancy
    wr = win_count / total
    lr = (total - win_count) / total
    expectancy = round(avg_win * wr - abs(avg_loss) * lr, 2)

    return {
        "total_trades": total,
        "filled_trades": total,
        "win_count": win_count,
        "loss_count": total - win_count,
        "win_rate": win_rate,
        "total_return_pct": total_return,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown_pct": round(max_dd, 2),
        "max_drawdown_days": max_dd_days,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "avg_return": avg_return,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_hold_days": avg_hold,
        "avg_cost_pct": avg_cost,
        "avg_mfe": avg_mfe,
        "avg_mae": avg_mae,
    }


# ─────────────────────────────────────────────────────
# 参数扫描
# ─────────────────────────────────────────────────────

def run_parameter_scan(
    df: pd.DataFrame,
    signals: list[dict],
    base_config: SimConfig,
    param1_name: str,
    param1_values: list[float],
    param2_name: str | None = None,
    param2_values: list[float] | None = None,
    metric: str = "sharpe",
) -> dict:
    """
    1-2 维参数扫描，返回所有组合结果 + 最优参数

    Returns:
        {
            "scan_results": [{params, win_rate, sharpe, expectancy, total_return}],
            "best_params": {},
            "heatmap": {x_label, x_values, y_label, y_values, z_label, data}
        }
    """
    from itertools import product as iterproduct
    from dataclasses import replace

    results = []
    best_result = None
    best_metric_value = None

    # 构建参数组合
    if param2_name and param2_values:
        combos = list(iterproduct(param1_values, param2_values))
    else:
        combos = [(v,) for v in param1_values]

    for combo in combos:
        overrides = {param1_name: combo[0]}
        if param2_name and len(combo) > 1:
            overrides[param2_name] = combo[1]

        # 应用参数覆盖
        config_kwargs = {}
        for k, v in overrides.items():
            if hasattr(base_config, k):
                config_kwargs[k] = type(getattr(base_config, k))(v)
        scan_config = replace(base_config, **config_kwargs)

        # 运行模拟
        sim = simulate_trades(df, signals, scan_config)
        kpi = sim.kpi

        row = {
            "params": overrides,
            "win_rate": kpi.get("win_rate", 0),
            "sharpe": kpi.get("sharpe", 0),
            "expectancy": kpi.get("expectancy", 0),
            "total_return": kpi.get("total_return_pct", 0),
            "profit_factor": kpi.get("profit_factor", 0),
            "max_drawdown": kpi.get("max_drawdown_pct", 0),
            "total_trades": kpi.get("total_trades", 0),
        }
        results.append(row)

        metric_val = kpi.get(metric, kpi.get(f"{metric}_pct", 0))
        if best_metric_value is None or metric_val > best_metric_value:
            best_metric_value = metric_val
            best_result = overrides

    # 构建热力图数据
    heatmap = None
    if param2_name and param2_values:
        data = []
        for y_val in param2_values:
            row_data = []
            for x_val in param1_values:
                matching = [r for r in results
                            if r["params"].get(param1_name) == x_val
                            and r["params"].get(param2_name) == y_val]
                val = matching[0].get(metric, 0) if matching else 0
                row_data.append(round(val, 2))
            data.append(row_data)
        heatmap = {
            "x_label": param1_name,
            "x_values": param1_values,
            "y_label": param2_name,
            "y_values": param2_values,
            "z_label": metric,
            "data": data,
        }

    return {
        "scan_results": results,
        "best_params": best_result or {},
        "heatmap": heatmap,
    }
