# -*- coding: utf-8 -*-
"""Audit and repair historical Mongo bar anomalies.

The first supported repair is deterministic de-duplication of daily bars:
for identical (market, symbol, freq, dt) keys, keep the best-quality row and
optionally back up then delete the extra rows.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId
from pymongo import DeleteOne, MongoClient, ReplaceOne


MONGO_URL = "mongodb://127.0.0.1:27017/signals"
DB_NAME = "signals"
DEFAULT_FREQ = "日线"
DEFAULT_BACKUP_PREFIX = "bars_duplicate_backup"

SOURCE_PRIORITY = {
    "eastmoney_spot_clist_batch_new_listing_repair": 100,
    "eastmoney_spot_clist_batch_gap_repair": 95,
    "eastmoney_spot_clist_batch": 90,
    "eastmoney_push2delay_clist": 88,
    "eastmoney": 85,
    "tencent": 80,
    "sina": 75,
    "akshare_stock_hk_hist": 70,
    "akshare_stock_hk_daily": 60,
    "daily_rollup": 40,
}


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


def _valid_ohlc(doc: dict[str, Any]) -> int:
    values = [_num(doc.get(key)) for key in ("open", "high", "low", "close")]
    if any(value is None for value in values):
        return 0
    open_, high, low, close = values
    if min(values) <= 0:
        return 0
    return int(high >= max(open_, low, close) and low <= min(open_, high, close))


def _field_score(doc: dict[str, Any]) -> int:
    return sum(
        1
        for key in (
            "open",
            "high",
            "low",
            "close",
            "vol",
            "volume",
            "amount",
            "change_pct",
            "pct_chg",
            "prev_close",
        )
        if doc.get(key) is not None
    )


def _object_id_time(doc: dict[str, Any]) -> float:
    oid = doc.get("_id")
    if isinstance(oid, ObjectId):
        return oid.generation_time.timestamp()
    return 0.0


def _source(doc: dict[str, Any]) -> str:
    meta = doc.get("meta") or {}
    return str(doc.get("source") or meta.get("source") or "")


def _score(doc: dict[str, Any]) -> tuple[int, int, int, float]:
    return (
        _valid_ohlc(doc),
        _field_score(doc),
        SOURCE_PRIORITY.get(_source(doc), 0),
        _object_id_time(doc),
    )


def _market(doc: dict[str, Any]) -> str:
    meta = doc.get("meta") or {}
    return str(meta.get("market") or "<missing>")


def _symbol(doc: dict[str, Any]) -> str:
    meta = doc.get("meta") or {}
    return str(meta.get("symbol") or "")


def _freq(doc: dict[str, Any]) -> str:
    meta = doc.get("meta") or {}
    return str(meta.get("freq") or "")


def _key(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": _market(doc),
        "symbol": _symbol(doc),
        "freq": _freq(doc),
        "dt": doc.get("dt"),
    }


def _base(doc: dict[str, Any]) -> tuple[str, str, Any]:
    return (_symbol(doc), _freq(doc), doc.get("dt"))


def _same_values(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = ("open", "high", "low", "close", "vol", "volume", "amount")
    return all(left.get(key) == right.get(key) for key in keys)


def _flush_repairs(bars, backup, backup_ops: list[ReplaceOne], delete_ops: list[DeleteOne]) -> tuple[int, int]:
    if not backup_ops:
        return 0, 0
    backup_result = backup.bulk_write(backup_ops, ordered=False)
    delete_result = bars.bulk_write(delete_ops, ordered=False)
    backed = backup_result.upserted_count + backup_result.modified_count + backup_result.matched_count
    return int(backed), int(delete_result.deleted_count)


def _process_block(
    *,
    block: list[dict[str, Any]],
    apply: bool,
    backup,
    backup_ops: list[ReplaceOne],
    delete_ops: list[DeleteOne],
    backup_collection: str,
    repair_started_at: datetime,
    max_extra_docs: int,
    state: dict[str, Any],
) -> None:
    if not block:
        return
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for doc in block:
        by_market[_market(doc)].append(doc)

    for docs in by_market.values():
        if len(docs) <= 1:
            continue
        keep = max(docs, key=_score)
        delete_docs = [doc for doc in docs if doc["_id"] != keep["_id"]]
        if max_extra_docs > 0:
            remaining = max_extra_docs - int(state["extra_docs"])
            if remaining <= 0:
                return
            delete_docs = delete_docs[:remaining]
        if not delete_docs:
            continue

        key = _key(keep)
        sources = sorted({_source(doc) for doc in docs})
        all_same_values = all(_same_values(keep, doc) for doc in delete_docs)
        state["duplicate_groups"] += 1
        state["extra_docs"] += len(delete_docs)
        state["distribution"][(key["market"], key["freq"], tuple(sources))] += len(delete_docs)
        if all_same_values:
            state["identical_value_extra_docs"] += len(delete_docs)
        if len(state["samples"]) < 20:
            state["samples"].append(
                {
                    "key": key,
                    "kept_id": keep["_id"],
                    "deleted_ids": [doc["_id"] for doc in delete_docs],
                    "sources": sources,
                    "all_same_values": all_same_values,
                    "kept_score": _score(keep),
                }
            )

        if not apply:
            continue

        for doc in delete_docs:
            backup_doc = dict(doc)
            backup_doc["repair"] = {
                "type": "bars_duplicate_dedupe",
                "source_collection": "bars",
                "backup_collection": backup_collection,
                "original_id": doc["_id"],
                "kept_id": keep["_id"],
                "key": key,
                "sources": sources,
                "all_same_values": all_same_values,
                "deleted_at": repair_started_at,
            }
            backup_ops.append(ReplaceOne({"_id": doc["_id"]}, backup_doc, upsert=True))
            delete_ops.append(DeleteOne({"_id": doc["_id"]}))

        if len(delete_ops) >= int(state["batch_size"]):
            backed, deleted = _flush_repairs(backup.database["bars"], backup, backup_ops, delete_ops)
            state["backed_up"] += backed
            state["deleted"] += deleted
            backup_ops.clear()
            delete_ops.clear()


def run_dedupe(args: argparse.Namespace) -> dict[str, Any]:
    client = MongoClient(args.mongo_url, serverSelectionTimeoutMS=3000)
    db = client[args.db_name]
    bars = db["bars"]
    backup_collection = args.backup_collection or f"{DEFAULT_BACKUP_PREFIX}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    backup = db[backup_collection]
    if args.apply:
        backup.create_index([("repair.deleted_at", 1)])
        backup.create_index(
            [
                ("repair.key.market", 1),
                ("repair.key.symbol", 1),
                ("repair.key.freq", 1),
                ("repair.key.dt", 1),
            ]
        )

    query: dict[str, Any] = {"meta.freq": args.freq}
    if args.market:
        query["meta.market"] = args.market
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
        bars.find(query, projection=projection)
        .hint("meta.freq_1_dt_-1_meta.symbol_1")
        .sort([("meta.freq", 1), ("dt", -1), ("meta.symbol", 1)])
        .batch_size(args.cursor_batch_size)
    )

    started_at = datetime.now(timezone.utc)
    state: dict[str, Any] = {
        "duplicate_groups": 0,
        "extra_docs": 0,
        "identical_value_extra_docs": 0,
        "distribution": Counter(),
        "samples": [],
        "batch_size": args.write_batch_size,
        "backed_up": 0,
        "deleted": 0,
    }
    backup_ops: list[ReplaceOne] = []
    delete_ops: list[DeleteOne] = []
    scanned = 0
    blocks = 0
    current_base: tuple[str, str, Any] | None = None
    block: list[dict[str, Any]] = []

    print(
        json.dumps(
            {
                "event": "dedupe_scan_start",
                "apply": args.apply,
                "query": query,
                "backup_collection": backup_collection if args.apply else None,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    for doc in cursor:
        scanned += 1
        base = _base(doc)
        if current_base is None:
            current_base = base
        if base != current_base:
            blocks += 1
            _process_block(
                block=block,
                apply=args.apply,
                backup=backup,
                backup_ops=backup_ops,
                delete_ops=delete_ops,
                backup_collection=backup_collection,
                repair_started_at=started_at,
                max_extra_docs=args.max_extra_docs,
                state=state,
            )
            block = []
            current_base = base
            if args.max_extra_docs > 0 and int(state["extra_docs"]) >= args.max_extra_docs:
                break
        block.append(doc)
        if scanned % args.progress_every == 0:
            print(
                json.dumps(
                    {
                        "event": "dedupe_scan_progress",
                        "scanned": scanned,
                        "blocks": blocks,
                        "duplicate_groups": state["duplicate_groups"],
                        "extra_docs": state["extra_docs"],
                        "deleted": state["deleted"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    if not (args.max_extra_docs > 0 and int(state["extra_docs"]) >= args.max_extra_docs):
        blocks += 1
        _process_block(
            block=block,
            apply=args.apply,
            backup=backup,
            backup_ops=backup_ops,
            delete_ops=delete_ops,
            backup_collection=backup_collection,
            repair_started_at=started_at,
            max_extra_docs=args.max_extra_docs,
            state=state,
        )
    if args.apply:
        backed, deleted = _flush_repairs(bars, backup, backup_ops, delete_ops)
        state["backed_up"] += backed
        state["deleted"] += deleted

    summary = {
        "mode": "dedupe",
        "apply": args.apply,
        "query": query,
        "scanned": scanned,
        "blocks": blocks,
        "duplicate_groups": state["duplicate_groups"],
        "extra_docs": state["extra_docs"],
        "identical_value_extra_docs": state["identical_value_extra_docs"],
        "backed_up": state["backed_up"],
        "deleted": state["deleted"],
        "backup_collection": backup_collection if args.apply else None,
        "distribution": [
            {"market": key[0], "freq": key[1], "sources": list(key[2]), "extra_docs": value}
            for key, value in state["distribution"].most_common(50)
        ],
        "samples": state["samples"],
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc),
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    print(json.dumps({"event": "dedupe_done", **summary}, ensure_ascii=False, default=_json_default), flush=True)
    client.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo-url", default=MONGO_URL)
    parser.add_argument("--db-name", default=DB_NAME)
    parser.add_argument("--apply", action="store_true", help="Back up and delete duplicate rows. Default is dry-run.")
    parser.add_argument("--freq", default=DEFAULT_FREQ)
    parser.add_argument("--market", default="", help="Optional market filter, e.g. HK or A.")
    parser.add_argument("--backup-collection", default="")
    parser.add_argument("--max-extra-docs", type=int, default=0, help="Stop after planning this many extra docs. 0 means unlimited.")
    parser.add_argument("--cursor-batch-size", type=int, default=20000)
    parser.add_argument("--write-batch-size", type=int, default=1000)
    parser.add_argument("--progress-every", type=int, default=500000)
    parser.add_argument("--output", default="/tmp/signals_bars_history_dedupe.json")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    run_dedupe(args)


if __name__ == "__main__":
    main()
