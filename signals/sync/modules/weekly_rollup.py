# -*- coding: utf-8 -*-
"""Build cached weekly bars from verified daily bars."""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd
from pymongo.database import Database

from signals.core.market_time import naive_market_now

logger = logging.getLogger("signals.sync.weekly_rollup")

DAILY_FREQS = ["日线", "daily", "D", "1d"]
WEEKLY_FREQ = "周线"


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


def _weekly_docs(symbol: str, docs: list[dict[str, Any]], *, collection: str) -> list[dict[str, Any]]:
    if not docs:
        return []
    df = pd.DataFrame(docs)
    if df.empty or "dt" not in df.columns:
        return []
    df["dt"] = pd.to_datetime(df["dt"], errors="coerce")
    df = df.dropna(subset=["dt"]).sort_values("dt").set_index("dt")
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
                "market": "A",
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
    inserted = 0
    symbols = _symbols_with_daily(db, collection)
    for symbol in symbols:
        docs = _weekly_docs(symbol, _daily_docs(db, collection, symbol), collection=collection)
        if not docs:
            continue
        db[collection].delete_many({"meta.symbol": symbol, "meta.freq": WEEKLY_FREQ})
        db[collection].insert_many(docs, ordered=False)
        inserted += len(docs)
    return inserted, len(symbols)


def sync_weekly_rollup(db: Database, proxy_url: str = None) -> dict:
    """Generate cached weekly bars for stocks and indices from daily cache."""
    now = naive_market_now("A")
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
    logger.info("weekly rollup: +%d bars stocks=%d indices=%d", inserted, stock_symbols, index_symbols)
    return {
        "inserted": inserted,
        "stock_symbols": stock_symbols,
        "index_symbols": index_symbols,
        "stock_inserted": stock_inserted,
        "index_inserted": index_inserted,
    }
