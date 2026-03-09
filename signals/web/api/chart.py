# -*- coding: utf-8 -*-
"""K线 + CZSC 结构数据 API"""
import logging
from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

from ..services.engine import get_engine
from ..services.serializers import (
    serialize_bars, serialize_bi_list, serialize_fx_list,
    serialize_zhongshu, serialize_signals,
)

router = APIRouter(prefix="/api/chart", tags=["chart"])


@router.get("/{symbol}")
def get_chart_data(
    symbol: str,
    freq: str = Query("daily", regex="^(daily|30min|15min)$"),
):
    """
    获取指定标的的 K 线 + CZSC 结构数据。

    symbol: 指数名称（如 "沪深300"）或代码（如 "sh000300"）
    freq: "daily" / "30min" / "15min"

    返回:
    {
        ohlcv: [{time, open, high, low, close, volume}],
        bi_list: [{sdt, edt, high, low, direction, power}],
        fx_list: [{dt, fx, mark}],
        zhongshu: [{zd, zg, start_dt, end_dt, bi_count}],
        signals: [{dt, type, freq, price, confidence, details}],
        meta: {name, symbol, freq}
    }
    """
    engine = get_engine()

    # 尝试按名称查找
    name = symbol
    if engine.get_index_analyzer(name) is None:
        # 尝试按 symbol 代码查找
        found = engine.find_index_name(symbol)
        if found:
            name = found
        else:
            raise HTTPException(
                status_code=404,
                detail=f"未找到指数: {symbol}")

    # 获取对应周期的 SymbolAnalyzer
    analyzer = engine.get_symbol_analyzer(name, freq)
    if analyzer is None:
        raise HTTPException(
            status_code=404,
            detail=f"{name} 无 {freq} 数据")

    # 序列化图表数据
    ohlcv = serialize_bars(analyzer)
    bi_list = serialize_bi_list(analyzer)
    fx_list = serialize_fx_list(analyzer)
    zhongshu = serialize_zhongshu(analyzer)

    # 从 IndexAnalyzer 获取信号（使用 index_analyzer 的检测结果）
    signals_data = []
    idx_az = engine.get_index_analyzer(name)
    if idx_az:
        from signals.core.detectors import detect_all_signals
        try:
            detected = detect_all_signals(analyzer.czsc, idx_az.symbol)
            signals_data = serialize_signals(detected)
        except Exception as e:
            logger.warning("信号检测失败 %s/%s: %s", name, freq, e)

    # MA 均线数据（仅日线周期提供）
    ma_lines = []
    if freq == "daily":
        try:
            from signals.core.ma_levels import _bars_to_df, _compute_ma
            df = _bars_to_df(analyzer.bars_raw)
            closes = df["close"]
            for period, label, color in [
                (5, "MA5", "#f7931a"),
                (10, "MA10", "#2962ff"),
                (20, "MA20", "#e040fb"),
                (60, "MA60", "#26a69a"),
            ]:
                if len(closes) >= period:
                    ma_vals = closes.rolling(period).mean()
                    line_data = []
                    for dt_idx, val in ma_vals.dropna().items():
                        line_data.append({
                            "time": int(dt_idx.timestamp()),
                            "value": round(val, 4),
                        })
                    ma_lines.append({
                        "label": label,
                        "color": color,
                        "data": line_data,
                    })
        except Exception as e:
            logger.warning("MA线计算失败 %s: %s", name, e)

    # MACD 数据
    macd_data = []
    if freq == "daily":
        try:
            from signals.core.ma_levels import _bars_to_df
            df = _bars_to_df(analyzer.bars_raw)
            closes = df["close"]
            if len(closes) >= 26:
                ema12 = closes.ewm(span=12, adjust=False).mean()
                ema26 = closes.ewm(span=26, adjust=False).mean()
                dif = ema12 - ema26
                dea = dif.ewm(span=9, adjust=False).mean()
                macd_bar = (dif - dea) * 2
                for dt_idx in dif.dropna().index:
                    if dt_idx in dea.dropna().index:
                        macd_data.append({
                            "time": int(dt_idx.timestamp()),
                            "dif": round(float(dif[dt_idx]), 4),
                            "dea": round(float(dea[dt_idx]), 4),
                            "bar": round(float(macd_bar[dt_idx]), 4),
                        })
        except Exception as e:
            logger.warning("MACD计算失败 %s: %s", name, e)

    # 获取 IndexReport 的摘要信息
    report_summary = {}
    for r in engine.get_index_reports():
        if r.name == name:
            report_summary = {
                "daily_trend": r.daily_trend,
                "f30_trend": r.f30_trend,
                "f15_trend": r.f15_trend,
                "daily_latest_signal": r.daily_latest_signal,
                "f30_latest_signal": r.f30_latest_signal,
                "f15_latest_signal": r.f15_latest_signal,
                "is_bullish": r.is_bullish,
                "three_level_aligned": r.three_level_aligned,
                "summary": r.summary,
            }
            # MA context
            ma_ctx = getattr(r, 'ma_context', None)
            if ma_ctx:
                report_summary["ma_trend"] = ma_ctx.trend_summary
                report_summary["key_levels"] = [
                    {"name": lv.name, "value": round(lv.value, 2),
                     "distance_pct": lv.distance_pct, "position": lv.position}
                    for lv in ma_ctx.key_levels
                ]

            # 生成操作结论
            conclusion = _generate_conclusion(r, signals_data)
            report_summary["conclusion"] = conclusion
            break

    return {
        "ohlcv": ohlcv,
        "bi_list": bi_list,
        "fx_list": fx_list,
        "zhongshu": zhongshu,
        "signals": signals_data,
        "ma_lines": ma_lines,
        "macd": macd_data,
        "report": report_summary,
        "meta": {
            "name": name,
            "symbol": idx_az.symbol if idx_az else symbol,
            "freq": freq,
        },
    }


def _generate_conclusion(report, signals) -> str:
    """根据 IndexReport + 信号生成操作结论"""
    has_buy = any(s.get("type", "").find("买") >= 0 for s in signals)
    has_sell = any(s.get("type", "").find("卖") >= 0 for s in signals)

    # 获取支撑位
    ma_ctx = getattr(report, 'ma_context', None)
    support_str = ""
    if ma_ctx and ma_ctx.key_levels:
        for lv in ma_ctx.key_levels:
            if lv.position == "下方":
                support_str = f"，支撑位 {lv.value:.0f}"
                break

    if report.three_level_aligned and has_buy:
        return f"📈 三级共振看多，建议轻仓试多{support_str}"
    elif report.is_bullish and has_buy:
        return f"📈 趋势偏多+买信号，可关注入场机会{support_str}"
    elif report.is_bullish and not has_buy:
        return f"⏳ 趋势偏多但无买信号，等待小级别止跌企稳{support_str}"
    elif not report.is_bullish and has_sell:
        return f"📉 趋势偏空+卖信号，规避或减仓"
    elif not report.is_bullish and not has_sell:
        return f"📉 趋势偏空，观望等待反转信号{support_str}"
    elif has_buy and has_sell:
        return f"↔️ 多空交织，关注关键位突破方向{support_str}"
    else:
        return f"↔️ 震荡格局，高抛低吸{support_str}"
