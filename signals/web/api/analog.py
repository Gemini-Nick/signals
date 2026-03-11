# -*- coding: utf-8 -*-
"""P3-6: 历史形态匹配 API — 支持自定义时间区间"""
import logging
from fastapi import APIRouter, HTTPException, Query

import config

logger = logging.getLogger(__name__)

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
def run_analog_match(
    index_name: str,
    start_date: str = Query("", description="自定义区间起始日期 YYYY-MM-DD"),
    end_date: str = Query("", description="自定义区间结束日期 YYYY-MM-DD"),
):
    """
    触发历史匹配（同步执行）。

    - 不指定日期: 使用最近 ANALOG_WINDOW 天作为当前走势
    - 指定 start_date/end_date: 使用该区间作为匹配模板，在全部历史中搜索
    """
    code = config.INDEX_AK_CODES.get(index_name)
    if not code:
        raise HTTPException(status_code=404,
                            detail=f"未知指数: {index_name}")

    from signals.data.fetcher import AKShareSource
    from signals.core.analog_matcher import (
        find_analogs, save_analog_results, load_analog_results, analog_to_dict,
    )

    fetcher = AKShareSource()
    try:
        bars = fetcher.get_index_daily(code, lookback_days=config.ANALOG_LOOKBACK_DAYS)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"数据加载失败: {e}")

    all_closes = [b.close for b in bars]
    all_dates = [str(b.dt)[:10] for b in bars]

    if len(bars) < 90:
        raise HTTPException(status_code=400,
                            detail=f"数据不足: {len(bars)}根K线")

    try:
        if start_date and end_date:
            # ── 自定义区间模式 ──
            # 从全部历史中提取选定区间的收盘价
            sel_closes = []
            sel_dates = []
            for i, d in enumerate(all_dates):
                if start_date <= d <= end_date:
                    sel_closes.append(all_closes[i])
                    sel_dates.append(all_dates[i])

            if len(sel_closes) < 5:
                raise HTTPException(
                    status_code=400,
                    detail=f"选定区间 {start_date}~{end_date} 仅有 {len(sel_closes)} 根K线，至少需要5根")

            analogs = find_analogs(
                current_closes=sel_closes,
                current_dates=sel_dates,
                history_closes=all_closes,
                history_dates=all_dates,
                index_name=index_name,
                window=len(sel_closes),
                top_k=config.ANALOG_TOP_K,
                min_similarity=config.ANALOG_MIN_SIMILARITY,
                exclude_start=start_date,
                exclude_end=end_date,
            )
        else:
            # ── 默认模式: 最近 ANALOG_WINDOW 天 ──
            analogs = find_analogs(
                current_closes=all_closes,
                current_dates=all_dates,
                history_closes=all_closes,
                history_dates=all_dates,
                index_name=index_name,
                window=config.ANALOG_WINDOW,
                top_k=config.ANALOG_TOP_K,
                min_similarity=config.ANALOG_MIN_SIMILARITY,
            )

        result = [analog_to_dict(a) for a in analogs]
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("匹配计算失败 %s", index_name)
        raise HTTPException(status_code=500, detail=f"匹配计算失败: {e}")

    # 合并到缓存
    cached = load_analog_results()
    all_results = cached.get("results", {}) if cached else {}
    all_results[index_name] = result
    save_analog_results(all_results)

    return {
        "index_name": index_name,
        "matches": result,
        "count": len(result),
        "custom_range": bool(start_date and end_date),
        "selected_start": start_date if start_date else "",
        "selected_end": end_date if end_date else "",
    }


@router.get("/chart/{index_name}")
def get_analog_chart_data(
    index_name: str,
    match_start: str = "",
    match_end: str = "",
    sel_start: str = Query("", description="选定区间起始日期"),
    sel_end: str = Query("", description="选定区间结束日期"),
):
    """
    获取匹配区间的K线数据（归一化收益率曲线，用于叠加显示）。

    - sel_start/sel_end: 用户选定的"当前"区间（自定义模式）
    - match_start/match_end: 匹配到的历史区间
    """
    code = config.INDEX_AK_CODES.get(index_name)
    if not code:
        raise HTTPException(status_code=404, detail=f"未知指数: {index_name}")

    from signals.data.fetcher import AKShareSource
    fetcher = AKShareSource()

    try:
        bars = fetcher.get_index_daily(code, lookback_days=config.ANALOG_LOOKBACK_DAYS)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"数据加载失败: {e}")

    if not bars:
        return {"current": [], "historical": []}

    # ── 当前走势（选定区间 or 最近 window 天）──
    if sel_start and sel_end:
        selected = [b for b in bars if sel_start <= str(b.dt)[:10] <= sel_end]
    else:
        window = config.ANALOG_WINDOW
        selected = bars[-window:]

    if not selected:
        return {"current": [], "historical": []}

    base_price = selected[0].close
    current_series = [
        {
            "time": str(b.dt)[:10],
            "value": round((b.close / base_price - 1) * 100, 2),
        }
        for b in selected
    ]

    # ── 历史匹配区间 ──
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
                start_idx = next(
                    (i for i, b in enumerate(bars)
                     if str(b.dt)[:10] == match_start), end_idx - len(matched) + 1)
                end_slice = min(end_idx + 31, len(bars))
                extended = bars[start_idx:end_slice]
                if extended:
                    h_base = extended[0].close
                    match_len = len(matched)
                    historical_series = [
                        {
                            "time": str(b.dt)[:10],
                            "value": round((b.close / h_base - 1) * 100, 2),
                            "is_future": idx >= match_len,
                        }
                        for idx, b in enumerate(extended)
                    ]

    return {
        "current": current_series,
        "historical": historical_series,
        "window": len(selected),
    }


@router.get("/kline/{index_name}")
def get_analog_kline(
    index_name: str,
    start_date: str = Query("", description="起始日期"),
    end_date: str = Query("", description="结束日期"),
):
    """
    获取指数完整K线数据（OHLCV + MACD），用于历史对照页面的图表选区。

    返回全部历史K线 + MACD 数据，前端用来画图 + 选日期区间。
    """
    code = config.INDEX_AK_CODES.get(index_name)
    if not code:
        raise HTTPException(status_code=404, detail=f"未知指数: {index_name}")

    from signals.data.fetcher import AKShareSource
    fetcher = AKShareSource()

    try:
        bars = fetcher.get_index_daily(code, lookback_days=config.ANALOG_LOOKBACK_DAYS)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"数据加载失败: {e}")

    if not bars:
        return {"ohlcv": [], "macd": [], "ma_lines": []}

    # 如果指定了日期范围，只返回范围内的数据
    if start_date and end_date:
        # 额外向前取 60 根（MA/MACD 预热）
        all_dates = [str(b.dt)[:10] for b in bars]
        try:
            start_i = next(i for i, d in enumerate(all_dates) if d >= start_date)
            end_i = next(i for i in range(len(all_dates) - 1, -1, -1) if all_dates[i] <= end_date)
            pre_i = max(0, start_i - 60)
            bars = bars[pre_i:end_i + 1]
        except StopIteration:
            pass

    # OHLCV
    ohlcv = []
    closes = []
    for b in bars:
        ohlcv.append({
            "time": str(b.dt)[:10],
            "open": round(b.open, 2),
            "high": round(b.high, 2),
            "low": round(b.low, 2),
            "close": round(b.close, 2),
            "volume": int(b.vol) if hasattr(b, 'vol') and b.vol else 0,
        })
        closes.append(b.close)

    # MACD(12,26,9)
    macd_data = []
    if len(closes) >= 26:
        import math
        ema12 = [0.0] * len(closes)
        ema26 = [0.0] * len(closes)
        k12 = 2.0 / 13.0
        k26 = 2.0 / 27.0

        ema12[0] = closes[0]
        ema26[0] = closes[0]
        for i in range(1, len(closes)):
            ema12[i] = closes[i] * k12 + ema12[i - 1] * (1 - k12)
            ema26[i] = closes[i] * k26 + ema26[i - 1] * (1 - k26)

        dif = [ema12[i] - ema26[i] for i in range(len(closes))]
        dea = [0.0] * len(closes)
        k9 = 2.0 / 10.0
        dea[0] = dif[0]
        for i in range(1, len(closes)):
            dea[i] = dif[i] * k9 + dea[i - 1] * (1 - k9)

        macd_bar = [(dif[i] - dea[i]) * 2 for i in range(len(closes))]

        # 只取 EMA 预热完成后的数据 (跳过前 25 根)
        for i in range(25, len(closes)):
            macd_data.append({
                "time": ohlcv[i]["time"],
                "dif": round(dif[i], 4),
                "dea": round(dea[i], 4),
                "bar": round(macd_bar[i], 4),
            })

    # MA 均线
    ma_lines = []
    for period, label, color in [
        (5, "MA5", "#f7931a"),
        (13, "MA13", "#e040fb"),
        (21, "MA21", "#2962ff"),
        (55, "MA55", "#26a69a"),
        (144, "MA144", "#787b86"),
    ]:
        if len(closes) >= period:
            line_data = []
            for i in range(period - 1, len(closes)):
                avg = sum(closes[i - period + 1:i + 1]) / period
                line_data.append({
                    "time": ohlcv[i]["time"],
                    "value": round(avg, 2),
                })
            ma_lines.append({"label": label, "color": color, "data": line_data})

    return {
        "ohlcv": ohlcv,
        "macd": macd_data,
        "ma_lines": ma_lines,
        "total_bars": len(ohlcv),
    }
