# -*- coding: utf-8 -*-
"""Repair A-share daily price mismatches against Tencent website qfq history."""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId
from pymongo import DeleteMany, InsertOne, MongoClient, ReplaceOne

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_market_data_against_online import _num, _row_by_date, _website_tencent_daily


MONGO_URL = "mongodb://127.0.0.1:27017/signals"
DB_NAME = "signals"
DAILY_FREQ = "日线"
BACKUP_PREFIX = "bars_a_daily_tencent_price_backup"
PRICE_FIELDS = ("open", "high", "low", "close")


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, ObjectId):
        return str(value)
    return str(value)


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    return None


def _date_from_cli(value: str) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _parse_symbols(raw: str) -> list[str]:
    symbols: list[str] = []
    for item in str(raw or "").replace(";", ",").split(","):
        value = item.strip().upper()
        if value.startswith(("SH.", "SZ.", "BJ.")):
            value = value.split(".", 1)[1]
        if value.isdigit() and len(value) == 6:
            symbols.append(value)
    return sorted(set(symbols))


def _parse_sources(raw: str) -> list[str]:
    result: list[str] = []
    for item in str(raw or "").replace(";", ",").split(","):
        value = item.strip()
        if value and value not in result:
            result.append(value)
    return result


def _rel_diff(local: float, online: float) -> float:
    if online == 0:
        return math.inf if local else 0.0
    return abs(local - online) / abs(online)


def _price_mismatches(doc: dict[str, Any], online: dict[str, float], args: argparse.Namespace) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for field in PRICE_FIELDS:
        local_value = _num(doc.get(field))
        online_value = online.get(field)
        if local_value is None or online_value is None or online_value <= 0:
            continue
        diff = round(local_value - online_value, 6)
        rel = _rel_diff(local_value, online_value)
        if abs(diff) > args.price_abs_tol and rel > args.price_rel_tol:
            mismatches.append(
                {
                    "field": field,
                    "local": local_value,
                    "online": online_value,
                    "diff": diff,
                    "rel_diff": round(rel, 6),
                }
            )
    return mismatches


def _volume_matches(local_vol: float | None, online_vol: float | None, args: argparse.Namespace) -> bool:
    if local_vol is None or online_vol is None:
        return False
    diff = abs(local_vol - online_vol)
    if diff <= args.volume_abs_tol:
        return True
    if online_vol == 0:
        return local_vol == 0
    return diff / abs(online_vol) <= args.volume_rel_tol


def _online_row_values(row) -> dict[str, float | None]:
    return {
        "open": _num(row.get("开盘")),
        "high": _num(row.get("最高")),
        "low": _num(row.get("最低")),
        "close": _num(row.get("收盘")),
        "vol": _num(row.get("成交量")),
    }


def _corrected_doc(original: dict[str, Any], online: dict[str, float | None]) -> dict[str, Any]:
    corrected = {key: value for key, value in original.items() if key != "_id"}
    meta = dict(corrected.get("meta") or {})
    meta["source"] = "website_tencent_a_price_repair"
    meta["source_price"] = "website_tencent_a"
    corrected["meta"] = meta
    corrected["source"] = "website_tencent_a_price_repair"
    for field in PRICE_FIELDS:
        corrected[field] = online[field]
    if online.get("vol") is not None:
        corrected["vol"] = int(online["vol"] or 0)
        meta["source_vol"] = float(online["vol"] or 0)
    return corrected


def _base_match(args: argparse.Namespace) -> dict[str, Any]:
    match: dict[str, Any] = {"meta.market": "A", "meta.freq": DAILY_FREQ}
    symbols = _parse_symbols(args.symbols)
    if symbols:
        match["meta.symbol"] = {"$in": symbols}
    sources = _parse_sources(args.sources)
    if sources:
        match["source"] = {"$in": sources}
    dt_filter: dict[str, Any] = {}
    date_from = _date_from_cli(args.date_from)
    date_to = _date_from_cli(args.date_to)
    if date_from:
        dt_filter["$gte"] = date_from
    if date_to:
        dt_filter["$lte"] = date_to
    if dt_filter:
        match["dt"] = dt_filter
    return match


def _symbol_rows(db, args: argparse.Namespace) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": _base_match(args)},
        {"$group": {"_id": "$meta.symbol", "count": {"$sum": 1}, "min_dt": {"$min": "$dt"}, "max_dt": {"$max": "$dt"}}},
        {"$sort": {"_id": 1}},
    ]
    if args.symbol_offset > 0:
        pipeline.append({"$skip": args.symbol_offset})
    if args.max_symbols > 0:
        pipeline.append({"$limit": args.max_symbols})
    return list(db["bars"].aggregate(pipeline, allowDiskUse=True))


def _docs_for_symbol(db, symbol: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    match = _base_match(args)
    match["meta.symbol"] = symbol
    return list(
        db["bars"]
        .find(
            match,
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


def _fetch_daily_with_retries(symbol: str, args: argparse.Namespace):
    errors = 0
    last_exc: Exception | None = None
    attempts = max(1, args.provider_retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            df, source_url = _website_tencent_daily(symbol, count=args.tencent_count, timeout=args.provider_timeout)
            return df, source_url, errors
        except Exception as exc:
            last_exc = exc
            errors += 1
            if attempt < attempts:
                time.sleep(args.retry_interval * attempt)
    assert last_exc is not None
    raise last_exc


def _flush(*, bars, backup, backup_ops: list[ReplaceOne], delete_dates: dict[tuple[Any, Any, Any], set[Any]], insert_ops: list[InsertOne]) -> tuple[int, int, int]:
    if not backup_ops:
        return 0, 0, 0
    backup_result = backup.bulk_write(backup_ops, ordered=False)
    bar_ops: list[DeleteMany | InsertOne] = []
    for (market, symbol, freq), dates in delete_dates.items():
        bar_ops.append(DeleteMany({"meta.market": market, "meta.symbol": symbol, "meta.freq": freq, "dt": {"$in": sorted(dates)}}))
    bar_ops.extend(insert_ops)
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

    symbol_rows = _symbol_rows(db, args)
    started_at = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "mode": "a_daily_price_mismatch_from_tencent",
        "apply": args.apply,
        "selected_symbols": len(symbol_rows),
        "scanned_docs": 0,
        "online_matched_docs": 0,
        "missing_online": 0,
        "price_mismatch_docs": 0,
        "volume_mismatch_skipped": 0,
        "planned": 0,
        "provider_errors": 0,
        "provider_retry_errors": 0,
        "backed_up": 0,
        "deleted": 0,
        "inserted": 0,
        "by_symbol": {},
        "by_source": {},
        "by_year": {},
        "samples": [],
        "errors": [],
        "backup_collection": backup_collection if args.apply else None,
        "started_at": started_at,
    }
    print(json.dumps({"event": "a_daily_price_repair_start", "apply": args.apply, "selected_symbols": len(symbol_rows)}, ensure_ascii=False), flush=True)

    source_counts: Counter[str] = Counter()
    year_counts: Counter[str] = Counter()
    symbol_counts: Counter[str] = Counter()
    backup_ops: list[ReplaceOne] = []
    delete_dates: dict[tuple[Any, Any, Any], set[Any]] = {}
    insert_ops: list[InsertOne] = []

    for idx, item in enumerate(symbol_rows, start=1):
        symbol = str(item.get("_id") or "")
        docs = _docs_for_symbol(db, symbol, args)
        try:
            df, source_url, retry_errors = _fetch_daily_with_retries(symbol, args)
            summary["provider_retry_errors"] += retry_errors
        except Exception as exc:
            summary["provider_errors"] += 1
            if len(summary["errors"]) < 30:
                summary["errors"].append({"symbol": symbol, "error": f"{type(exc).__name__}: {str(exc)[:220]}"})
            continue
        for doc in docs:
            target_dt = _dt(doc.get("dt"))
            if not target_dt:
                continue
            summary["scanned_docs"] += 1
            row = _row_by_date(df, "时间", target_dt)
            if row is None:
                summary["missing_online"] += 1
                continue
            online = _online_row_values(row)
            if any(online.get(field) is None or online.get(field) <= 0 for field in PRICE_FIELDS):
                summary["missing_online"] += 1
                continue
            summary["online_matched_docs"] += 1
            mismatches = _price_mismatches(doc, online, args)
            if not mismatches:
                continue
            summary["price_mismatch_docs"] += 1
            if args.require_volume_match and not _volume_matches(_num(doc.get("vol")), _num(online.get("vol")), args):
                summary["volume_mismatch_skipped"] += 1
                continue

            corrected = _corrected_doc(doc, online)
            meta = doc.get("meta") or {}
            key = {"market": meta.get("market"), "symbol": meta.get("symbol"), "freq": meta.get("freq"), "dt": doc.get("dt")}
            delta = {
                "old": {field: doc.get(field) for field in (*PRICE_FIELDS, "vol")},
                "new": {field: corrected.get(field) for field in (*PRICE_FIELDS, "vol")},
                "mismatches": mismatches,
            }
            summary["planned"] += 1
            source_counts[str(doc.get("source") or meta.get("source") or "unknown")] += 1
            year_counts[str(target_dt.year)] += 1
            symbol_counts[symbol] += 1
            if len(summary["samples"]) < args.sample_limit:
                summary["samples"].append({"key": key, "delta": delta, "source_url": source_url})

            if args.apply:
                backup_doc = dict(doc)
                backup_doc["repair"] = {
                    "type": "a_daily_price_mismatch_from_tencent",
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
                    backed, deleted, inserted = _flush(bars=bars, backup=backup, backup_ops=backup_ops, delete_dates=delete_dates, insert_ops=insert_ops)
                    summary["backed_up"] += backed
                    summary["deleted"] += deleted
                    summary["inserted"] += inserted
                    backup_ops.clear()
                    delete_dates.clear()
                    insert_ops.clear()
            if args.max_docs > 0 and summary["planned"] >= args.max_docs:
                break
        if args.apply and backup_ops:
            backed, deleted, inserted = _flush(bars=bars, backup=backup, backup_ops=backup_ops, delete_dates=delete_dates, insert_ops=insert_ops)
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
                        "event": "a_daily_price_repair_progress",
                        "symbols": idx,
                        "scanned_docs": summary["scanned_docs"],
                        "online_matched_docs": summary["online_matched_docs"],
                        "price_mismatch_docs": summary["price_mismatch_docs"],
                        "planned": summary["planned"],
                        "missing_online": summary["missing_online"],
                        "provider_errors": summary["provider_errors"],
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

    summary["by_source"] = dict(source_counts.most_common(30))
    summary["by_year"] = dict(sorted(year_counts.items()))
    summary["by_symbol"] = dict(symbol_counts.most_common(30))
    summary["finished_at"] = datetime.now(timezone.utc)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    print(json.dumps({"event": "a_daily_price_repair_done", **summary}, ensure_ascii=False, default=_json_default), flush=True)
    client.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo-url", default=MONGO_URL)
    parser.add_argument("--db-name", default=DB_NAME)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-collection", default="")
    parser.add_argument("--symbols", default="", help="Comma list such as 600000,SH.600000.")
    parser.add_argument("--sources", default="sina", help="Optional source filter, default limits to historical Sina fallback docs.")
    parser.add_argument("--date-from", default="", help="Inclusive lower dt bound, e.g. 2021-01-01.")
    parser.add_argument("--date-to", default="", help="Inclusive upper dt bound, e.g. 2026-05-29.")
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--symbol-offset", type=int, default=0)
    parser.add_argument("--max-docs", type=int, default=0)
    parser.add_argument("--tencent-count", type=int, default=1400)
    parser.add_argument("--provider-timeout", type=float, default=10.0)
    parser.add_argument("--provider-retries", type=int, default=2)
    parser.add_argument("--retry-interval", type=float, default=0.8)
    parser.add_argument("--price-abs-tol", type=float, default=0.005)
    parser.add_argument("--price-rel-tol", type=float, default=0.0005)
    parser.add_argument("--volume-abs-tol", type=float, default=100.0)
    parser.add_argument("--volume-rel-tol", type=float, default=0.02)
    parser.add_argument("--require-volume-match", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--write-batch-size", type=int, default=500)
    parser.add_argument("--call-interval", type=float, default=0.02)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--sample-limit", type=int, default=30)
    parser.add_argument("--output", default="/tmp/signals_a_daily_price_mismatch_from_tencent.json")
    return parser


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
