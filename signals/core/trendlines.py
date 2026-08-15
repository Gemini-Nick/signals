"""K-line-aware, multi-timeframe support/resistance trendline analysis.

The detector works on normalized OHLCV bars. It scores anchors using candle
prominence, wick rejection, ATR distance, and volume participation instead of
connecting arbitrary local extrema. ``analyze_multitimeframe_trendlines``
keeps higher-timeframe context separate from current-chart trigger signals.
"""
from __future__ import annotations

from statistics import median
from typing import Any, Mapping

import pandas as pd


PIVOT_WINDOW = 2
LINE_TOLERANCE = 0.012
NEAR_LINE_TOLERANCE = 0.015
BREAK_TOLERANCE = 0.006
MIN_PAIR_SPAN = 4
ATR_PERIOD = 14
# Keep low-prominence pivots available for scoring; thin/volatile instruments
# often form usable swing anchors below one full ATR.  The line score, touch
# count, rejection ratio, and volume fields decide whether they are credible.
MIN_PROMINENCE_ATR = 0.05
MIN_VOLUME_CONFIRMATION = 1.15
TIMEFRAME_ORDER = ("weekly", "daily", "60min", "30min", "15min", "5min")


def _iso(value: Any) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(parsed) else parsed.isoformat()


def _clean_frame(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    if not {"high", "low", "close"}.issubset(frame.columns):
        return pd.DataFrame()
    out = frame.copy().sort_index()
    for column in ("open", "high", "low", "close", "vol", "volume", "amount", "turnover"):
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    if "open" not in out.columns:
        out["open"] = out["close"]
    if "vol" not in out.columns and "volume" in out.columns:
        out["vol"] = out["volume"]
    if "vol" not in out.columns:
        out["vol"] = pd.NA
    return out.dropna(subset=["high", "low", "close"])


def _atr(frame: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / max(2, period), adjust=False, min_periods=1).mean().bfill().fillna(1.0)


def _volume_ratio(frame: pd.DataFrame, period: int = 20) -> pd.Series:
    volume = pd.to_numeric(frame["vol"], errors="coerce")
    baseline = volume.rolling(period, min_periods=5).median()
    ratio = volume / baseline.replace(0, pd.NA)
    return ratio.replace([float("inf"), float("-inf")], pd.NA).fillna(1.0)


def _pivot_quality(
    frame: pd.DataFrame,
    index: int,
    column: str,
    prominence: float,
    atr_value: float,
    volume_ratio: float,
) -> tuple[float, float]:
    row = frame.iloc[index]
    candle_range = max(float(row["high"]) - float(row["low"]), 0.000001)
    if column == "high":
        rejection = (float(row["high"]) - max(float(row["open"]), float(row["close"]))) / candle_range
    else:
        rejection = (min(float(row["open"]), float(row["close"])) - float(row["low"])) / candle_range
    prominence_score = min(1.0, max(0.0, prominence / max(atr_value, 0.000001) / 1.5))
    volume_score = min(1.0, max(0.0, volume_ratio / 1.5))
    quality = min(1.0, 0.45 * prominence_score + 0.30 * max(0.0, rejection) + 0.25 * volume_score)
    return round(quality, 4), round(max(0.0, rejection), 4)


def _pivots(frame: pd.DataFrame, column: str, window: int, atr: pd.Series, volume: pd.Series) -> list[dict[str, Any]]:
    values = frame[column].tolist()
    output: list[dict[str, Any]] = []
    for index in range(window, len(values) - window):
        current = float(values[index])
        left = [float(value) for value in values[index - window:index]]
        right = [float(value) for value in values[index + 1:index + window + 1]]
        if column == "high":
            is_pivot = current >= max(left) and current >= max(right) and (current > max(left) or current > max(right))
            prominence = current - max(left + right)
        else:
            is_pivot = current <= min(left) and current <= min(right) and (current < min(left) or current < min(right))
            prominence = min(left + right) - current
        atr_value = float(atr.iloc[index])
        if not is_pivot or prominence < atr_value * MIN_PROMINENCE_ATR:
            continue
        volume_ratio = float(volume.iloc[index])
        quality, rejection = _pivot_quality(frame, index, column, prominence, atr_value, volume_ratio)
        output.append(
            {
                "index": index,
                "price": current,
                "dt": _iso(frame.index[index]),
                "atr": round(atr_value, 6),
                "prominence": round(float(prominence), 6),
                "prominence_atr": round(float(prominence) / max(atr_value, 0.000001), 4),
                "volume_ratio": round(volume_ratio, 4),
                "rejection_ratio": rejection,
                "quality": quality,
            }
        )
    return output


def _line_value(start: dict[str, Any], end: dict[str, Any], index: int) -> float:
    span = max(1, int(end["index"]) - int(start["index"]))
    slope = (float(end["price"]) - float(start["price"])) / span
    return float(start["price"]) + slope * (index - int(start["index"]))


def _line_tolerance(line_price: float, pivot: dict[str, Any]) -> float:
    return max(abs(line_price) * LINE_TOLERANCE, float(pivot.get("atr") or 0.0) * 0.8)


def _candidate_payload(
    start: dict[str, Any],
    end: dict[str, Any],
    touches: list[dict[str, Any]],
    *,
    kind: str,
    direction: str,
    latest_index: int,
    score: float,
) -> dict[str, Any]:
    span = max(1, int(end["index"]) - int(start["index"]))
    slope = (float(end["price"]) - float(start["price"])) / span
    projected = _line_value(start, end, latest_index)
    return {
        "kind": kind,
        "direction": direction,
        "start_dt": start["dt"],
        "end_dt": end["dt"],
        "start_index": int(start["index"]),
        "end_index": int(end["index"]),
        "projection_dt": "",
        "start_price": round(float(start["price"]), 6),
        "end_price": round(float(end["price"]), 6),
        "projected_price": round(projected, 6),
        "slope_per_bar": round(slope, 8),
        "anchor_count": len(touches),
        "anchor_quality": round(sum(float(item.get("quality") or 0.0) for item in touches) / max(1, len(touches)), 4),
        "touch_dates": [item["dt"] for item in touches],
        "touch_rejection_ratio": round(median([float(item.get("rejection_ratio") or 0.0) for item in touches]), 4),
        "touch_volume_ratio": round(median([float(item.get("volume_ratio") or 1.0) for item in touches]), 4),
        "score": round(score, 3),
    }


def _trend_candidate(
    pivots: list[dict[str, Any]],
    *,
    kind: str,
    direction: str,
    latest_index: int,
) -> dict[str, Any] | None:
    if len(pivots) < 2:
        return None
    best: dict[str, Any] | None = None
    for left_index, start in enumerate(pivots[:-1]):
        for end in pivots[left_index + 1:]:
            span = int(end["index"]) - int(start["index"])
            if span < MIN_PAIR_SPAN:
                continue
            slope = (float(end["price"]) - float(start["price"])) / span
            if direction == "descending" and slope >= 0:
                continue
            if direction == "ascending" and slope <= 0:
                continue
            touches: list[dict[str, Any]] = []
            violated = False
            for pivot in pivots:
                if int(pivot["index"]) < int(start["index"]):
                    continue
                line_price = _line_value(start, end, int(pivot["index"]))
                distance = float(pivot["price"]) - line_price
                tolerance = _line_tolerance(line_price, pivot)
                if abs(distance) <= tolerance:
                    touches.append(pivot)
                if int(pivot["index"]) > int(start["index"]):
                    if kind == "resistance" and distance > tolerance:
                        violated = True
                    if kind == "support" and distance < -tolerance:
                        violated = True
            if len(touches) < 2 or violated:
                continue
            quality = sum(float(item.get("quality") or 0.0) for item in touches)
            volume_bonus = sum(min(1.0, float(item.get("volume_ratio") or 1.0) / 1.5) for item in touches)
            score = len(touches) * 35 + quality * 18 + volume_bonus * 6 + span * 0.22 + int(end["index"]) * 0.01
            candidate = _candidate_payload(start, end, touches, kind=kind, direction=direction, latest_index=latest_index, score=score)
            if best is None or candidate["score"] > best["score"]:
                best = candidate
    return best


def _horizontal_candidate(pivots: list[dict[str, Any]], *, kind: str) -> dict[str, Any] | None:
    if len(pivots) < 2:
        return None
    best: dict[str, Any] | None = None
    for anchor in pivots[-12:]:
        cluster = [
            pivot
            for pivot in pivots
            if abs(float(pivot["price"]) - float(anchor["price"]))
            <= max(abs(float(anchor["price"])) * LINE_TOLERANCE, float(pivot.get("atr") or 0.0) * 0.8)
        ]
        if len(cluster) < 2:
            continue
        level = float(median([float(pivot["price"]) for pivot in cluster]))
        quality = sum(float(item.get("quality") or 0.0) for item in cluster)
        score = len(cluster) * 35 + quality * 18 + int(cluster[-1]["index"]) * 0.01
        candidate = {
            "kind": kind,
            "direction": "horizontal",
            "start_dt": cluster[0]["dt"],
            "end_dt": cluster[-1]["dt"],
            "start_index": int(cluster[0]["index"]),
            "end_index": int(cluster[-1]["index"]),
            "projection_dt": "",
            "start_price": round(level, 6),
            "end_price": round(level, 6),
            "projected_price": round(level, 6),
            "slope_per_bar": 0.0,
            "anchor_count": len(cluster),
            "anchor_quality": round(quality / len(cluster), 4),
            "touch_dates": [pivot["dt"] for pivot in cluster],
            "touch_rejection_ratio": round(median([float(item.get("rejection_ratio") or 0.0) for item in cluster]), 4),
            "touch_volume_ratio": round(median([float(item.get("volume_ratio") or 1.0) for item in cluster]), 4),
            "score": round(score, 3),
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate
    return best


def _signal_for_line(line: dict[str, Any], frame: pd.DataFrame, *, freq: str, source: str) -> dict[str, Any] | None:
    if len(frame) < 2:
        return None
    latest_row = frame.iloc[-1]
    previous_row = frame.iloc[-2]
    latest = float(latest_row["close"])
    previous = float(previous_row["close"])
    latest_level = float(line["projected_price"])
    previous_level = _line_value(
        {"index": int(line.get("start_index", 0)), "price": line["start_price"]},
        {"index": int(line.get("end_index", len(frame) - 1)), "price": line.get("end_price", line["projected_price"])},
        len(frame) - 2,
    )
    distance_pct = (latest - latest_level) / max(abs(latest_level), 0.000001)
    candle_range = max(float(latest_row["high"]) - float(latest_row["low"]), 0.000001)
    body_ratio = abs(float(latest_row["close"]) - float(latest_row["open"])) / candle_range
    volume_ratio = float(_volume_ratio(frame).iloc[-1])
    has_volume = pd.notna(latest_row.get("vol"))
    is_resistance = line["kind"] == "resistance"
    crossed_up = previous <= previous_level * (1 + BREAK_TOLERANCE) and latest > latest_level * (1 + BREAK_TOLERANCE)
    crossed_down = previous >= previous_level * (1 - BREAK_TOLERANCE) and latest < latest_level * (1 - BREAK_TOLERANCE)
    wick_touch = (
        float(latest_row["high"]) >= latest_level * (1 - NEAR_LINE_TOLERANCE)
        if is_resistance
        else float(latest_row["low"]) <= latest_level * (1 + NEAR_LINE_TOLERANCE)
    )
    body_confirmed = body_ratio >= 0.35
    volume_confirmed = not has_volume or volume_ratio >= MIN_VOLUME_CONFIRMATION
    if is_resistance and (crossed_up or distance_pct > BREAK_TOLERANCE):
        signal_type, side, status = "突破下降趋势线" if line["direction"] == "descending" else "突破水平阻力", "buy", "breakout"
    elif not is_resistance and (crossed_down or distance_pct < -BREAK_TOLERANCE):
        signal_type, side, status = "跌破上升趋势线" if line["direction"] == "ascending" else "跌破水平支撑", "sell", "breakdown"
    elif is_resistance and (distance_pct >= -NEAR_LINE_TOLERANCE or wick_touch):
        signal_type, side, status = "下降趋势线阻力" if line["direction"] == "descending" else "水平阻力测试", "sell", "resistance_test"
    elif not is_resistance and (distance_pct <= NEAR_LINE_TOLERANCE or wick_touch):
        signal_type, side, status = "上升趋势线支撑" if line["direction"] == "ascending" else "水平支撑回踩", "buy", "support_test"
    else:
        return None
    confirmation = "confirmed" if body_confirmed and volume_confirmed else "watch"
    confidence = min(
        0.95,
        0.45
        + min(0.25, float(line.get("anchor_count") or 0) * 0.07)
        + min(0.12, float(line.get("anchor_quality") or 0.0) * 0.12)
        + (0.08 if body_confirmed else 0.0)
        + (0.05 if volume_confirmed else 0.0)
        + min(0.10, abs(distance_pct) * 1.5),
    )
    return {
        "dt": _iso(frame.index[-1]),
        "date_str": _iso(frame.index[-1])[:10],
        "type": signal_type,
        "signal_type": signal_type,
        "signal_side": side,
        "signal_family": "trendline",
        "status": status,
        "confirmation": confirmation,
        "actionable": confirmation == "confirmed" or not has_volume,
        "body_ratio": round(body_ratio, 4),
        "volume_ratio": round(volume_ratio, 4),
        "wick_touch": wick_touch,
        "price": round(latest, 6),
        "level_price": round(latest_level, 6),
        "distance_pct": round(distance_pct * 100, 4),
        "confidence": round(confidence, 4),
        "freq": freq,
        "timeframe": freq,
        "source": source or "normalized_ohlcv",
        "details": f"{signal_type}，线位{latest_level:.2f}，距线{distance_pct * 100:+.2f}% · {line['anchor_count']}个K线锚点 · {confirmation}",
        "trendline": line,
    }


def analyze_trendlines(
    frame: pd.DataFrame | None,
    *,
    freq: str = "daily",
    source: str = "",
    lookback: int = 260,
    pivot_window: int = PIVOT_WINDOW,
) -> dict[str, list[dict[str, Any]]]:
    """Return scored K-line geometry and latest trendline signals for one timeframe."""
    working = _clean_frame(frame)
    if len(working) < max(12, pivot_window * 2 + MIN_PAIR_SPAN):
        return {"trendlines": [], "signals": []}
    working = working.tail(max(lookback, 20))
    atr = _atr(working)
    volume = _volume_ratio(working)
    highs = _pivots(working, "high", pivot_window, atr, volume)
    lows = _pivots(working, "low", pivot_window, atr, volume)
    latest_index = len(working) - 1
    candidates = [
        _trend_candidate(highs, kind="resistance", direction="descending", latest_index=latest_index),
        _trend_candidate(lows, kind="support", direction="ascending", latest_index=latest_index),
        _horizontal_candidate(lows, kind="support"),
        _horizontal_candidate(highs, kind="resistance"),
    ]
    trendlines = [line for line in candidates if line]
    projection_dt = _iso(working.index[-1])
    for line in trendlines:
        line["projection_dt"] = projection_dt
        line["timeframe"] = freq
        latest_level = float(line["projected_price"])
        close = float(working["close"].iloc[-1])
        distance_pct = (close - latest_level) / max(abs(latest_level), 0.000001)
        line["distance_pct"] = round(distance_pct * 100, 4)
        if line["kind"] == "resistance":
            line["status"] = "breakout" if distance_pct > BREAK_TOLERANCE else "resistance_test" if distance_pct >= -NEAR_LINE_TOLERANCE else "below_resistance"
        else:
            line["status"] = "breakdown" if distance_pct < -BREAK_TOLERANCE else "support_test" if distance_pct <= NEAR_LINE_TOLERANCE else "above_support"
        line["confidence"] = round(min(0.95, 0.45 + min(0.25, line["anchor_count"] * 0.07) + min(0.15, line["anchor_quality"] * 0.15)), 4)
        line["source"] = source or "normalized_ohlcv"
    signals = [signal for line in trendlines if (signal := _signal_for_line(line, working, freq=freq, source=source))]
    return {"trendlines": trendlines, "signals": signals}


def analyze_multitimeframe_trendlines(
    frames: Mapping[str, pd.DataFrame] | None,
    *,
    primary_freq: str = "daily",
    source: str = "",
) -> dict[str, Any]:
    """Analyze each supplied timeframe and return context plus trigger layers."""
    if not frames:
        return {"primary_freq": primary_freq, "timeframes": {}, "trendlines": [], "signals": [], "context": {"alignment": "neutral", "confluence_count": 0}}
    ordered = sorted(frames, key=lambda freq: (TIMEFRAME_ORDER.index(freq) if freq in TIMEFRAME_ORDER else len(TIMEFRAME_ORDER), freq))
    results: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for freq in ordered:
        results[freq] = analyze_trendlines(frames[freq], freq=freq, source=source)
    primary = results.get(primary_freq) or next(iter(results.values()))
    flat_lines: list[dict[str, Any]] = []
    flat_signals: list[dict[str, Any]] = []
    for freq in ordered:
        flat_lines.extend(results[freq]["trendlines"])
        flat_signals.extend(results[freq]["signals"])
    confluence_count = 0
    for line in flat_lines:
        same_kind = [
            other for other in flat_lines
            if other["kind"] == line["kind"]
            and abs(float(other["projected_price"]) - float(line["projected_price"])) / max(abs(float(line["projected_price"])), 0.000001) <= LINE_TOLERANCE
        ]
        line["confluence_count"] = len(same_kind)
        if len(same_kind) > 1:
            confluence_count += 1
    higher = [line for freq in ("weekly", "daily") if freq in results for line in results[freq]["trendlines"]]
    bullish = sum(1 for line in higher if (line["kind"] == "support" and line["status"] in {"above_support", "support_test"}) or (line["kind"] == "resistance" and line["status"] == "breakout"))
    bearish = sum(1 for line in higher if (line["kind"] == "support" and line["status"] == "breakdown") or (line["kind"] == "resistance" and line["status"] == "below_resistance"))
    alignment = "bullish" if bullish > bearish else "bearish" if bearish > bullish else "mixed" if bullish or bearish else "neutral"
    return {
        "primary_freq": primary_freq,
        "timeframes": results,
        "trendlines": flat_lines,
        "signals": flat_signals,
        "context": {
            "alignment": alignment,
            "higher_timeframe_bullish": bullish,
            "higher_timeframe_bearish": bearish,
            "confluence_count": confluence_count,
            "primary_line_count": len(primary["trendlines"]),
            "timeframe_states": {
                freq: {"line_count": len(results[freq]["trendlines"]), "signal_count": len(results[freq]["signals"])}
                for freq in ordered
            },
        },
    }
