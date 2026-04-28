# -*- coding: utf-8 -*-
"""Build the next-session realtime universe for the terminal."""
from __future__ import annotations

import logging
import os
from typing import Any

from pymongo.database import Database

from signals.core.market_time import naive_market_now

logger = logging.getLogger("signals.sync.terminal_pool")


def _pure_a_code(symbol: Any) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    pure = raw.split(".", 1)[-1] if "." in raw else raw
    pure = pure.replace("SH", "").replace("SZ", "").replace("BJ", "")
    return pure if pure.isdigit() and len(pure) == 6 else ""


def _add_stock(stocks: list[str], value: Any) -> None:
    code = _pure_a_code(value)
    if code and code not in stocks:
        stocks.append(code)


def _strategy_symbols(db: Database) -> list[str]:
    doc = db["strategy_snapshots"].find_one(
        {"snapshot": {"$exists": True}},
        {"snapshot": 1},
        sort=[("updated_at", -1), ("as_of", -1)],
    ) or {}
    snapshot = doc.get("snapshot") or {}
    symbols: list[str] = []
    for key in ("candidates", "decision_queue", "warnings", "buy_candidates", "sell_warnings"):
        for row in snapshot.get(key) or []:
            if not isinstance(row, dict):
                continue
            for field in ("symbol", "code", "raw_code"):
                _add_stock(symbols, row.get(field))
            metadata = row.get("metadata")
            if isinstance(metadata, dict):
                for field in ("symbol", "code", "raw_code"):
                    _add_stock(symbols, metadata.get(field))
    for row in snapshot.get("themes") or []:
        if isinstance(row, dict):
            for field in ("leader_symbol", "leader_code", "representative_symbol", "representative_code"):
                _add_stock(symbols, row.get(field))
    return symbols


def _active_pool_symbols(db: Database) -> list[str]:
    doc = db["market_pools"].find_one({"pool": "active"}, {"symbols": 1, "items": 1}, sort=[("dt", -1), ("updated_at", -1)]) or {}
    symbols: list[str] = []
    for symbol in doc.get("symbols") or []:
        _add_stock(symbols, symbol)
    for item in doc.get("items") or []:
        if isinstance(item, dict):
            _add_stock(symbols, item.get("symbol") or item.get("code"))
    return symbols


def _recent_symbols(db: Database) -> list[str]:
    symbols: list[str] = []
    for doc in db["sync_log"].find(
        {"module": {"$in": ["stock_minute", "stock_daily"]}, "status": "ok", "symbol": {"$exists": True}},
        {"symbol": 1},
    ).sort("last_run", -1).limit(120):
        _add_stock(symbols, doc.get("symbol"))
    return symbols


def _top_heat_names(db: Database, kind: str, limit: int) -> list[str]:
    docs = list(db["board_heat_ticks"].find(
        {"kind": kind},
        {"name": 1, "trade_minute": 1, "rank_idx": 1},
    ).sort([("trade_minute", -1), ("rank_idx", 1)]).limit(limit * 4))
    names: list[str] = []
    for doc in docs:
        name = str(doc.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


def sync_terminal_realtime_pool(db: Database, proxy_url: str = None) -> dict:
    import config

    now = naive_market_now("A")
    stock_limit = int(os.getenv("TERMINAL_REALTIME_STOCK_LIMIT", os.getenv("STOCK_MINUTE_SIGNAL_MAX_CODES", "24")))
    stocks: list[str] = []
    for value in os.getenv("TERMINAL_REALTIME_PRIORITY_CODES", "688802,300575").replace(";", ",").split(","):
        _add_stock(stocks, value)
    for value in getattr(config, "WHITELIST", []):
        _add_stock(stocks, value)
    for value in _strategy_symbols(db) + _active_pool_symbols(db) + _recent_symbols(db):
        _add_stock(stocks, value)
    stocks = stocks[:stock_limit]

    doc = {
        "pool": "terminal_realtime",
        "market": "A",
        "dt": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "updated_at": now,
        "stocks": stocks,
        "indices": list(getattr(config, "INDEX_AK_CODES", {}).values()),
        "industries": _top_heat_names(db, "industry", 20),
        "concepts": _top_heat_names(db, "concept", 20),
        "stock_limit": stock_limit,
        "source": "postmarket_pool_builder",
    }
    db["terminal_realtime_pool"].update_one(
        {"pool": "terminal_realtime", "market": "A"},
        {"$set": doc},
        upsert=True,
    )
    db["data_freshness"].update_one(
        {"domain": "terminal_pool", "market": "A", "mode": "realtime", "collection": "terminal_realtime_pool"},
        {"$set": {
            "domain": "terminal_pool",
            "market": "A",
            "mode": "realtime",
            "lane": "workbench_lane",
            "collection": "terminal_realtime_pool",
            "freshness": "fresh" if stocks else "empty",
            "latest_dt": now.date().isoformat(),
            "as_of": now.date().isoformat(),
            "updated_at": now,
            "stale_reason": "" if stocks else "terminal_realtime_pool_empty",
            "count": len(stocks),
        }},
        upsert=True,
    )
    logger.info("terminal realtime pool: stocks=%d industries=%d concepts=%d", len(stocks), len(doc["industries"]), len(doc["concepts"]))
    return {
        "inserted": len(stocks),
        "stocks": len(stocks),
        "indices": len(doc["indices"]),
        "industries": len(doc["industries"]),
        "concepts": len(doc["concepts"]),
    }
