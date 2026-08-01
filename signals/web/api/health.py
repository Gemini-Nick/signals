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


def _bar_sync_ids(symbol: str) -> set[str]:
    raw = str(symbol or "").strip().upper()
    pure = raw.split(".", 1)[-1] if "." in raw else raw
    ids: set[str] = set()
    if pure.isdigit() and len(pure) == 6:
        ids.add(f"stock_daily:{pure}")
    if raw.startswith("HK.") and pure.isdigit():
        ids.add(f"hk_stock_daily:HK.{pure.zfill(5)}")
    if "." in raw and raw.split(".", 1)[0] in {"SH", "SZ", "BJ"} and pure.isdigit():
        ids.add(f"index_daily:{raw.split('.', 1)[0].lower()}{pure}")
    return ids


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
    candidate_map = {symbol: _symbol_candidates(symbol) for symbol in symbols}
    all_candidates = list(dict.fromkeys(
        candidate
        for candidates in candidate_map.values()
        for candidate in candidates
    ))
    quote_ids = [f"{candidate}:latest" for candidate in all_candidates]
    sync_ids_by_symbol = {symbol: _bar_sync_ids(symbol) for symbol in symbols}
    bar_sync_ids = list(dict.fromkeys(
        sync_id
        for sync_ids in sync_ids_by_symbol.values()
        for sync_id in sync_ids
    ))
    try:
        bar_sync_found = set(db["sync_log"].distinct(
            "_id",
            {"_id": {"$in": bar_sync_ids}},
            maxTimeMS=500,
        ))
        quote_symbols_found = set(db["quote_snapshots"].distinct(
            "symbol",
            {"symbol": {"$in": all_candidates}},
            maxTimeMS=500,
        ))
        quote_ids_found = set(db["quote_snapshots"].distinct(
            "_id",
            {"_id": {"$in": quote_ids}},
            maxTimeMS=500,
        ))
    except Exception as exc:
        return {
            "pool": "active",
            "pool_id": str(doc.get("_id", "")),
            "dt": str(doc.get("dt", "")),
            "updated_at": str(doc.get("updated_at", "")),
            "freshness": doc.get("freshness", ""),
            "count": len(symbols),
            "bars_covered": 0,
            "quote_covered": 0,
            "bars_missing": [],
            "quote_missing": [],
            "coverage_status": "unavailable",
            "coverage_error": f"{exc.__class__.__name__}: {str(exc)[:160]}",
        }

    bars_missing = []
    quote_missing = []
    bars_covered = 0
    quote_covered = 0
    for symbol in symbols:
        candidates = candidate_map[symbol]
        if bar_sync_found.intersection(sync_ids_by_symbol[symbol]):
            bars_covered += 1
        elif len(bars_missing) < 10:
            bars_missing.append(symbol)

        if (
            quote_symbols_found.intersection(candidates)
            or quote_ids_found.intersection(f"{candidate}:latest" for candidate in candidates)
        ):
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
        "coverage_status": "ok",
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
            freshness = db["data_freshness"].find_one(
                {"collection": collection},
                {"_id": 0, "count": 1, "freshness": 1, "stale_reason": 1, "latest_dt": 1, "updated_at": 1},
                sort=[("updated_at", -1)],
            ) or {}
            items.append({
                "collection": collection,
                "count": int(freshness.get("count") or 0),
                "latest_dt": str(freshness.get("latest_dt") or ""),
                "updated_at": str(freshness.get("updated_at", "")),
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
