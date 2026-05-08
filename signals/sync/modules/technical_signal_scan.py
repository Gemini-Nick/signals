# -*- coding: utf-8 -*-
"""Scan cached bars and publish explainable hard-technical signals."""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

import pandas as pd
from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.market_time import naive_market_now, to_market_naive
from signals.sync.task_context import get_task_env

logger = logging.getLogger("signals.sync.technical_signal_scan")

DAILY_FREQS = ["日线", "daily", "D", "1d"]
WEEKLY_FREQS = ["周线", "weekly", "W", "1w"]
MINUTE_FREQS = {
    "30分钟": ["30分钟", "30min", "30m", "F30"],
    "15分钟": ["15分钟", "15min", "15m", "F15"],
    "5分钟": ["5分钟", "5min", "5m", "F5"],
}
REQUIRED_FULL_FREQS = ("日线", "周线", "30分钟")
OPTIONAL_ON_DEMAND_FREQS = ("15分钟", "5分钟")
INTRADAY_SCAN_SCOPE = "intraday_active"
POSTMARKET_SCAN_SCOPE = "postmarket"
PRIMARY_MA_PERIODS = (5, 10, 20)
FIBONACCI_MA_PERIODS = (8, 13, 21, 34, 55, 89)
FREQ_ORDER = {
    "周线": 0,
    "weekly": 0,
    "1w": 0,
    "w": 0,
    "日线": 1,
    "daily": 1,
    "1d": 1,
    "d": 1,
    "30分钟": 2,
    "30min": 2,
    "30m": 2,
    "f30": 2,
    "15分钟": 3,
    "15min": 3,
    "15m": 3,
    "f15": 3,
    "5分钟": 4,
    "5min": 4,
    "5m": 4,
    "f5": 4,
}


def _bar_freq_priority(meta: object) -> int:
    if not isinstance(meta, dict):
        return 10
    freq = str(meta.get("freq") or "").strip()
    if freq in {"周线", "日线", "30分钟", "15分钟", "5分钟"}:
        return 0
    if freq in {"weekly", "1w", "W", "daily", "1d", "D", "30min", "30m", "F30", "15min", "15m", "F15", "5min", "5m", "F5"}:
        return 1
    return 5


def _dedupe_bar_docs_by_dt(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[datetime, tuple[tuple[int, int], dict[str, Any]]] = {}
    for idx, doc in enumerate(docs):
        dt = pd.to_datetime(doc.get("dt"), errors="coerce")
        if pd.isna(dt):
            continue
        key = dt.to_pydatetime()
        score = (_bar_freq_priority(doc.get("meta")), idx)
        if key not in best or score < best[key][0]:
            best[key] = (score, doc)
    return [item[1] for _, item in sorted(best.items(), key=lambda item: item[0], reverse=True)]


def _env_text(name: str, default: str = "") -> str:
    return str(get_task_env(name, os.getenv(name, default)) or default).strip()


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(_env_text(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _env_text(name, ",".join(default))
    values = [value.strip() for value in raw.replace(";", ",").split(",") if value.strip()]
    return tuple(values) if values else default


def _pure_a_code(symbol: Any) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    pure = raw.split(".", 1)[-1] if "." in raw else raw
    pure = pure.replace("SH", "").replace("SZ", "").replace("BJ", "")
    return pure if pure.isdigit() and len(pure) == 6 else ""


def _pure_hk_code(symbol: Any) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        raw = raw.split(".", 1)[1]
    raw = raw.replace("HK", "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits.zfill(5) if digits and len(digits) <= 5 else ""


def _symbol_market(symbol: Any) -> str:
    raw = str(symbol or "").strip().upper()
    if raw.startswith("HK.") or _pure_hk_code(raw) and not _pure_a_code(raw):
        return "HK"
    return "A"


def _raw_symbol_code(symbol: Any) -> str:
    return _pure_hk_code(symbol) if _symbol_market(symbol) == "HK" else _pure_a_code(symbol)


def _canonical_symbol(symbol: Any) -> str:
    market = _symbol_market(symbol)
    if market == "HK":
        code = _pure_hk_code(symbol)
        return f"HK.{code}" if code else ""
    return _pure_a_code(symbol)


def _prefixed_symbol(symbol: Any) -> str:
    if _symbol_market(symbol) == "HK":
        code = _pure_hk_code(symbol)
        return f"HK.{code}" if code else str(symbol or "").strip()
    code = _pure_a_code(symbol)
    if not code:
        return str(symbol or "").strip()
    if code.startswith(("6", "9")):
        return f"SH.{code}"
    if code.startswith(("4", "8")):
        return f"BJ.{code}"
    return f"SZ.{code}"


def _symbol_query_values(symbol: str) -> list[str]:
    market = _symbol_market(symbol)
    code = _raw_symbol_code(symbol)
    if not code:
        return [str(symbol or "").strip()]
    if market == "HK":
        return [f"HK.{code}", code]
    return [code, _prefixed_symbol(code)]


def _scan_markets(scan_scope: str | None = None) -> tuple[str, ...]:
    default = ("A",) if scan_scope == INTRADAY_SCAN_SCOPE else ("A", "HK")
    markets = tuple(value.upper() for value in _env_list("TECHNICAL_SIGNAL_SCAN_MARKETS", default))
    normalized = tuple("HK" if value in {"H", "HK"} else "A" for value in markets if value in {"A", "CN", "SH", "SZ", "BJ", "H", "HK"})
    return tuple(dict.fromkeys(normalized)) or default


def _symbols_with_daily(db: Database, *, markets: tuple[str, ...] | None = None) -> list[str]:
    allowed_markets = set(markets or ("A", "HK"))
    symbols = db["bars"].distinct("meta.symbol", {"meta.freq": {"$in": DAILY_FREQS}})
    clean = sorted({
        canonical
        for symbol in symbols
        for canonical in (_canonical_symbol(symbol),)
        if canonical and _symbol_market(canonical) in allowed_markets
    })
    max_symbols = _env_int("TECHNICAL_SIGNAL_SCAN_MAX_SYMBOLS", 0)
    if max_symbols > 0:
        return clean[:max_symbols]
    return clean


def _append_symbol(out: list[str], value: Any, *, allowed_markets: tuple[str, ...] = ("A",)) -> None:
    symbol = _canonical_symbol(value)
    if symbol and _symbol_market(symbol) in set(allowed_markets) and symbol not in out:
        out.append(symbol)


def _symbols_from_stock_minute_selection(db: Database) -> list[str]:
    try:
        doc = db["sync_log"].find_one(
            {"_id": "stock_minute:selection:_meta"},
            {"selected_symbols": 1, "priority_symbols": 1, "pinned_symbols": 1},
        ) or {}
    except Exception:
        return []
    symbols: list[str] = []
    for key in ("pinned_symbols", "priority_symbols", "selected_symbols"):
        for value in doc.get(key) or []:
            _append_symbol(symbols, value)
    return symbols


def _symbols_from_terminal_pool(db: Database) -> list[str]:
    try:
        doc = db["terminal_stock_pool"].find_one(
            {"pool": "terminal_stock_pool", "market": "A"},
            {"focus_stocks": 1, "stocks": 1, "watch_stocks": 1, "clue_stocks": 1},
            sort=[("updated_at", -1)],
        ) or {}
    except Exception:
        return []
    symbols: list[str] = []
    for key in ("focus_stocks", "stocks", "watch_stocks", "clue_stocks"):
        for row in doc.get(key) or []:
            if isinstance(row, dict):
                _append_symbol(symbols, row.get("raw_code") or row.get("symbol") or row.get("code"))
            else:
                _append_symbol(symbols, row)
    return symbols


def _scan_scope(scope: str | None = None) -> str:
    configured = _env_text("TECHNICAL_SIGNAL_SCAN_SCOPE", scope or "").lower()
    if configured:
        return configured
    lane = _env_text("SIGNALS_CURRENT_SYNC_LANE")
    market = _env_text("SIGNALS_CURRENT_SYNC_MARKET")
    if lane == "signal_lane" and market == "A":
        return INTRADAY_SCAN_SCOPE
    return POSTMARKET_SCAN_SCOPE


def _symbols_for_scope(db: Database, scope: str) -> tuple[list[str], str]:
    if scope == INTRADAY_SCAN_SCOPE:
        symbols: list[str] = []
        for value in _symbols_from_stock_minute_selection(db):
            _append_symbol(symbols, value)
        for value in _symbols_from_terminal_pool(db):
            _append_symbol(symbols, value)
        limit = _env_int("TECHNICAL_SIGNAL_INTRADAY_MAX_SYMBOLS", 120, minimum=1, maximum=500)
        return symbols[:limit], "stock_minute_selection+terminal_stock_pool"
    markets = _scan_markets(scope)
    return _symbols_with_daily(db, markets=markets), f"daily_bars:{'+'.join(markets)}"


def _freq_aliases(label: str) -> list[str]:
    if label == "日线":
        return DAILY_FREQS
    if label == "周线":
        return WEEKLY_FREQS
    return MINUTE_FREQS.get(label, [label])


def _coverage_by_freq(
    db: Database,
    symbols: list[str],
    *,
    required_freqs: tuple[str, ...] = REQUIRED_FULL_FREQS,
    optional_freqs: tuple[str, ...] = OPTIONAL_ON_DEMAND_FREQS,
) -> dict[str, dict[str, Any]]:
    symbol_set = {_canonical_symbol(symbol) for symbol in symbols if _canonical_symbol(symbol)}
    total = len(symbol_set)
    coverage: dict[str, dict[str, Any]] = {}
    required_set = set(required_freqs)
    for label in (*required_freqs, *optional_freqs):
        aliases = _freq_aliases(label)
        try:
            raw_symbols = db["bars"].distinct("meta.symbol", {"meta.freq": {"$in": aliases}})
            covered = {
                _canonical_symbol(symbol)
                for symbol in raw_symbols
                if _canonical_symbol(symbol) in symbol_set
            }
            latest = db["bars"].find_one(
                {"meta.freq": {"$in": aliases}},
                {"dt": 1},
                sort=[("dt", -1)],
            ) or {}
        except Exception:
            covered = set()
            latest = {}
        missing_count = max(0, total - len(covered))
        required = label in required_set
        coverage[label] = {
            "freq": label,
            "required": required,
            "mode": "full_market_required" if required else "on_demand",
            "symbol_count": len(covered),
            "total_symbols": total,
            "missing_count": missing_count,
            "coverage_pct": round((len(covered) / total * 100), 2) if total else 0.0,
            "latest_dt": latest.get("dt"),
            "status": "complete" if missing_count == 0 else ("coverage_incomplete" if required else "on_demand_missing"),
        }
    return coverage


def _rawbar_dt(value: Any, *, symbol: str = "", source: str = "") -> pd.Timestamp:
    # czsc.RawBar shifts naive Python datetime through the host timezone; keep
    # cached market labels as pandas Timestamp to avoid local-machine drift.
    normalized = to_market_naive(value, market=_symbol_market(symbol), symbol=symbol, source=source)
    if normalized is None:
        return pd.to_datetime(value)
    return pd.Timestamp(normalized)


def _doc_to_rawbar(doc: dict[str, Any], symbol: str, freq, idx: int) -> Any:
    from czsc import RawBar

    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    market = str(meta.get("market") or _symbol_market(symbol))
    normalized_dt = to_market_naive(doc.get("dt"), market=market, symbol=symbol, source=str(meta.get("source") or ""))
    return RawBar(
        symbol=symbol,
        dt=pd.Timestamp(normalized_dt) if normalized_dt is not None else _rawbar_dt(doc.get("dt"), symbol=symbol, source=str(meta.get("source") or "")),
        id=idx,
        freq=freq,
        open=float(doc.get("open") or 0),
        high=float(doc.get("high") or 0),
        low=float(doc.get("low") or 0),
        close=float(doc.get("close") or 0),
        vol=int(float(doc.get("vol") or 0)),
        amount=int(float(doc.get("amount") or 0)),
    )


def _latest_doc_dt(docs: list[dict[str, Any]]) -> datetime | None:
    values: list[datetime] = []
    for doc in docs:
        value = doc.get("dt")
        if not value:
            continue
        try:
            parsed = pd.to_datetime(value).to_pydatetime()
        except Exception:
            continue
        values.append(parsed.replace(tzinfo=None) if parsed.tzinfo else parsed)
    return max(values) if values else None


def _resampled_5m_docs(db: Database, symbol: str, label: str, *, limit: int) -> list[dict[str, Any]]:
    rule = {"15分钟": "15min", "30分钟": "30min"}.get(label)
    if not rule:
        return []
    try:
        docs = list(db["bars"].find(
            {"meta.symbol": symbol, "meta.freq": {"$in": MINUTE_FREQS["5分钟"]}},
            {"_id": 0, "dt": 1, "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1},
        ).sort("dt", -1).limit(max(limit * 8, 400)))
    except Exception:
        return []
    if len(docs) < 20:
        return []
    rows: list[dict[str, Any]] = []
    for doc in docs:
        dt_value = to_market_naive(doc.get("dt"), market="A", symbol=symbol, source="5min_resample")
        if dt_value is None:
            continue
        rows.append({
            "dt": dt_value,
            "open": float(doc.get("open") or 0),
            "high": float(doc.get("high") or 0),
            "low": float(doc.get("low") or 0),
            "close": float(doc.get("close") or 0),
            "vol": int(float(doc.get("vol") or 0)),
            "amount": int(float(doc.get("amount") or 0)),
        })
    if len(rows) < 20:
        return []
    df = pd.DataFrame(rows).sort_values("dt").set_index("dt")
    resampled = df.resample(rule, label="right", closed="right", origin="start_day").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "vol": "sum",
        "amount": "sum",
    })
    resampled = resampled.dropna(subset=["open", "high", "low", "close"]).tail(limit)
    out: list[dict[str, Any]] = []
    for dt_value, row in resampled.iterrows():
        out.append({
            "dt": dt_value.to_pydatetime() if hasattr(dt_value, "to_pydatetime") else dt_value,
            "meta": {"symbol": symbol, "freq": label, "source": "5min_resampled_intraday", "market": "A"},
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "vol": int(float(row.get("vol") or 0)),
            "amount": int(float(row.get("amount") or 0)),
        })
    return out


def _load_bars(db: Database, symbol: str, freq_values: list[str], freq, *, limit: int, label: str = "", resample_intraday: bool = False) -> list[Any]:
    fetch_limit = limit * 2 if len(freq_values) > 1 else limit
    docs = list(db["bars"].find(
        {"meta.symbol": {"$in": _symbol_query_values(symbol)}, "meta.freq": {"$in": freq_values}},
        {"_id": 0, "dt": 1, "meta": 1, "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1},
    ).sort("dt", -1).limit(fetch_limit))
    docs = _dedupe_bar_docs_by_dt(docs)[:limit]
    if resample_intraday and label in {"15分钟", "30分钟"}:
        resampled = _resampled_5m_docs(db, symbol, label, limit=limit)
        if resampled and (_latest_doc_dt(resampled) or datetime.min) > (_latest_doc_dt(docs) or datetime.min):
            docs = resampled
    docs.reverse()
    out = []
    output_symbol = _prefixed_symbol(symbol)
    for idx, doc in enumerate(docs):
        try:
            out.append(_doc_to_rawbar(doc, output_symbol, freq, idx))
        except Exception:
            continue
    return out


def _signal_side(signal_type: str, score: float) -> str:
    if "卖" in signal_type or "顶" in signal_type or "风险" in signal_type or score < 0:
        return "sell"
    return "buy"


def _freq_sort_key(freq: Any) -> tuple[int, str]:
    text = str(freq or "").strip()
    return FREQ_ORDER.get(text.lower(), FREQ_ORDER.get(text, 99)), text


def _event_dt_value(event: Any) -> datetime | None:
    value = getattr(event, "dt", None)
    if not value:
        return None
    try:
        parsed = pd.to_datetime(value).to_pydatetime()
    except Exception:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _event_side(event: Any, weights: dict[str, Any]) -> str:
    signal_type = str(getattr(event, "signal_type", "") or "")
    score = float(weights.get(signal_type, 0) or 0) * float(getattr(event, "confidence", 0) or 0)
    return _signal_side(signal_type, score)


def _resonance_grade(aligned_freqs: list[str], conflict_freqs: list[str]) -> str:
    if conflict_freqs:
        return "conflict"
    if len(aligned_freqs) >= 3:
        return "strong_resonance"
    if len(aligned_freqs) >= 2:
        return "multi_period"
    return "single_period"


def _resonance_context(events: list[Any], *, side: str, primary_freq: str, direction: str, weights: dict[str, Any]) -> dict[str, Any]:
    aligned_freqs = sorted(
        {str(getattr(event, "freq", "") or "") for event in events if _event_side(event, weights) == side},
        key=_freq_sort_key,
    )
    conflict_freqs = sorted(
        {str(getattr(event, "freq", "") or "") for event in events if _event_side(event, weights) != side},
        key=_freq_sort_key,
    )
    latest_values = [value for value in (_event_dt_value(event) for event in events) if value]
    latest_dt = max(latest_values).isoformat() if latest_values else ""
    grade = _resonance_grade(aligned_freqs, conflict_freqs)
    tags: list[str] = []
    if grade == "conflict":
        tags.append("周期冲突")
    if grade in {"multi_period", "strong_resonance"}:
        tags.append("多周期共振")
    if grade == "strong_resonance":
        tags.append("强共振")
    if "周线" in aligned_freqs and "日线" in aligned_freqs:
        tags.append("日周同向")
    if any(freq in {"5分钟", "5min", "5m", "F5"} for freq in aligned_freqs):
        tags.append("5m确认")
    if not tags:
        tags.append("硬技术")
    side_text = "买点" if side == "buy" else "风险"
    if grade == "conflict":
        summary = f"{side_text}信号存在周期冲突：同向 {','.join(aligned_freqs) or primary_freq}；冲突 {','.join(conflict_freqs)}"
    else:
        summary = f"{side_text}信号获得 {','.join(aligned_freqs) or primary_freq} 确认"
    return {
        "direction": direction or side,
        "primary_freq": primary_freq,
        "aligned_freqs": aligned_freqs,
        "conflict_freqs": conflict_freqs,
        "grade": grade,
        "tags": tags[:5],
        "summary": summary[:240],
        "latest_dt": latest_dt,
    }


def _rolling_ma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _ma_direction(closes: list[float], period: int) -> str:
    current = _rolling_ma(closes, period)
    previous = _rolling_ma(closes[:-3], period) if len(closes) >= period + 3 else None
    if current is None or previous in (None, 0):
        return "未知"
    slope_pct = (current - previous) / previous * 100
    if abs(slope_pct) < 0.2:
        return "走平"
    return "向上" if slope_pct > 0 else "向下"


def _ma_alignment_from_daily_bars(bars: list[Any]) -> dict[str, Any]:
    closes = [float(getattr(bar, "close", 0) or 0) for bar in bars if float(getattr(bar, "close", 0) or 0) > 0]
    if len(closes) < 5:
        return {}
    latest = closes[-1]
    previous_close = closes[-2] if len(closes) >= 2 else None
    score = 0.0
    above_count = 0
    reclaim_count = 0
    out: dict[str, Any] = {
        "latest_close": round(latest, 3),
    }
    stand_weights = {5: 6.0, 10: 10.0, 20: 16.0}
    reclaim_weights = {5: 4.0, 10: 7.0, 20: 11.0}
    fib_weights = {8: 2.0, 13: 2.5, 21: 3.0, 34: 4.0, 55: 5.0, 89: 6.0}
    fib_support_score = 0.0
    fib_above_count = 0
    fib_reclaim_count = 0
    for period in PRIMARY_MA_PERIODS + FIBONACCI_MA_PERIODS:
        ma_value = _rolling_ma(closes, period)
        if ma_value is None:
            continue
        previous_ma = _rolling_ma(closes[:-1], period) if len(closes) > period else None
        distance_pct = (latest - ma_value) / ma_value * 100 if ma_value else 0.0
        above = latest >= ma_value
        near = abs(distance_pct) <= 1.0
        reclaim = bool(previous_close is not None and previous_ma is not None and previous_close < previous_ma and above)
        out[f"ma{period}"] = round(ma_value, 3)
        out[f"above_ma{period}"] = above
        out[f"near_ma{period}"] = near
        out[f"reclaim_ma{period}"] = reclaim
        out[f"distance_ma{period}_pct"] = round(distance_pct, 3)
        if period in stand_weights:
            if above:
                above_count += 1
                score += stand_weights[period]
            elif near:
                score += stand_weights[period] * 0.5
            if reclaim:
                reclaim_count += 1
                score += reclaim_weights[period]
        elif period in fib_weights:
            weight = fib_weights[period]
            if above:
                fib_above_count += 1
                fib_support_score += weight
            elif near:
                fib_support_score += weight * 0.55
            if reclaim:
                fib_reclaim_count += 1
                fib_support_score += min(4.0, weight)
    ma5 = out.get("ma5")
    ma10 = out.get("ma10")
    ma20 = out.get("ma20")
    if ma5 and ma10 and ma20:
        if ma5 >= ma10 >= ma20:
            out["ma_stack"] = "bullish"
            score += 8.0
        elif ma5 <= ma10 <= ma20:
            out["ma_stack"] = "bearish"
            score -= 6.0
        else:
            out["ma_stack"] = "mixed"
    direction = _ma_direction(closes, 20)
    out["ma20_direction"] = direction
    if direction == "向上":
        score += 4.0
    elif direction == "走平":
        score += 1.0
    elif direction == "向下":
        score -= 4.0
    tags: list[str] = []
    if out.get("above_ma20"):
        tags.append("站上20日线")
    if out.get("above_ma10"):
        tags.append("站上10日线")
    if out.get("above_ma5"):
        tags.append("站上5日线")
    if reclaim_count:
        tags.append("重新站上均线")
    if out.get("ma_stack") == "bullish":
        tags.append("均线多头")
    if fib_support_score > 0:
        tags.append("Fibonacci均线支撑")
    out["above_count"] = above_count
    out["reclaim_count"] = reclaim_count
    out["fib_above_count"] = fib_above_count
    out["fib_reclaim_count"] = fib_reclaim_count
    out["fib_support_score"] = round(min(16.0, fib_support_score), 3)
    out["score"] = round(max(0.0, min(60.0, score + out["fib_support_score"])), 3)
    out["summary"] = " / ".join(tags[:5]) if tags else "均线未确认"
    out["tags"] = tags[:6]
    return out


def _details_dict(details: str) -> dict[str, Any]:
    if not details:
        return {}
    return {"summary": details[:800]}


def _scan_symbol(db: Database, symbol: str, *, scan_scope: str = "postmarket") -> list[dict[str, Any]]:
    from czsc import Freq
    from signals.core.analyzer import SymbolAnalyzer
    from signals.core.detectors import detect_all_signals
    from signals.core.scorer import SIGNAL_WEIGHTS, score_signals

    market = _symbol_market(symbol)
    resample_intraday = scan_scope == INTRADAY_SCAN_SCOPE and market == "A"
    daily_bars = _load_bars(db, symbol, DAILY_FREQS, Freq.D, limit=360, label="日线")
    ma_alignment = _ma_alignment_from_daily_bars(daily_bars)
    bars_by_freq: list[tuple[str, Any, list[Any], int]] = [
        ("日线", Freq.D, daily_bars, 200),
        ("周线", Freq.W, _load_bars(db, symbol, WEEKLY_FREQS, Freq.W, limit=180, label="周线"), 100),
        ("30分钟", Freq.F30, _load_bars(db, symbol, MINUTE_FREQS["30分钟"], Freq.F30, limit=260, label="30分钟", resample_intraday=resample_intraday), 80),
        ("15分钟", Freq.F15, _load_bars(db, symbol, MINUTE_FREQS["15分钟"], Freq.F15, limit=260, label="15分钟", resample_intraday=resample_intraday), 80),
        ("5分钟", Freq.F5, _load_bars(db, symbol, MINUTE_FREQS["5分钟"], Freq.F5, limit=260, label="5分钟"), 80),
    ]
    events = []
    bi_counts: dict[str, int] = {}
    for label, freq, bars, max_bi in bars_by_freq:
        if len(bars) < 20:
            continue
        try:
            analyzer = SymbolAnalyzer(_prefixed_symbol(symbol), freq, bars, max_bi_num=max_bi)
            detected = detect_all_signals(analyzer.czsc, _prefixed_symbol(symbol))
            events.extend(detected)
            bi_counts[label] = len(analyzer.finished_bis)
        except Exception as exc:
            logger.debug("technical scan failed %s/%s: %s", symbol, label, exc)
    if not events:
        return []

    scored = score_signals(_prefixed_symbol(symbol), events)
    now = naive_market_now(market)
    buy_freqs = sorted({event.freq for event in events if _event_side(event, SIGNAL_WEIGHTS) == "buy"}, key=_freq_sort_key)
    sell_freqs = sorted({event.freq for event in events if _event_side(event, SIGNAL_WEIGHTS) == "sell"}, key=_freq_sort_key)
    docs: list[dict[str, Any]] = []
    for event in events:
        base_score = float(SIGNAL_WEIGHTS.get(event.signal_type, 0)) * float(event.confidence or 0)
        side = _signal_side(event.signal_type, base_score)
        dt_value = event.dt.replace(tzinfo=None) if getattr(event.dt, "tzinfo", None) else event.dt
        dedupe_key = f"{_prefixed_symbol(symbol)}|{event.freq}|{event.signal_type}|{dt_value.isoformat()}"
        resonance_context = _resonance_context(
            events,
            side=side,
            primary_freq=str(event.freq or ""),
            direction=scored.direction,
            weights=SIGNAL_WEIGHTS,
        )
        docs.append({
            "dedupe_key": dedupe_key,
            "symbol": _prefixed_symbol(symbol),
            "raw_code": _raw_symbol_code(symbol),
            "market": market,
            "freq": event.freq,
            "dt": dt_value,
            "as_of": now.date().isoformat(),
            "updated_at": now,
            "signal_type": event.signal_type,
            "signal_side": side,
            "signal_family": "hard_technical",
            "price": float(event.price or 0),
            "score": round(base_score, 3),
            "total_score": float(scored.total_score or 0),
            "direction": scored.direction,
            "confidence": float(event.confidence or 0),
            "resonance_freqs": buy_freqs if side == "buy" else sell_freqs,
            "resonance_context": resonance_context,
            "ma_alignment": ma_alignment,
            "technical_evidence": {
                "signal_type": event.signal_type,
                "freq": event.freq,
                "details": event.details,
                "score_details": scored.details[:1600],
                "bi_counts": bi_counts,
                "direction": scored.direction,
                "resonance_context": resonance_context,
                "ma_alignment": ma_alignment,
            },
            "invalidates_when": "跌破信号触发价或上级周期转弱" if side == "buy" else "重新站回风险触发周期并出现买点确认",
            "source": "sync.technical_signal_scan",
            "scan_scope": scan_scope,
        })
    return docs


def _sync_technical_signal_scan(db: Database, proxy_url: str = None, *, scope: str | None = None) -> dict:
    """Scan cached hard-technical bars and publish explainable A/H signals."""
    del proxy_url
    scan_scope = _scan_scope(scope)
    scan_markets = _scan_markets(scan_scope)
    primary_market = scan_markets[0] if scan_markets else "A"
    now = naive_market_now(primary_market)
    required_freqs = _env_list("TECHNICAL_SIGNAL_SCAN_REQUIRED_FREQS", REQUIRED_FULL_FREQS)
    optional_freqs = _env_list("TECHNICAL_SIGNAL_SCAN_OPTIONAL_FREQS", OPTIONAL_ON_DEMAND_FREQS)
    try:
        symbols, symbol_source = _symbols_for_scope(db, scan_scope)
    except Exception as exc:
        return {"status": "error", "error_msg": f"symbol_universe_failed: {exc}"}
    if not symbols:
        return {
            "status": "empty",
            "inserted": 0,
            "symbols": 0,
            "signals": 0,
            "failed": 0,
            "scan_scope": scan_scope,
            "markets": list(scan_markets),
            "required_freqs": list(required_freqs),
            "optional_freqs": list(optional_freqs),
            "symbol_source": "empty",
            "error_msg": "symbol_universe_empty",
        }
    coverage_by_freq = _coverage_by_freq(db, symbols, required_freqs=required_freqs, optional_freqs=optional_freqs)
    skipped_by_freq = {
        freq: {
            "missing_symbols": int(meta.get("missing_count") or 0),
            "status": str(meta.get("status") or ""),
            "mode": str(meta.get("mode") or ""),
        }
        for freq, meta in coverage_by_freq.items()
        if int(meta.get("missing_count") or 0) > 0
    }
    required_complete = all(
        coverage_by_freq.get(freq, {}).get("status") == "complete"
        for freq in required_freqs
    )
    is_full_market_complete = bool(scan_scope != INTRADAY_SCAN_SCOPE and required_complete)
    if scan_scope == INTRADAY_SCAN_SCOPE:
        coverage_status = "active_universe_complete" if required_complete else "active_universe_incomplete"
    else:
        coverage_status = "full_market_complete" if required_complete else "coverage_incomplete"
    workers = _env_int("TECHNICAL_SIGNAL_SCAN_WORKERS", 4, minimum=1, maximum=12)
    operations: list[UpdateOne] = []
    scanned = 0
    failed = 0
    signal_count = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="technical-scan") as executor:
        futures = {executor.submit(_scan_symbol, db, symbol, scan_scope=scan_scope): symbol for symbol in symbols}
        for future in as_completed(futures):
            scanned += 1
            symbol = futures[future]
            try:
                docs = future.result()
            except Exception as exc:
                failed += 1
                logger.debug("technical scan symbol failed %s: %s", symbol, exc)
                continue
            signal_count += len(docs)
            for doc in docs:
                operations.append(UpdateOne({"dedupe_key": doc["dedupe_key"]}, {"$set": doc}, upsert=True))
            if len(operations) >= 500:
                db["terminal_technical_signals"].bulk_write(operations, ordered=False)
                operations.clear()
    if operations:
        db["terminal_technical_signals"].bulk_write(operations, ordered=False)

    freshness_mode = "realtime" if scan_scope == INTRADAY_SCAN_SCOPE else "postmarket"
    freshness_lane = _env_text("SIGNALS_CURRENT_SYNC_LANE", "signal_lane" if scan_scope == INTRADAY_SCAN_SCOPE else "postmarket")
    freshness_market = "A" if "A" in scan_markets else primary_market
    db["data_freshness"].update_one(
        {"domain": "technical_signal", "market": freshness_market, "mode": freshness_mode, "collection": "terminal_technical_signals"},
        {"$set": {
            "domain": "technical_signal",
            "market": freshness_market,
            "mode": freshness_mode,
            "lane": freshness_lane,
            "collection": "terminal_technical_signals",
            "freshness": "fresh",
            "latest_dt": now.date().isoformat(),
            "as_of": now.date().isoformat(),
            "updated_at": now,
            "stale_reason": "" if signal_count or not symbols else "no_technical_signal_detected",
            "count": signal_count,
            "scanned_symbols": scanned,
            "failed_symbols": failed,
            "scan_scope": scan_scope,
            "symbol_source": symbol_source,
            "markets": list(scan_markets),
            "required_freqs": list(required_freqs),
            "optional_freqs": list(optional_freqs),
            "coverage_by_freq": coverage_by_freq,
            "skipped_by_freq": skipped_by_freq,
            "is_full_market_complete": is_full_market_complete,
            "is_scan_universe_complete": required_complete,
            "coverage_status": coverage_status,
        }},
        upsert=True,
    )
    logger.info("technical signal scan: scope=%s symbols=%d signals=%d failed=%d", scan_scope, scanned, signal_count, failed)
    return {
        "status": "ok",
        "inserted": signal_count,
        "symbols": scanned,
        "signals": signal_count,
        "failed": failed,
        "scan_scope": scan_scope,
        "symbol_source": symbol_source,
        "markets": list(scan_markets),
        "required_freqs": list(required_freqs),
        "optional_freqs": list(optional_freqs),
        "coverage_by_freq": coverage_by_freq,
        "skipped_by_freq": skipped_by_freq,
        "is_full_market_complete": is_full_market_complete,
        "is_scan_universe_complete": required_complete,
        "coverage_status": coverage_status,
    }


def sync_technical_signal_scan(db: Database, proxy_url: str = None) -> dict:
    """Scan cached postmarket hard-technical bars and publish explainable signals."""
    return _sync_technical_signal_scan(db, proxy_url=proxy_url)


def sync_intraday_technical_signal_scan(db: Database, proxy_url: str = None) -> dict:
    """Refresh hard-technical signals for the live minute universe only."""
    return _sync_technical_signal_scan(db, proxy_url=proxy_url, scope=INTRADAY_SCAN_SCOPE)
