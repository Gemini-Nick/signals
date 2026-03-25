# -*- coding: utf-8 -*-
"""回测结果富格式微信推送

支持两种调用方式:
1. Web2 API: push_backtest_report(result_dict) — 传入 /analyze 返回的完整结果
2. 独立调用: run_and_push("002466") — 自动拉数据、检测信号、模拟、推送
"""
import logging
import sys

logger = logging.getLogger(__name__)

SEP = "━" * 20


def format_backtest_report(data: dict) -> str:
    """将回测结果 dict 格式化为微信富文本。

    :param data: /analyze 或 /simulate 返回的完整结果 dict
    :return: 格式化后的纯文本字符串
    """
    code = data.get("code", "???")
    freq = data.get("freq", "日线")
    stock_name = data.get("stock_name", "")

    # ── 标题 ──
    title = f"📊 回测报告 — {stock_name} ({code})" if stock_name else f"📊 回测报告 — {code}"
    parts = [title, SEP]

    # ── 基本信息 ──
    signals = data.get("signals", [])
    total_signals = len(signals)

    # 按 group 统计: macd / czsc / entry_factor
    groups = {}
    for s in signals:
        g = s.get("group", "other")
        groups[g] = groups.get(g, 0) + 1
    group_parts = " / ".join(f"{k} {v}" for k, v in groups.items())

    parts.append("📋 基本信息")
    parts.append(f"标的: {code}  频率: {freq}")
    parts.append(f"信号数: {total_signals}" + (f" ({group_parts})" if group_parts else ""))
    parts.append(SEP)

    # ── 交易模拟 ──
    sim_kpi = data.get("sim_kpi", {})
    if sim_kpi and sim_kpi.get("total_trades", 0) > 0:
        parts.append("💰 交易模拟")
        filled = sim_kpi.get("filled_trades", 0)
        total_trades = sim_kpi.get("total_trades", 0)
        parts.append(f"成交: {filled}/{total_trades} 笔")
        parts.append(f"胜率: {sim_kpi.get('win_rate', 0)}%")
        pf = sim_kpi.get("profit_factor", 0)
        parts.append(f"盈亏比: {pf}")
        parts.append(f"总收益: {_sign(sim_kpi.get('total_return_pct', 0))}%")
        parts.append(f"期望: {_sign(sim_kpi.get('expectancy', 0))}%/笔")
        parts.append(f"最大回撤: {sim_kpi.get('max_drawdown_pct', 0)}%")
        parts.append(f"夏普: {sim_kpi.get('sharpe', 0)}")
        parts.append(f"平均持仓: {sim_kpi.get('avg_hold_days', 0)} 天")
        parts.append(SEP)

    # ── 信号前瞻 ──
    forward_kpi = data.get("forward_kpi", data.get("kpi", {}))
    t5, t10, t20 = _compute_forward_avg(signals)

    if forward_kpi.get("evaluated", 0) > 0 or any(v is not None for v in [t5, t10, t20]):
        parts.append("📈 信号前瞻")
        if t5 is not None or t10 is not None or t20 is not None:
            t_parts = []
            if t5 is not None:
                t_parts.append(f"T+5: {_sign(t5)}%")
            if t10 is not None:
                t_parts.append(f"T+10: {_sign(t10)}%")
            if t20 is not None:
                t_parts.append(f"T+20: {_sign(t20)}%")
            parts.append("  ".join(t_parts))

        avg_mfe = forward_kpi.get("avg_mfe", 0)
        avg_mae = forward_kpi.get("avg_mae", 0)
        if avg_mfe or avg_mae:
            parts.append(f"MFE: +{avg_mfe}%  MAE: {avg_mae}%")
        parts.append(SEP)

    # ── 分类表现 ──
    by_type = forward_kpi.get("by_type", {})
    if by_type:
        parts.append("🔍 分类表现")
        for sig_type, info in by_type.items():
            cnt = info.get("count", 0)
            wr = info.get("win_rate", 0)
            avg_r = info.get("avg_return_t10", 0)
            label = _short_type(sig_type)
            parts.append(f"  {label}: {cnt}次 胜率{wr}% 收益{_sign(avg_r)}%")

        # MA 确认对比
        by_ma = forward_kpi.get("by_ma", {})
        if by_ma:
            for label, info in by_ma.items():
                cnt = info.get("count", 0)
                wr = info.get("win_rate", 0)
                parts.append(f"  {label}: {cnt}次 胜率{wr}%")
        parts.append(SEP)

    # ── 风控参数 ──
    sim_config = data.get("sim_config", {})
    if sim_config:
        sl = sim_config.get("stop_loss_pct", 5)
        ts = sim_config.get("trail_stop_pct", 50)
        mh = sim_config.get("max_hold_days", 20)
        parts.append("⚙️ 风控")
        parts.append(f"止损{sl}% | 移动止盈{ts}% | 最大持仓{mh}天")
        # 高级出场
        advanced = []
        if sim_config.get("take_profit_pct", 0) > 0:
            advanced.append(f"固定止盈{sim_config['take_profit_pct']}%")
        if sim_config.get("ma_exit_period", 0) > 0:
            advanced.append(f"MA{sim_config['ma_exit_period']}离场")
        if sim_config.get("batch_exit_enabled"):
            advanced.append("分批出场")
        if advanced:
            parts.append("  ".join(advanced))
        parts.append(SEP)

    # ── 诊断 ──
    diags = _auto_diagnose(sim_kpi, forward_kpi, by_type)
    if diags:
        parts.append("💡 诊断")
        for d in diags:
            parts.append(d)
        parts.append(SEP)

    # ── 署名 ──
    parts.append("🐲 隆小侠 LONG CLAW")

    return "\n".join(parts)


def _sign(val) -> str:
    """数字加正号前缀"""
    if val is None:
        return "N/A"
    return f"+{val}" if val > 0 else str(val)


def _short_type(sig_type: str) -> str:
    """缩短信号类型名"""
    replacements = {
        "Pattern A (MACD金叉确认)": "MACD金叉",
        "Pattern B (底背离)": "MACD底背离",
        "Pattern A": "MACD-A",
        "Pattern B": "MACD-B",
    }
    return replacements.get(sig_type, sig_type[:12])


def _compute_forward_avg(signals: list):
    """从信号列表计算 T+5/T+10/T+20 平均收益"""
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


def _auto_diagnose(sim_kpi: dict, forward_kpi: dict, by_type: dict) -> list:
    """根据数据自动生成 2-3 条诊断建议"""
    diags = []

    win_rate = sim_kpi.get("win_rate", 0)
    if win_rate >= 60:
        diags.append("🟢 买入信号质量较高")
    elif win_rate > 0 and win_rate < 40:
        diags.append("🔴 买入信号需优化，胜率偏低")

    # MFE vs 实际收益
    avg_mfe = forward_kpi.get("avg_mfe", 0)
    avg_return = sim_kpi.get("avg_return", forward_kpi.get("avg_return_t10", 0))
    if avg_mfe > 0 and avg_return is not None:
        capture_ratio = avg_return / avg_mfe if avg_mfe > 0 else 1
        if capture_ratio < 0.4:
            diags.append("⚠️ 卖出偏早，可优化止盈位")
        elif capture_ratio > 0.7:
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

    return diags[:4]  # 最多 4 条


def push_backtest_report(data: dict) -> bool:
    """格式化并推送回测报告到微信。

    :param data: 回测结果 dict
    :return: 推送是否成功
    """
    text = format_backtest_report(data)
    try:
        from signals.notify.weclaw import send_text
        send_text(text)
        return True
    except Exception as e:
        logger.warning("回测报告推送失败: %s", e)
        return False


def run_and_push(code: str, freq: str = "daily", **kwargs) -> bool:
    """独立运行回测并推送 — 不依赖 web2 服务。

    :param code: 股票代码 (如 002466)
    :param freq: daily / weekly
    :param kwargs: 可选覆盖参数 (stop_loss, trail_stop, max_hold 等)
    :return: 推送是否成功
    """
    import dataclasses

    # 复用 backtest API 中的核心函数
    from signals.web2.api.backtest import (
        _detect_market, _build_symbol, _fetch_kline,
        _detect_all_signals, _annotate_signals_ma_vol, _compute_kpi,
    )
    from signals.core.trade_simulator import SimConfig, simulate_trades

    code = code.strip()
    market = _detect_market(code)
    symbol = _build_symbol(code, market)
    freq_label = "日线" if freq == "daily" else "周线"

    # 1. 拉取K线
    print(f"  拉取 {code} {freq_label}数据...")
    df = _fetch_kline(code, market, freq)
    if df.empty:
        print(f"  无法获取 {code} 的{freq_label}数据")
        return False

    # 2. 信号检测
    print(f"  检测信号...")
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

    # 3. 交易模拟
    print(f"  运行交易模拟...")
    sim_kwargs = {
        "stop_loss_pct": kwargs.get("stop_loss", 5.0),
        "trail_stop_pct": kwargs.get("trail_stop", 50.0),
        "max_hold_days": kwargs.get("max_hold", 20),
        "slippage": kwargs.get("slippage", 0.001),
    }
    valid_fields = {f.name for f in dataclasses.fields(SimConfig)}
    sim_kwargs = {k: v for k, v in sim_kwargs.items() if k in valid_fields}
    sim = simulate_trades(df, all_signals, SimConfig(**sim_kwargs))

    # 4. 计算 KPI
    forward_kpi = _compute_kpi(all_signals)

    # 5. 组装结果
    result = {
        "symbol": symbol,
        "code": code,
        "freq": freq_label,
        "signals": all_signals,
        "forward_kpi": forward_kpi,
        "sim_kpi": sim.kpi,
        "sim_config": sim.config,
        "sim_trades": sim.trades,
    }

    # 6. 格式化 + 推送
    text = format_backtest_report(result)
    print(f"\n{text}\n")

    try:
        from signals.notify.weclaw import send_text
        send_text(text)
        return True
    except Exception as e:
        logger.warning("推送失败: %s", e)
        print(f"  推送失败: {e}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("用法: python -m signals.notify.backtest_notify <股票代码> [频率]")
        print("示例: python -m signals.notify.backtest_notify 002466")
        print("      python -m signals.notify.backtest_notify 002466 weekly")
        sys.exit(1)

    stock_code = sys.argv[1]
    frequency = sys.argv[2] if len(sys.argv) > 2 else "daily"
    run_and_push(stock_code, frequency)
