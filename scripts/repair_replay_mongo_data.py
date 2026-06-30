#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Repair Mongo evidence needed by Signals replay notes for one trade date."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from math import isfinite
from typing import Any

from pymongo import UpdateOne

from signals.sync.db import get_db
from signals.sync.modules.market_limit_pools import sync_market_limit_pools


BACKFILL_SOURCE = "daily_board_ranking_backfill"


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _int(value: Any, default: int = 0) -> int:
    number = _float(value)
    return int(number) if number is not None else default


def _day_range(trade_date: str) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(trade_date)
    return start, start.replace(hour=23, minute=59, second=59, microsecond=999999)


def _ranking_query(trade_date: str) -> dict[str, Any]:
    start, end = _day_range(trade_date)
    return {"dt": {"$gte": start, "$lte": end}, "source": "canonical"}


def _ranking_row_doc(
    row: dict[str, Any],
    *,
    kind: str,
    source_collection: str,
    trade_date: str,
    trade_minute: datetime,
) -> dict[str, Any] | None:
    name = _text(row.get("board_name") or row.get("concept") or row.get("name") or row.get("label"))
    change_pct = _float(row.get("change_pct"))
    if not name or change_pct is None:
        return None
    trade_day = datetime.fromisoformat(trade_date)
    return {
        "kind": kind,
        "name": name,
        "board_name": name,
        "code": _text(row.get("board_code") or row.get("concept_code") or row.get("code")),
        "source": BACKFILL_SOURCE,
        "source_collection": source_collection,
        "source_snapshot": f"{source_collection}:canonical",
        "dt": trade_day,
        "trade_date": trade_date,
        "trade_minute": trade_minute,
        "snapshot_at": trade_minute,
        "rank_idx": _int(row.get("rank_idx"), 9999),
        "price": _float(row.get("avg_price") or row.get("price")),
        "change_pct": change_pct,
        "change_amount": _float(row.get("change_amount")),
        "market_value": _float(row.get("market_value")),
        "amount": _float(row.get("amount")),
        "turnover_pct": _float(row.get("turnover_pct")),
        "up_count": _int(row.get("up_count")),
        "down_count": _int(row.get("down_count")),
        "leader_name": _text(row.get("leader_name") or row.get("leader")),
        "leader_symbol": _text(row.get("leader_symbol") or row.get("leader_code")),
        "leader_change_pct": _float(row.get("leader_change_pct")),
        "is_backfill": True,
        "evidence_level": "eod_backfill",
        "backfill_note": "Derived from daily board/concept ranking at close; not intraday minute evidence.",
    }


def _backfill_board_heat_eod(db: Any, trade_date: str, *, apply: bool) -> dict[str, Any]:
    trade_minute = datetime.fromisoformat(trade_date).replace(hour=14, minute=58, second=0, microsecond=0)
    specs = [
        ("board_ranking", "industry"),
        ("concept_ranking", "concept"),
    ]
    docs: list[dict[str, Any]] = []
    for collection_name, kind in specs:
        if collection_name not in db.list_collection_names():
            continue
        rows = list(db[collection_name].find(_ranking_query(trade_date), {"_id": 0}).sort([("rank_idx", 1), ("change_pct", -1)]))
        for row in rows:
            doc = _ranking_row_doc(
                row,
                kind=kind,
                source_collection=collection_name,
                trade_date=trade_date,
                trade_minute=trade_minute,
            )
            if doc:
                docs.append(doc)
    if not apply:
        return {
            "status": "dry_run",
            "trade_date": trade_date,
            "candidate_docs": len(docs),
            "snapshot_minute": trade_minute.isoformat(timespec="minutes"),
            "source": BACKFILL_SOURCE,
            "sample": docs[:5],
        }
    delete_result = db["board_heat_ticks"].delete_many({"trade_date": trade_date, "source": BACKFILL_SOURCE})
    ops = [
        UpdateOne(
            {
                "kind": doc["kind"],
                "name": doc["name"],
                "source": doc["source"],
                "trade_minute": doc["trade_minute"],
            },
            {"$set": doc},
            upsert=True,
        )
        for doc in docs
    ]
    write_result = db["board_heat_ticks"].bulk_write(ops, ordered=False) if ops else None
    db["data_freshness"].update_one(
        {
            "domain": "board_heat_ticks",
            "market": "A",
            "mode": "backfill",
            "collection": "board_heat_ticks",
            "scope": "daily_eod",
            "trade_date": trade_date,
        },
        {
            "$set": {
                "domain": "board_heat_ticks",
                "market": "A",
                "mode": "backfill",
                "collection": "board_heat_ticks",
                "scope": "daily_eod",
                "trade_date": trade_date,
                "freshness": "partial",
                "latest_dt": trade_minute.isoformat(timespec="minutes"),
                "as_of": trade_date,
                "updated_at": datetime.now(),
                "stale_reason": "eod_backfill_not_intraday_minute",
                "count": len(docs),
                "source": BACKFILL_SOURCE,
            }
        },
        upsert=True,
    )
    return {
        "status": "ok",
        "trade_date": trade_date,
        "deleted_existing": delete_result.deleted_count,
        "matched": write_result.matched_count if write_result else 0,
        "modified": write_result.modified_count if write_result else 0,
        "upserted": len(write_result.upserted_ids) if write_result else 0,
        "count": len(docs),
        "snapshot_minute": trade_minute.isoformat(timespec="minutes"),
        "source": BACKFILL_SOURCE,
    }


def _repair_limit_pools(db: Any, trade_date: str, *, apply: bool, replace: bool) -> dict[str, Any]:
    existing = db["market_limit_pools"].count_documents({"trade_date": trade_date}, maxTimeMS=5000)
    if not apply:
        return {"status": "dry_run", "trade_date": trade_date, "existing": existing, "replace": replace}
    deleted = 0
    if replace:
        deleted = db["market_limit_pools"].delete_many({"trade_date": trade_date}).deleted_count
    result = sync_market_limit_pools(db, trade_date=trade_date)
    return {"status": "ok", "trade_date": trade_date, "deleted_existing": deleted, "sync_result": result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True, help="YYYY-MM-DD trade date to repair.")
    parser.add_argument("--apply", action="store_true", help="write Mongo updates; default is dry-run")
    parser.add_argument("--skip-limit-pools", action="store_true", help="skip AkShare/Eastmoney market_limit_pools repair")
    parser.add_argument("--skip-board-heat-eod", action="store_true", help="skip derived close board_heat_ticks repair")
    parser.add_argument("--no-replace-limit-pools", action="store_true", help="do not delete existing pool rows for this date before refetching")
    args = parser.parse_args()

    datetime.fromisoformat(args.trade_date)
    db = get_db()
    result: dict[str, Any] = {"trade_date": args.trade_date, "apply": args.apply}
    if not args.skip_limit_pools:
        result["market_limit_pools"] = _repair_limit_pools(
            db,
            args.trade_date,
            apply=args.apply,
            replace=not args.no_replace_limit_pools,
        )
    if not args.skip_board_heat_eod:
        result["board_heat_ticks_eod"] = _backfill_board_heat_eod(db, args.trade_date, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, default=_json_default, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
