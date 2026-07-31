# -*- coding: utf-8 -*-
"""Scan cached bars and publish explainable hard-technical signals."""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time
from typing import Any

import pandas as pd
from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.macro_universe import canonical_macro_industry_etf_symbol
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
POSTMARKET_CANDIDATE_SCAN_SCOPE = "postmarket_candidates"
KEY_MA_PERIODS = (5, 8, 10, 13, 20, 21)
PRIMARY_MA_PERIODS = (5, 10, 20)
FIB_MA_TOUCH_UNDERSHOOT_PCT = 1.5
FIB_MA_TOUCH_OVERSHOOT_PCT = 0.8
FIB_MA_NEAR_PCT = 1.0
DAILY_ANALYSIS_BAR_LIMIT = 1320
WEEKLY_ANALYSIS_BAR_LIMIT = 280
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


def _scan_as_of(now: datetime) -> str:
    return _env_text("SIGNALS_POSTMARKET_TRADE_DATE")[:10] or now.date().isoformat()


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
    macro_symbol = canonical_macro_industry_etf_symbol(code)
    if macro_symbol:
        return macro_symbol
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
    default = ("A",) if scan_scope in {INTRADAY_SCAN_SCOPE, POSTMARKET_CANDIDATE_SCAN_SCOPE} else ("A", "HK")
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


def _selection_meta_ids_for_scope(scope: str) -> list[str]:
    configured = _env_text("TECHNICAL_SIGNAL_SELECTION_META_ID")
    if configured:
        return [configured]
    ids: list[str] = []
    if scope == POSTMARKET_CANDIDATE_SCAN_SCOPE:
        ids.append("stock_minute:postmarket_selection:_meta")
    ids.append("stock_minute:selection:_meta")
    return list(dict.fromkeys(ids))


def _symbols_from_stock_minute_selection(db: Database, *, scope: str = INTRADAY_SCAN_SCOPE) -> list[str]:
    doc: dict[str, Any] = {}
    for meta_id in _selection_meta_ids_for_scope(scope):
        try:
            doc = db["sync_log"].find_one(
                {"_id": meta_id},
                {"selected_symbols": 1, "priority_symbols": 1, "pinned_symbols": 1},
            ) or {}
        except Exception:
            doc = {}
        if doc.get("selected_symbols") or doc.get("priority_symbols") or doc.get("pinned_symbols"):
            break
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
    if scope in {INTRADAY_SCAN_SCOPE, POSTMARKET_CANDIDATE_SCAN_SCOPE}:
        symbols: list[str] = []
        for value in _symbols_from_stock_minute_selection(db, scope=scope):
            _append_symbol(symbols, value)
        for value in _symbols_from_terminal_pool(db):
            _append_symbol(symbols, value)
        env_name = "TECHNICAL_SIGNAL_INTRADAY_MAX_SYMBOLS" if scope == INTRADAY_SCAN_SCOPE else "TECHNICAL_SIGNAL_POSTMARKET_MAX_SYMBOLS"
        default = 120 if scope == INTRADAY_SCAN_SCOPE else 300
        limit = _env_int(env_name, default, minimum=1, maximum=500)
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
            query: dict[str, Any] = {"meta.freq": {"$in": aliases}}
            symbol_values = list({
                value
                for symbol in symbol_set
                for value in _symbol_query_values(symbol)
                if value
            })
            if symbol_values:
                query["meta.symbol"] = {"$in": symbol_values}
            raw_symbols = db["bars"].distinct("meta.symbol", query)
            covered = {
                _canonical_symbol(symbol)
                for symbol in raw_symbols
                if _canonical_symbol(symbol) in symbol_set
            }
            latest = db["bars"].find_one(
                query,
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


def _use_intraday_daily_acceptance(scan_scope: str, market: str, now: datetime) -> bool:
    if scan_scope != INTRADAY_SCAN_SCOPE or market != "A":
        return False
    try:
        from signals.core.trading_dates import is_trading_day

        if not is_trading_day("A", now.date()):
            return False
    except Exception:
        if now.weekday() >= 5:
            return False
    return time(9, 15) <= now.time() < time(15, 0)


def _latest_quote_daily_doc(db: Database, symbol: str) -> dict[str, Any] | None:
    if _symbol_market(symbol) != "A":
        return None
    raw_code = _raw_symbol_code(symbol)
    prefixed = _prefixed_symbol(symbol)
    if not raw_code:
        return None
    try:
        doc = db["quote_snapshots"].find_one(
            {
                "$or": [
                    {"symbol": {"$in": [prefixed, raw_code]}},
                    {"code": raw_code},
                    {"raw_code": raw_code},
                ]
            },
            {
                "_id": 0,
                "dt": 1,
                "symbol": 1,
                "code": 1,
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "price": 1,
                "latest": 1,
                "vol": 1,
                "amount": 1,
                "updated_at": 1,
            },
            sort=[("dt", -1), ("updated_at", -1)],
        )
    except Exception:
        return None
    if not doc:
        return None
    close = _safe_float(doc.get("close") or doc.get("price") or doc.get("latest"))
    if close <= 0:
        return None
    parsed_dt = pd.to_datetime(doc.get("dt"), errors="coerce")
    if pd.isna(parsed_dt):
        return None
    open_price = _safe_float(doc.get("open"), close)
    high = max(close, open_price, _safe_float(doc.get("high"), close))
    low = min(close, open_price, _safe_float(doc.get("low"), close))
    return {
        "dt": parsed_dt.to_pydatetime().replace(hour=0, minute=0, second=0, microsecond=0),
        "meta": {
            "symbol": raw_code,
            "freq": "日线",
            "market": "A",
            "source": "quote_snapshots_intraday_daily",
            "quality": "provisional_intraday",
        },
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "vol": int(_safe_float(doc.get("vol"))),
        "amount": int(_safe_float(doc.get("amount"))),
    }


def _append_quote_snapshot_daily_bar(db: Database, symbol: str, daily_bars: list[Any], freq, *, enabled: bool) -> list[Any]:
    if not enabled:
        return daily_bars
    doc = _latest_quote_daily_doc(db, symbol)
    if not doc:
        return daily_bars
    quote_dt = pd.to_datetime(doc.get("dt"), errors="coerce")
    if pd.isna(quote_dt):
        return daily_bars
    quote_day = quote_dt.date()
    out = list(daily_bars)
    latest_day = None
    if out:
        latest_dt = pd.to_datetime(getattr(out[-1], "dt", None), errors="coerce")
        if not pd.isna(latest_dt):
            latest_day = latest_dt.date()
    try:
        quote_bar = _doc_to_rawbar(doc, _prefixed_symbol(symbol), freq, len(out))
    except Exception:
        return daily_bars
    if latest_day is None or quote_day > latest_day:
        out.append(quote_bar)
    elif quote_day == latest_day:
        out[-1] = quote_bar
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
    rows: list[dict[str, float]] = []
    for bar in bars:
        close = _safe_float(getattr(bar, "close", 0))
        if close <= 0:
            continue
        high = max(close, _safe_float(getattr(bar, "high", close), close))
        low = min(close, _safe_float(getattr(bar, "low", close), close))
        rows.append({
            "open": _safe_float(getattr(bar, "open", close), close),
            "high": high,
            "low": low,
            "close": close,
        })
    closes = [row["close"] for row in rows]
    if len(closes) < 5:
        return {}
    latest_row = rows[-1]
    latest = closes[-1]
    latest_low = latest_row["low"]
    latest_high = latest_row["high"]
    latest_range = latest_high - latest_low
    latest_close_position = (latest - latest_low) / latest_range if latest_range > 0 else 1.0
    previous_close = closes[-2] if len(closes) >= 2 else None
    score = 0.0
    above_count = 0
    reclaim_count = 0
    out: dict[str, Any] = {
        "latest_close": round(latest, 3),
        "latest_low": round(latest_low, 3),
    }
    stand_weights = {5: 6.0, 10: 10.0, 20: 16.0}
    reclaim_weights = {5: 4.0, 10: 7.0, 20: 11.0}
    fib_weights = {5: 1.6, 8: 2.0, 10: 2.2, 13: 2.5, 20: 2.8, 21: 3.0}
    fib_support_score = 0.0
    fib_above_count = 0
    fib_reclaim_count = 0
    fib_touch_count = 0
    fib_accept_count = 0
    fib_breakdown_count = 0
    fib_ma_array: list[dict[str, Any]] = []
    for period in KEY_MA_PERIODS:
        ma_value = _rolling_ma(closes, period)
        if ma_value is None:
            continue
        previous_ma = _rolling_ma(closes[:-1], period) if len(closes) > period else None
        distance_pct = (latest - ma_value) / ma_value * 100 if ma_value else 0.0
        low_distance_pct = (latest_low - ma_value) / ma_value * 100 if ma_value else 0.0
        touch_reference = previous_ma or ma_value
        touch_distance_pct = (latest_low - touch_reference) / touch_reference * 100 if touch_reference else low_distance_pct
        above = latest >= ma_value
        near = abs(distance_pct) <= FIB_MA_NEAR_PCT
        reclaim = bool(previous_close is not None and previous_ma is not None and previous_close < previous_ma and above)
        out[f"ma{period}"] = round(ma_value, 3)
        if previous_ma is not None:
            out[f"previous_ma{period}"] = round(previous_ma, 3)
        out[f"above_ma{period}"] = above
        out[f"near_ma{period}"] = near
        out[f"reclaim_ma{period}"] = reclaim
        out[f"distance_ma{period}_pct"] = round(distance_pct, 3)
        out[f"low_distance_ma{period}_pct"] = round(low_distance_pct, 3)
        if period in stand_weights:
            if above:
                above_count += 1
                score += stand_weights[period]
            elif near:
                score += stand_weights[period] * 0.5
            if reclaim:
                reclaim_count += 1
                score += reclaim_weights[period]
        if period in fib_weights:
            weight = fib_weights[period]
            touched = (
                -FIB_MA_TOUCH_UNDERSHOOT_PCT <= touch_distance_pct <= FIB_MA_TOUCH_OVERSHOOT_PCT
                or -FIB_MA_TOUCH_UNDERSHOOT_PCT <= low_distance_pct <= FIB_MA_TOUCH_OVERSHOOT_PCT
            )
            accepted = bool(touched and latest >= touch_reference and latest_close_position >= 0.45)
            breakdown = bool(touched and not accepted and latest < ma_value)
            touch_reclaim = bool(touched and not accepted and not breakdown and latest >= ma_value)
            if accepted:
                interaction = "acceptance"
            elif breakdown:
                interaction = "breakdown"
            elif touch_reclaim:
                interaction = "touch_reclaim"
            elif touched:
                interaction = "touch_pending"
            elif reclaim:
                interaction = "reclaim"
            elif near:
                interaction = "near"
            else:
                interaction = "above" if above else "below"
            if above:
                fib_above_count += 1
            elif near:
                pass
            if reclaim:
                fib_reclaim_count += 1
            if touched:
                fib_touch_count += 1
            if breakdown:
                fib_breakdown_count += 1
            if accepted:
                fib_accept_count += 1
                fib_support_score += weight
            elif reclaim:
                fib_support_score += min(4.0, weight)
            elif near and not breakdown:
                fib_support_score += weight * 0.45
            fib_ma_array.append({
                "period": period,
                "name": f"MA{period}",
                "value": round(ma_value, 3),
                "previous_value": round(previous_ma, 3) if previous_ma is not None else None,
                "above": above,
                "near": near,
                "reclaim": reclaim,
                "pullback_touch": touched,
                "pullback_acceptance": accepted,
                "pullback_breakdown": breakdown,
                "touch_reclaim": touch_reclaim,
                "interaction": interaction,
                "distance_pct": round(distance_pct, 3),
                "low_distance_pct": round(low_distance_pct, 3),
                "touch_distance_pct": round(touch_distance_pct, 3),
                "acceptance_score": round(weight if accepted else min(4.0, weight) if reclaim else weight * 0.45 if near and not breakdown else 0.0, 3),
            })
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
    fib_ma_values = [out.get(f"ma{period}") for period in KEY_MA_PERIODS if out.get(f"ma{period}")]
    if len(fib_ma_values) >= 2:
        if all(left >= right for left, right in zip(fib_ma_values, fib_ma_values[1:])):
            out["fib_ma_array_state"] = "bullish"
        elif all(left <= right for left, right in zip(fib_ma_values, fib_ma_values[1:])):
            out["fib_ma_array_state"] = "bearish"
        else:
            out["fib_ma_array_state"] = "mixed"
    accepted_periods = [item["period"] for item in fib_ma_array if item.get("pullback_acceptance")]
    touched_periods = [item["period"] for item in fib_ma_array if item.get("pullback_touch")]
    breakdown_periods = [item["period"] for item in fib_ma_array if item.get("pullback_breakdown")]
    touch_reclaim_periods = [item["period"] for item in fib_ma_array if item.get("touch_reclaim")]
    pending_touch_periods = [
        item["period"]
        for item in fib_ma_array
        if item.get("pullback_touch") and not item.get("pullback_acceptance") and not item.get("pullback_breakdown")
        and not item.get("touch_reclaim")
    ]
    if accepted_periods:
        tags.append("关键均线回踩承接")
    elif breakdown_periods:
        tags.append("关键均线跌破待修复")
        if pending_touch_periods:
            tags.append("关键均线回踩待确认")
    elif touch_reclaim_periods:
        tags.append("关键均线触线收回")
    elif pending_touch_periods:
        tags.append("关键均线回踩待确认")
    out["above_count"] = above_count
    out["reclaim_count"] = reclaim_count
    out["fib_above_count"] = fib_above_count
    out["fib_reclaim_count"] = fib_reclaim_count
    out["fib_touch_count"] = fib_touch_count
    out["fib_accept_count"] = fib_accept_count
    out["fib_breakdown_count"] = fib_breakdown_count
    out["fib_accept_periods"] = accepted_periods[:6]
    out["fib_touch_periods"] = touched_periods[:6]
    out["fib_breakdown_periods"] = breakdown_periods[:6]
    out["fib_touch_reclaim_periods"] = touch_reclaim_periods[:6]
    out["fib_ma_array"] = fib_ma_array
    summary_accept_periods = sorted(accepted_periods, reverse=True)[:3]
    summary_breakdown_periods = sorted(breakdown_periods, reverse=True)[:3]
    summary_reclaim_periods = sorted(touch_reclaim_periods, reverse=True)[: max(0, 3 - len(summary_breakdown_periods))]
    summary_pending_periods = sorted(pending_touch_periods, reverse=True)[: max(0, 3 - len(summary_breakdown_periods) - len(summary_reclaim_periods))]
    out["fib_array_summary"] = (
        " / ".join(f"MA{period}回踩承接" for period in summary_accept_periods)
        or " / ".join(
            [
                *(f"MA{period}跌破待修复" for period in summary_breakdown_periods),
                *(f"MA{period}触线收回" for period in summary_reclaim_periods),
                *(f"MA{period}触碰待确认" for period in summary_pending_periods),
            ]
        )
    )
    out["fib_support_score"] = round(min(16.0, fib_support_score), 3)
    out["score"] = round(max(0.0, min(60.0, score + out["fib_support_score"])), 3)
    out["summary"] = " / ".join(tags[:5]) if tags else "均线未确认"
    out["tags"] = tags[:6]
    return out


def _bars_to_ohlcv_frame(bars: list[Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for bar in bars:
        dt_value = getattr(bar, "dt", None)
        if dt_value is None:
            continue
        rows.append({
            "dt": pd.Timestamp(dt_value),
            "open": float(getattr(bar, "open", 0) or 0),
            "high": float(getattr(bar, "high", 0) or 0),
            "low": float(getattr(bar, "low", 0) or 0),
            "close": float(getattr(bar, "close", 0) or 0),
            "vol": float(getattr(bar, "vol", 0) or 0),
            "amount": float(getattr(bar, "amount", 0) or 0),
        })
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "vol", "amount"])
    df = pd.DataFrame(rows).dropna(subset=["dt", "open", "high", "low", "close"])
    return df.sort_values("dt").set_index("dt")


def _latest_volume_ratio(df: pd.DataFrame, *, period: int = 20) -> float:
    if df.empty or "vol" not in df.columns or len(df) < period + 1:
        return 0.0
    vols = pd.to_numeric(df["vol"], errors="coerce").fillna(0.0)
    baseline = vols.shift(1).rolling(period).mean().iloc[-1]
    latest = vols.iloc[-1]
    if not baseline or baseline <= 0:
        return 0.0
    return round(float(latest / baseline), 4)


def _entry_factor_score(signal: dict[str, Any]) -> float:
    explicit = _safe_float(signal.get("score"))
    if explicit > 0:
        return round(min(100.0, explicit), 3)
    breakout_pct = max(0.0, float(signal.get("breakout_pct") or 0.0))
    five_day_gain_pct = max(0.0, float(signal.get("five_day_gain_pct") or 0.0))
    volume_ratio = max(0.0, float(signal.get("volume_ratio") or 0.0))
    score = 45.0
    score += min(22.0, breakout_pct * 1.8)
    score += min(22.0, five_day_gain_pct * 0.45)
    score += min(11.0, max(0.0, volume_ratio - 1.0) * 5.0)
    return round(min(100.0, score), 3)


def _entry_factor_resonance_context(signal: dict[str, Any]) -> dict[str, Any]:
    group = str(signal.get("group") or "")
    if group == "relative_resilience_refusal_pullback":
        tags = ["拒绝回调", "相对强度", "硬技术"]
        summary = str(signal.get("details") or "上升趋势中近3日拒绝回调")[:240]
    else:
        tags = ["200日新高", "新高突破", "硬技术"]
        summary = str(signal.get("details") or "200日新高突破")[:240]
    return {
        "direction": "buy",
        "primary_freq": "日线",
        "aligned_freqs": ["日线"],
        "conflict_freqs": [],
        "grade": "single_period",
        "tags": tags,
        "summary": summary,
        "latest_dt": str(signal.get("date_str") or ""),
    }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _refusal_pullback_factor(daily_bars: list[Any], ma_alignment: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for bar in daily_bars:
        close = _safe_float(getattr(bar, "close", 0))
        if close <= 0:
            continue
        high = max(close, _safe_float(getattr(bar, "high", close), close))
        low = min(close, _safe_float(getattr(bar, "low", close), close))
        rows.append({
            "dt": getattr(bar, "dt", None),
            "open": _safe_float(getattr(bar, "open", close), close),
            "high": high,
            "low": low,
            "close": close,
            "vol": max(0.0, _safe_float(getattr(bar, "vol", 0))),
        })
    if len(rows) < 30:
        return {}

    anchor = rows[-4]
    recent = rows[-3:]
    latest = rows[-1]
    anchor_close = anchor["close"]
    latest_close = latest["close"]
    if anchor_close <= 0 or latest_close <= 0:
        return {}

    recent_low = min(row["low"] for row in recent)
    recent_close_low = min(row["close"] for row in recent)
    max_drawdown_pct = max(0.0, (anchor_close - recent_low) / anchor_close * 100)
    max_close_drawdown_pct = max(0.0, (anchor_close - recent_close_low) / anchor_close * 100)
    three_day_change_pct = (latest_close - anchor_close) / anchor_close * 100

    prior_20 = rows[-23:-3]
    if len(prior_20) < 10:
        return {}
    prior_high = max(row["high"] for row in prior_20)
    high_proximity_pct = latest_close / prior_high * 100 if prior_high > 0 else 0.0
    base_20 = rows[-21]["close"]
    twenty_day_gain_pct = (latest_close - base_20) / base_20 * 100 if base_20 > 0 else 0.0

    close_positions: list[float] = []
    for row in recent:
        width = row["high"] - row["low"]
        if width > 0:
            close_positions.append(max(0.0, min(1.0, (row["close"] - row["low"]) / width)))
        else:
            close_positions.append(1.0 if row["close"] >= row["open"] else 0.5)
    close_position_avg = sum(close_positions) / len(close_positions)
    strong_close_days = sum(1 for value in close_positions if value >= 0.62)

    recent_avg_vol = sum(row["vol"] for row in recent) / len(recent)
    prior_vols = [row["vol"] for row in prior_20 if row["vol"] > 0]
    prior_avg_vol = sum(prior_vols) / len(prior_vols) if prior_vols else 0.0
    recent_volume_ratio = recent_avg_vol / prior_avg_vol if prior_avg_vol > 0 else 0.0

    trend_ok = (
        _safe_float(ma_alignment.get("above_count")) >= 2
        and bool(ma_alignment.get("above_ma20"))
        and str(ma_alignment.get("ma20_direction") or "") in {"向上", "走平"}
    ) or twenty_day_gain_pct >= 5.0
    if not trend_ok:
        return {}
    if max_drawdown_pct > 3.5 or max_close_drawdown_pct > 2.0:
        return {}
    if three_day_change_pct < -0.6 or high_proximity_pct < 97.0 or strong_close_days < 2:
        return {}

    score = 52.0
    score += max(0.0, 3.5 - max_drawdown_pct) * 3.0
    score += max(0.0, 2.0 - max_close_drawdown_pct) * 2.0
    score += min(10.0, max(0.0, high_proximity_pct - 97.0) * 2.0)
    score += min(12.0, max(0.0, twenty_day_gain_pct) * 0.45)
    score += close_position_avg * 8.0
    score += strong_close_days * 2.0
    if 0 < recent_volume_ratio <= 1.25:
        score += 6.0
    elif 0 < recent_volume_ratio <= 1.6:
        score += 3.0
    if str(ma_alignment.get("ma_stack") or "") == "bullish":
        score += 5.0
    score += min(2.0, _safe_float(ma_alignment.get("fib_support_score")) * 0.15)
    score = round(min(90.0, score), 3)

    dt_value = latest["dt"]
    date_str = ""
    if dt_value is not None:
        parsed = pd.to_datetime(dt_value, errors="coerce")
        if not pd.isna(parsed):
            date_str = parsed.date().isoformat()
    details = (
        "近3日拒绝回调，"
        f"最大回撤{max_drawdown_pct:.1f}%，收盘回撤{max_close_drawdown_pct:.1f}%，"
        f"3日涨跌{three_day_change_pct:.1f}%，距20日高点{high_proximity_pct:.1f}%，"
        f"强收盘{strong_close_days}/3日"
    )
    return {
        "group": "relative_resilience_refusal_pullback",
        "type": "拒绝回调相对强度",
        "price": latest_close,
        "date_str": date_str,
        "max_drawdown_pct": round(max_drawdown_pct, 3),
        "max_close_drawdown_pct": round(max_close_drawdown_pct, 3),
        "three_day_change_pct": round(three_day_change_pct, 3),
        "twenty_day_gain_pct": round(twenty_day_gain_pct, 3),
        "high_proximity_pct": round(high_proximity_pct, 3),
        "close_position_avg": round(close_position_avg, 3),
        "strong_close_days": strong_close_days,
        "recent_volume_ratio": round(recent_volume_ratio, 3),
        "score": score,
        "confidence": round(min(0.9, 0.58 + score / 300.0), 3),
        "details": details,
    }


def _entry_factor_docs(
    symbol: str,
    daily_bars: list[Any],
    *,
    ma_alignment: dict[str, Any],
    now: datetime,
    scan_scope: str,
) -> list[dict[str, Any]]:
    if not daily_bars:
        return []
    try:
        from signals.core.entry_factors import detect_200d_new_high_entries
    except Exception as exc:
        logger.debug("entry factor import failed %s: %s", symbol, exc)
        return []

    df = _bars_to_ohlcv_frame(daily_bars)
    if df.empty:
        return []
    signals = detect_200d_new_high_entries(df, lookback=1)
    refusal_signal = _refusal_pullback_factor(daily_bars, ma_alignment)
    if refusal_signal:
        signals.append(refusal_signal)
    docs: list[dict[str, Any]] = []
    market = _symbol_market(symbol)
    prefixed = _prefixed_symbol(symbol)
    for signal in signals:
        dt = pd.to_datetime(signal.get("date_str"), errors="coerce")
        if pd.isna(dt):
            continue
        dt_value = dt.to_pydatetime().replace(hour=0, minute=0, second=0, microsecond=0)
        score = _entry_factor_score(signal)
        resonance_context = _entry_factor_resonance_context(signal)
        signal_type = str(signal.get("type") or "200日新高突破")
        group = str(signal.get("group") or "200d_new_high_breakout")
        dedupe_key = f"{prefixed}|日线|{group}|{dt_value.date().isoformat()}"
        technical_evidence = {
            "signal_type": signal_type,
            "freq": "日线",
            "details": str(signal.get("details") or ""),
            "entry_factor": signal,
            "score_details": str(signal.get("details") or ""),
            "direction": "偏多",
            "resonance_context": resonance_context,
            "ma_alignment": ma_alignment,
        }
        invalidates_when = (
            "跌破近3日整理低点，或同细分回调时不再保持相对抗跌"
            if group == "relative_resilience_refusal_pullback"
            else "跌回前199日高点下方，或突破后放量回落无法维持"
        )
        docs.append({
            "dedupe_key": dedupe_key,
            "symbol": prefixed,
            "raw_code": _raw_symbol_code(symbol),
            "market": market,
            "freq": "日线",
            "dt": dt_value,
            "as_of": _scan_as_of(now),
            "updated_at": now,
            "signal_type": signal_type,
            "signal_side": "buy",
            "signal_family": "entry_factor",
            "price": float(signal.get("price") or 0),
            "score": score,
            "total_score": score,
            "direction": "偏多",
            "confidence": float(signal.get("confidence") or 0),
            "resonance_freqs": ["日线"],
            "resonance_context": resonance_context,
            "ma_alignment": ma_alignment,
            "technical_evidence": technical_evidence,
            "invalidates_when": invalidates_when,
            "source": f"sync.technical_signal_scan.entry_factors.{group}",
            "scan_scope": scan_scope,
        })
    return docs


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
    daily_bars = _load_bars(db, symbol, DAILY_FREQS, Freq.D, limit=DAILY_ANALYSIS_BAR_LIMIT, label="日线")
    now = naive_market_now(market)
    daily_bars = _append_quote_snapshot_daily_bar(
        db,
        symbol,
        daily_bars,
        Freq.D,
        enabled=_use_intraday_daily_acceptance(scan_scope, market, now),
    )
    ma_alignment = _ma_alignment_from_daily_bars(daily_bars)
    daily_frame = _bars_to_ohlcv_frame(daily_bars)
    volume_ratio = _latest_volume_ratio(daily_frame)
    entry_factor_docs = _entry_factor_docs(
        symbol,
        daily_bars,
        ma_alignment=ma_alignment,
        now=now,
        scan_scope=scan_scope,
    )
    bars_by_freq: list[tuple[str, Any, list[Any], int]] = [
        ("日线", Freq.D, daily_bars, 200),
        ("周线", Freq.W, _load_bars(db, symbol, WEEKLY_FREQS, Freq.W, limit=WEEKLY_ANALYSIS_BAR_LIMIT, label="周线"), 100),
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
        return entry_factor_docs

    scored = score_signals(_prefixed_symbol(symbol), events, volume_ratio=volume_ratio)
    buy_freqs = sorted({event.freq for event in events if _event_side(event, SIGNAL_WEIGHTS) == "buy"}, key=_freq_sort_key)
    sell_freqs = sorted({event.freq for event in events if _event_side(event, SIGNAL_WEIGHTS) == "sell"}, key=_freq_sort_key)
    docs: list[dict[str, Any]] = list(entry_factor_docs)
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
            "as_of": _scan_as_of(now),
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
    is_full_market_complete = bool(scan_scope not in {INTRADAY_SCAN_SCOPE, POSTMARKET_CANDIDATE_SCAN_SCOPE} and required_complete)
    if scan_scope in {INTRADAY_SCAN_SCOPE, POSTMARKET_CANDIDATE_SCAN_SCOPE}:
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
            "latest_dt": _scan_as_of(now),
            "as_of": _scan_as_of(now),
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
        }, "$inc": {"manifest_revision": 1}},
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
