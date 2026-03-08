# -*- coding: utf-8 -*-
"""K线 + CZSC 结构数据 API"""
from fastapi import APIRouter, HTTPException, Query

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
        except Exception:
            pass

    return {
        "ohlcv": ohlcv,
        "bi_list": bi_list,
        "fx_list": fx_list,
        "zhongshu": zhongshu,
        "signals": signals_data,
        "meta": {
            "name": name,
            "symbol": idx_az.symbol if idx_az else symbol,
            "freq": freq,
        },
    }
