# -*- coding: utf-8 -*-
"""One-shot repair for historical A-share volume-unit drift in Mongo."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.sync.volume_units import CANONICAL_STOCK_VOLUME_UNIT, normalize_stock_volume
from .weekly_rollup import sync_weekly_rollup

logger = logging.getLogger("signals.sync.volume_unit_repair")

_HAND_DAILY_SOURCES = {"eastmoney", "tencent", "eastmoney_spot_clist_batch"}
_BATCH_SIZE = 1000


def _is_stock_symbol(symbol: Any, code: Any = "") -> bool:
    text = str(symbol or "").upper()
    raw_code = str(code or "")
    code_text = raw_code or (text.split(".", 1)[-1] if "." in text else text[2:] if text[:2] in {"SH", "SZ", "BJ"} else text)
    if text.startswith("SH.000") or text.startswith("SZ.399") or text.startswith("SH000") or text.startswith("SZ399"):
        return False
    return len(code_text) == 6 and code_text.isdigit()


def _bulk_apply(collection, ops: list[UpdateOne]) -> int:
    if not ops:
        return 0
    result = collection.bulk_write(ops, ordered=False)
    return int(getattr(result, "modified_count", 0) or 0)


def repair_fullmarket_spot_volume_units(db: Database) -> dict[str, int]:
    ops: list[UpdateOne] = []
    scanned = 0
    modified = 0
    for row in db["fullmarket_spot_snapshots"].find(
        {
            "volume_unit": {"$ne": CANONICAL_STOCK_VOLUME_UNIT},
            "vol": {"$gt": 0},
        },
        {"_id": 1, "symbol": 1, "code": 1, "vol": 1, "source_vol": 1, "source_volume_unit": 1},
    ):
        if not _is_stock_symbol(row.get("symbol"), row.get("code")):
            continue
        scanned += 1
        source_vol = row.get("source_vol", row.get("vol"))
        vol, source_unit = normalize_stock_volume(
            row.get("vol"),
            source="eastmoney_push2delay_clist",
            source_unit=row.get("source_volume_unit") or "hands",
            default_source_unit="hands",
        )
        ops.append(UpdateOne(
            {"_id": row["_id"]},
            {"$set": {
                "vol": vol,
                "volume_unit": CANONICAL_STOCK_VOLUME_UNIT,
                "source_vol": source_vol,
                "source_volume_unit": source_unit,
                "volume_unit_repaired_at": naive_market_now("A"),
            }},
        ))
        if len(ops) >= _BATCH_SIZE:
            modified += _bulk_apply(db["fullmarket_spot_snapshots"], ops)
            ops.clear()
    modified += _bulk_apply(db["fullmarket_spot_snapshots"], ops)
    return {"scanned": scanned, "modified": modified}


def repair_daily_bar_volume_units(db: Database) -> dict[str, int]:
    scanned = 0
    corrected: list[dict[str, Any]] = []
    query = {
        "meta.freq": "日线",
        "meta.source": {"$in": sorted(_HAND_DAILY_SOURCES)},
        "meta.volume_unit": {"$ne": CANONICAL_STOCK_VOLUME_UNIT},
        "meta.asset_type": {"$ne": "index"},
    }
    cursor = db["bars"].find(query, {"_id": 0})
    for row in cursor:
        meta = row.get("meta") or {}
        if not _is_stock_symbol(meta.get("symbol")):
            continue
        scanned += 1
        source = meta.get("source")
        source_vol = meta.get("source_vol", row.get("vol"))
        vol, source_unit = normalize_stock_volume(row.get("vol"), source=source, default_source_unit="hands")
        next_meta = {
            **meta,
            "volume_unit": CANONICAL_STOCK_VOLUME_UNIT,
            "source_volume_unit": source_unit,
            "source_vol": source_vol,
            "volume_unit_repaired_at": naive_market_now("A"),
        }
        corrected.append({**row, "vol": vol, "meta": next_meta})
    if not corrected:
        return {"scanned": scanned, "deleted": 0, "inserted": 0}
    # Mongo time-series measurements cannot be updated by _id. Delete by the
    # meta field predicate, then insert corrected measurements back.
    deleted = int(db["bars"].delete_many(query).deleted_count)
    inserted = 0
    for idx in range(0, len(corrected), _BATCH_SIZE):
        result = db["bars"].insert_many(corrected[idx:idx + _BATCH_SIZE], ordered=False)
        inserted += len(getattr(result, "inserted_ids", []) or [])
    return {"scanned": scanned, "deleted": deleted, "inserted": inserted}


def _volume_ratio(row: dict[str, Any]) -> float | None:
    try:
        amount = float(row.get("amount") or 0)
        price = float(row.get("close") or row.get("price") or 0)
        vol = float(row.get("vol") or 0)
    except (TypeError, ValueError):
        return None
    if amount <= 0 or price <= 0 or vol <= 0:
        return None
    return amount / price / vol


def repair_quote_snapshot_volume_units(db: Database) -> dict[str, int]:
    ops: list[UpdateOne] = []
    scanned = 0
    modified = 0
    cursor = db["quote_snapshots"].find(
        {
            "volume_unit": {"$ne": CANONICAL_STOCK_VOLUME_UNIT},
            "vol": {"$gt": 0},
        },
        {"_id": 1, "symbol": 1, "code": 1, "source": 1, "vol": 1, "amount": 1, "close": 1, "price": 1},
    )
    for row in cursor:
        if not _is_stock_symbol(row.get("symbol"), row.get("code")):
            continue
        scanned += 1
        ratio = _volume_ratio(row)
        source_unit = "shares"
        vol = int(float(row.get("vol") or 0))
        if ratio is not None and 50 <= ratio <= 150:
            vol, source_unit = normalize_stock_volume(row.get("vol"), source_unit="hands")
        ops.append(UpdateOne(
            {"_id": row["_id"]},
            {"$set": {
                "vol": vol,
                "volume_unit": CANONICAL_STOCK_VOLUME_UNIT,
                "source_vol": row.get("vol"),
                "source_volume_unit": source_unit,
                "volume_unit_repaired_at": naive_market_now("A"),
            }},
        ))
        if len(ops) >= _BATCH_SIZE:
            modified += _bulk_apply(db["quote_snapshots"], ops)
            ops.clear()
    modified += _bulk_apply(db["quote_snapshots"], ops)
    return {"scanned": scanned, "modified": modified}


def sync_volume_unit_repair(db: Database, proxy_url: str = None) -> dict[str, Any]:
    del proxy_url
    now = naive_market_now("A")
    fullmarket = repair_fullmarket_spot_volume_units(db)
    daily = repair_daily_bar_volume_units(db)
    quotes = repair_quote_snapshot_volume_units(db)
    weekly = sync_weekly_rollup(db)
    result = {
        "status": "ok",
        "fullmarket_spot_snapshots": fullmarket,
        "daily_bars": daily,
        "quote_snapshots": quotes,
        "weekly_rollup": weekly,
        "updated_at": now,
    }
    db["sync_log"].update_one(
        {"_id": "volume_unit_repair:_meta"},
        {"$set": {
            "module": "volume_unit_repair",
            "status": "ok",
            "last_run": now,
            "updated_at": now,
            "result": result,
        }},
        upsert=True,
    )
    logger.info("volume unit repair completed: %s", result)
    return result


if __name__ == "__main__":
    from signals.sync.db import close, get_db

    database = get_db()
    try:
        print(sync_volume_unit_repair(database))
    finally:
        close()
