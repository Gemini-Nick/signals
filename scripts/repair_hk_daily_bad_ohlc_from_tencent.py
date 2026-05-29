# -*- coding: utf-8 -*-
"""Repair remaining HK daily bad OHLC rows using Tencent HK website history."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId
from pymongo import DeleteMany, InsertOne, MongoClient, ReplaceOne

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_market_data_against_online import _num, _row_by_date, _website_tencent_hk_daily


MONGO_URL = "mongodb://127.0.0.1:27017/signals"
DB_NAME = "signals"
BACKUP_PREFIX = "bars_hk_daily_tencent_backup"


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, ObjectId):
        return str(value)
    return str(value)


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


def _group_bad_docs(db, *, max_symbols: int) -> list[dict[str, Any]]:
    pipeline = [
        {"$match": _bad_ohlc_query()},
        {"$group": {"_id": "$meta.symbol", "count": {"$sum": 1}, "min_dt": {"$min": "$dt"}, "max_dt": {"$max": "$dt"}}},
        {"$sort": {"count": -1, "_id": 1}},
    ]
    if max_symbols > 0:
        pipeline.append({"$limit": max_symbols})
    return list(db["bars"].aggregate(pipeline, allowDiskUse=True))


def _docs_for_symbol(db, symbol: str) -> list[dict[str, Any]]:
    return list(
        db["bars"]
        .find(
            {**_bad_ohlc_query(), "meta.symbol": symbol},
            {
                "_id": 1,
                "dt": 1,
                "meta": 1,
                "source": 1,
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "vol": 1,
                "amount": 1,
                "change_pct": 1,
                "pct_chg": 1,
                "prev_close": 1,
            },
        )
        .sort("dt", 1)
    )


def _doc_from_online(original: dict[str, Any], row) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    open_ = _num(row.get("开盘"))
    high = _num(row.get("最高"))
    low = _num(row.get("最低"))
    close = _num(row.get("收盘"))
    if any(value is None or value <= 0 for value in (open_, high, low, close)):
        return None, None
    corrected = {key: value for key, value in original.items() if key != "_id"}
    meta = dict(corrected.get("meta") or {})
    meta["source"] = "website_tencent_hk"
    corrected["meta"] = meta
    corrected["source"] = "website_tencent_hk"
    corrected["open"] = open_
    corrected["high"] = high
    corrected["low"] = low
    corrected["close"] = close
    online_vol = _num(row.get("成交量"))
    if online_vol is not None:
        corrected["vol"] = int(online_vol)
        meta["source_vol"] = float(online_vol)
    return corrected, {
        "old": {key: original.get(key) for key in ("open", "high", "low", "close", "vol")},
        "new": {key: corrected.get(key) for key in ("open", "high", "low", "close", "vol")},
    }


def _flush(
    *,
    bars,
    backup,
    backup_ops: list[ReplaceOne],
    delete_dates: dict[tuple[Any, Any, Any], set[Any]],
    insert_ops: list[InsertOne],
) -> tuple[int, int, int]:
    if not backup_ops:
        return 0, 0, 0
    backup_result = backup.bulk_write(backup_ops, ordered=False)
    bar_ops: list[DeleteMany | InsertOne] = []
    for (market, symbol, freq), dates in delete_dates.items():
        bar_ops.append(
            DeleteMany(
                {
                    "meta.market": market,
                    "meta.symbol": symbol,
                    "meta.freq": freq,
                    "dt": {"$in": sorted(dates)},
                }
            )
        )
    bar_ops.extend(insert_ops)
    bar_result = bars.bulk_write(bar_ops, ordered=True)
    backed = backup_result.upserted_count + backup_result.modified_count + backup_result.matched_count
    return int(backed), int(bar_result.deleted_count), int(bar_result.inserted_count)


def _fetch_hk_daily_with_retries(code: str, args: argparse.Namespace):
    errors = 0
    last_exc: Exception | None = None
    attempts = max(1, args.provider_retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            df, source_url = _website_tencent_hk_daily(code, count=args.tencent_count, timeout=args.provider_timeout)
            return df, source_url, errors
        except Exception as exc:
            last_exc = exc
            errors += 1
            if attempt < attempts:
                time.sleep(args.retry_interval * attempt)
    assert last_exc is not None
    raise last_exc


def run(args: argparse.Namespace) -> dict[str, Any]:
    client = MongoClient(args.mongo_url, serverSelectionTimeoutMS=3000)
    db = client[args.db_name]
    bars = db["bars"]
    backup_collection = args.backup_collection or f"{BACKUP_PREFIX}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    backup = db[backup_collection]
    if args.apply:
        backup.create_index([("repair.repaired_at", 1)])
        backup.create_index([("repair.key.symbol", 1), ("repair.key.dt", 1)])

    symbol_rows = _group_bad_docs(db, max_symbols=args.max_symbols)
    started_at = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "mode": "hk_daily_bad_ohlc_from_tencent",
        "apply": args.apply,
        "selected_symbols": len(symbol_rows),
        "planned": 0,
        "matched_online": 0,
        "missing_online": 0,
        "provider_errors": 0,
        "provider_retry_errors": 0,
        "backed_up": 0,
        "deleted": 0,
        "inserted": 0,
        "samples": [],
        "errors": [],
        "backup_collection": backup_collection if args.apply else None,
        "started_at": started_at,
    }
    print(json.dumps({"event": "tencent_hk_repair_start", "apply": args.apply, "selected_symbols": len(symbol_rows)}, ensure_ascii=False), flush=True)
    backup_ops: list[ReplaceOne] = []
    delete_dates: dict[tuple[Any, Any, Any], set[Any]] = {}
    insert_ops: list[InsertOne] = []
    for idx, item in enumerate(symbol_rows, start=1):
        symbol = str(item.get("_id") or "")
        code = symbol.split(".", 1)[-1]
        docs = _docs_for_symbol(db, symbol)
        try:
            df, source_url, retry_errors = _fetch_hk_daily_with_retries(code, args)
            summary["provider_retry_errors"] += retry_errors
        except Exception as exc:
            summary["provider_errors"] += 1
            if len(summary["errors"]) < 20:
                summary["errors"].append({"symbol": symbol, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            continue
        for doc in docs:
            row = _row_by_date(df, "时间", doc.get("dt"))
            if row is None:
                summary["missing_online"] += 1
                continue
            corrected, delta = _doc_from_online(doc, row)
            if corrected is None or delta is None:
                summary["missing_online"] += 1
                continue
            meta = doc.get("meta") or {}
            key = {"market": meta.get("market"), "symbol": meta.get("symbol"), "freq": meta.get("freq"), "dt": doc.get("dt")}
            summary["planned"] += 1
            summary["matched_online"] += 1
            if len(summary["samples"]) < 20:
                summary["samples"].append({"key": key, "delta": delta, "source_url": source_url})
            if args.apply:
                backup_doc = dict(doc)
                backup_doc["repair"] = {
                    "type": "hk_daily_bad_ohlc_from_tencent",
                    "source_collection": "bars",
                    "backup_collection": backup_collection,
                    "original_id": doc["_id"],
                    "key": key,
                    "delta": delta,
                    "source_url": source_url,
                    "repaired_at": started_at,
                }
                backup_ops.append(ReplaceOne({"_id": doc["_id"]}, backup_doc, upsert=True))
                delete_dates.setdefault((key["market"], key["symbol"], key["freq"]), set()).add(key["dt"])
                insert_ops.append(InsertOne(corrected))
                if len(insert_ops) >= args.write_batch_size:
                    backed, deleted, inserted = _flush(
                        bars=bars,
                        backup=backup,
                        backup_ops=backup_ops,
                        delete_dates=delete_dates,
                        insert_ops=insert_ops,
                    )
                    summary["backed_up"] += backed
                    summary["deleted"] += deleted
                    summary["inserted"] += inserted
                    backup_ops.clear()
                    delete_dates.clear()
                    insert_ops.clear()
            if args.max_docs > 0 and summary["planned"] >= args.max_docs:
                break
        if args.apply and backup_ops:
            backed, deleted, inserted = _flush(
                bars=bars,
                backup=backup,
                backup_ops=backup_ops,
                delete_dates=delete_dates,
                insert_ops=insert_ops,
            )
            summary["backed_up"] += backed
            summary["deleted"] += deleted
            summary["inserted"] += inserted
            backup_ops.clear()
            delete_dates.clear()
            insert_ops.clear()
        if args.progress_every and (idx % args.progress_every == 0 or summary["planned"] >= args.max_docs > 0):
            print(
                json.dumps(
                    {
                        "event": "tencent_hk_repair_progress",
                        "symbols": idx,
                        "planned": summary["planned"],
                        "matched_online": summary["matched_online"],
                        "missing_online": summary["missing_online"],
                        "provider_errors": summary["provider_errors"],
                        "provider_retry_errors": summary["provider_retry_errors"],
                        "inserted": summary["inserted"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        if args.call_interval:
            time.sleep(args.call_interval)
        if args.max_docs > 0 and summary["planned"] >= args.max_docs:
            break

    summary["finished_at"] = datetime.now(timezone.utc)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    print(json.dumps({"event": "tencent_hk_repair_done", **summary}, ensure_ascii=False, default=_json_default), flush=True)
    client.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo-url", default=MONGO_URL)
    parser.add_argument("--db-name", default=DB_NAME)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-collection", default="")
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--max-docs", type=int, default=0)
    parser.add_argument("--tencent-count", type=int, default=2000)
    parser.add_argument("--provider-timeout", type=float, default=10.0)
    parser.add_argument("--provider-retries", type=int, default=2)
    parser.add_argument("--retry-interval", type=float, default=0.8)
    parser.add_argument("--write-batch-size", type=int, default=200)
    parser.add_argument("--call-interval", type=float, default=0.05)
    parser.add_argument("--progress-every", type=int, default=50)
    parser.add_argument("--output", default="/tmp/signals_hk_daily_bad_ohlc_from_tencent.json")
    return parser


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
