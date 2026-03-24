# -*- coding: utf-8 -*-
"""K线 + CZSC 结构数据 API（web2 版本，使用 MarketCache）"""
import logging
from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

from ..services.market_cache import get_cache
from ..services.serializers import (
    serialize_bars, serialize_bi_list, serialize_fx_list,
    serialize_zhongshu, serialize_signals,
    compute_ma_lines, compute_macd,
)

router = APIRouter(prefix="/api/chart", tags=["chart"])


@router.get("/{symbol}")
def get_chart_data(
    symbol: str,
    freq: str = Query("daily", regex="^(daily|30min|15min)$"),
):
    """
    获取指定指数的 K 线 + CZSC 结构数据。

    symbol: 指数名称（如 "沪深300"）
    freq: "daily" / "30min" / "15min"
    """
    cache = get_cache()
    state = cache.state

    if not state.analyzers:
        raise HTTPException(status_code=503, detail="市场数据加载中，请稍后重试")

    # 查找分析器
    name = symbol
    analyzer = _find_analyzer(state, name, freq)
    if analyzer is None:
        raise HTTPException(status_code=404, detail=f"未找到: {symbol}/{freq}")

    # 序列化图表数据
    ohlcv = serialize_bars(analyzer)
    bi_list = serialize_bi_list(analyzer)
    fx_list = serialize_fx_list(analyzer)
    zhongshu = serialize_zhongshu(analyzer)

    # 信号检测
    signals_data = []
    idx_az = state.analyzers.get(name)
    if idx_az:
        from signals.core.detectors import detect_all_signals
        try:
            detected = detect_all_signals(analyzer.czsc, idx_az.symbol)
            signals_data = serialize_signals(detected)
        except Exception as e:
            logger.warning("信号检测失败 %s/%s: %s", name, freq, e)

    # MA 均线 + MACD（仅日线）
    ma_lines = []
    macd_data = []
    if freq == "daily":
        try:
            ma_lines = compute_ma_lines(analyzer.bars_raw)
        except Exception as e:
            logger.warning("MA线计算失败 %s: %s", name, e)
        try:
            macd_data = compute_macd(analyzer.bars_raw)
        except Exception as e:
            logger.warning("MACD计算失败 %s: %s", name, e)

    # 报告摘要
    report_summary = {}
    report_signals = []
    for r in state.index_reports:
        if r.name == name:
            report_summary = _extract_report_summary(r, signals_data)
            report_signals = _extract_report_signals(r)
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
        "report_signals": report_signals,
        "meta": {"name": name, "symbol": getattr(idx_az, 'symbol', symbol), "freq": freq},
    }


def _find_analyzer(state, name: str, freq: str):
    """按名称+频率查找 SymbolAnalyzer"""
    # IndexAnalyzer 有 get_symbol_analyzer 方法
    idx_az = state.analyzers.get(name)
    if idx_az is None:
        return None

    if freq == "daily":
        return getattr(idx_az, 'daily_analyzer', None)
    elif freq == "30min":
        return getattr(idx_az, 'f30_analyzer', None)
    elif freq == "15min":
        return getattr(idx_az, 'f15_analyzer', None)
    return None


def _extract_report_summary(report, signals) -> dict:
    """从 IndexReport 提取摘要"""
    summary = {
        "daily_trend": report.daily_trend,
        "f30_trend": report.f30_trend,
        "f15_trend": report.f15_trend,
        "daily_latest_signal": report.daily_latest_signal,
        "f30_latest_signal": report.f30_latest_signal,
        "f15_latest_signal": report.f15_latest_signal,
        "is_bullish": report.is_bullish,
        "three_level_aligned": report.three_level_aligned,
        "summary": report.summary,
    }

    ma_ctx = getattr(report, 'ma_context', None)
    if ma_ctx:
        summary["ma_trend"] = ma_ctx.trend_summary
        summary["key_levels"] = [
            {"name": lv.name, "value": round(lv.value, 2),
             "distance_pct": lv.distance_pct, "position": lv.position}
            for lv in ma_ctx.key_levels
        ]

    summary["conclusion"] = _generate_conclusion(report, signals)
    return summary


def _extract_report_signals(report) -> list:
    """跨级别信号摘要"""
    signals = []
    if report.daily_latest_signal != "无":
        signals.append({"type": report.daily_latest_signal, "freq": "日线"})
    if report.f30_latest_signal != "无":
        signals.append({"type": report.f30_latest_signal, "freq": "30M"})
    if report.f15_latest_signal != "无":
        signals.append({"type": report.f15_latest_signal, "freq": "15M"})
    return signals


def _generate_conclusion(report, signals) -> str:
    """根据 IndexReport + 信号生成操作结论"""
    has_buy = any(s.get("type", "").find("买") >= 0 for s in signals)
    has_sell = any(s.get("type", "").find("卖") >= 0 for s in signals)

    ma_ctx = getattr(report, 'ma_context', None)
    support_str = ""
    if ma_ctx and ma_ctx.key_levels:
        for lv in ma_ctx.key_levels:
            if lv.position == "下方":
                support_str = f"，支撑位 {lv.value:.0f}"
                break

    if report.three_level_aligned and has_buy:
        return f"三级共振看多，建议轻仓试多{support_str}"
    elif report.is_bullish and has_buy:
        return f"趋势偏多+买信号，可关注入场机会{support_str}"
    elif report.is_bullish and not has_buy:
        return f"趋势偏多但无买信号，等待小级别止跌企稳{support_str}"
    elif not report.is_bullish and has_sell:
        return f"趋势偏空+卖信号，规避或减仓"
    elif not report.is_bullish and not has_sell:
        return f"趋势偏空，观望等待反转信号{support_str}"
    elif has_buy and has_sell:
        return f"多空交织，关注关键位突破方向{support_str}"
    else:
        return f"震荡格局，高抛低吸{support_str}"
