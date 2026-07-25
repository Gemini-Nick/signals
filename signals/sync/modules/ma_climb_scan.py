# -*- coding: utf-8 -*-
"""Full-market moving-average climb scan for the terminal buy/watch pools."""
from __future__ import annotations

import logging
import math
import os
from collections import OrderedDict
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.core.trading_dates import is_trading_day
from signals.sync.task_context import get_task_env

logger = logging.getLogger("signals.sync.ma_climb_scan")

CLIMB_PERIODS = (5, 10)
CLIMB_WINDOW = 5
ATR_PERIOD = 14
WATCH_SCORE = 60.0
BUY_REVIEW_SCORE = 80.0
MIN_SLOPE_ATR_PER_BAR = 0.03
MIN_R_SQUARED = 0.50
MAX_MEDIAN_DISTANCE_ATR = 1.10
MAX_P80_DISTANCE_ATR = 1.80
DEFAULT_LOOKBACK_DAYS = 260
DEFAULT_MAX_BARS_PER_SYMBOL = 180


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(get_task_env(name, os.getenv(name, str(default))) or default)))
    except (TypeError, ValueError):
        return default


def _pure_a_code(value: Any) -> str:
    raw = str(value or "").strip().upper()
    pure = raw.split(".", 1)[-1] if "." in raw else raw
    pure = pure.replace("SH", "").replace("SZ", "").replace("BJ", "")
    if pure.startswith(("900", "200")):
        return ""
    return pure if pure.isdigit() and len(pure) == 6 else ""


def _prefixed_symbol(code: str) -> str:
    if code.startswith(("6", "9")):
        return f"SH.{code}"
    if code.startswith(("4", "8")):
        return f"BJ.{code}"
    return f"SZ.{code}"


def _as_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame["dt"] = pd.to_datetime(frame["dt"], errors="coerce")
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["dt", "open", "high", "low", "close"])
    return frame.sort_values("dt").drop_duplicates("dt", keep="last").reset_index(drop=True)


def _week_has_later_trading_day(latest_day: date) -> bool:
    friday = latest_day + timedelta(days=4 - latest_day.weekday())
    day = latest_day + timedelta(days=1)
    while day <= friday:
        if is_trading_day("A", day):
            return True
        day += timedelta(days=1)
    return False


def _completed_weekly_frame(daily: pd.DataFrame) -> pd.DataFrame:
    """Aggregate daily bars and omit the current incomplete trading week."""
    if daily.empty:
        return pd.DataFrame()
    source = daily.set_index("dt")
    weekly = source.resample("W-FRI", label="right", closed="right").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }).dropna(subset=["open", "high", "low", "close"])
    latest_day = source.index.max().date()
    if not weekly.empty and _week_has_later_trading_day(latest_day):
        weekly = weekly.iloc[:-1]
    return weekly.reset_index()


def _r_squared(values: pd.Series) -> float:
    ys = [math.log(float(value)) for value in values if float(value) > 0]
    if len(ys) != len(values) or len(ys) < 2:
        return 0.0
    xs = list(range(len(ys)))
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    ss_x = sum((x - x_mean) ** 2 for x in xs)
    ss_y = sum((y - y_mean) ** 2 for y in ys)
    if ss_x <= 0 or ss_y <= 0:
        return 0.0
    covariance = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    return max(0.0, min(1.0, (covariance * covariance) / (ss_x * ss_y)))


def _evaluate_ma_climb(frame: pd.DataFrame, period: int) -> dict[str, Any] | None:
    """Validate hold(C >= MA), ATR-normalized MA slope, log-MA R2, and ATR distance."""
    if frame.empty or len(frame) < max(ATR_PERIOD + 1, period + CLIMB_WINDOW):
        return None
    work = frame.copy()
    previous_close = work["close"].shift(1)
    true_range = pd.concat(
        [
            work["high"] - work["low"],
            (work["high"] - previous_close).abs(),
            (work["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    work["atr"] = true_range.rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()
    work["ma"] = work["close"].rolling(period, min_periods=period).mean()
    recent = work.tail(CLIMB_WINDOW)
    if recent[["close", "low", "ma", "atr"]].isna().any().any():
        return None
    atr = float(recent["atr"].iloc[-1])
    if atr <= 0:
        return None

    close_distance = (recent["close"] - recent["ma"]) / recent["atr"]
    low_distance = (recent["low"] - recent["ma"]) / recent["atr"]
    hold_count = int((recent["close"] >= recent["ma"]).sum())
    slope = float(recent["ma"].iloc[-1] - recent["ma"].iloc[0]) / ((CLIMB_WINDOW - 1) * atr)
    r_squared = _r_squared(recent["ma"])
    median_distance = float(close_distance.median())
    p80_distance = float(close_distance.quantile(0.80))
    touch_count = int(((low_distance <= 0.35) & (low_distance >= -0.75)).sum())

    hard_valid = (
        hold_count == CLIMB_WINDOW
        and slope >= MIN_SLOPE_ATR_PER_BAR
        and r_squared >= MIN_R_SQUARED
        and 0 <= median_distance <= MAX_MEDIAN_DISTANCE_ATR
        and p80_distance <= MAX_P80_DISTANCE_ATR
    )
    hold_score = 30.0 * hold_count / CLIMB_WINDOW
    slope_score = 25.0 * min(1.0, max(0.0, slope) / 0.12)
    linearity_score = 15.0 * r_squared
    proximity_score = 20.0 * max(0.0, 1.0 - median_distance / MAX_P80_DISTANCE_ATR)
    touch_score = 10.0 * min(1.0, touch_count / 2.0)
    score = round(hold_score + slope_score + linearity_score + proximity_score + touch_score, 3)
    running = bool(hard_valid and score >= WATCH_SCORE)

    return {
        "running": running,
        "period": period,
        "window": CLIMB_WINDOW,
        "hold_count": hold_count,
        "slope_atr_per_bar": round(slope, 5),
        "r_squared": round(r_squared, 5),
        "median_distance_atr": round(median_distance, 5),
        "p80_distance_atr": round(p80_distance, 5),
        "touch_count": touch_count,
        "latest_close": round(float(recent["close"].iloc[-1]), 4),
        "latest_ma": round(float(recent["ma"].iloc[-1]), 4),
        "climb_score": score,
        "climb_grade": "buy_review" if running and score >= BUY_REVIEW_SCORE else "watch" if running else "invalid",
    }


def _best_climb(frame: pd.DataFrame) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    candidates = [
        result
        for period in CLIMB_PERIODS
        if (result := _evaluate_ma_climb(frame, period)) is not None and result["running"]
    ]
    candidates.sort(
        key=lambda item: (
            float(item["climb_score"]),
            -float(item["median_distance_atr"]),
            -int(item["period"]),
        ),
        reverse=True,
    )
    return (candidates[0] if candidates else None), candidates[1:]


def _continue_climb(frame: pd.DataFrame, previous: dict[str, Any]) -> dict[str, Any] | None:
    """Keep an established climb alive until a completed close breaks its MA."""
    period = int(previous.get("period") or 0)
    if period not in CLIMB_PERIODS:
        return None
    current = _evaluate_ma_climb(frame, period)
    if current is None or int(current.get("hold_count") or 0) < CLIMB_WINDOW:
        return None
    prior_score = float(previous.get("climb_score") or 0)
    current_score = float(current.get("climb_score") or 0) if current.get("running") else 0.0
    score = round(max(prior_score, current_score, WATCH_SCORE), 3)
    current.update({
        "running": True,
        "continued": True,
        "shape_valid_now": bool(current.get("running")),
        "climb_score": score,
        "climb_grade": "buy_review" if score >= BUY_REVIEW_SCORE else "watch",
    })
    return current


def _signal_doc(
    code: str,
    freq: str,
    result: dict[str, Any],
    alternates: list[dict[str, Any]],
    *,
    event_dt: datetime,
    now: datetime,
    aligned_freqs: list[str],
    as_of: str,
    source_last_trade_date: str,
) -> dict[str, Any]:
    unit = "日" if freq == "日线" else "周"
    period = int(result["period"])
    score = float(result["climb_score"])
    ma_name = f"MA{period}"
    climb = {
        **result,
        "state": "running",
        "freq": freq,
        "effective_ma": ma_name,
        "alternates": [
            {
                "period": item["period"],
                "effective_ma": f"MA{item['period']}",
                "climb_score": item["climb_score"],
                "median_distance_atr": item["median_distance_atr"],
            }
            for item in alternates
        ],
    }
    resonance_context = {
        "aligned_freqs": aligned_freqs,
        "conflict_freqs": [],
        "resonance_grade": "multi_period" if len(aligned_freqs) > 1 else "single_period",
    }
    ma_alignment = {
        f"ma{period}": result["latest_ma"],
        f"above_ma{period}": True,
        f"near_ma{period}": result["median_distance_atr"] <= 0.75,
        f"reclaim_ma{period}": False,
        "effective_ma": ma_name,
        "effective_period": period,
        "ma_stack": "climbing",
        "score": score,
        "summary": f"沿{period}{unit}线攀爬",
        "tags": [f"沿{period}{unit}线攀爬", "收盘未跌破有效均线"],
    }
    event_date = event_dt.date().isoformat()
    return {
        "dedupe_key": f"ma_climb_scan:A:{code}:{freq}:ma{period}:{event_date}",
        "symbol": code,
        "raw_code": code,
        "display_symbol": _prefixed_symbol(code),
        "market": "A",
        "freq": freq,
        "dt": event_dt,
        "as_of": as_of,
        "signal_type": f"沿{period}{unit}线攀爬",
        "signal_side": "buy",
        "signal_family": "ma_climb",
        "score": score,
        "total_score": score,
        "confidence": round(score / 100.0, 4),
        "active": True,
        "producer": "ma_climb_scan",
        "bar_as_of": event_date,
        "source_last_trade_date": source_last_trade_date,
        "week_period_end": event_date if freq == "周线" else "",
        "scan_run_id": str(get_task_env("SIGNALS_POSTMARKET_RUN_ID", "") or ""),
        "ma_alignment": ma_alignment,
        "technical_evidence": {
            "signal_type": f"沿{period}{unit}线攀爬",
            "freq": freq,
            "ma_climb": climb,
            "ma_alignment": ma_alignment,
            "resonance_context": resonance_context,
        },
        "resonance_context": resonance_context,
        "invalidates_when": f"收盘跌破有效{period}{unit}均线，攀爬逻辑失效",
        "source": "sync.ma_climb_scan",
        "updated_at": now,
    }


def _scan_symbol(
    code: str,
    daily: pd.DataFrame,
    now: datetime,
    *,
    as_of: str | None = None,
    previous: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    frames = {"日线": daily, "周线": _completed_weekly_frame(daily)}
    source_last_trade_date = daily["dt"].iloc[-1].date().isoformat() if not daily.empty else ""
    selected: dict[str, tuple[dict[str, Any], list[dict[str, Any]], datetime]] = {}
    for freq, frame in frames.items():
        prior = (previous or {}).get(freq) or {}
        best = _continue_climb(frame, prior) if prior else None
        if prior and best is None:
            continue
        detected, alternates = _best_climb(frame)
        if best is None:
            best = detected
        elif detected and int(detected.get("period") or 0) != int(best.get("period") or 0):
            alternates = [detected, *alternates]
        if best and not frame.empty:
            selected[freq] = (best, alternates, frame["dt"].iloc[-1].to_pydatetime())
    aligned_freqs = list(selected)
    return [
        _signal_doc(
            code,
            freq,
            result,
            alternates,
            event_dt=event_dt,
            now=now,
            aligned_freqs=aligned_freqs,
            as_of=as_of or now.date().isoformat(),
            source_last_trade_date=source_last_trade_date,
        )
        for freq, (result, alternates, event_dt) in selected.items()
    ]


def _load_daily_frames(db: Database, now: datetime) -> tuple[dict[str, pd.DataFrame], str]:
    lookback_days = _env_int(
        "MA_CLIMB_LOOKBACK_DAYS",
        DEFAULT_LOOKBACK_DAYS,
        minimum=120,
        maximum=730,
    )
    max_bars = _env_int(
        "MA_CLIMB_MAX_BARS_PER_SYMBOL",
        DEFAULT_MAX_BARS_PER_SYMBOL,
        minimum=80,
        maximum=360,
    )
    cutoff = now - timedelta(days=lookback_days)
    cursor = db["bars"].find(
        {
            "meta.market": "A",
            "meta.freq": "日线",
            "dt": {"$gte": cutoff},
        },
        {
            "_id": 0,
            "dt": 1,
            "meta.symbol": 1,
            "open": 1,
            "high": 1,
            "low": 1,
            "close": 1,
        },
    ).sort([("dt", 1)])
    grouped: dict[str, OrderedDict[date, dict[str, Any]]] = {}
    latest_date: date | None = None
    for doc in cursor:
        meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
        code = _pure_a_code(meta.get("symbol"))
        dt = pd.to_datetime(doc.get("dt"), errors="coerce")
        if not code or pd.isna(dt):
            continue
        bar_date = dt.date()
        latest_date = max(latest_date, bar_date) if latest_date else bar_date
        bars = grouped.setdefault(code, OrderedDict())
        bars[bar_date] = {
            "dt": dt.to_pydatetime(),
            "open": doc.get("open"),
            "high": doc.get("high"),
            "low": doc.get("low"),
            "close": doc.get("close"),
        }
        while len(bars) > max_bars:
            bars.popitem(last=False)
    if latest_date is None:
        return {}, ""
    frames = {
        code: frame
        for code, bars in grouped.items()
        if bars and next(reversed(bars)) == latest_date and not (frame := _as_frame(list(bars.values()))).empty
    }
    return frames, latest_date.isoformat()


def _active_climb_states(db: Database) -> dict[str, dict[str, dict[str, Any]]]:
    states: dict[str, dict[str, dict[str, Any]]] = {}
    cursor = db["terminal_technical_signals"].find(
        {"signal_family": "ma_climb", "active": {"$ne": False}},
        {
            "raw_code": 1,
            "symbol": 1,
            "freq": 1,
            "technical_evidence.ma_climb": 1,
            "updated_at": 1,
        },
    ).sort([("updated_at", -1)])
    for doc in cursor:
        code = _pure_a_code(doc.get("raw_code") or doc.get("symbol"))
        freq = str(doc.get("freq") or "")
        evidence = doc.get("technical_evidence") if isinstance(doc.get("technical_evidence"), dict) else {}
        climb = evidence.get("ma_climb") if isinstance(evidence.get("ma_climb"), dict) else {}
        if code and freq in {"日线", "周线"} and climb and freq not in states.setdefault(code, {}):
            states[code][freq] = climb
    return states


def sync_ma_climb_scan(db: Database, proxy_url: str = None) -> dict:
    """Scan the full cached A-share daily universe, independent of hot-rank candidates."""
    del proxy_url
    now = naive_market_now("A")
    as_of = str(get_task_env("SIGNALS_POSTMARKET_TRADE_DATE", "") or "").strip()[:10] or now.date().isoformat()
    frames, latest_dt = _load_daily_frames(db, now)
    if not frames:
        return {
            "status": "empty",
            "inserted": 0,
            "symbols": 0,
            "signals": 0,
            "error_msg": "daily_bar_universe_empty",
        }

    previous_states = _active_climb_states(db)
    minimum_fullmarket_symbols = _env_int(
        "MA_CLIMB_MIN_FULLMARKET_SYMBOLS",
        4000,
        minimum=1,
        maximum=10000,
    )
    fullmarket_complete = len(frames) >= minimum_fullmarket_symbols
    operations: list[UpdateOne] = []
    active_keys: list[str] = []
    signals = 0
    buy_review = 0
    watch = 0
    for code, daily in frames.items():
        for doc in _scan_symbol(code, daily, now, as_of=as_of, previous=previous_states.get(code)):
            active_keys.append(doc["dedupe_key"])
            signals += 1
            climb = doc["technical_evidence"]["ma_climb"]
            if climb["climb_grade"] == "buy_review":
                buy_review += 1
            else:
                watch += 1
            operations.append(UpdateOne({"dedupe_key": doc["dedupe_key"]}, {"$set": doc}, upsert=True))
            if len(operations) >= 500:
                db["terminal_technical_signals"].bulk_write(operations, ordered=False)
                operations.clear()
    if operations:
        db["terminal_technical_signals"].bulk_write(operations, ordered=False)

    invalidated = db["terminal_technical_signals"].update_many(
        {
            "signal_family": "ma_climb",
            "active": {"$ne": False},
            "dedupe_key": {"$nin": active_keys},
        },
        {"$set": {"active": False, "invalidated_at": now, "updated_at": now}},
    )
    db["data_freshness"].update_one(
        {
            "domain": "technical_signal",
            "market": "A",
            "mode": "ma_climb",
            "collection": "terminal_technical_signals",
        },
        {"$set": {
            "domain": "technical_signal",
            "market": "A",
            "mode": "ma_climb",
            "lane": str(get_task_env("SIGNALS_CURRENT_SYNC_LANE", "postmarket") or "postmarket"),
            "collection": "terminal_technical_signals",
            "freshness": "fresh" if fullmarket_complete else "partial",
            "latest_dt": latest_dt,
            "as_of": as_of,
            "updated_at": now,
            "stale_reason": "" if fullmarket_complete else "cached_daily_universe_below_minimum",
            "count": signals,
            "scanned_symbols": len(frames),
            "buy_review_count": buy_review,
            "watch_count": watch,
            "invalidated_count": int(getattr(invalidated, "modified_count", 0) or 0),
            "scan_scope": "full_market_cached_daily",
            "minimum_fullmarket_symbols": minimum_fullmarket_symbols,
            "is_full_market_complete": fullmarket_complete,
        }},
        upsert=True,
    )
    logger.info(
        "ma climb scan: symbols=%d signals=%d buy_review=%d watch=%d",
        len(frames),
        signals,
        buy_review,
        watch,
    )
    return {
        "status": "ok",
        "inserted": signals,
        "symbols": len(frames),
        "signals": signals,
        "buy_review": buy_review,
        "watch": watch,
        "invalidated": int(getattr(invalidated, "modified_count", 0) or 0),
        "latest_dt": latest_dt,
        "scan_scope": "full_market_cached_daily",
        "is_full_market_complete": fullmarket_complete,
    }
