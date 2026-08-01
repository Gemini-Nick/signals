# -*- coding: utf-8 -*-
"""Lightweight A-share close stabilization and one-shot snapshot sealing."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, time as dt_time, timedelta
from typing import Any, Callable

from pymongo import ReturnDocument

from signals.core.market_time import naive_market_now


PROBE_TIMES = (dt_time(15, 0, 30), dt_time(15, 2, 30), dt_time(15, 5), dt_time(15, 10), dt_time(15, 20))
SEAL_NOT_BEFORE = dt_time(15, 10)
HARD_SEAL_TIME = dt_time(15, 10)
END_TIME = dt_time(16, 10)
SEAL_MODULES = (
    "fullmarket_spot_snapshot",
    "etf_spot_snapshot",
)
HANDOFF_MODULES = frozenset(SEAL_MODULES)
DEFAULT_CANARY_SYMBOLS = (
    "SH.600519",
    "SH.601318",
    "SH.600036",
    "SH.601899",
    "SH.601012",
    "SH.600276",
    "SZ.000001",
    "SZ.000858",
    "SZ.002594",
    "SZ.300750",
    "SZ.300059",
    "SZ.002475",
    "SH.688981",
    "SH.688041",
    "SH.510300",
    "SH.510500",
    "SZ.159915",
    "SH.588000",
    "SH.512100",
)


def _local_naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _slot_at(day: str, value: dt_time) -> datetime:
    return datetime.fromisoformat(day).replace(
        hour=value.hour,
        minute=value.minute,
        second=value.second,
        microsecond=0,
    )


def probe_fingerprint(docs: dict[str, dict[str, Any]]) -> str:
    rows = []
    for symbol, doc in sorted(docs.items()):
        rows.append(
            (
                symbol,
                str(doc.get("trade_date") or doc.get("dt") or "")[:10],
                doc.get("open"),
                doc.get("high"),
                doc.get("low"),
                doc.get("close", doc.get("price")),
                doc.get("vol"),
                doc.get("amount"),
            )
        )
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest()


def _nested_result(result: dict[str, Any]) -> dict[str, Any]:
    nested = result.get("result")
    return nested if isinstance(nested, dict) else result


def seal_result_usable(module: str, result: dict[str, Any], trade_date: str) -> tuple[bool, str]:
    if str(result.get("status") or "").lower() != "ok":
        return False, str(result.get("status") or "missing_status")
    nested = _nested_result(result)
    result_day = str(nested.get("trade_date") or nested.get("date_key") or "")[:10]
    if result_day != trade_date:
        return False, "trade_date_mismatch"
    count = int(nested.get("count") or nested.get("valid_quote_count") or 0)
    if module == "fullmarket_spot_snapshot":
        try:
            minimum = max(1, int(os.getenv("SIGNALS_CLOSE_SEAL_FULLMARKET_MIN_SYMBOLS", "5000")))
        except (TypeError, ValueError):
            minimum = 5000
        if count < minimum:
            return False, "fullmarket_coverage_below_threshold"
    if module == "etf_spot_snapshot":
        try:
            minimum = max(1, int(os.getenv("SIGNALS_CLOSE_SEAL_ETF_MIN_SYMBOLS", "1000")))
        except (TypeError, ValueError):
            minimum = 1000
        if count < minimum:
            return False, "etf_coverage_below_threshold"
    return True, ""


class CloseSealRunner:
    """Probe a bounded quote sample, then seal each close dataset at most once."""

    def __init__(
        self,
        db: Any,
        module_runner: Callable[[str, str], dict[str, Any]],
        *,
        probe_fetcher: Callable[[str, datetime], tuple[int, dict[str, dict[str, Any]]]] | None = None,
        owner: str | None = None,
    ):
        self.db = db
        self.module_runner = module_runner
        self.probe_fetcher = probe_fetcher or self._fetch_probe
        self.owner = owner or f"pid:{os.getpid()}"

    @staticmethod
    def run_id(trade_date: str) -> str:
        return f"close_seal:{trade_date}"

    def _fetch_probe(self, trade_date: str, now: datetime) -> tuple[int, dict[str, dict[str, Any]]]:
        from signals.sync.modules.quote_snapshots import (
            _a_quote_symbols,
            _fetch_eastmoney_ulist_docs,
        )

        try:
            limit = max(8, min(40, int(os.getenv("SIGNALS_CLOSE_SEAL_PROBE_SYMBOLS", "24"))))
        except (TypeError, ValueError):
            limit = 24
        run_id = self.run_id(trade_date)
        run_doc = self.db["sync_runs"].find_one({"_id": run_id}, {"canary_symbols": 1}) or {}
        symbols = _a_quote_symbols(run_doc.get("canary_symbols") or [])
        if not symbols:
            candidates = list(DEFAULT_CANARY_SYMBOLS)
            try:
                pool = self.db["market_pools"].find_one(
                    {"pool": "active"},
                    {"symbols": 1},
                    sort=[("dt", -1), ("updated_at", -1)],
                ) or {}
                candidates.extend(str(item) for item in pool.get("symbols") or [])
            except Exception:
                pass
            symbols = _a_quote_symbols(candidates)[:limit]
            self.db["sync_runs"].update_one(
                {"_id": run_id},
                {"$set": {"canary_symbols": symbols, "updated_at": now}},
            )
        docs, _observations = _fetch_eastmoney_ulist_docs(self.db, symbols, now, trade_date)
        return len(symbols), docs

    def _ensure_run(self, trade_date: str, now: datetime) -> dict[str, Any]:
        run_id = self.run_id(trade_date)
        self.db["sync_runs"].update_one(
            {"_id": run_id},
            {
                "$setOnInsert": {
                    "run_type": "close_seal",
                    "trade_date": trade_date,
                    "status": "pending",
                    "probe_count": 0,
                    "stable_probe_count": 0,
                    "created_at": now,
                },
                "$set": {"updated_at": now},
            },
            upsert=True,
        )
        return self.db["sync_runs"].find_one({"_id": run_id}) or {}

    def _claim(self, trade_date: str, now: datetime) -> dict[str, Any] | None:
        run_id = self.run_id(trade_date)
        lease_until = now + timedelta(minutes=15)
        return self.db["sync_runs"].find_one_and_update(
            {
                "_id": run_id,
                "$or": [
                    {"lease_until": {"$lt": now}},
                    {"lease_until": {"$exists": False}},
                    {"lease_owner": self.owner},
                ],
            },
            {
                "$set": {
                    "lease_owner": self.owner,
                    "lease_until": lease_until,
                    "heartbeat_at": now,
                    "updated_at": now,
                },
                "$inc": {"fence_token": 1},
            },
            return_document=ReturnDocument.AFTER,
        )

    def _release(self, trade_date: str, now: datetime) -> None:
        self.db["sync_runs"].update_one(
            {"_id": self.run_id(trade_date), "lease_owner": self.owner},
            {"$set": {"lease_owner": "", "lease_until": now, "heartbeat_at": now, "updated_at": now}},
        )

    @staticmethod
    def _next_probe_at(trade_date: str, now: datetime) -> datetime | None:
        for slot in PROBE_TIMES:
            candidate = _slot_at(trade_date, slot)
            if candidate > now:
                return candidate
        return None

    def _record_probe(self, trade_date: str, now: datetime, run_doc: dict[str, Any]) -> dict[str, Any]:
        requested, docs = self.probe_fetcher(trade_date, now)
        valid = {
            symbol: doc
            for symbol, doc in docs.items()
            if str(doc.get("trade_date") or doc.get("dt") or "")[:10] == trade_date
            and float(doc.get("close") or doc.get("price") or 0) > 0
            and isinstance(doc.get("source_updated_at"), datetime)
            and _local_naive(doc["source_updated_at"]) >= _slot_at(trade_date, dt_time(15, 0))
        }
        minimum = max(1, int(requested * 0.8))
        fingerprint = probe_fingerprint(valid) if len(valid) >= minimum else ""
        previous_hash = str(run_doc.get("last_probe_hash") or "")
        previous_at = run_doc.get("last_probe_at")
        enough_gap = isinstance(previous_at, datetime) and (now - _local_naive(previous_at)).total_seconds() >= 60
        stable_count = int(run_doc.get("stable_probe_count") or 0)
        stable_count = stable_count + 1 if fingerprint and fingerprint == previous_hash and enough_gap else (1 if fingerprint else 0)
        stable = stable_count >= 2
        next_probe = self._next_probe_at(trade_date, now)
        self.db["sync_runs"].update_one(
            {"_id": self.run_id(trade_date), "lease_owner": self.owner},
            {
                "$set": {
                    "status": "ready" if stable else "probing",
                    "last_probe_at": now,
                    "last_probe_hash": fingerprint,
                    "stable_probe_count": stable_count,
                    "source_trade_date": trade_date if valid else "",
                    "probe_requested": requested,
                    "probe_valid": len(valid),
                    "probe_coverage_pct": round(len(valid) / requested * 100, 2) if requested else 0.0,
                    "next_probe_at": next_probe,
                    "updated_at": now,
                },
                "$inc": {"probe_count": 1},
            },
        )
        return {"stable": stable, "requested": requested, "valid": len(valid), "fingerprint": fingerprint}

    def _run_module_once(self, trade_date: str, module: str, now: datetime) -> dict[str, Any]:
        run_id = self.run_id(trade_date)
        task_id = f"{run_id}:{module}:all"
        existing = self.db["sync_tasks"].find_one({"_id": task_id}) or {}
        if str(existing.get("status") or "") == "ok":
            usable, _reason = seal_result_usable(module, existing.get("result_summary") or {}, trade_date)
            if usable:
                return {"module": module, "status": "ok", "skipped": True}
            self.db["sync_tasks"].update_one(
                {"_id": task_id, "status": "ok"},
                {"$set": {"status": "partial", "error_msg": "stored_result_validation_failed", "updated_at": now}},
            )
        lease_now = naive_market_now("A")
        claimed = self.db["sync_tasks"].find_one_and_update(
            {
                "_id": task_id,
                "$or": [
                    {"status": {"$in": ["pending", "error", "partial"]}},
                    {"status": {"$exists": False}},
                    {"lease_until": {"$lt": lease_now}},
                ],
            },
            {
                "$setOnInsert": {
                    "run_id": run_id,
                    "trade_date": trade_date,
                    "module": module,
                    "task_key": f"{module}:all",
                    "created_at": now,
                },
                "$set": {
                    "status": "running",
                    "owner_pid": self.owner,
                    "lease_until": lease_now + timedelta(minutes=15),
                    "started_at": lease_now,
                    "updated_at": lease_now,
                },
                "$inc": {"attempts": 1},
            },
            upsert=not bool(existing),
            return_document=ReturnDocument.AFTER,
        )
        if not claimed:
            return {"module": module, "status": "busy"}
        self.db["sync_runs"].update_one(
            {"_id": run_id, "lease_owner": self.owner},
            {
                "$set": {
                    "lease_until": lease_now + timedelta(minutes=15),
                    "heartbeat_at": lease_now,
                    "updated_at": lease_now,
                }
            },
        )
        try:
            result = self.module_runner(module, trade_date)
            usable, validation_error = seal_result_usable(module, result, trade_date)
            if usable:
                status = "ok"
            else:
                status = str(result.get("status") or "partial").lower()
                if status == "ok":
                    status = "partial"
            error = str(result.get("error") or validation_error or "")[:1000]
        except Exception as exc:
            result = {}
            status = "error"
            error = f"{exc.__class__.__name__}: {exc}"[:1000]
        finished = naive_market_now("A")
        self.db["sync_tasks"].update_one(
            {"_id": task_id, "owner_pid": self.owner},
            {
                "$set": {
                    "status": status,
                    "result_summary": result,
                    "error_msg": error,
                    "finished_at": finished,
                    "updated_at": finished,
                    "lease_until": finished,
                    "owner_pid": "",
                }
            },
        )
        return {"module": module, "status": status, "result": result, "error": error}

    def _seal(self, trade_date: str, now: datetime, *, stable: bool) -> dict[str, Any]:
        results = [self._run_module_once(trade_date, module, now) for module in SEAL_MODULES]
        critical_ok = all(
            next((item["status"] for item in results if item["module"] == module), "") == "ok"
            for module in ("fullmarket_spot_snapshot", "etf_spot_snapshot")
        )
        status = "sealed" if stable and critical_ok else "partial"
        finality = "stable_close" if status == "sealed" else "validation_failed"
        self.db["sync_runs"].update_one(
            {"_id": self.run_id(trade_date), "lease_owner": self.owner},
            {
                "$set": {
                    "status": status,
                    "close_finality": finality,
                    "sealed_at": now if critical_ok else None,
                    "module_results": {item["module"]: item["status"] for item in results},
                    "updated_at": now,
                }
            },
        )
        return {"status": status, "close_finality": finality, "results": results}

    def _mark_forced_partial(self, trade_date: str, now: datetime, *, terminal: bool) -> dict[str, Any]:
        self.db["sync_runs"].update_one(
            {"_id": self.run_id(trade_date), "lease_owner": self.owner},
            {
                "$set": {
                    "status": "partial",
                    "close_finality": "forced_provisional",
                    "terminal_partial": terminal,
                    "updated_at": now,
                }
            },
        )
        return {"status": "partial", "close_finality": "forced_provisional", "terminal": terminal}

    def tick(self, trade_date: str, now: datetime | None = None) -> dict[str, Any]:
        now = _local_naive(now or naive_market_now("A"))
        start = _slot_at(trade_date, PROBE_TIMES[0])
        end = _slot_at(trade_date, END_TIME)
        if now < start or now >= end:
            return {"status": "outside_window", "next_probe_at": self._next_probe_at(trade_date, now)}
        run_doc = self._ensure_run(trade_date, now)
        if run_doc.get("status") == "sealed":
            return {"status": "sealed", "skipped": True}
        if run_doc.get("terminal_partial"):
            return {"status": "partial", "skipped": True, "terminal": True}
        next_probe_at = run_doc.get("next_probe_at")
        if isinstance(next_probe_at, datetime) and _local_naive(next_probe_at) > now and now.time() < HARD_SEAL_TIME:
            return {"status": str(run_doc.get("status") or "pending"), "next_probe_at": next_probe_at}
        claimed = self._claim(trade_date, now)
        if not claimed:
            return {"status": "busy"}
        try:
            probe = self._record_probe(trade_date, now, claimed)
            if probe["stable"] and now.time() >= SEAL_NOT_BEFORE:
                result = self._seal(trade_date, naive_market_now("A"), stable=True)
                if now.time() >= PROBE_TIMES[-1] and result.get("status") != "sealed":
                    self.db["sync_runs"].update_one(
                        {"_id": self.run_id(trade_date), "lease_owner": self.owner},
                        {"$set": {"terminal_partial": True, "updated_at": now}},
                    )
                    result["terminal"] = True
                return result
            if now.time() >= HARD_SEAL_TIME:
                return self._mark_forced_partial(
                    trade_date,
                    now,
                    terminal=now.time() >= PROBE_TIMES[-1],
                )
            return {"status": "probing", **probe, "next_probe_at": self._next_probe_at(trade_date, now)}
        finally:
            self._release(trade_date, now)

    def seconds_until_next_event(self, trade_date: str, now: datetime | None = None) -> int:
        now = _local_naive(now or naive_market_now("A"))
        run_doc = self.db["sync_runs"].find_one({"_id": self.run_id(trade_date)}, {"next_probe_at": 1, "status": 1}) or {}
        if run_doc.get("status") == "sealed":
            return 300
        candidate = run_doc.get("next_probe_at")
        if not isinstance(candidate, datetime):
            candidate = self._next_probe_at(trade_date, now)
        if not isinstance(candidate, datetime):
            return 300
        return max(1, min(300, int((_local_naive(candidate) - now).total_seconds())))
