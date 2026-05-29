# -*- coding: utf-8 -*-
"""Repair HK daily OHLC bounds in the Mongo bars time-series collection.

Some HK daily providers return open/close outside the reported high/low range.
For those rows the stable, provider-consistent repair is to keep open/close and
expand high/low to contain all OHLC prices. The script backs up every original
row before replacing it in the time-series collection.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId
from pymongo import DeleteMany, InsertOne, MongoClient, ReplaceOne


MONGO_URL = "mongodb://127.0.0.1:27017/signals"
DB_NAME = "signals"
BACKUP_PREFIX = "bars_hk_daily_ohlc_backup"


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, ObjectId):
        return str(value)
    return str(value)


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return None
        return result
    except Exception:
        return None


def _bad_ohlc_query() -> dict[str, Any]:
    return {
        "meta.market": "HK",
        "meta.freq": "日线",
        "$expr": {
            "$or": [
                {"$gt": ["$low", "$high"]},
                {"$gt": ["$open", "$high"]},
                {"$lt": ["$open", "$low"]},
                {"$gt": ["$close", "$high"]},
                {"$lt": ["$close", "$low"]},
            ]
        },
    }


def _correct_doc(doc: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, float] | None]:
    values = {key: _num(doc.get(key)) for key in ("open", "high", "low", "close")}
    if any(value is None or value <= 0 for value in values.values()):
        return None, None
    corrected_high = max(values.values())
    corrected_low = min(values.values())
    if corrected_high == values["high"] and corrected_low == values["low"]:
        return None, None
    corrected = {key: value for key, value in doc.items() if key != "_id"}
    corrected["high"] = corrected_high
    corrected["low"] = corrected_low
    return corrected, {"old_high": values["high"], "old_low": values["low"], "new_high": corrected_high, "new_low": corrected_low}


def _flush(
    *,
    bars,
    backup,
    backup_ops: list[ReplaceOne],
    bar_ops: list[DeleteMany | InsertOne],
) -> tuple[int, int, int]:
    if not backup_ops:
        return 0, 0, 0
    backup_result = backup.bulk_write(backup_ops, ordered=False)
    bar_result = bars.bulk_write(bar_ops, ordered=True)
    backed = backup_result.upserted_count + backup_result.modified_count + backup_result.matched_count
    return int(backed), int(bar_result.deleted_count), int(bar_result.inserted_count)


def run(args: argparse.Namespace) -> dict[str, Any]:
    client = MongoClient(args.mongo_url, serverSelectionTimeoutMS=3000)
    db = client[args.db_name]
    bars = db["bars"]
    backup_collection = args.backup_collection or f"{BACKUP_PREFIX}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    backup = db[backup_collection]
    if args.apply:
        backup.create_index([("repair.repaired_at", 1)])
        backup.create_index([("repair.key.symbol", 1), ("repair.key.dt", 1)])

    projection = {
        "_id": 1,
        "dt": 1,
        "meta": 1,
        "source": 1,
        "open": 1,
        "high": 1,
        "low": 1,
        "close": 1,
        "vol": 1,
        "volume": 1,
        "amount": 1,
        "change_pct": 1,
        "pct_chg": 1,
        "prev_close": 1,
    }
    cursor = (
        bars.find(_bad_ohlc_query(), projection=projection)
        .hint("meta.freq_1_dt_-1_meta.symbol_1")
        .sort([("meta.freq", 1), ("dt", -1), ("meta.symbol", 1)])
        .batch_size(args.cursor_batch_size)
    )
    started_at = datetime.now(timezone.utc)
    scanned = 0
    planned = 0
    skipped_uncorrectable = 0
    backed_up = 0
    deleted = 0
    inserted = 0
    samples: list[dict[str, Any]] = []
    backup_ops: list[ReplaceOne] = []
    bar_ops: list[DeleteMany | InsertOne] = []

    print(json.dumps({"event": "ohlc_repair_start", "apply": args.apply, "backup_collection": backup_collection if args.apply else None}, ensure_ascii=False), flush=True)
    for doc in cursor:
        scanned += 1
        corrected, delta = _correct_doc(doc)
        if corrected is None or delta is None:
            skipped_uncorrectable += 1
            continue
        meta = doc.get("meta") or {}
        key = {"market": meta.get("market"), "symbol": meta.get("symbol"), "freq": meta.get("freq"), "dt": doc.get("dt")}
        planned += 1
        if len(samples) < 20:
            samples.append({"key": key, "delta": delta, "source": doc.get("source") or meta.get("source")})
        if args.max_docs > 0 and planned > args.max_docs:
            planned -= 1
            break
        if args.apply:
            backup_doc = dict(doc)
            backup_doc["repair"] = {
                "type": "hk_daily_ohlc_bounds",
                "source_collection": "bars",
                "backup_collection": backup_collection,
                "original_id": doc["_id"],
                "key": key,
                "delta": delta,
                "repaired_at": started_at,
            }
            backup_ops.append(ReplaceOne({"_id": doc["_id"]}, backup_doc, upsert=True))
            bar_ops.extend(
                [
                    DeleteMany(
                        {
                            "meta.market": key["market"],
                            "meta.symbol": key["symbol"],
                            "meta.freq": key["freq"],
                            "dt": key["dt"],
                        }
                    ),
                    InsertOne(corrected),
                ]
            )
            if len(bar_ops) >= args.write_batch_size:
                backed, removed, added = _flush(bars=bars, backup=backup, backup_ops=backup_ops, bar_ops=bar_ops)
                backed_up += backed
                deleted += removed
                inserted += added
                backup_ops.clear()
                bar_ops.clear()
        if args.progress_every and scanned % args.progress_every == 0:
            print(
                json.dumps(
                    {
                        "event": "ohlc_repair_progress",
                        "scanned": scanned,
                        "planned": planned,
                        "backed_up": backed_up,
                        "deleted": deleted,
                        "inserted": inserted,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    if args.apply and backup_ops:
        backed, removed, added = _flush(bars=bars, backup=backup, backup_ops=backup_ops, bar_ops=bar_ops)
        backed_up += backed
        deleted += removed
        inserted += added

    summary = {
        "mode": "hk_daily_ohlc_bounds",
        "apply": args.apply,
        "scanned": scanned,
        "planned": planned,
        "skipped_uncorrectable": skipped_uncorrectable,
        "backed_up": backed_up,
        "deleted": deleted,
        "inserted": inserted,
        "backup_collection": backup_collection if args.apply else None,
        "samples": samples,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    print(json.dumps({"event": "ohlc_repair_done", **summary}, ensure_ascii=False, default=_json_default), flush=True)
    client.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo-url", default=MONGO_URL)
    parser.add_argument("--db-name", default=DB_NAME)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-collection", default="")
    parser.add_argument("--max-docs", type=int, default=0, help="Maximum rows to repair. 0 means unlimited.")
    parser.add_argument("--cursor-batch-size", type=int, default=20000)
    parser.add_argument("--write-batch-size", type=int, default=500)
    parser.add_argument("--progress-every", type=int, default=10000)
    parser.add_argument("--output", default="/tmp/signals_hk_daily_ohlc_bounds_repair.json")
    return parser


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
