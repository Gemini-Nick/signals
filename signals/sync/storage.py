# -*- coding: utf-8 -*-
"""Mongo storage contracts for the Signals data plane."""
from __future__ import annotations

import logging
from datetime import datetime

from pymongo import ASCENDING, DESCENDING
from pymongo.database import Database
from pymongo.errors import OperationFailure, PyMongoError

logger = logging.getLogger("signals.sync.storage")


def _safe_create_index(collection, keys, **kwargs) -> None:
    try:
        collection.create_index(keys, **kwargs)
    except OperationFailure as exc:
        if getattr(exc, "code", None) in {85, 86}:
            logger.debug("index already exists for %s: %s", collection.name, exc)
            return
        logger.warning("create index skipped for %s: %s", collection.name, exc)
    except PyMongoError as exc:
        logger.warning("create index failed for %s: %s", collection.name, exc)


def _drop_ttl_indexes(collection) -> None:
    """Canonical collections must not expire; remove old TTL indexes if present."""
    try:
        for name, spec in collection.index_information().items():
            if name == "_id_":
                continue
            if "expireAfterSeconds" in spec:
                collection.drop_index(name)
                logger.info("dropped TTL index %s on %s", name, collection.name)
    except OperationFailure as exc:
        if getattr(exc, "code", None) == 27:
            logger.debug("ttl index already dropped for %s", collection.name)
            return
        logger.warning("drop ttl indexes failed for %s: %s", collection.name, exc)
    except PyMongoError as exc:
        logger.warning("drop ttl indexes failed for %s: %s", collection.name, exc)


def _drop_index_if_present(collection, name: str) -> None:
    try:
        if name in collection.index_information():
            collection.drop_index(name)
            logger.info("dropped legacy index %s on %s", name, collection.name)
    except OperationFailure as exc:
        if getattr(exc, "code", None) == 27:
            return
        logger.warning("drop legacy index failed for %s.%s: %s", collection.name, name, exc)
    except PyMongoError as exc:
        logger.warning("drop legacy index failed for %s.%s: %s", collection.name, name, exc)


def _cleanup_legacy_freshness_docs(db: Database) -> None:
    """Remove module-name freshness rows that duplicate the canonical domains."""
    legacy_pairs = [
        ("quote_snapshots", "quote_snapshots"),
        ("market_pools", "market_pools"),
        ("signal_pool", "signals"),
        ("stock_daily", "bars"),
        ("cache_preheat", "bars"),
        ("index_daily", "index_bars"),
        ("index_minute", "index_bars"),
    ]
    try:
        db["data_freshness"].delete_many({
            "$or": [{"domain": domain, "collection": collection} for domain, collection in legacy_pairs]
        })
    except PyMongoError as exc:
        logger.warning("legacy freshness cleanup skipped: %s", exc)


def ensure_storage_model(db: Database) -> None:
    """Create/update indexes for the unified cache model.

    This function is intentionally idempotent and safe to run at daemon start.
    """
    now = datetime.now()

    for name in ("board_ranking", "concept_ranking", "board_heat_ticks", "chain_heat_snapshots", "bars", "index_bars"):
        _drop_ttl_indexes(db[name])

    _safe_create_index(db["bars"], [("meta.symbol", ASCENDING), ("meta.freq", ASCENDING), ("dt", ASCENDING)])
    _safe_create_index(db["bars"], [("meta.freq", ASCENDING), ("dt", DESCENDING), ("meta.symbol", ASCENDING)])
    _safe_create_index(db["kline_cache"], [("code", ASCENDING), ("freq", ASCENDING), ("dt", ASCENDING)])
    _safe_create_index(db["index_bars"], [("meta.symbol", ASCENDING), ("meta.freq", ASCENDING), ("dt", ASCENDING)])
    _safe_create_index(db["index_bars"], [("meta.freq", ASCENDING), ("dt", DESCENDING), ("meta.symbol", ASCENDING)])
    _safe_create_index(db["board_ranking"], [("source", ASCENDING), ("dt", ASCENDING)])
    _safe_create_index(db["concept_ranking"], [("source", ASCENDING), ("dt", ASCENDING)])
    _safe_create_index(db["board_heat_ticks"], [("kind", ASCENDING), ("name", ASCENDING), ("trade_minute", ASCENDING)])
    _safe_create_index(db["board_heat_ticks"], [("source", ASCENDING), ("trade_minute", ASCENDING)])
    _safe_create_index(db["chain_heat_snapshots"], [("market", ASCENDING), ("trade_minute", ASCENDING), ("rank", ASCENDING)])
    _safe_create_index(db["chain_heat_snapshots"], [("chain_id", ASCENDING), ("node_id", ASCENDING), ("trade_minute", ASCENDING)])
    _safe_create_index(db["minute_readiness"], [("trade_date", ASCENDING), ("domain", ASCENDING), ("symbol", ASCENDING), ("freq", ASCENDING)])
    _safe_create_index(db["minute_preheat_universe"], [("trade_date", ASCENDING), ("status", ASCENDING), ("order", ASCENDING)])
    _safe_create_index(db["minute_preheat_universe"], [("trade_date", ASCENDING), ("symbol", ASCENDING)], unique=True, sparse=True)
    _safe_create_index(db["fullmarket_spot_snapshots"], [("date_key", ASCENDING), ("code", ASCENDING)], unique=True)
    _safe_create_index(db["fullmarket_spot_snapshots"], [("date_key", ASCENDING), ("symbol", ASCENDING)])
    _safe_create_index(db["fullmarket_spot_snapshots"], [("expires_at", ASCENDING)], expireAfterSeconds=0)

    for name in ("board_em", "board_ths", "board_sina", "concept_em", "concept_ths", "concept_sina"):
        _safe_create_index(db[name], [("expires_at", ASCENDING)], expireAfterSeconds=0)

    _safe_create_index(db["quote_snapshots"], [("symbol", ASCENDING), ("snapshot_at", ASCENDING)])
    _safe_create_index(db["quote_snapshots"], [("expires_at", ASCENDING)], expireAfterSeconds=0)
    _safe_create_index(db["market_pools"], [("pool", ASCENDING), ("dt", ASCENDING)])
    _safe_create_index(db["market_pools"], [("expires_at", ASCENDING)], expireAfterSeconds=0)
    _safe_create_index(db["terminal_realtime_pool"], [("pool", ASCENDING), ("market", ASCENDING), ("updated_at", ASCENDING)])
    _safe_create_index(db["terminal_stock_pool"], [("pool", ASCENDING), ("market", ASCENDING), ("updated_at", ASCENDING)])
    _safe_create_index(db["terminal_technical_signals"], [("dedupe_key", ASCENDING)], unique=True, sparse=True)
    _safe_create_index(db["terminal_technical_signals"], [("symbol", ASCENDING), ("as_of", ASCENDING), ("freq", ASCENDING), ("updated_at", ASCENDING)])
    _safe_create_index(db["knowledge_market_views"], [("view_id", ASCENDING)], unique=True, sparse=True)
    _safe_create_index(db["knowledge_market_views"], [("target_type", ASCENDING), ("symbol", ASCENDING), ("as_of", ASCENDING), ("updated_at", ASCENDING)])
    _safe_create_index(db["signals"], [("dedupe_key", ASCENDING)], unique=True, sparse=True)
    _safe_create_index(db["signals"], [("symbol", ASCENDING), ("signal_date", ASCENDING), ("freq", ASCENDING)])
    _safe_create_index(db["strategy_snapshots"], [("as_of", ASCENDING)], unique=True, sparse=True)
    _safe_create_index(db["strategy_snapshots"], [("updated_at", ASCENDING)])
    _safe_create_index(db["trade_pairs"], [("dedupe_key", ASCENDING)], unique=True, sparse=True)
    _safe_create_index(db["sync_log"], [("module", ASCENDING), ("market", ASCENDING), ("lane", ASCENDING), ("last_run", ASCENDING)])
    _safe_create_index(db["sync_runs"], [("run_id", ASCENDING)], unique=True, sparse=True)
    _safe_create_index(db["sync_runs"], [("trade_date", ASCENDING), ("status", ASCENDING), ("updated_at", ASCENDING)])
    _safe_create_index(db["sync_tasks"], [("run_id", ASCENDING), ("phase", ASCENDING), ("status", ASCENDING), ("updated_at", ASCENDING)])
    _safe_create_index(db["sync_tasks"], [("module", ASCENDING), ("shard_key", ASCENDING), ("status", ASCENDING)])
    _safe_create_index(db["provider_health"], [("provider", ASCENDING), ("endpoint", ASCENDING), ("domain", ASCENDING)])
    _drop_index_if_present(db["data_freshness"], "domain_1_market_1_mode_1_collection_1")
    _safe_create_index(db["data_freshness"], [("domain", ASCENDING), ("market", ASCENDING), ("mode", ASCENDING), ("lane", ASCENDING), ("collection", ASCENDING)])
    _safe_create_index(db["data_freshness"], [("domain", ASCENDING), ("market", ASCENDING), ("mode", ASCENDING), ("collection", ASCENDING), ("freq", ASCENDING), ("shard_key", ASCENDING)])
    _cleanup_legacy_freshness_docs(db)

    db["data_freshness"].update_one(
        {"domain": "storage", "market": "all", "mode": "system", "collection": "mongo_indexes"},
        {"$set": {
            "domain": "storage",
            "market": "all",
            "mode": "system",
            "collection": "mongo_indexes",
            "freshness": "fresh",
            "latest_dt": now.date().isoformat(),
            "as_of": now.date().isoformat(),
            "updated_at": now,
            "stale_reason": "",
        }},
        upsert=True,
    )
