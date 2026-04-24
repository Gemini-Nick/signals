# -*- coding: utf-8 -*-
"""Data health endpoints."""
from fastapi import APIRouter

import config
from signals.data.gateway import (
    get_cache_contracts,
    get_data_freshness,
    get_provider_health,
)

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("/data-sources")
def data_sources():
    return get_provider_health()


@router.get("/data-freshness")
def data_freshness():
    return get_data_freshness()


@router.get("/contracts")
def cache_contracts():
    return get_cache_contracts()


@router.get("/cache")
def cache_health():
    from signals.data.mongo_fallback import get_db

    db = get_db()
    if db is None:
        return {
            "mongo_enabled": bool(config.MONGO_URL),
            "status": "disabled",
            "items": [],
        }

    items = []
    for collection in [
        "bars",
        "board_ranking",
        "concept_ranking",
        "board_constituents",
        "board_em",
        "board_ths",
        "board_sina",
        "concept_em",
        "concept_ths",
        "concept_sina",
        "stock_names",
        "concept_constituents",
        "social_comment",
        "social_weibo",
        "social_heat",
        "index_bars",
        "quote_snapshots",
        "market_pools",
        "rotation_history",
        "cluster_history",
        "refresh_requests",
    ]:
        try:
            col = db[collection]
            latest = col.find_one({}, {"dt": 1, "updated_at": 1}, sort=[("dt", -1)])
            items.append({
                "collection": collection,
                "count": col.estimated_document_count(),
                "latest_dt": str((latest or {}).get("dt", "")),
                "updated_at": str((latest or {}).get("updated_at", "")),
            })
        except Exception as e:
            items.append({"collection": collection, "error": str(e)})
    return {"mongo_enabled": True, "status": "ok", "items": items}
