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


def _canonical_a_code(value: Any) -> str:
    raw = str(value or "").strip().upper().replace(".", "")
    for prefix in ("SH", "SZ", "BJ"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    return raw if len(raw) == 6 and raw.isdigit() else ""


def _strict_fullmarket_codes(db: Any, trade_date: str) -> set[str]:
    """Return the dated, non-ETF A-share quote universe used by minute scans."""
    date_key = str(trade_date or "").replace("-", "")[:8]
    try:
        from signals.sync.modules.stock_minute import _explicit_index_symbol, _index_codes, _pure_a_code

        index_codes = _index_codes()
    except Exception:
        _explicit_index_symbol = lambda symbol, _codes: str(symbol or "").upper().startswith(("SH.000", "SZ.399"))
        _pure_a_code = _canonical_a_code
        index_codes = set()
    query = {
        "date_key": date_key,
        "code": {"$regex": r"^\d{6}$"},
        "price": {"$gt": 0},
        "open": {"$gt": 0},
        "high": {"$gt": 0},
        "low": {"$gt": 0},
        "prev_close": {"$gt": 0},
    }
    codes: set[str] = set()
    try:
        rows = db["fullmarket_spot_snapshots"].find(
            query,
            {"code": 1, "symbol": 1, "asset_class": 1, "security_type": 1},
        )
        for row in rows:
            if str(row.get("asset_class") or row.get("security_type") or "").lower() == "etf":
                continue
            raw_symbol = row.get("symbol") or row.get("code")
            code = _pure_a_code(row.get("code") or raw_symbol)
            if not code or _explicit_index_symbol(raw_symbol, index_codes) or code.startswith(("200", "900")):
                continue
            codes.add(code)
    except Exception:
        return set()
    return codes


def _minute_channel(db: Any, trade_date: str) -> dict[str, Any]:
    codes = _strict_fullmarket_codes(db, trade_date)
    if not codes:
        return {"expected_symbols": 0, "frequencies": {}, "status": "partial"}
    try:
        bars = db["bars"]
    except (KeyError, TypeError):
        return {"expected_symbols": len(codes), "frequencies": {}, "status": "partial"}
    try:
        from datetime import datetime, timedelta

        start = datetime.strptime(str(trade_date)[:10], "%Y-%m-%d")
        end = start + timedelta(days=1)
    except ValueError:
        return {"expected_symbols": len(codes), "frequencies": {}, "status": "invalid_trade_date"}
    frequencies: dict[str, Any] = {}
    for freq in ("5分钟", "15分钟", "30分钟"):
        query = {
            "meta.freq": freq,
            "dt": {"$gte": start, "$lt": end},
            "meta.symbol": {"$in": sorted(codes)},
        }
        raw_symbols = _distinct(bars, "meta.symbol", query)
        covered = {_canonical_a_code(symbol) for symbol in raw_symbols}
        covered.discard("")
        frequencies[freq] = {
            "expected": len(codes),
            "covered": len(covered & codes),
            "missing": max(0, len(codes) - len(covered & codes)),
            "coverage_pct": round(len(covered & codes) / len(codes) * 100, 2) if codes else 0.0,
        }
    return {
        "expected_symbols": len(codes),
        "frequencies": frequencies,
        "status": "ok" if codes and all(item["missing"] == 0 for item in frequencies.values()) else "partial",
    }


def _channel_acceptance(db: Any, trade_date: str) -> dict[str, Any]:
    minute = _minute_channel(db, trade_date)
    try:
        sync_log = db["sync_log"]
    except (KeyError, TypeError):
        sync_log = None
    try:
        sync_tasks = db["sync_tasks"]
    except (KeyError, TypeError):
        sync_tasks = None
    quote = sync_log.find_one({"_id": "quote_snapshots:A:_meta"}, {"_id": 0, "status": 1, "result": 1}) if sync_log is not None else {}
    readiness = sync_log.find_one({"_id": "minute_readiness_probe:A:_meta"}, {"_id": 0, "status": 1, "result": 1}) if sync_log is not None else {}
    index = sync_log.find_one({"_id": "index_minute:A:_meta"}, {"_id": 0, "status": 1, "result": 1, "unsupported_calls": 1}) if sync_log is not None else {}
    daily = sync_log.find_one({"_id": "stock_daily:progress:_meta"}, {"_id": 0, "status": 1, "quality": 1, "coverage_pct": 1, "covered_codes": 1, "expected_codes": 1}) if sync_log is not None else {}
    quote = quote or {}
    readiness = readiness or {}
    index = index or {}
    daily = daily or {}
    # Optional catch-up lanes (currently HK daily) are deliberately allowed
    # to continue after the formal A-share close path is complete.  They must
    # not make the user-facing channel gate look stuck; only tasks that block
    # the postmarket run participate in the runtime health checks.
    critical_filter = {"blocks_run": {"$ne": False}}
    running = _count(sync_tasks, {"status": "running", **critical_filter}) if sync_tasks is not None else 0
    errors = _count(sync_tasks, {"status": "error", **critical_filter}) if sync_tasks is not None else 0
    optional_running = _count(sync_tasks, {"status": "running", "blocks_run": False}) if sync_tasks is not None else 0
    optional_errors = _count(sync_tasks, {"status": "error", "blocks_run": False}) if sync_tasks is not None else 0
    checks = {
        "fullmarket_minute_complete": minute.get("status") == "ok",
        "quote_channel_ok": quote.get("status") == "ok" and int((quote.get("result") or {}).get("errors") or 0) == 0,
        "minute_readiness_ok": readiness.get("status") == "ok" and int((readiness.get("result") or {}).get("not_ready") or 0) == 0,
        "index_minute_accounted": index.get("status") == "ok",
        "daily_progress_final_close": daily.get("status") == "ok" and daily.get("quality") == "final_close" and float(daily.get("coverage_pct") or 0) >= 100.0,
        "no_running_tasks": running == 0,
        "no_error_tasks": errors == 0,
    }
    return {
        "status": "PASS" if all(checks.values()) else "NOT_READY",
        "checks": checks,
        "minute": minute,
        "quote": quote,
        "minute_readiness": readiness,
        "index_minute": {"status": index.get("status"), "unsupported_calls": index.get("unsupported_calls") or (index.get("result") or {}).get("unsupported_calls", 0)},
        "daily_progress": daily,
        "runtime": {
            "running_tasks": running,
            "error_tasks": errors,
            "optional_running_tasks": optional_running,
            "optional_error_tasks": optional_errors,
        },
    }


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
        "channel_acceptance": _channel_acceptance(db, trade_date),
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
    parser.add_argument(
        "--channels-only",
        action="store_true",
        help="验收 Mongo/盘中/盘后数据通道，不要求历史正式复盘快照门槛",
    )
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
    if args.channels_only:
        exit_status = 0 if result.get("channel_acceptance", {}).get("status") == "PASS" else 2
    else:
        exit_status = 0 if result["status"] == "PASS" else 2
    raise SystemExit(exit_status)


if __name__ == "__main__":
    main()
