# -*- coding: utf-8 -*-
"""Minute-bar day-change calculation backed by daily closes."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

import pandas as pd
from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.market_time import naive_market_now

DAILY_FREQS = ("日线", "daily", "D", "1d")
MINUTE_FREQS = ("5分钟", "5min", "5m", "5", "15分钟", "15min", "15m", "15", "30分钟", "30min", "30m", "30")


def _float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        parsed = float(value)
    except Exception:
        return None
    return parsed if pd.notna(parsed) else None


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        parsed = pd.to_datetime(value, errors="coerce")
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    return parsed.date()


def _symbol_candidates(symbol: str, *, asset_type: str) -> list[str]:
    raw = str(symbol or "").strip()
    if not raw:
        return []
    lowered = raw.lower()
    uppered = raw.upper()
    candidates = [raw, lowered, uppered]
    if asset_type == "index":
        pure = raw.split(".")[-1] if "." in raw else raw
        if len(pure) >= 8 and pure[:2].lower() in {"sh", "sz", "bj"}:
            pure = pure[2:]
        if "." in raw:
            prefix, code = raw.split(".", 1)
            if prefix.upper() in {"SH", "SZ", "BJ"}:
                candidates.extend([f"{prefix.lower()}{code}", f"{prefix.upper()}.{code}"])
        elif len(raw) >= 8 and lowered[:2] in {"sh", "sz", "bj"}:
            candidates.append(f"{raw[:2].upper()}.{raw[2:]}")
        elif pure.isdigit() and pure.startswith("399"):
            candidates.extend([f"sz{pure}", f"SZ.{pure}"])
        elif pure.isdigit() and pure.startswith("000"):
            candidates.extend([f"sh{pure}", f"SH.{pure}"])
        return list(dict.fromkeys(candidates))

    pure = raw.split(".")[-1] if "." in raw else raw
    if len(pure) >= 8 and pure[:2].lower() in {"sh", "sz", "bj"}:
        pure = pure[2:]
    candidates.extend([
        pure,
        f"SH.{pure}" if pure.startswith(("5", "6", "9")) else f"SZ.{pure}",
        f"sh{pure}" if pure.startswith(("5", "6", "9")) else f"sz{pure}",
    ])
    return list(dict.fromkeys(candidates))


def _daily_docs(db: Database, symbol: str, *, asset_type: str) -> list[dict]:
    projection = {"_id": 0, "dt": 1, "close": 1, "prev_close": 1, "meta.prev_close": 1}
    symbols = _symbol_candidates(symbol, asset_type=asset_type)
    if not symbols:
        return []

    if asset_type == "index":
        docs = list(db["index_bars"].find(
            {"meta.symbol": {"$in": symbols}, "meta.freq": {"$in": list(DAILY_FREQS)}},
            projection,
        ).sort("dt", 1))
        if docs:
            return docs
        return list(db["bars"].find(
            {
                "meta.symbol": {"$in": symbols},
                "meta.freq": {"$in": list(DAILY_FREQS)},
                "meta.asset_type": "index",
            },
            projection,
        ).sort("dt", 1))

    return list(db["bars"].find(
        {"meta.symbol": {"$in": symbols}, "meta.freq": {"$in": list(DAILY_FREQS)}},
        projection,
    ).sort("dt", 1))


def previous_close_by_trade_date(
    db: Database | None,
    symbol: str,
    trade_dates: Iterable[date],
    *,
    asset_type: str = "stock",
) -> dict[date, tuple[float, str]]:
    """Return {trade_date: (previous_close, base_date_iso)} for minute bars."""
    if db is None:
        return {}
    dates = sorted({d for d in trade_dates if d is not None})
    if not dates:
        return {}

    by_day: dict[date, dict[str, float | None]] = {}
    for doc in _daily_docs(db, symbol, asset_type=asset_type):
        day = _date(doc.get("dt"))
        close = _float(doc.get("close"))
        meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
        prev_close = _float(doc.get("prev_close"))
        if prev_close is None:
            prev_close = _float(meta.get("prev_close"))
        if day:
            by_day[day] = {"close": close, "prev_close": prev_close}

    ordered = sorted((day, values.get("close"), values.get("prev_close")) for day, values in by_day.items())
    if not ordered:
        return {}

    result: dict[date, tuple[float, str]] = {}
    last_close: float | None = None
    last_close_day: date | None = None
    idx = 0
    for trade_date in dates:
        while idx < len(ordered) and ordered[idx][0] < trade_date:
            day, close, _prev = ordered[idx]
            if close and close > 0:
                last_close = float(close)
                last_close_day = day
            idx += 1

        same_day = by_day.get(trade_date)
        same_prev = _float(same_day.get("prev_close")) if same_day else None
        if same_prev and same_prev > 0:
            result[trade_date] = (same_prev, trade_date.isoformat())
        elif last_close and last_close > 0 and last_close_day is not None:
            result[trade_date] = (last_close, last_close_day.isoformat())
    return result


def recalculate_minute_change_pct(
    db: Database | None,
    symbol: str,
    docs: list[dict],
    *,
    asset_type: str = "stock",
) -> list[dict]:
    if not docs:
        return []
    trade_dates = [_date(doc.get("dt")) for doc in docs]
    prev_by_date = previous_close_by_trade_date(db, symbol, [d for d in trade_dates if d], asset_type=asset_type)
    out: list[dict] = []
    for doc, trade_date in zip(docs, trade_dates):
        item = dict(doc)
        meta = dict(item.get("meta") or {})
        close = _float(item.get("close"))
        prev_tuple = prev_by_date.get(trade_date) if trade_date else None
        if close is not None and prev_tuple:
            prev_close, base_date = prev_tuple
            if prev_close > 0:
                change_pct = round((close - prev_close) / prev_close * 100, 4)
                item["prev_close"] = round(prev_close, 6)
                item["change_pct"] = change_pct
                item["pct_chg"] = change_pct
                meta["change_pct_source"] = "daily_prev_close"
                meta["change_pct_base_date"] = base_date
        if meta:
            item["meta"] = meta
        out.append(item)
    return out


def _looks_like_index_symbol(symbol: str) -> bool:
    text = str(symbol or "").lower()
    if text.startswith("sh."):
        return text[3:].startswith("000")
    if text.startswith("sz."):
        return text[3:].startswith("399")
    return (
        (len(text) >= 8 and text.startswith("sh000") and text[2:].isdigit())
        or (len(text) >= 8 and text.startswith("sz399") and text[2:].isdigit())
    )


def _asset_type_for_symbol(col, collection: str, symbol: str) -> str:
    if collection == "index_bars":
        return "index"
    sample = col.find_one(
        {"meta.symbol": symbol, "meta.freq": {"$in": list(MINUTE_FREQS)}},
        {"_id": 0, "meta.asset_type": 1},
        sort=[("dt", -1)],
    ) or {}
    meta = sample.get("meta") if isinstance(sample.get("meta"), dict) else {}
    if meta.get("asset_type") == "index" or _looks_like_index_symbol(symbol):
        return "index"
    if str(symbol or "").strip().isdigit() and str(symbol).startswith("399"):
        return "index"
    return "stock"


def _is_timeseries(db: Database, collection: str) -> bool:
    try:
        info = next(db.list_collections(filter={"name": collection}), None)
    except Exception:
        return False
    return bool((info or {}).get("options", {}).get("timeseries"))


def _change_set(doc: dict) -> dict | None:
    if doc.get("change_pct") is None or doc.get("prev_close") is None:
        return None
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    set_doc = {
        "prev_close": doc.get("prev_close"),
        "change_pct": doc.get("change_pct"),
        "pct_chg": doc.get("pct_chg"),
    }
    if meta.get("change_pct_source"):
        set_doc["meta.change_pct_source"] = meta.get("change_pct_source")
    if meta.get("change_pct_base_date"):
        set_doc["meta.change_pct_base_date"] = meta.get("change_pct_base_date")
    return set_doc


def _replace_timeseries_docs(col, symbol: str, freq: str, docs: list[dict]) -> int:
    if not docs:
        return 0
    dts = [doc.get("dt") for doc in docs if doc.get("dt") is not None]
    if not dts:
        return 0
    prepared = []
    for doc in docs:
        item = {key: value for key, value in doc.items() if key != "_id"}
        prepared.append(item)
    col.delete_many({"meta.symbol": symbol, "meta.freq": freq, "dt": {"$in": dts}})
    return len(col.insert_many(prepared, ordered=False).inserted_ids)


def backfill_minute_change_pct(
    db: Database,
    *,
    collections: Iterable[str] = ("index_bars", "bars"),
    freqs: Iterable[str] = MINUTE_FREQS,
    batch_size: int = 1000,
    symbols: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Refresh existing Mongo minute bars with prev_close/change_pct fields."""
    now = naive_market_now("A")
    freq_values = list(dict.fromkeys(str(freq) for freq in freqs))
    symbol_filter = list(dict.fromkeys(str(symbol) for symbol in symbols)) if symbols else None
    summary: dict[str, Any] = {"collections": {}, "started_at": now.isoformat()}

    for collection in collections:
        col = db[collection]
        is_timeseries = _is_timeseries(db, collection)
        base_query: dict[str, Any] = {"meta.freq": {"$in": freq_values}}
        if symbol_filter:
            base_query["meta.symbol"] = {"$in": symbol_filter}
        collection_summary = {
            "symbols": 0,
            "seen": 0,
            "updated": 0,
            "skipped_no_prev_close": 0,
            "timeseries_replace": is_timeseries,
        }
        for symbol in col.distinct("meta.symbol", base_query):
            if not symbol:
                continue
            collection_summary["symbols"] += 1
            asset_type = _asset_type_for_symbol(col, collection, str(symbol))
            freq_query = {"meta.symbol": symbol, "meta.freq": {"$in": freq_values}}
            for freq in col.distinct("meta.freq", freq_query):
                cursor = col.find(
                    {"meta.symbol": symbol, "meta.freq": freq},
                    {
                        "_id": 1,
                        "dt": 1,
                        "meta": 1,
                        "open": 1,
                        "high": 1,
                        "low": 1,
                        "close": 1,
                        "vol": 1,
                        "amount": 1,
                        "prev_close": 1,
                        "change_pct": 1,
                        "pct_chg": 1,
                    },
                ).sort("dt", 1)
                batch: list[dict] = []
                for doc in cursor:
                    batch.append(doc)
                    if len(batch) >= batch_size:
                        _backfill_batch(db, col, str(symbol), str(freq), batch, asset_type, is_timeseries, collection_summary)
                        batch = []
                if batch:
                    _backfill_batch(db, col, str(symbol), str(freq), batch, asset_type, is_timeseries, collection_summary)
        summary["collections"][collection] = collection_summary
    summary["finished_at"] = naive_market_now("A").isoformat()
    return summary


def _backfill_batch(
    db: Database,
    col,
    symbol: str,
    freq: str,
    batch: list[dict],
    asset_type: str,
    is_timeseries: bool,
    summary: dict[str, Any],
) -> None:
    recalculated = recalculate_minute_change_pct(db, symbol, batch, asset_type=asset_type)
    writable = [doc for doc in recalculated if doc.get("change_pct") is not None and doc.get("prev_close") is not None]
    summary["seen"] += len(batch)
    summary["skipped_no_prev_close"] += len(batch) - len(writable)
    if not writable:
        return
    if is_timeseries:
        summary["updated"] += _replace_timeseries_docs(col, symbol, freq, writable)
        return
    ops = []
    for original, updated in zip(batch, recalculated):
        set_doc = _change_set(updated)
        if set_doc is None:
            continue
        ops.append(UpdateOne({"_id": original["_id"]}, {"$set": set_doc}))
    if ops:
        result = col.bulk_write(ops, ordered=False)
        summary["updated"] += int(getattr(result, "modified_count", 0) or 0) + len(getattr(result, "upserted_ids", {}) or {})
