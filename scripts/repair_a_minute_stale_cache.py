# -*- coding: utf-8 -*-
"""Repair stale A-share minute bars in Mongo by refreshing public website tails.

The stock_minute sync module normally follows the terminal pool. This repair
runner walks the existing Mongo minute cache itself, finds symbol/frequency
pairs whose latest bar is older than the collection's expected latest dt, and
refreshes a rolling online tail through the same public Sina/Tencent adapters.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from pymongo import MongoClient

from signals.sync.modules.stock_minute import _insert_new_minute_docs, _pure_a_code, _sync_one_minute


MONGO_URL = "mongodb://127.0.0.1:27017/signals"
DB_NAME = "signals"
MINUTE_FREQS = ("5分钟", "15分钟", "30分钟")
FREQ_ALIASES = {
    "5m": "5分钟",
    "5min": "5分钟",
    "5分钟": "5分钟",
    "15m": "15分钟",
    "15min": "15分钟",
    "15分钟": "15分钟",
    "30m": "30分钟",
    "30min": "30分钟",
    "30分钟": "30分钟",
}


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse_freqs(raw: str) -> list[str]:
    values: list[str] = []
    for item in str(raw or "").replace(";", ",").split(","):
        freq = FREQ_ALIASES.get(item.strip().lower(), item.strip())
        if freq in MINUTE_FREQS and freq not in values:
            values.append(freq)
    return values or list(MINUTE_FREQS)


def _parse_symbols(raw: str) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for item in str(raw or "").replace(";", ",").split(","):
        code = _pure_a_code(item)
        if code and code not in seen:
            seen.add(code)
            symbols.append(code)
    return symbols


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("T", " ")
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit(f"invalid datetime: {value}") from exc


def _expected_latest_dt(db, freq: str, override: datetime | None) -> datetime | None:
    if override:
        return override
    row = next(
        db["bars"].aggregate(
            [
                {"$match": {"meta.market": "A", "meta.freq": freq}},
                {"$group": {"_id": None, "latest_dt": {"$max": "$dt"}}},
            ],
            allowDiskUse=True,
        ),
        None,
    )
    return row.get("latest_dt") if row else None


def _excluded(code: str, prefixes: tuple[str, ...]) -> bool:
    return any(code.startswith(prefix) for prefix in prefixes)


def _stale_tasks(
    db,
    *,
    freqs: list[str],
    expected_override: datetime | None,
    symbols: list[str],
    exclude_prefixes: tuple[str, ...],
    include_missing: bool,
    sort: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    expected_by_freq: dict[str, datetime] = {}
    symbol_filter = {"$in": symbols} if symbols else {"$regex": r"^\d{6}$"}

    daily_symbols: set[str] = set()
    if include_missing:
        for symbol in db["bars"].distinct("meta.symbol", {"meta.market": "A", "meta.freq": "日线", "meta.symbol": {"$regex": r"^\d{6}$"}}):
            code = _pure_a_code(symbol)
            if code and not _excluded(code, exclude_prefixes) and (not symbols or code in symbols):
                daily_symbols.add(code)

    for freq in freqs:
        expected = _expected_latest_dt(db, freq, expected_override)
        if expected is None:
            continue
        expected_by_freq[freq] = expected
        seen: set[str] = set()
        pipeline = [
            {"$match": {"meta.market": "A", "meta.freq": freq, "meta.symbol": symbol_filter}},
            {"$group": {"_id": "$meta.symbol", "latest_dt": {"$max": "$dt"}, "bar_count": {"$sum": 1}}},
        ]
        for row in db["bars"].aggregate(pipeline, allowDiskUse=True):
            code = _pure_a_code(row.get("_id"))
            if not code or _excluded(code, exclude_prefixes):
                continue
            seen.add(code)
            latest_dt = row.get("latest_dt")
            if latest_dt is None or latest_dt < expected:
                tasks.append({"symbol": code, "freq": freq, "latest_dt": latest_dt, "expected_dt": expected, "bar_count": int(row.get("bar_count") or 0)})
        if include_missing:
            for code in sorted(daily_symbols - seen):
                tasks.append({"symbol": code, "freq": freq, "latest_dt": None, "expected_dt": expected, "bar_count": 0})

    reverse = sort != "oldest"
    tasks.sort(key=lambda item: (item["latest_dt"] is None, item["latest_dt"] or datetime.min, item["symbol"], item["freq"]), reverse=reverse)
    return tasks, {"expected_by_freq": expected_by_freq, "total_stale_tasks": len(tasks)}


def _repair_one(db, task: dict[str, Any], *, tail_count: int, call_interval: float, provider_timeout: float, skip_change_pct: bool) -> dict[str, Any]:
    symbol = task["symbol"]
    freq = task["freq"]
    try:
        docs = _sync_one_minute(symbol, freq, None, tail_count=tail_count, db=None if skip_change_pct else db)
        if call_interval:
            time.sleep(call_interval)
        if not docs:
            return {**task, "status": "empty", "written": 0, "inserted": 0, "refreshed": 0, "deleted": 0, "skipped_existing": 0}
        write_result = _insert_new_minute_docs(db["bars"], symbol, freq, docs)
        return {**task, "status": "ok", **write_result}
    except Exception as exc:
        return {**task, "status": "error", "error": f"{type(exc).__name__}: {str(exc)[:240]}", "written": 0, "inserted": 0, "refreshed": 0, "deleted": 0, "skipped_existing": 0}


def run(args: argparse.Namespace) -> dict[str, Any]:
    client = MongoClient(args.mongo_url, serverSelectionTimeoutMS=3000)
    db = client[args.db_name]
    freqs = _parse_freqs(args.freqs)
    symbols = _parse_symbols(args.symbols)
    exclude_prefixes = tuple(prefix.strip() for prefix in str(args.exclude_prefixes or "").split(",") if prefix.strip())
    expected_override = _parse_dt(args.expected_dt)
    tasks, selection = _stale_tasks(
        db,
        freqs=freqs,
        expected_override=expected_override,
        symbols=symbols,
        exclude_prefixes=exclude_prefixes,
        include_missing=args.include_missing,
        sort=args.sort,
    )
    selected = tasks[: args.max_tasks] if args.max_tasks > 0 else tasks
    started_at = datetime.now()
    summary: dict[str, Any] = {
        "started_at": started_at,
        "freqs": freqs,
        "tail_count": args.tail_count,
        "workers": args.workers,
        "providers": args.providers,
        "exclude_prefixes": exclude_prefixes,
        "skip_change_pct": args.skip_change_pct,
        "dry_run": args.dry_run,
        "selected_tasks": len(selected),
        **selection,
    }
    if args.dry_run:
        summary["status_counts"] = {"selected": len(selected)}
        summary["sample_tasks"] = selected[:50]
        _write_output(args.output, summary)
        client.close()
        print(json.dumps({k: v for k, v in summary.items() if k != "sample_tasks"}, ensure_ascii=False, default=_json_default))
        return summary

    old_timeout = os.environ.get("STOCK_MINUTE_TIMEOUT")
    old_providers = os.environ.get("STOCK_MINUTE_PROVIDERS")
    old_cooldown = os.environ.get("SIGNALS_PROVIDER_COOLDOWN_SECONDS")
    old_jitter = os.environ.get("SIGNALS_PROVIDER_JITTER_SECONDS")
    os.environ["STOCK_MINUTE_TIMEOUT"] = str(args.provider_timeout)
    os.environ["STOCK_MINUTE_PROVIDERS"] = args.providers
    if args.provider_cooldown_seconds:
        os.environ["SIGNALS_PROVIDER_COOLDOWN_SECONDS"] = args.provider_cooldown_seconds
    if args.provider_jitter_seconds:
        os.environ["SIGNALS_PROVIDER_JITTER_SECONDS"] = args.provider_jitter_seconds

    results: list[dict[str, Any]] = []
    totals = Counter()
    by_freq: dict[str, Counter] = defaultdict(Counter)
    last_progress = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_map = {
                executor.submit(
                    _repair_one,
                    db,
                    task,
                    tail_count=args.tail_count,
                    call_interval=args.call_interval,
                    provider_timeout=args.provider_timeout,
                    skip_change_pct=args.skip_change_pct,
                ): task
                for task in selected
            }
            for idx, future in enumerate(as_completed(future_map), start=1):
                result = future.result()
                results.append(result)
                status = str(result.get("status") or "unknown")
                totals[status] += 1
                by_freq[str(result.get("freq"))][status] += 1
                for field in ("written", "inserted", "refreshed", "deleted", "skipped_existing"):
                    totals[field] += int(result.get(field) or 0)
                now = time.monotonic()
                if args.progress_every and (idx % args.progress_every == 0 or now - last_progress >= 30):
                    print(
                        json.dumps(
                            {
                                "processed": idx,
                                "selected": len(selected),
                                "status_counts": {key: value for key, value in totals.items() if key not in {"written", "inserted", "refreshed", "deleted", "skipped_existing"}},
                                "written": totals["written"],
                                "refreshed": totals["refreshed"],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    last_progress = now
    finally:
        if old_timeout is None:
            os.environ.pop("STOCK_MINUTE_TIMEOUT", None)
        else:
            os.environ["STOCK_MINUTE_TIMEOUT"] = old_timeout
        if old_providers is None:
            os.environ.pop("STOCK_MINUTE_PROVIDERS", None)
        else:
            os.environ["STOCK_MINUTE_PROVIDERS"] = old_providers
        if old_cooldown is None:
            os.environ.pop("SIGNALS_PROVIDER_COOLDOWN_SECONDS", None)
        else:
            os.environ["SIGNALS_PROVIDER_COOLDOWN_SECONDS"] = old_cooldown
        if old_jitter is None:
            os.environ.pop("SIGNALS_PROVIDER_JITTER_SECONDS", None)
        else:
            os.environ["SIGNALS_PROVIDER_JITTER_SECONDS"] = old_jitter

    summary.update(
        {
            "finished_at": datetime.now(),
            "elapsed_seconds": round((datetime.now() - started_at).total_seconds(), 3),
            "status_counts": dict(totals),
            "by_freq_status": {freq: dict(counter) for freq, counter in by_freq.items()},
            "error_samples": [item for item in results if item.get("status") == "error"][:20],
            "empty_samples": [item for item in results if item.get("status") == "empty"][:20],
            "results": results,
        }
    )
    _write_output(args.output, summary)
    client.close()
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, ensure_ascii=False, default=_json_default))
    print(f"wrote {args.output}")
    return summary


def _write_output(path: str, payload: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo-url", default=MONGO_URL)
    parser.add_argument("--db-name", default=DB_NAME)
    parser.add_argument("--freqs", default="5min,15min,30min")
    parser.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist.")
    parser.add_argument("--exclude-prefixes", default="", help="Comma-separated raw-code prefixes to skip, e.g. 920,8,4.")
    parser.add_argument("--expected-dt", default="", help="Override expected latest dt, e.g. 2026-05-29 15:00:00.")
    parser.add_argument("--include-missing", action="store_true", help="Also repair daily symbols with no existing bars for a frequency.")
    parser.add_argument("--sort", choices=["newest", "oldest"], default="newest")
    parser.add_argument("--max-tasks", type=int, default=500)
    parser.add_argument("--tail-count", type=int, default=1970)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--provider-timeout", type=float, default=8.0)
    parser.add_argument("--providers", default="sina,tencent", help="Provider order, e.g. tencent,sina or sina.")
    parser.add_argument("--provider-cooldown-seconds", default="", help="Override provider risk-error cooldown for this repair run, e.g. 1,1.")
    parser.add_argument("--provider-jitter-seconds", default="", help="Override provider jitter for this repair run, e.g. 0,0.")
    parser.add_argument("--skip-change-pct", action="store_true", help="Skip daily prev-close lookup while repairing OHLCV/latest.")
    parser.add_argument("--call-interval", type=float, default=0.05)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", default="/tmp/signals_a_minute_stale_repair.json")
    return parser


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
