# -*- coding: utf-8 -*-
"""
CZSC 对象 → JSON 序列化工具

将 czsc 库的 BI / FX / RawBar 等 C/Rust 加速对象转换为可 JSON 序列化的 dict。
"""
from datetime import datetime
from typing import List, Optional

from czsc import Direction


def _dt_to_unix(dt) -> int:
    """datetime / Timestamp → UNIX timestamp (秒级，Lightweight Charts 要求)"""
    if dt is None:
        return 0
    if hasattr(dt, 'timestamp'):
        return int(dt.timestamp())
    return 0


def _dt_to_str(dt) -> str:
    """datetime → ISO 格式字符串"""
    if dt is None:
        return ""
    if hasattr(dt, 'strftime'):
        return dt.strftime("%Y-%m-%d %H:%M")
    return str(dt)


def serialize_bars(analyzer) -> list:
    """
    SymbolAnalyzer.bars_raw → Lightweight Charts OHLCV 格式
    [{time: unix_ts, open, high, low, close, volume}, ...]
    """
    bars = []
    for bar in analyzer.bars_raw:
        bars.append({
            "time": _dt_to_unix(bar.dt),
            "open": round(bar.open, 4),
            "high": round(bar.high, 4),
            "low": round(bar.low, 4),
            "close": round(bar.close, 4),
            "volume": round(bar.vol, 2) if hasattr(bar, 'vol') else 0,
        })
    return bars


def serialize_bi_list(analyzer) -> list:
    """
    SymbolAnalyzer.bi_list → 笔端点列表
    [{sdt, edt, high, low, direction, power}, ...]
    """
    bis = []
    for bi in analyzer.finished_bis:
        try:
            direction = "up" if bi.direction == Direction.Up else "down"
        except Exception:
            direction = "unknown"
        bis.append({
            "sdt": _dt_to_unix(bi.sdt if hasattr(bi, 'sdt') else bi.fx_a.dt),
            "edt": _dt_to_unix(bi.edt if hasattr(bi, 'edt') else bi.fx_b.dt),
            "high": round(bi.high, 4),
            "low": round(bi.low, 4),
            "direction": direction,
            "power": round(bi.power_price, 4) if hasattr(bi, 'power_price') else 0,
        })
    return bis


def serialize_fx_list(analyzer) -> list:
    """
    SymbolAnalyzer.fx_list → 分型标记
    [{dt, fx, mark}, ...]
    """
    fxs = []
    for fx in analyzer.fx_list:
        try:
            mark_str = str(fx.mark).lower()  # Rust enum: "顶" / "底" or "Top" / "Bottom"
            if "顶" in mark_str or "top" in mark_str:
                mark = "top"
            elif "底" in mark_str or "bottom" in mark_str:
                mark = "bottom"
            else:
                mark = "unknown"
        except Exception:
            mark = "unknown"
        fxs.append({
            "dt": _dt_to_unix(fx.dt),
            "fx": round(fx.fx, 4),
            "mark": mark,
        })
    return fxs


def serialize_zhongshu(analyzer) -> list:
    """
    从 finished_bis 中提取所有可识别的中枢。
    中枢定义：至少 3 笔有重叠区间。
    [{zd, zg, start_dt, end_dt}, ...]
    """
    bis = analyzer.finished_bis
    if len(bis) < 3:
        return []

    result = []
    i = 0
    while i < len(bis) - 2:
        b1, b2, b3 = bis[i], bis[i + 1], bis[i + 2]
        zd = max(b1.low, b3.low)
        zg = min(b1.high, b3.high)
        if zg > zd:
            # 有效中枢，尝试向后延伸
            end_idx = i + 2
            for j in range(i + 3, len(bis)):
                if bis[j].low < zg and bis[j].high > zd:
                    end_idx = j
                else:
                    break
            start_dt = _dt_to_unix(
                bis[i].sdt if hasattr(bis[i], 'sdt') else bis[i].fx_a.dt)
            end_dt = _dt_to_unix(
                bis[end_idx].edt if hasattr(bis[end_idx], 'edt')
                else bis[end_idx].fx_b.dt)
            result.append({
                "zd": round(zd, 4),
                "zg": round(zg, 4),
                "start_dt": start_dt,
                "end_dt": end_dt,
                "bi_count": end_idx - i + 1,
            })
            i = end_idx + 1
        else:
            i += 1
    return result


def serialize_signals(signals: list) -> list:
    """
    SignalEvent 列表 → JSON
    [{dt, type, freq, price, confidence, details}, ...]
    """
    return [
        {
            "dt": _dt_to_unix(s.dt),
            "type": s.signal_type,
            "freq": s.freq,
            "price": round(s.price, 4),
            "confidence": round(s.confidence, 2),
            "details": s.details,
        }
        for s in signals
    ]


def _serialize_ma_context(ma_ctx) -> Optional[dict]:
    """MAContext → JSON dict"""
    if ma_ctx is None:
        return None
    return {
        "latest_price": ma_ctx.latest_price,
        "trend_summary": ma_ctx.trend_summary,
        "levels": [
            {
                "name": lv.name,
                "value": round(lv.value, 2),
                "distance_pct": lv.distance_pct,
                "position": lv.position,
            }
            for lv in ma_ctx.levels
        ],
        "support_levels": [
            {
                "name": lv.name,
                "value": round(lv.value, 2),
                "distance_pct": lv.distance_pct,
            }
            for lv in ma_ctx.support_levels[:3]
        ],
        "resistance_levels": [
            {
                "name": lv.name,
                "value": round(lv.value, 2),
                "distance_pct": lv.distance_pct,
            }
            for lv in ma_ctx.resistance_levels[:3]
        ],
        "key_levels": [
            {
                "name": lv.name,
                "value": round(lv.value, 2),
                "distance_pct": lv.distance_pct,
                "position": lv.position,
            }
            for lv in ma_ctx.key_levels
        ],
    }


def serialize_index_report(report) -> dict:
    """IndexReport → JSON dict"""
    zs_to_dict = lambda zs: (
        {"zd": round(zs.zd, 4), "zg": round(zs.zg, 4), "bi_count": zs.bi_count}
        if zs else None
    )

    result = {
        "name": report.name,
        "symbol": report.symbol,
        "data_available": report.data_available,
        "latest_price": round(report.latest_price, 2) if report.latest_price else 0,
        "daily_last_dt": _dt_to_str(report.daily_last_dt),
        "snapshot_price": round(report.snapshot_price, 2) if report.snapshot_price else 0,
        "snapshot_dt": _dt_to_str(report.snapshot_dt),
        "snapshot_freq": report.snapshot_freq or "",
        "intraday_change": report.intraday_change,
        "summary": report.summary,
        # 日线
        "daily_trend": report.daily_trend,
        "daily_last_direction": report.daily_last_direction,
        "daily_latest_signal": report.daily_latest_signal,
        "daily_bi_count": report.daily_bi_count,
        "daily_zs": zs_to_dict(report.daily_zs),
        # 30M
        "f30_trend": report.f30_trend,
        "f30_last_direction": report.f30_last_direction,
        "f30_latest_signal": report.f30_latest_signal,
        "f30_bi_count": report.f30_bi_count,
        "f30_zs": zs_to_dict(report.f30_zs),
        # 15M
        "f15_trend": report.f15_trend,
        "f15_last_direction": report.f15_last_direction,
        "f15_latest_signal": report.f15_latest_signal,
        "f15_bi_count": report.f15_bi_count,
        "f15_zs": zs_to_dict(report.f15_zs),
        # 计算属性
        "has_buy_signal": report.has_buy_signal,
        "has_sell_signal": report.has_sell_signal,
        "is_bullish": report.is_bullish,
        "three_level_aligned": report.three_level_aligned,
        # MA 均线关键位
        "ma_context": _serialize_ma_context(getattr(report, 'ma_context', None)),
        # P3-2: 情景分叉
        "scenarios": _serialize_scenarios(getattr(report, 'scenario_branches', None)),
        # P3-5: 近5日收益率
        "recent_5d_return": getattr(report, 'recent_5d_return', None),
    }
    return result


def _serialize_scenarios(branches) -> list:
    """ScenarioBranch[] → JSON list"""
    if not branches:
        return []
    return [
        {
            "level_name": b.level_name,
            "level_price": round(b.level_price, 2),
            "distance_pct": round(b.distance_pct, 2),
            "is_support": b.is_support,
            "urgency": b.urgency,
            "hold": b.hold,
            "break": b.break_,
        }
        for b in branches
    ]


def serialize_market_context(ctx) -> dict:
    """MarketContext → JSON dict (不含 reports，reports 单独序列化)"""
    result = {
        "overall_direction": ctx.overall_direction,
        "direction_strength": round(ctx.direction_strength, 2),
        "structural_divergence": ctx.structural_divergence,
        "growth_vs_value": ctx.growth_vs_value,
        "recommended_style": ctx.recommended_style,
        "gate_industry_scan": ctx.gate_industry_scan,
        "sentiment_phase": ctx.sentiment_phase,
        "divergence_score": round(ctx.divergence_score, 1),
        "position_suggestion": ctx.position_suggestion,
        "rotation_stage": ctx.rotation_stage,
        "rotation_detail": ctx.rotation_detail,
        "allocation_suggestion": ctx.allocation_suggestion,
        "buy_indices": ctx.buy_indices,
        "sell_indices": ctx.sell_indices,
        "bullish_indices": ctx.bullish_indices,
        "bearish_indices": ctx.bearish_indices,
        "shield_sectors": ctx.shield_sectors,
        "sword_sectors": ctx.sword_sectors,
        "summary": ctx.summary,
        # P3-4: 轮动持续时间 & 速度
        "rotation_duration": getattr(ctx, "rotation_duration", 0),
        "rotation_velocity": getattr(ctx, "rotation_velocity", "稳定"),
        "rotation_peak_warning": getattr(ctx, "rotation_peak_warning", False),
        "rotation_peak_detail": getattr(ctx, "rotation_peak_detail", ""),
        # P3-5: 风格切换
        "style_switch": _serialize_style_switch(getattr(ctx, "style_switch", None)),
    }
    return result


def _serialize_style_switch(sw) -> Optional[dict]:
    """StyleSwitch → JSON dict"""
    if sw is None:
        return None
    return {
        "detected": sw.detected,
        "direction": sw.direction,
        "evidence": sw.evidence,
        "confidence": sw.confidence,
        "suggestion": sw.suggestion,
    }


def serialize_scored_symbol(scored) -> dict:
    """ScoredSymbol → JSON dict"""
    result = {
        "symbol": scored.symbol,
        "name": getattr(scored, "name", "") or scored.symbol,
        "total_score": round(scored.total_score, 1),
        "fused_total": round(scored.fused_total, 1) if getattr(scored, "fused_total", 0) else None,
        "signal_count": scored.signal_count,
        "direction": scored.direction,
        "ma_confirmation": scored.ma_confirmation,
        "details": scored.details,
        "signals": serialize_signals(scored.signals),
        # P3-1: 情绪标签
        "sentiment_tag": getattr(scored, "sentiment_tag", ""),
    }
    # 异常画像（精简版，给 Dashboard 用）
    ap = getattr(scored, "anomaly_profile", None)
    if ap:
        result["anomaly"] = {
            "items": [
                {"name": item.name, "z_score": round(item.z_score, 2),
                 "is_anomaly": item.is_anomaly, "label": item.label}
                for item in ap.items.values()
            ],
            "anomaly_count": ap.anomaly_count,
            "convergence": ap.convergence,
            "capitulation_score": round(ap.capitulation_score, 1),
        }
    else:
        result["anomaly"] = None
    # 融合置信度
    fs = getattr(scored, "fused_score", None)
    if fs:
        result["confidence_level"] = fs.confidence_level
    # 社交舆情
    result["social_heat"] = getattr(scored, "social_heat", "")
    result["social_tag"] = getattr(scored, "social_tag", "")
    result["theme_tags"] = getattr(scored, "theme_tags", []) or []
    return result


# ─────────────────────────────────────────────────────────
# MA / MACD 共享计算（chart.py + industry detail 复用）
# ─────────────────────────────────────────────────────────

def compute_ma_lines(bars_raw) -> list:
    """
    从 bars_raw (RawBar list) 计算 MA5/10/20/60 线。
    返回 [{label, color, data: [{time, value}]}]
    """
    from signals.core.ma_levels import _bars_to_df
    df = _bars_to_df(bars_raw)
    closes = df["close"]

    ma_lines = []
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
    return ma_lines


def compute_macd(bars_raw) -> list:
    """
    从 bars_raw (RawBar list) 计算 MACD（DIF/DEA/BAR）。
    返回 [{time, dif, dea, bar}]
    """
    from signals.core.ma_levels import _bars_to_df
    df = _bars_to_df(bars_raw)
    closes = df["close"]

    if len(closes) < 26:
        return []

    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd_bar = (dif - dea) * 2

    result = []
    for dt_idx in dif.dropna().index:
        if dt_idx in dea.dropna().index:
            result.append({
                "time": int(dt_idx.timestamp()),
                "dif": round(float(dif[dt_idx]), 4),
                "dea": round(float(dea[dt_idx]), 4),
                "bar": round(float(macd_bar[dt_idx]), 4),
            })
    return result
