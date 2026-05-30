# -*- coding: utf-8 -*-
"""Drop HK daily zero-volume placeholder bars that are absent from Tencent history."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId
from pymongo import DeleteMany, MongoClient, ReplaceOne

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_market_data_against_online import _row_by_date, _website_tencent_hk_daily


MONGO_URL = "mongodb://127.0.0.1:27017/signals"
DB_NAME = "signals"
BACKUP_PREFIX = "bars_hk_daily_missing_online_placeholder_backup"


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, ObjectId):
        return str(value)
    return str(value)


def _placeholder_query() -> dict[str, Any]:
    return {
        "meta.market": "HK",
        "meta.freq": "日线",
        "source": "akshare_stock_hk_daily",
        "vol": 0,
        "open": 0.0,
        "high": 0.0,
        "low": 0.0,
        "$or": [
            {"close": 0.0},
            {
                "$expr": {
                    "$or": [
                        {"$gt": ["$low", "$high"]},
                        {"$gt": ["$open", "$high"]},
                        {"$lt": ["$open", "$low"]},
                        {"$gt": ["$close", "$high"]},
                        {"$lt": ["$close", "$low"]},
                    ]
                }
            },
        ],
    }


def _docs_by_symbol(db) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {}
    cursor = db["bars"].find(
        _placeholder_query(),
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
        },
    ).sort([("meta.symbol", 1), ("dt", 1)])
    for doc in cursor:
        symbol = str((doc.get("meta") or {}).get("symbol") or "")
        rows.setdefault(symbol, []).append(doc)
    return rows


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

    grouped = _docs_by_symbol(db)
    started_at = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "mode": "hk_daily_drop_missing_online_placeholders",
        "apply": args.apply,
        "selected_symbols": len(grouped),
        "selected_docs": sum(len(docs) for docs in grouped.values()),
        "planned_delete": 0,
        "online_present": 0,
        "provider_errors": 0,
        "provider_retry_errors": 0,
        "backed_up": 0,
        "deleted": 0,
        "samples": [],
        "errors": [],
        "backup_collection": backup_collection if args.apply else None,
        "started_at": started_at,
    }
    print(
        json.dumps(
            {
                "event": "hk_missing_placeholder_start",
                "apply": args.apply,
                "selected_symbols": summary["selected_symbols"],
                "selected_docs": summary["selected_docs"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    backup_ops: list[ReplaceOne] = []
    delete_dates: dict[tuple[Any, Any, Any], set[Any]] = {}
    for symbol, docs in grouped.items():
        code = symbol.split(".", 1)[-1]
        try:
            df, source_url, retry_errors = _fetch_hk_daily_with_retries(code, args)
            summary["provider_retry_errors"] += retry_errors
        except Exception as exc:
            summary["provider_errors"] += 1
            if len(summary["errors"]) < 20:
                summary["errors"].append({"symbol": symbol, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            continue
        for doc in docs:
            if _row_by_date(df, "时间", doc.get("dt")) is not None:
                summary["online_present"] += 1
                continue
            meta = doc.get("meta") or {}
            key = {"market": meta.get("market"), "symbol": meta.get("symbol"), "freq": meta.get("freq"), "dt": doc.get("dt")}
            summary["planned_delete"] += 1
            if len(summary["samples"]) < 20:
                summary["samples"].append(
                    {
                        "key": key,
                        "local": {field: doc.get(field) for field in ("open", "high", "low", "close", "vol")},
                        "source_url": source_url,
                    }
                )
            if args.apply:
                backup_doc = dict(doc)
                backup_doc["repair"] = {
                    "type": "hk_daily_drop_missing_online_placeholder",
                    "source_collection": "bars",
                    "backup_collection": backup_collection,
                    "original_id": doc["_id"],
                    "key": key,
                    "reason": "zero-volume placeholder absent from Tencent HK daily history",
                    "source_url": source_url,
                    "repaired_at": started_at,
                }
                backup_ops.append(ReplaceOne({"_id": doc["_id"]}, backup_doc, upsert=True))
                delete_dates.setdefault((key["market"], key["symbol"], key["freq"]), set()).add(key["dt"])

    if args.apply and backup_ops:
        backup_result = backup.bulk_write(backup_ops, ordered=False)
        delete_ops = [
            DeleteMany({"meta.market": market, "meta.symbol": symbol, "meta.freq": freq, "dt": {"$in": sorted(dates)}})
            for (market, symbol, freq), dates in delete_dates.items()
        ]
        delete_result = bars.bulk_write(delete_ops, ordered=True)
        summary["backed_up"] = int(backup_result.upserted_count + backup_result.modified_count + backup_result.matched_count)
        summary["deleted"] = int(delete_result.deleted_count)

    summary["finished_at"] = datetime.now(timezone.utc)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    print(json.dumps({"event": "hk_missing_placeholder_done", **summary}, ensure_ascii=False, default=_json_default), flush=True)
    client.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo-url", default=MONGO_URL)
    parser.add_argument("--db-name", default=DB_NAME)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-collection", default="")
    parser.add_argument("--tencent-count", type=int, default=2000)
    parser.add_argument("--provider-timeout", type=float, default=10.0)
    parser.add_argument("--provider-retries", type=int, default=2)
    parser.add_argument("--retry-interval", type=float, default=0.8)
    parser.add_argument("--output", default="/tmp/signals_hk_daily_missing_online_placeholders.json")
    return parser


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
