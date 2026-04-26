# -*- coding: utf-8 -*-
"""Build and persist the canonical Signals strategy read model."""
from __future__ import annotations

import logging

from pymongo.database import Database

logger = logging.getLogger("signals.sync.strategy_snapshot")


def sync_strategy_snapshot(db: Database, proxy_url: str = None) -> dict:
    """Persist a derived strategy snapshot after raw caches are warm."""
    del proxy_url
    from signals.strategy.snapshot import build_strategy_snapshot, persist_strategy_snapshot

    snapshot = build_strategy_snapshot(db=db)
    result = persist_strategy_snapshot(snapshot, db=db)
    logger.info(
        "strategy snapshot persisted: candidates=%d warnings=%d themes=%d",
        len(snapshot.get("candidates") or []),
        len(snapshot.get("warnings") or []),
        len(snapshot.get("themes") or []),
    )
    return {
        "inserted": 1 if result.get("upserted") else 0,
        "modified": int(result.get("modified") or 0),
        "count": 1 if result.get("ok") else 0,
        "as_of": snapshot.get("as_of"),
        "candidate_count": len(snapshot.get("candidates") or []),
        "warning_count": len(snapshot.get("warnings") or []),
        "theme_count": len(snapshot.get("themes") or []),
        "target_collection": "strategy_snapshots",
        "errors": [] if result.get("ok") else [result.get("reason", "persist_failed")],
    }
