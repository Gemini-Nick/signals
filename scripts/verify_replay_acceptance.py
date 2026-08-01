#!/usr/bin/env python3
"""Read-only postmarket acceptance probe for one Signals trade date.

This command never repairs, backfills, or writes MongoDB.  It is intentionally
separate from report generation: a day can only be marked ready when the
runtime close seal, official stock-daily coverage, and immutable snapshots are
all visible at the same trade date.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _count(collection: Any, query: dict[str, Any]) -> int:
    try:
        return int(collection.count_documents(query))
    except AttributeError:
        return len(list(collection.find(query)))


def _distinct(collection: Any, field: str, query: dict[str, Any]) -> list[str]:
    try:
        values = collection.distinct(field, query)
    except AttributeError:
        values = [doc.get(field) for doc in collection.find(query)]
    return sorted({str(value) for value in values if str(value or "").strip()})


def audit(db: Any, trade_date: str) -> dict[str, Any]:
    from signals.replay.market_replay import build_market_replay_readiness

    readiness = build_market_replay_readiness(db, trade_date=trade_date, report_stage="formal_postmarket")
    seal = readiness.get("close_seal") if isinstance(readiness.get("close_seal"), dict) else {}
    collections = set(db.list_collection_names()) if hasattr(db, "list_collection_names") else set()
    immutable = db["replay_immutable_snapshots"] if "replay_immutable_snapshots" in collections else None
    backfill = db["replay_backfill_snapshots"] if "replay_backfill_snapshots" in collections else None
    immutable_query = {"trade_date": trade_date}
    backfill_query = {"trade_date": trade_date, "backfill": True}
    backfill_date_query = {"trade_date": trade_date}
    immutable_count = _count(immutable, immutable_query) if immutable is not None else 0
    backfill_count = _count(backfill, backfill_query) if backfill is not None else 0
    backfill_date_count = _count(backfill, backfill_date_query) if backfill is not None else 0
    unmarked_backfill_count = max(0, backfill_date_count - backfill_count)
    immutable_run_ids = _distinct(immutable, "run_id", immutable_query) if immutable is not None else []
    modules = seal.get("modules") if isinstance(seal.get("modules"), dict) else {}
    stock_daily = readiness.get("stock_daily") if isinstance(readiness.get("stock_daily"), dict) else {}
    checks = {
        "formal_ready": readiness.get("formal_ready") is True,
        "close_seal_stable": (
            seal.get("status") == "sealed"
            and seal.get("close_finality") == "stable_close"
            and seal.get("formal_ready") is True
        ),
        "close_seal_modules_ok": all(str(value).lower() == "ok" for value in modules.values()) and bool(modules),
        "stock_daily_official_coverage": float(stock_daily.get("official_coverage_pct") or 0.0) >= 98.0,
        "stock_daily_status_available": stock_daily.get("status") == "available",
        "stock_daily_all_shards": (
            int(stock_daily.get("expected_shards") or 0) <= 0
            or int(stock_daily.get("shard_count") or 0) >= int(stock_daily.get("expected_shards") or 0)
        ),
        "immutable_snapshot_present": immutable_count > 0 and bool(immutable_run_ids),
        "backfill_is_explicit": unmarked_backfill_count == 0,
    }
    return {
        "status": "PASS" if all(checks.values()) else "NOT_READY",
        "trade_date": trade_date,
        "checks": checks,
        "readiness": readiness,
        "immutable_snapshot": {"count": immutable_count, "run_ids": immutable_run_ids},
        "backfill_snapshot": {
            "count": backfill_count,
            "unmarked_count": unmarked_backfill_count,
            "collection_present": backfill is not None,
        },
        "read_only": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        from signals.sync.db import get_db

        result = audit(get_db(), args.trade_date)
    except Exception as exc:  # diagnostic command must return a structured failure
        result = {
            "status": "ERROR",
            "trade_date": args.trade_date,
            "checks": {},
            "error": f"{exc.__class__.__name__}: {exc}",
            "read_only": True,
        }
    rendered = json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    raise SystemExit(0 if result["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
