# -*- coding: utf-8 -*-
"""Build active trading pools used by realtime caches."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Iterable

from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.core.trading_dates import trading_day_key

logger = logging.getLogger("signals.sync.market_pools")


def _normalize_symbol(value: object) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    if raw.startswith(("SH.", "SZ.", "BJ.", "HK.", "US.")):
        return raw
    if len(raw) >= 8 and raw[:2] in {"SH", "SZ", "BJ", "HK", "US"}:
        return f"{raw[:2]}.{raw[2:]}"
    pure = raw.replace("SH", "").replace("SZ", "").replace("BJ", "")
    if pure.isdigit() and len(pure) == 6:
        if pure.startswith(("5", "6", "9")):
            return f"SH.{pure}"
        if pure.startswith(("0", "2", "3")):
            return f"SZ.{pure}"
        if pure.startswith(("4", "8")):
            return f"BJ.{pure}"
    if pure.isdigit() and len(pure) == 5:
        return f"HK.{pure}"
    return raw


def _add_many(target: dict[str, set[str]], values: Iterable[object], source: str) -> None:
    for value in values:
        symbol = _normalize_symbol(value)
        if symbol:
            target.setdefault(symbol, set()).add(source)


def _symbols_from_doc(doc: dict) -> list[str]:
    symbols: list[str] = []
    for key in ("symbol", "code", "raw_code"):
        if doc.get(key):
            symbols.append(str(doc[key]))
    for key in ("symbols", "stocks", "constituents"):
        value = doc.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    symbols.append(str(item.get("symbol") or item.get("code") or item.get("raw_code") or ""))
                else:
                    symbols.append(str(item))
    return symbols


def _collect_pool_symbols(db: Database) -> dict[str, set[str]]:
    import config

    pool: dict[str, set[str]] = {}
    _add_many(pool, getattr(config, "WHITELIST", []), "whitelist")

    for doc in db["trades"].find({}, {"symbol": 1}).sort("updated_at", -1).limit(100):
        _add_many(pool, [doc.get("symbol")], "trades")

    for doc in db["signals"].find({}, {"symbol": 1}).sort("signal_date", -1).limit(200):
        _add_many(pool, [doc.get("symbol")], "signals")

    for collection in ("board_constituents", "concept_constituents"):
        cursor = db[collection].find({}, {"symbols": 1, "stocks": 1, "constituents": 1}).sort("updated_at", -1).limit(20)
        for doc in cursor:
            _add_many(pool, _symbols_from_doc(doc), collection)

    for doc in db["bars"].aggregate([
        {"$sort": {"dt": -1}},
        {"$group": {"_id": "$meta.symbol", "latest_dt": {"$first": "$dt"}}},
        {"$limit": 200},
    ]):
        _add_many(pool, [doc.get("_id")], "bars")

    return pool


def _write_data_freshness(db: Database, count: int, status: str) -> None:
    now = naive_market_now("A")
    trade_date = trading_day_key("A", now=now)
    freshness = "fresh" if count else "empty"
    db["data_freshness"].update_one(
        {"domain": "market_pool", "market": "A", "mode": "realtime", "collection": "market_pools"},
        {"$set": {
            "domain": "market_pool",
            "market": "A",
            "mode": "realtime",
            "collection": "market_pools",
            "freshness": freshness,
            "latest_dt": trade_date if count else None,
            "as_of": trade_date if count else None,
            "date_key": trade_date.replace("-", "") if count else None,
            "updated_at": now,
            "stale_reason": "" if count else status,
        }},
        upsert=True,
    )


def sync_market_pools(db: Database, proxy_url: str = None) -> dict:
    """Create active market pool from configured, held, signaled, and sector symbols."""
    del proxy_url
    now = naive_market_now("A")
    trade_date = trading_day_key("A", now=now)
    sources_by_symbol = _collect_pool_symbols(db)
    source_priority = {
        "whitelist": 0,
        "trades": 1,
        "bars": 2,
        "signals": 3,
        "board_constituents": 4,
        "concept_constituents": 5,
    }

    def rank_symbol(symbol: str) -> tuple[int, str]:
        sources = sources_by_symbol.get(symbol, set())
        rank = min((source_priority.get(source, 9) for source in sources), default=9)
        return rank, symbol

    symbols = sorted(sources_by_symbol, key=rank_symbol)

    import config

    max_size = int(getattr(config, "MAX_POOL_SIZE", 50) or 50)
    if max_size > 0:
        symbols = symbols[:max_size]

    items = [
        {"symbol": symbol, "sources": sorted(sources_by_symbol.get(symbol, []))}
        for symbol in symbols
    ]
    doc = {
        "_id": f"active:{trade_date}",
        "pool": "active",
        "dt": trade_date,
        "trade_date": trade_date,
        "symbols": symbols,
        "items": items,
        "count": len(symbols),
        "source": "whitelist+trades+signals+constituents+bars",
        "freshness": "fresh" if symbols else "empty",
        "updated_at": now,
        "expires_at": now + timedelta(days=7),
    }
    result = db["market_pools"].update_one({"_id": doc["_id"]}, {"$set": doc}, upsert=True)
    _write_data_freshness(db, len(symbols), "market_pool_empty")
    logger.info("market pool active: %d symbols", len(symbols))
    return {
        "inserted": 1 if result.upserted_id else 0,
        "modified": result.modified_count,
        "count": len(symbols),
        "target_collection": "market_pools",
    }
