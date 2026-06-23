# -*- coding: utf-8 -*-
"""Build cached weekly bars from verified daily bars."""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.sync.task_context import get_task_env
from signals.sync.volume_units import CANONICAL_STOCK_VOLUME_UNIT

logger = logging.getLogger("signals.sync.weekly_rollup")

DAILY_FREQS = ["日线", "daily", "D", "1d"]
WEEKLY_FREQ = "周线"
ROLLUP_INSERT_BATCH_SIZE = 1000
ROLLUP_MAX_SYMBOLS = 6000
ROLLUP_POSTMARKET_CANDIDATE_MAX_SYMBOLS = 300


def _daily_freq_priority(meta: object) -> int:
    if not isinstance(meta, dict):
        return 10
    freq = str(meta.get("freq") or "").strip()
    if freq == "日线":
        return 0
    if freq in {"daily", "D", "1d"}:
        return 1
    return 5


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(str(get_task_env(name, str(default)) or default).strip())
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _raw_a_code(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        raw = raw.split(".", 1)[1]
    if len(raw) >= 8 and raw[:2] in {"SH", "SZ", "BJ"}:
        raw = raw[2:]
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits if len(digits) == 6 else ""


def _prefixed_a_symbol(code: str) -> str:
    pure = _raw_a_code(code)
    if not pure:
        return str(code or "").strip()
    if pure.startswith(("5", "6", "9")):
        return f"SH.{pure}"
    if pure.startswith(("4", "8")):
        return f"BJ.{pure}"
    return f"SZ.{pure}"


def _append_candidate_symbol(out: list[str], value: Any) -> None:
    code = _raw_a_code(value)
    if code and code not in out:
        out.append(code)


def _symbol_query_values(symbols: list[str]) -> list[str]:
    values: list[str] = []
    for symbol in symbols:
        code = _raw_a_code(symbol)
        if not code:
            continue
        for value in (code, _prefixed_a_symbol(code)):
            if value and value not in values:
                values.append(value)
    return values


def _postmarket_rollup_scope() -> str:
    configured = str(get_task_env("WEEKLY_ROLLUP_SCOPE", "") or "").strip().lower()
    if configured:
        return configured
    if get_task_env("SIGNALS_POSTMARKET_RUN_ID") or get_task_env("SIGNALS_POSTMARKET_TRADE_DATE"):
        return "postmarket_candidates"
    return "all"


def _rollup_symbol_limit(scope: str) -> int:
    default = ROLLUP_POSTMARKET_CANDIDATE_MAX_SYMBOLS if scope == "postmarket_candidates" else ROLLUP_MAX_SYMBOLS
    return _env_int("WEEKLY_ROLLUP_MAX_SYMBOLS", default, minimum=1, maximum=ROLLUP_MAX_SYMBOLS)


def _symbols_with_daily(db: Database, collection: str) -> list[str]:
    try:
        return [
            symbol for symbol in db[collection].distinct("meta.symbol", {"meta.freq": {"$in": DAILY_FREQS}})
            if symbol
        ]
    except Exception:
        return []


def _daily_docs(db: Database, collection: str, symbol: str) -> list[dict[str, Any]]:
    return list(db[collection].find(
        {"meta.symbol": symbol, "meta.freq": {"$in": DAILY_FREQS}},
        {"_id": 0},
    ).sort("dt", 1))


def _symbols_from_stock_minute_selection(db: Database) -> list[str]:
    configured = str(get_task_env("WEEKLY_ROLLUP_SELECTION_META_ID", "") or "").strip()
    meta_ids = [configured] if configured else []
    if not meta_ids and (_postmarket_rollup_scope() == "postmarket_candidates" or get_task_env("SIGNALS_POSTMARKET_RUN_ID")):
        meta_ids.append("stock_minute:postmarket_selection:_meta")
    meta_ids.append("stock_minute:selection:_meta")
    doc: dict[str, Any] = {}
    for meta_id in dict.fromkeys(meta_ids):
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
            _append_candidate_symbol(symbols, value)
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
                _append_candidate_symbol(symbols, row.get("raw_code") or row.get("symbol") or row.get("code"))
            else:
                _append_candidate_symbol(symbols, row)
    return symbols


def _postmarket_candidate_symbols(db: Database, *, limit: int) -> list[str]:
    symbols: list[str] = []
    for value in _symbols_from_stock_minute_selection(db):
        _append_candidate_symbol(symbols, value)
    for value in _symbols_from_terminal_pool(db):
        _append_candidate_symbol(symbols, value)
    return symbols[:limit]


def _postmarket_stock_symbols(db: Database) -> list[str]:
    trade_date = str(get_task_env("SIGNALS_POSTMARKET_TRADE_DATE", "") or "").strip()[:10]
    if not trade_date:
        return []
    limit = _rollup_symbol_limit("all")
    symbols: list[str] = []
    seen: set[str] = set()
    try:
        cursor = db["fullmarket_spot_snapshots"].find(
            {"trade_date": trade_date},
            {"_id": 0, "code": 1},
        )
    except Exception:
        return []
    for row in cursor:
        code = str(row.get("code") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        symbols.append(code)
        if len(symbols) >= limit:
            break
    return symbols


def _daily_docs_by_symbol(db: Database, collection: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    query: dict[str, Any] = {"meta.freq": {"$in": DAILY_FREQS}}
    if collection == "bars":
        scope = _postmarket_rollup_scope()
        limit = _rollup_symbol_limit(scope)
        symbols = _postmarket_candidate_symbols(db, limit=limit) if scope == "postmarket_candidates" else []
        if not symbols and (scope == "postmarket_candidates" or get_task_env("SIGNALS_POSTMARKET_TRADE_DATE")):
            symbols = _postmarket_stock_symbols(db)
        if symbols:
            query["meta.symbol"] = {"$in": _symbol_query_values(symbols)}
        elif scope == "postmarket_candidates" or get_task_env("SIGNALS_POSTMARKET_RUN_ID"):
            return grouped
    cursor = db[collection].find(
        query,
        {"_id": 0},
    )
    for doc in cursor:
        meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
        symbol = str(meta.get("symbol") or "").strip()
        if not symbol:
            continue
        grouped.setdefault(symbol, []).append(doc)
    return grouped


def _weekly_docs(symbol: str, docs: list[dict[str, Any]], *, collection: str) -> list[dict[str, Any]]:
    if not docs:
        return []
    source_meta = next((doc.get("meta") for doc in docs if isinstance(doc.get("meta"), dict)), {}) or {}
    market = str(source_meta.get("market") or ("HK" if str(symbol).upper().startswith("HK.") else "A")).upper()
    df = pd.DataFrame(docs)
    if df.empty or "dt" not in df.columns:
        return []
    df["dt"] = pd.to_datetime(df["dt"], errors="coerce")
    df = df.dropna(subset=["dt"])
    if df.empty:
        return []
    df["_freq_priority"] = df["meta"].map(_daily_freq_priority) if "meta" in df.columns else 10
    df["_row_order"] = range(len(df))
    df = (
        df.sort_values(["dt", "_freq_priority", "_row_order"])
        .drop_duplicates(subset=["dt"], keep="first")
        .sort_values("dt")
        .set_index("dt")
    )
    for column in ("open", "high", "low", "close", "vol", "amount"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    required = ["open", "high", "low", "close"]
    if any(column not in df.columns for column in required):
        return []
    df["_source_dt"] = df.index
    weekly = df.resample("W-FRI").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "vol": "sum" if "vol" in df.columns else "first",
        "amount": "sum" if "amount" in df.columns else "first",
        "_source_dt": "last",
    })
    weekly = weekly.dropna(subset=required, how="any")
    source = "daily_rollup"
    if collection == "index_bars":
        source = "index_daily_rollup"
    out: list[dict[str, Any]] = []
    for dt_idx, row in weekly.iterrows():
        period_end = pd.to_datetime(dt_idx)
        data_as_of = pd.to_datetime(row.get("_source_dt") or dt_idx)
        is_partial_period = data_as_of.date() < period_end.date()
        display_dt = data_as_of if is_partial_period else period_end
        out.append({
            "dt": display_dt,
            "meta": {
                "symbol": symbol,
                "freq": WEEKLY_FREQ,
                "source": source,
                "market": market,
                "volume_unit": CANONICAL_STOCK_VOLUME_UNIT,
                "source_volume_unit": "daily_shares_rollup",
                "period_end": period_end.date().isoformat(),
                "data_as_of": data_as_of.date().isoformat(),
                "time_semantics": "period_data_as_of" if is_partial_period else "period_end",
                "is_partial_period": is_partial_period,
                **({"asset_type": "index"} if collection == "index_bars" else {}),
            },
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "vol": int(row.get("vol") or 0),
            "amount": int(row.get("amount") or 0),
            "source": source,
        })
    return out


def _rollup_collection(db: Database, collection: str) -> tuple[int, int]:
    grouped = _daily_docs_by_symbol(db, collection)
    symbols = list(grouped)
    if not symbols:
        return 0, 0
    weekly_docs: list[dict[str, Any]] = []
    for symbol, docs in grouped.items():
        weekly_docs.extend(_weekly_docs(symbol, docs, collection=collection))
    db[collection].delete_many({"meta.symbol": {"$in": symbols}, "meta.freq": WEEKLY_FREQ})
    for start in range(0, len(weekly_docs), ROLLUP_INSERT_BATCH_SIZE):
        batch = weekly_docs[start:start + ROLLUP_INSERT_BATCH_SIZE]
        if batch:
            db[collection].insert_many(batch, ordered=False)
    return len(weekly_docs), len(symbols)


def sync_weekly_rollup(db: Database, proxy_url: str = None) -> dict:
    """Generate cached weekly bars for stocks and indices from daily cache."""
    now = naive_market_now("A")
    stock_scope = _postmarket_rollup_scope()
    stock_inserted, stock_symbols = _rollup_collection(db, "bars")
    index_inserted, index_symbols = _rollup_collection(db, "index_bars")
    inserted = stock_inserted + index_inserted
    db["data_freshness"].update_one(
        {"domain": "kline", "market": "A", "mode": "historical", "collection": "weekly_rollup"},
        {"$set": {
            "domain": "kline",
            "market": "A",
            "mode": "historical",
            "lane": "workbench_lane",
            "collection": "weekly_rollup",
            "freshness": "fresh" if inserted else "empty",
            "latest_dt": now.date().isoformat(),
            "as_of": now.date().isoformat(),
            "updated_at": now,
            "stale_reason": "" if inserted else "weekly_cache_missing",
            "count": inserted,
        }},
        upsert=True,
    )
    logger.info("weekly rollup: +%d bars stocks=%d indices=%d scope=%s", inserted, stock_symbols, index_symbols, stock_scope)
    return {
        "inserted": inserted,
        "stock_symbols": stock_symbols,
        "index_symbols": index_symbols,
        "stock_inserted": stock_inserted,
        "index_inserted": index_inserted,
        "stock_scope": stock_scope,
        "stock_symbol_limit": _rollup_symbol_limit(stock_scope),
    }
