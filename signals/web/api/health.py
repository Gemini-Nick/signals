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


def _date_key(value):
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _latest_collection_doc(col):
    fields = ("dt", "latest_dt", "signal_date", "snapshot_at", "updated_at")
    candidates = []
    projection = {field: 1 for field in fields}
    for field in fields:
        try:
            doc = col.find_one(
                {field: {"$exists": True, "$ne": None}},
                projection,
                sort=[(field, -1)],
            )
            if doc and doc.get(field) is not None:
                candidates.append(doc)
        except Exception:
            continue
    if not candidates:
        return {}
    return max(
        candidates,
        key=lambda doc: max((_date_key(doc.get(field)) for field in fields), default=""),
    )


def _latest_date_from_doc(doc):
    for field in ("dt", "latest_dt", "signal_date", "snapshot_at", "updated_at"):
        value = doc.get(field)
        if value is not None:
            return str(value.date()) if hasattr(value, "date") else str(value)[:10]
    return ""


def _symbol_candidates(symbol: str) -> list[str]:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return []
    pure = raw.split(".", 1)[-1] if "." in raw else raw
    candidates = [raw, pure, raw.lower(), pure.lower()]
    if "." in raw and raw.split(".", 1)[0] in {"SH", "SZ", "BJ"}:
        prefix = raw.split(".", 1)[0]
        candidates.extend([f"{prefix}{pure}", f"{prefix.lower()}{pure}"])
    if len(raw) >= 8 and raw[:2] in {"SH", "SZ", "BJ"}:
        candidates.append(f"{raw[:2]}.{raw[2:]}")
    if pure.isdigit() and len(pure) == 6:
        prefix = "SH" if pure.startswith(("5", "6", "9")) else "SZ"
        candidates.extend([f"{prefix}.{pure}", f"{prefix}{pure}", f"{prefix.lower()}{pure}"])
    return list(dict.fromkeys(candidates))


def _active_pool_coverage(db):
    doc = db["market_pools"].find_one(
        {"pool": "active"},
        {"_id": 1, "dt": 1, "symbols": 1, "count": 1, "updated_at": 1, "freshness": 1},
        sort=[("dt", -1), ("updated_at", -1)],
    )
    if not doc:
        return {
            "pool": "active",
            "count": 0,
            "bars_covered": 0,
            "quote_covered": 0,
            "bars_missing": [],
            "quote_missing": [],
        }

    symbols = [str(item) for item in doc.get("symbols", []) if item]
    bars_missing = []
    quote_missing = []
    bars_covered = 0
    quote_covered = 0
    for symbol in symbols:
        candidates = _symbol_candidates(symbol)
        if db["bars"].find_one({
            "meta.symbol": {"$in": candidates},
            "meta.freq": {"$in": ["daily", "日线", "D", "1d"]},
        }, {"_id": 1}):
            bars_covered += 1
        elif len(bars_missing) < 10:
            bars_missing.append(symbol)

        quote_ids = [f"{candidate}:latest" for candidate in candidates]
        if db["quote_snapshots"].find_one({
            "$or": [
                {"symbol": {"$in": candidates}},
                {"_id": {"$in": quote_ids}},
            ]
        }, {"_id": 1}):
            quote_covered += 1
        elif len(quote_missing) < 10:
            quote_missing.append(symbol)

    count = len(symbols)
    return {
        "pool": "active",
        "pool_id": str(doc.get("_id", "")),
        "dt": str(doc.get("dt", "")),
        "updated_at": str(doc.get("updated_at", "")),
        "freshness": doc.get("freshness", ""),
        "count": count,
        "bars_covered": bars_covered,
        "quote_covered": quote_covered,
        "bars_coverage_pct": round(bars_covered / count * 100, 2) if count else 0,
        "quote_coverage_pct": round(quote_covered / count * 100, 2) if count else 0,
        "bars_missing": bars_missing,
        "quote_missing": quote_missing,
    }


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
            "coverage": {
                "pool": "active",
                "count": 0,
                "bars_covered": 0,
                "quote_covered": 0,
                "bars_missing": [],
                "quote_missing": [],
            },
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
        "signals",
        "index_bars",
        "quote_snapshots",
        "market_pools",
        "rotation_history",
        "cluster_history",
        "refresh_requests",
    ]:
        try:
            col = db[collection]
            latest = _latest_collection_doc(col)
            freshness = db["data_freshness"].find_one(
                {"collection": collection},
                {"_id": 0, "freshness": 1, "stale_reason": 1, "latest_dt": 1, "updated_at": 1},
                sort=[("updated_at", -1)],
            ) or {}
            items.append({
                "collection": collection,
                "count": col.estimated_document_count(),
                "latest_dt": _latest_date_from_doc(latest) or str(freshness.get("latest_dt") or ""),
                "updated_at": str((latest or {}).get("updated_at", "") or freshness.get("updated_at", "")),
                "freshness": freshness.get("freshness", ""),
                "stale_reason": freshness.get("stale_reason", ""),
            })
        except Exception as e:
            items.append({"collection": collection, "error": str(e)})
    return {
        "mongo_enabled": True,
        "status": "ok",
        "items": items,
        "coverage": _active_pool_coverage(db),
    }


@router.get("/calendar")
def calendar_health():
    from signals.core.calendar.engine import get_calendar
    cal = get_calendar()
    info = cal.validate()
    return {
        "status": "warning" if info["warnings"] else "ok",
        **info,
    }
