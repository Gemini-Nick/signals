# -*- coding: utf-8 -*-
"""Deterministic chart-pattern semantics for index/ETF chart rows."""
from __future__ import annotations

from typing import Any

KEY_MA_PERIODS = (5, 8, 10, 13, 20, 21)
MA_NEAR_PCT = 1.0


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _freq_bucket(freq: Any) -> str:
    raw = _text(freq).lower()
    if raw in {"w", "week", "weekly", "1w", "周", "周线"}:
        return "weekly"
    if raw in {"m", "month", "monthly", "1m", "月", "月线"}:
        return "monthly"
    if raw in {"d", "day", "daily", "1d", "日", "日线"}:
        return "daily"
    return raw or "daily"


def _line_name(period: int, bucket: str) -> str:
    if bucket == "weekly":
        return f"{period}周线"
    if bucket == "monthly":
        return f"{period}月线"
    return f"MA{period}" if bucket not in {"daily", ""} else f"MA{period}"


def _rolling_mean(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    numerator = sum((idx - x_mean) * (value - y_mean) for idx, value in enumerate(values))
    denominator = sum((idx - x_mean) ** 2 for idx in range(n))
    return numerator / denominator if denominator else 0.0


def _normalise_rows(rows: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows or []:
        close = _float(_row_value(row, "close"))
        if close is None or close <= 0:
            continue
        open_ = _float(_row_value(row, "open"), close) or close
        high = max(close, open_, _float(_row_value(row, "high"), close) or close)
        low = min(close, open_, _float(_row_value(row, "low"), close) or close)
        out.append({
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "time": _row_value(row, "time", _row_value(row, "dt", _row_value(row, "date"))),
        })
    return out


def _channel_state(rows: list[dict[str, Any]], bucket: str) -> dict[str, Any]:
    if len(rows) < 5:
        return {"type": "none", "confidence": 0.0, "evidence": []}
    recent3 = rows[-3:]
    highs3 = [row["high"] for row in recent3]
    lows3 = [row["low"] for row in recent3]
    lower_highs3 = highs3[0] > highs3[1] > highs3[2]
    lower_lows3 = lows3[0] > lows3[1] > lows3[2]
    higher_highs3 = highs3[0] < highs3[1] < highs3[2]
    higher_lows3 = lows3[0] < lows3[1] < lows3[2]

    recent5 = rows[-5:]
    highs5 = [row["high"] for row in recent5]
    lows5 = [row["low"] for row in recent5]
    closes5 = [row["close"] for row in recent5]
    high_slope = _slope(highs5)
    low_slope = _slope(lows5)
    close_slope = _slope(closes5)
    latest_close = closes5[-1]
    slope_floor = max(abs(latest_close) * 0.0002, 0.01)
    evidence: list[str] = []

    if lower_highs3 and lower_lows3:
        evidence.append("最近三根K线高点、低点连续下移")
        confidence = 0.88 if bucket == "weekly" else 0.82
        return {"type": "descending_channel", "confidence": confidence, "evidence": evidence}
    if high_slope < -slope_floor and low_slope < -slope_floor and close_slope < -slope_floor:
        evidence.append("最近五根K线高点、低点、收盘重心同步下移")
        return {"type": "descending_channel", "confidence": 0.74, "evidence": evidence}
    if higher_highs3 and higher_lows3:
        evidence.append("最近三根K线高点、低点连续抬高")
        return {"type": "ascending_channel", "confidence": 0.82, "evidence": evidence}
    return {"type": "none", "confidence": 0.0, "evidence": []}


def _interaction_for_level(
    *,
    row: dict[str, Any],
    ma_value: float,
    previous_close: float | None,
    previous_ma: float | None,
) -> str:
    close = row["close"]
    open_ = row["open"]
    high = row["high"]
    low = row["low"]
    candle_range = high - low
    close_position = (close - low) / candle_range if candle_range > 0 else 1.0
    touched = low <= ma_value <= high
    if touched and close >= ma_value:
        return "acceptance" if close_position >= 0.45 else "touch_reclaim"
    if touched and close < ma_value:
        was_above = bool(
            (previous_close is not None and previous_ma is not None and previous_close >= previous_ma)
            or open_ >= ma_value
        )
        return "breakdown" if was_above else "pressure_rejection"
    distance_pct = (close - ma_value) / ma_value * 100 if ma_value else 0.0
    if abs(distance_pct) <= MA_NEAR_PCT:
        return "near"
    return "above" if close > ma_value else "below"


def _line_detail(level: dict[str, Any]) -> str:
    return (
        f"收盘 {level['latest_close']:.2f} / "
        f"最低 {level['latest_low']:.2f} / "
        f"{level['name']} {level['value']:.2f} / "
        f"距线 {level['distance_pct']:+.2f}%"
    )


def _level_priority(level: dict[str, Any]) -> int:
    period_score = {21: 95, 20: 90, 13: 78, 10: 65, 8: 55, 5: 30}.get(int(level["period"]), 0)
    interaction_score = {
        "acceptance": 35,
        "touch_reclaim": 30,
        "breakdown": 30,
        "pressure_rejection": 15,
        "near": 5,
    }.get(_text(level.get("interaction")), 0)
    return period_score + interaction_score


def _primary_from_levels(levels: list[dict[str, Any]], bucket: str) -> dict[str, Any]:
    candidates = [
        level
        for level in levels
        if int(level.get("period") or 0) != 5
        and level.get("interaction") in {"acceptance", "touch_reclaim", "breakdown", "pressure_rejection"}
    ]
    if candidates:
        level = sorted(candidates, key=_level_priority, reverse=True)[0]
        interaction = level["interaction"]
        if interaction == "acceptance":
            label = f"{level['name']}回踩承接"
            signal_type = label
            side = "buy"
        elif interaction == "touch_reclaim":
            label = f"{level['name']}触线收回"
            signal_type = label
            side = "neutral"
        elif interaction == "breakdown":
            label = f"{level['name']}跌破待修复"
            signal_type = label
            side = "sell"
        else:
            label = f"{level['name']}反抽未过"
            signal_type = label
            side = "sell"
        return {
            "label": label,
            "signal_type": signal_type,
            "side": side,
            "priority": _level_priority(level),
            "timeframe": bucket,
            "details": _line_detail(level),
            "level": level,
        }

    ma5 = next((level for level in levels if int(level.get("period") or 0) == 5), None)
    if ma5 and ma5.get("latest_close") < ma5.get("value"):
        label_name = _line_name(5, bucket)
        detail = _line_detail(ma5)
        if ma5.get("latest_high") >= ma5.get("value"):
            detail = f"{detail}，盘中上探{label_name}后收回线下"
        return {
            "label": f"未站稳{label_name}",
            "signal_type": f"未站稳{label_name}",
            "side": "sell",
            "priority": 20,
            "timeframe": bucket,
            "details": detail,
            "level": ma5,
        }
    return {}


def classify_latest_chart_pattern(rows: list[Any], freq: Any = "daily") -> dict[str, Any]:
    """Classify the latest chart state from OHLC rows.

    The output is intentionally plain dict data so it can be embedded in API
    payloads, regression fixtures, and offline visual-audit reports.
    """
    bucket = _freq_bucket(freq)
    normalized = _normalise_rows(rows)
    if len(normalized) < 5:
        return {}
    latest = normalized[-1]
    closes = [row["close"] for row in normalized]
    previous_close = closes[-2] if len(closes) >= 2 else None
    levels: list[dict[str, Any]] = []
    for period in KEY_MA_PERIODS:
        ma_value = _rolling_mean(closes, period)
        if ma_value is None or ma_value <= 0:
            continue
        previous_ma = _rolling_mean(closes[:-1], period)
        distance_pct = (latest["close"] - ma_value) / ma_value * 100
        low_distance_pct = (latest["low"] - ma_value) / ma_value * 100
        high_distance_pct = (latest["high"] - ma_value) / ma_value * 100
        interaction = _interaction_for_level(
            row=latest,
            ma_value=ma_value,
            previous_close=previous_close,
            previous_ma=previous_ma,
        )
        levels.append({
            "period": period,
            "name": _line_name(period, bucket),
            "value": round(ma_value, 4),
            "previous_value": round(previous_ma, 4) if previous_ma is not None else None,
            "latest_close": round(latest["close"], 4),
            "latest_low": round(latest["low"], 4),
            "latest_high": round(latest["high"], 4),
            "above": latest["close"] >= ma_value,
            "near": abs(distance_pct) <= MA_NEAR_PCT,
            "distance_pct": round(distance_pct, 4),
            "low_distance_pct": round(low_distance_pct, 4),
            "high_distance_pct": round(high_distance_pct, 4),
            "interaction": interaction,
        })

    channel = _channel_state(normalized, bucket)
    level_primary = _primary_from_levels(levels, bucket)
    primary = {}
    channel_should_lead = (
        channel.get("type") == "descending_channel"
        and (bucket == "weekly" or int(level_primary.get("priority") or 0) < 80)
    )
    if channel_should_lead:
        ma5 = next((level for level in levels if int(level.get("period") or 0) == 5), None)
        detail_parts = list(channel.get("evidence") or [])
        if ma5 and ma5.get("latest_close") < ma5.get("value") and ma5.get("latest_high") >= ma5.get("value"):
            detail_parts.append(f"{_line_name(5, bucket)}反抽未过")
        label = "周线下降通道" if bucket == "weekly" else "日线下降通道"
        primary = {
            "label": label,
            "signal_type": label,
            "dominant_pattern": "descending_channel",
            "side": "sell",
            "priority": 140 if bucket == "weekly" else 100,
            "timeframe": bucket,
            "details": "；".join(detail_parts) if detail_parts else "高低点下移",
            "level": ma5 or {},
        }
    else:
        primary = level_primary

    return {
        "timeframe": bucket,
        "primary_chart_signal": primary,
        "level_interactions": levels,
        "channel_state": channel,
    }
