# -*- coding: utf-8 -*-
"""回测结果富格式微信推送 V2

对齐 Web2 UI 数据丰富度：交易模拟 KPI、信号分类、最佳/最差交易、
出场分布、跳过原因、MA/量能确认率、自动诊断。

支持两种调用方式:
1. Web2 API: push_backtest_report(result_dict)
2. 独立调用: run_and_push("002466")
"""
import logging
import sys
from collections import Counter

logger = logging.getLogger(__name__)

SEP = "━" * 25

# 出场原因中→英映射
EXIT_LABELS = {
    "stop_loss": "止损", "trail_stop": "移动止盈", "time_exit": "时间",
    "signal_exit": "信号", "data_end": "终点", "take_profit": "固定止盈",
    "ma_exit": "均线", "profit_drawdown": "利润回撤", "batch_exit": "分批",
}

SKIP_LABELS = {
    "unfilled": "未触发", "zero_volume": "零成交量", "locked_bar": "一字板",
    "insufficient_data": "数据不足", "date_not_found": "日期未匹配",
    "overlapping_position": "持仓重叠", "no_exit_data": "无出场数据",
}


def format_backtest_report(data: dict) -> str:
    """将回测结果 dict 格式化为微信富文本报告。"""
    code = data.get("code", "???")
    freq = data.get("freq", "日线")
    stock_name = data.get("stock_name", "")
    signals = data.get("signals", [])
    sim_kpi = data.get("sim_kpi", {})
    forward_kpi = data.get("forward_kpi", data.get("kpi", {}))
    sim_trades = data.get("sim_trades", [])
    sim_config = data.get("sim_config", {})
    skip_reasons = data.get("sim_skip_reasons", {})

    parts = []

    # ═══ 标题区 ═══
    title = f"📊 回测报告 — {stock_name} ({code})" if stock_name else f"📊 回测报告 — {code}"
    parts.append(title)
    parts.append(SEP)

    # 信号分组统计
    groups = {}
    for s in signals:
        g = s.get("group", "other")
        groups[g] = groups.get(g, 0) + 1
    group_str = " | ".join(f"{k} {v}" for k, v in groups.items())

    # 回测区间
    date_range = data.get("date_range", "")
    parts.append(f"标的: {code} · {freq} · {len(signals)} 个信号")
    if date_range:
        parts.append(f"区间: {date_range}")
    if group_str:
        parts.append(group_str)
    parts.append("")

    # ═══ 交易模拟 ═══
    if sim_kpi and sim_kpi.get("total_trades", 0) > 0:
        parts.append(SEP)
        parts.append("")
        parts.append("💰 交易模拟")
        filled = sim_kpi.get("filled_trades", 0)
        total_t = sim_kpi.get("total_trades", 0)
        wr = sim_kpi.get("win_rate", 0)
        parts.append(f"成交 {filled}/{total_t} 笔  胜率 {wr}%")

        tr = _sign(sim_kpi.get("total_return_pct", 0))
        pf = sim_kpi.get("profit_factor", 0)
        parts.append(f"总收益 {tr}%  盈亏比 {pf}")

        sharpe = sim_kpi.get("sharpe", 0)
        sortino = sim_kpi.get("sortino", 0)
        parts.append(f"Sharpe {sharpe}  Sortino {sortino}")

        mdd = sim_kpi.get("max_drawdown_pct", 0)
        exp = _sign(sim_kpi.get("expectancy", 0))
        parts.append(f"最大回撤 -{mdd}%  期望 {exp}%/笔")

        hold = sim_kpi.get("avg_hold_days", 0)
        cost = sim_kpi.get("avg_cost_pct", 0)
        parts.append(f"平均持仓 {hold}天  成本均 {cost}%")
        parts.append("")

    # ═══ 信号前瞻 ═══
    t5, t10, t20 = _compute_forward_avg(signals)
    if any(v is not None for v in [t5, t10, t20]):
        parts.append("📈 信号前瞻")
        t_parts = []
        if t5 is not None:
            t_parts.append(f"T+5 {_sign(t5)}%")
        if t10 is not None:
            t_parts.append(f"T+10 {_sign(t10)}%")
        if t20 is not None:
            t_parts.append(f"T+20 {_sign(t20)}%")
        parts.append("  ".join(t_parts))

        avg_mfe = forward_kpi.get("avg_mfe", 0)
        avg_mae = forward_kpi.get("avg_mae", 0)
        if avg_mfe or avg_mae:
            parts.append(f"MFE +{avg_mfe}%  MAE {avg_mae}%")
        parts.append("")

    # ═══ 信号分类 ═══
    by_type = forward_kpi.get("by_type", {})
    if by_type:
        parts.append(SEP)
        parts.append("")
        parts.append("🔍 信号分类")
        for sig_type, info in by_type.items():
            cnt = info.get("count", 0)
            wr = info.get("win_rate", 0)
            avg_r = info.get("avg_return_t10", 0)
            label = _short_type(sig_type)
            parts.append(f"  {label}  {cnt}次 胜率{wr}% {_sign(avg_r)}%")
        parts.append("")

        # MA 确认对比
        by_ma = forward_kpi.get("by_ma", {})
        if by_ma:
            ma_parts = []
            for label, info in by_ma.items():
                cnt = info.get("count", 0)
                wr = info.get("win_rate", 0)
                ma_parts.append(f"{label} {cnt}次 胜率{wr}%")
            parts.append(" vs ".join(ma_parts))

        # 确认率
        ma_rate, vol_rate = _confirmation_rates(signals)
        if ma_rate is not None or vol_rate is not None:
            cr_parts = []
            if ma_rate is not None:
                cr_parts.append(f"MA确认率 {ma_rate}%")
            if vol_rate is not None:
                cr_parts.append(f"量能确认率 {vol_rate}%")
            parts.append("  ".join(cr_parts))
        parts.append("")

    # ═══ 交易摘要 ═══
    filled_trades = [t for t in sim_trades if t.get("entry_price") is not None]
    if filled_trades:
        parts.append(SEP)
        parts.append("")
        parts.append("📝 交易摘要")

        best, worst = _best_worst_trades(filled_trades)
        if best:
            parts.append(f"最佳: {_sign(best['ret'])}% {best['type']} {best['entry']}→{best['exit']} ({best['reason']})")
        if worst:
            parts.append(f"最差: {_sign(worst['ret'])}% {worst['type']} {worst['entry']}→{worst['exit']} ({worst['reason']})")
        parts.append("")

        # 出场分布
        exit_dist = _exit_reason_dist(filled_trades)
        if exit_dist:
            parts.append("出场: " + " | ".join(f"{k} {v}" for k, v in exit_dist.items()))

        # 跳过原因
        if skip_reasons:
            skip_str = " | ".join(f"{SKIP_LABELS.get(k, k)} {v}" for k, v in skip_reasons.items() if v > 0)
            if skip_str:
                parts.append(f"跳过: {skip_str}")
        parts.append("")

    # ═══ 风控参数（一行） ═══
    if sim_config:
        sl = sim_config.get("stop_loss_pct", 5)
        ts = sim_config.get("trail_stop_pct", 50)
        mh = sim_config.get("max_hold_days", 20)
        config_parts = [f"⚙️ 止损{sl}%", f"移动止盈{ts}%", f"最大{mh}天"]
        if sim_config.get("take_profit_pct", 0) > 0:
            config_parts.append(f"固定止盈{sim_config['take_profit_pct']}%")
        if sim_config.get("ma_exit_period", 0) > 0:
            config_parts.append(f"MA{sim_config['ma_exit_period']}离场")
        parts.append(" | ".join(config_parts))
        parts.append("")

    # ═══ 诊断 ═══
    diags = _auto_diagnose(sim_kpi, forward_kpi, signals)
    if diags:
        parts.append(SEP)
        parts.append("")
        parts.append("💡 诊断")
        for d in diags:
            parts.append(d)
        parts.append("")

    # ═══ 周期提示 ═══
    parts.append("📅 可选周期：924新政 | DeepSeek行情 | 关税暴跌 | 17连阳 | 近3月 | 近1月")
    parts.append("回复「回测 XXX 924」可指定起始周期")
    parts.append("")

    # ═══ 署名 ═══
    parts.append(SEP)
    parts.append("🐲 隆小侠 LONG CLAW")

    return "\n".join(parts)


# ── 辅助函数 ──────────────────────────────────────


def _sign(val) -> str:
    if val is None:
        return "N/A"
    return f"+{val}" if val > 0 else str(val)


def _short_type(sig_type: str) -> str:
    replacements = {
        "Pattern A (MACD金叉确认)": "MACD金叉",
        "Pattern B (底背离)": "MACD底背离",
        "Pattern A": "MACD-A",
        "Pattern B": "MACD-B",
    }
    return replacements.get(sig_type, sig_type[:16])


def _compute_forward_avg(signals: list):
    t5_vals, t10_vals, t20_vals = [], [], []
    for s in signals:
        ev = s.get("eval", {})
        if ev.get("return_t5") is not None:
            t5_vals.append(ev["return_t5"])
        if ev.get("return_t10") is not None:
            t10_vals.append(ev["return_t10"])
        if ev.get("return_t20") is not None:
            t20_vals.append(ev["return_t20"])
    t5 = round(sum(t5_vals) / len(t5_vals), 2) if t5_vals else None
    t10 = round(sum(t10_vals) / len(t10_vals), 2) if t10_vals else None
    t20 = round(sum(t20_vals) / len(t20_vals), 2) if t20_vals else None
    return t5, t10, t20


def _confirmation_rates(signals: list):
    """计算 MA 确认率和量能确认率。"""
    if not signals:
        return None, None
    total = len(signals)
    ma_confirmed = sum(1 for s in signals if s.get("ma_confirmed"))
    vol_confirmed = sum(1 for s in signals if s.get("vol_confirmed"))
    ma_rate = round(ma_confirmed / total * 100) if total > 0 else None
    vol_rate = round(vol_confirmed / total * 100) if total > 0 else None
    return ma_rate, vol_rate


def _best_worst_trades(trades: list):
    """提取最佳和最差交易摘要。"""
    if not trades:
        return None, None

    best_t = max(trades, key=lambda t: t.get("net_return_pct", 0))
    worst_t = min(trades, key=lambda t: t.get("net_return_pct", 0))

    def _summarize(t):
        ret = t.get("net_return_pct", 0)
        sig_type = t.get("signal_type", "?")[:12]
        entry = (t.get("entry_date") or "?")[-5:]  # MM-DD
        exit_d = (t.get("exit_date") or "?")[-5:]
        reason = EXIT_LABELS.get(t.get("exit_reason", ""), t.get("exit_reason", "?"))
        return {"ret": ret, "type": sig_type, "entry": entry, "exit": exit_d, "reason": reason}

    return _summarize(best_t), _summarize(worst_t)


def _exit_reason_dist(trades: list) -> dict:
    """统计出场原因分布。"""
    counter = Counter(t.get("exit_reason", "unknown") for t in trades)
    return {EXIT_LABELS.get(k, k): v for k, v in counter.most_common()}


def _auto_diagnose(sim_kpi: dict, forward_kpi: dict, signals: list) -> list:
    """根据数据自动生成 2-4 条诊断建议。"""
    diags = []

    win_rate = sim_kpi.get("win_rate", 0)
    if win_rate >= 60:
        diags.append("🟢 买入信号质量较高")
    elif 0 < win_rate < 40:
        diags.append("🔴 买入信号需优化，胜率偏低")

    # MFE vs 实际收益
    avg_mfe = forward_kpi.get("avg_mfe", 0)
    avg_return = sim_kpi.get("avg_return", forward_kpi.get("avg_return_t10", 0))
    if avg_mfe and avg_mfe > 0 and avg_return is not None:
        capture = avg_return / avg_mfe if avg_mfe > 0 else 1
        if capture < 0.4:
            diags.append(f"⚠️ 卖出偏早，MFE +{avg_mfe}% 但实际 {_sign(avg_return)}%")
        elif capture > 0.7:
            diags.append("🟢 出场时机良好")

    # 最大回撤
    max_dd = sim_kpi.get("max_drawdown_pct", 0)
    if max_dd > 15:
        diags.append("⚠️ 回撤较大，建议收紧止损")

    # MA 确认对比
    by_ma = forward_kpi.get("by_ma", {})
    ma_wr = by_ma.get("MA确认", {}).get("win_rate", 0)
    no_ma_wr = by_ma.get("无MA锚点", {}).get("win_rate", 0)
    if ma_wr > 0 and no_ma_wr > 0 and ma_wr - no_ma_wr > 10:
        diags.append(f"🟢 MA确认有效 (胜率 {ma_wr}% vs {no_ma_wr}%)")

    # 盈亏比
    pf = sim_kpi.get("profit_factor", 0)
    if 0 < pf < 1:
        diags.append("🔴 盈亏比<1，亏损大于盈利")
    elif pf >= 2:
        diags.append("🟢 盈亏比优秀")

    # Sortino
    sortino = sim_kpi.get("sortino", 0)
    if sortino and sortino >= 2:
        diags.append("🟢 下行风险控制优秀 (Sortino≥2)")
    elif sortino and sortino < 0:
        diags.append("🔴 下行风险过高 (Sortino<0)")

    return diags[:4]


# ── 推送入口 ──────────────────────────────────────


def push_backtest_report(data: dict) -> bool:
    """格式化并推送回测报告到微信。"""
    text = format_backtest_report(data)
    try:
        from signals.notify.weclaw import send_text
        send_text(text)
        return True
    except Exception as e:
        logger.warning("回测报告推送失败: %s", e)
        return False


def run_and_push(code: str, freq: str = "daily", dry_run: bool = False, **kwargs) -> bool:
    """独立运行回测并推送 — 不依赖 web2 服务。dry_run=True 时仅打印不推送。"""
    import dataclasses

    from signals.web2.api.backtest import (
        _detect_market, _build_symbol, _fetch_kline,
        _detect_all_signals, _annotate_signals_ma_vol, _compute_kpi,
    )
    from signals.core.trade_simulator import SimConfig, simulate_trades

    code = code.strip()
    market = _detect_market(code)
    symbol = _build_symbol(code, market)
    freq_label = "日线" if freq == "daily" else "周线"

    # 查询股票名称（get_name 需要 futu_code 如 SH.002466）
    stock_name = ""
    try:
        from signals.core.stock_names import get_resolver
        name = get_resolver().get_name(symbol)
        # 排除 fallback 返回代码本身的情况
        if name and name != code and name != symbol.split(".")[-1]:
            stock_name = name
    except Exception:
        pass

    print(f"  拉取 {code} ({stock_name or '?'}) {freq_label}数据...")
    df = _fetch_kline(code, market, freq)
    if df.empty:
        print(f"  无法获取 {code} 的{freq_label}数据")
        return False

    # 计算回测区间
    date_range = ""
    try:
        dt_col = df.index if df.index.name == "dt" or hasattr(df.index[0], 'strftime') else df.get("dt")
        if dt_col is not None:
            start_dt = dt_col.min().strftime("%Y-%m-%d")
            end_dt = dt_col.max().strftime("%Y-%m-%d")
            date_range = f"{start_dt} ~ {end_dt}"
    except Exception:
        pass

    print("  检测信号...")
    signal_group = kwargs.get("signal_group", "all")
    lookback = kwargs.get("lookback", 999)
    all_signals, bi_list, zhongshu, warnings = _detect_all_signals(
        df, symbol, freq_label, signal_group, lookback,
        kwargs.get("factor", ""),
        kwargs.get("gap_pct_min", 2.0),
        kwargs.get("volume_ratio_min", 1.5),
        kwargs.get("trend_lookback", 20),
        kwargs.get("bb_period", 20),
        kwargs.get("squeeze_threshold", 0.05),
    )
    _annotate_signals_ma_vol(df, all_signals)

    print("  运行交易模拟...")
    sim_kwargs = {
        "stop_loss_pct": kwargs.get("stop_loss", 5.0),
        "trail_stop_pct": kwargs.get("trail_stop", 50.0),
        "max_hold_days": kwargs.get("max_hold", 20),
        "slippage": kwargs.get("slippage", 0.001),
    }
    valid_fields = {f.name for f in dataclasses.fields(SimConfig)}
    sim_kwargs = {k: v for k, v in sim_kwargs.items() if k in valid_fields}
    sim = simulate_trades(df, all_signals, SimConfig(**sim_kwargs))

    forward_kpi = _compute_kpi(all_signals)

    result = {
        "symbol": symbol,
        "code": code,
        "stock_name": stock_name,
        "freq": freq_label,
        "date_range": date_range,
        "signals": all_signals,
        "forward_kpi": forward_kpi,
        "sim_kpi": sim.kpi,
        "sim_config": sim.config,
        "sim_trades": sim.trades,
        "sim_skip_reasons": getattr(sim, "skip_reasons", {}),
    }

    text = format_backtest_report(result)
    print(f"\n{text}\n")

    if dry_run:
        print("  [dry-run] 跳过微信推送")
        return True

    try:
        from signals.notify.weclaw import send_text
        send_text(text)
        return True
    except Exception as e:
        logger.warning("推送失败: %s", e)
        print(f"  推送失败: {e}")
        return False


def _resolve_stock_input(raw: str) -> str:
    """将各种格式的股票输入统一转为纯数字代码。

    支持格式：
    - 纯数字: 002759
    - 带前缀: SZ.002759, SH.600000, HK.09988
    - 股票名称: 天际股份（通过 get_resolver 模糊匹配）
    """
    raw = raw.strip()

    # 已有前缀格式 → 提取纯数字部分
    if "." in raw:
        parts = raw.split(".", 1)
        if parts[0].upper() in ("SZ", "SH", "HK"):
            return parts[1]

    # 纯数字 → 直接返回
    if raw.isdigit():
        return raw

    # 非数字 → 尝试名称解析
    try:
        from signals.core.stock_names import get_resolver
        code = get_resolver().get_code(raw)
        if code:
            # get_code 返回 Futu 格式如 SZ.002759，提取纯数字
            if "." in code:
                code = code.split(".", 1)[1]
            print(f"  📌 名称解析: {raw} → {code}")
            return code
    except Exception as e:
        print(f"  ⚠️ 名称解析失败: {e}")

    # fallback: 原样返回
    return raw


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]

    if not args:
        print("用法: python -m signals.notify.backtest_notify <股票代码|名称> [频率] [--dry-run]")
        print("示例: python -m signals.notify.backtest_notify 002466")
        print("      python -m signals.notify.backtest_notify 天际股份 --dry-run")
        print("      python -m signals.notify.backtest_notify SZ.002759 --dry-run")
        sys.exit(1)

    stock_code = _resolve_stock_input(args[0])
    frequency = args[1] if len(args) > 1 else "daily"
    is_dry_run = "--dry-run" in flags
    run_and_push(stock_code, frequency, dry_run=is_dry_run)
