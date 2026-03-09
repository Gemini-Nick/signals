# -*- coding: utf-8 -*-
"""P3-6: 历史形态匹配 API"""
from fastapi import APIRouter, HTTPException

import config

router = APIRouter(prefix="/api/analog", tags=["analog"])


@router.get("/results")
def get_analog_results():
    """获取最新缓存的匹配结果"""
    from signals.core.analog_matcher import load_analog_results
    cached = load_analog_results()
    if cached is None:
        return {"results": {}, "timestamp": None}
    return cached


@router.get("/run/{index_name}")
def run_analog_match(index_name: str):
    """触发单指数历史匹配（同步执行）"""
    code = config.INDEX_AK_CODES.get(index_name)
    if not code:
        raise HTTPException(status_code=404,
                            detail=f"未知指数: {index_name}")

    from signals.data.fetcher import DataFetcher
    from signals.core.analog_matcher import (
        find_analogs, save_analog_results, load_analog_results, analog_to_dict,
    )

    fetcher = DataFetcher()
    try:
        bars = fetcher.get_index_daily(code, lookback_days=config.ANALOG_LOOKBACK_DAYS)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"数据加载失败: {e}")

    if len(bars) < config.ANALOG_WINDOW + 60:
        raise HTTPException(status_code=400,
                            detail=f"数据不足: {len(bars)}根K线")

    closes = [b.close for b in bars]
    dates = [str(b.dt)[:10] for b in bars]

    analogs = find_analogs(
        current_closes=closes,
        current_dates=dates,
        history_closes=closes,
        history_dates=dates,
        index_name=index_name,
        window=config.ANALOG_WINDOW,
        top_k=config.ANALOG_TOP_K,
        min_similarity=config.ANALOG_MIN_SIMILARITY,
    )

    result = [analog_to_dict(a) for a in analogs]

    # 合并到缓存（保留其他指数的结果）
    cached = load_analog_results()
    all_results = cached.get("results", {}) if cached else {}
    all_results[index_name] = result
    save_analog_results(all_results)

    return {"index_name": index_name, "matches": result, "count": len(result)}


@router.get("/chart/{index_name}")
def get_analog_chart_data(index_name: str, match_start: str = "", match_end: str = ""):
    """获取匹配区间的历史K线（归一化收益率曲线，用于叠加显示）"""
    code = config.INDEX_AK_CODES.get(index_name)
    if not code:
        raise HTTPException(status_code=404, detail=f"未知指数: {index_name}")

    from signals.data.fetcher import DataFetcher
    fetcher = DataFetcher()

    try:
        bars = fetcher.get_index_daily(code, lookback_days=config.ANALOG_LOOKBACK_DAYS)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"数据加载失败: {e}")

    if not bars:
        return {"current": [], "historical": []}

    # 当前走势（最近 window 天归一化）
    window = config.ANALOG_WINDOW
    recent = bars[-window:]
    base_price = recent[0].close if recent else 1
    current_series = [
        {
            "time": str(b.dt)[:10],
            "value": round((b.close / base_price - 1) * 100, 2),
        }
        for b in recent
    ]

    # 历史匹配区间（如果提供了日期）
    historical_series = []
    if match_start and match_end:
        matched = [b for b in bars
                   if match_start <= str(b.dt)[:10] <= match_end]
        if matched:
            # 归一化 + 向后延展30天
            end_idx = next(
                (i for i, b in enumerate(bars)
                 if str(b.dt)[:10] == match_end), None)
            if end_idx is not None:
                extended = bars[end_idx - len(matched) + 1:end_idx + 31]
                if extended:
                    h_base = extended[0].close
                    historical_series = [
                        {
                            "day": i,
                            "value": round((b.close / h_base - 1) * 100, 2),
                        }
                        for i, b in enumerate(extended)
                    ]

    return {
        "current": current_series,
        "historical": historical_series,
        "window": window,
    }
