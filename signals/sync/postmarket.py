# -*- coding: utf-8 -*-
"""Postmarket DAG runner with Mongo-backed resume/checkpoint state."""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from signals.core.market_hours import TZ_BEIJING

from .engine import LANE_MAINTENANCE_PLANS
from .task_context import task_env

logger = logging.getLogger("signals.sync.postmarket")

RUN_OK_STATUSES = {"ok"}
TASK_OK_STATUSES = {"ok"}
RUN_TERMINAL_STATUSES = {"ok"}
RETRYABLE_TASK_STATUSES = {"pending", "running", "stale", "partial", "degraded", "error", "deferred"}


@dataclass(frozen=True)
class PostmarketTaskSpec:
    module: str
    phase: str
    shard_key: str = "all"
    depends_on: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    blocks_run: bool = True

    @property
    def task_key(self) -> str:
        return f"{self.module}:{self.shard_key}"


def _const_env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 64) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _stock_daily_shard_tasks() -> tuple[PostmarketTaskSpec, ...]:
    shard_count = _const_env_int("SIGNALS_POSTMARKET_STOCK_DAILY_SHARDS", 16, minimum=1, maximum=64)
    return tuple(
        PostmarketTaskSpec(
            "stock_daily",
            "market_data",
            shard_key=f"shard_{idx:02d}",
            depends_on=("fullmarket_spot_snapshot:all",),
            env={
                "SIGNALS_SYNC_FULL_STOCK_DAILY": "true",
                "STOCK_DAILY_SCOPE": "all",
                "STOCK_DAILY_SHARD_COUNT": str(shard_count),
                "STOCK_DAILY_SHARD_INDEX": str(idx),
                "STOCK_DAILY_SHARD_KEY": f"shard_{idx:02d}",
            },
        )
        for idx in range(shard_count)
    )


def _hk_stock_daily_shard_tasks() -> tuple[PostmarketTaskSpec, ...]:
    shard_count = _const_env_int("SIGNALS_POSTMARKET_HK_STOCK_DAILY_SHARDS", 8, minimum=1, maximum=64)
    return tuple(
        PostmarketTaskSpec(
            "hk_stock_daily",
            "hk_market_data",
            shard_key=f"shard_{idx:02d}",
            env={
                "HK_STOCK_DAILY_SCOPE": "all",
                "HK_STOCK_DAILY_SHARD_COUNT": str(shard_count),
                "HK_STOCK_DAILY_SHARD_INDEX": str(idx),
                "HK_STOCK_DAILY_SHARD_KEY": f"shard_{idx:02d}",
            },
            blocks_run=False,
        )
        for idx in range(shard_count)
    )


def _board_cons_shard_tasks() -> tuple[PostmarketTaskSpec, ...]:
    return (
        PostmarketTaskSpec(
            "board_cons",
            "market_data",
            shard_key="board",
            depends_on=("board_ranking:all",),
            env={"BOARD_CONS_KIND": "board", "BOARD_CONS_SHARD_KEY": "board", "BOARD_CONS_BATCH_SIZE": "500"},
        ),
        PostmarketTaskSpec(
            "board_cons",
            "market_data",
            shard_key="concept",
            depends_on=("board_ranking:all",),
            env={"BOARD_CONS_KIND": "concept", "BOARD_CONS_SHARD_KEY": "concept", "BOARD_CONS_BATCH_SIZE": "500"},
        ),
    )


def _stock_30m_fullmarket_shard_tasks() -> tuple[PostmarketTaskSpec, ...]:
    shard_count = _const_env_int("SIGNALS_POSTMARKET_STOCK_30M_SHARDS", 16, minimum=1, maximum=64)
    return tuple(
        PostmarketTaskSpec(
            "stock_30m_fullmarket",
            "minute_fullmarket",
            shard_key=f"shard_{idx:02d}",
            depends_on=(*_STOCK_DAILY_DEPS,),
            env={
                "STOCK_30M_FULLMARKET_SHARD_COUNT": str(shard_count),
                "STOCK_30M_FULLMARKET_SHARD_INDEX": str(idx),
                "STOCK_30M_FULLMARKET_SHARD_KEY": f"shard_{idx:02d}",
                "STOCK_30M_FULLMARKET_MAX_CODES_PER_RUN": "500",
                "STOCK_30M_FULLMARKET_CALL_INTERVAL": "0.1",
                "STOCK_MINUTE_STRICT_PUBLIC_ERRORS": "true",
                "SIGNALS_PROVIDER_JITTER_SECONDS": "0,0.15",
            },
            blocks_run=False,
        )
        for idx in range(shard_count)
    )


_STOCK_DAILY_TASKS = _stock_daily_shard_tasks()
_HK_STOCK_DAILY_TASKS = _hk_stock_daily_shard_tasks()
_BOARD_CONS_TASKS = _board_cons_shard_tasks()
_STOCK_DAILY_DEPS = tuple(task.task_key for task in _STOCK_DAILY_TASKS)
_HK_STOCK_DAILY_DEPS = tuple(task.task_key for task in _HK_STOCK_DAILY_TASKS)
_BOARD_CONS_DEPS = tuple(task.task_key for task in _BOARD_CONS_TASKS)
_STOCK_30M_TASKS = _stock_30m_fullmarket_shard_tasks()
_STOCK_30M_DEPS = tuple(task.task_key for task in _STOCK_30M_TASKS)


POSTMARKET_TASKS: tuple[PostmarketTaskSpec, ...] = (
    PostmarketTaskSpec("fullmarket_spot_snapshot", "market_data"),
    PostmarketTaskSpec("market_pools", "market_data"),
    PostmarketTaskSpec("quote_snapshots", "market_data"),
    PostmarketTaskSpec("index_daily", "market_data", depends_on=("quote_snapshots:all",)),
    *_STOCK_DAILY_TASKS,
    PostmarketTaskSpec("board_ranking", "market_data"),
    *_BOARD_CONS_TASKS,
    PostmarketTaskSpec("chain_heat_snapshots", "chain_context", depends_on=("board_ranking:all",)),
    PostmarketTaskSpec(
        "postmarket_chain_rebuild",
        "chain_context",
        depends_on=("fullmarket_spot_snapshot:all", *_STOCK_DAILY_DEPS, "board_ranking:all", *_BOARD_CONS_DEPS, "chain_heat_snapshots:all"),
    ),
    PostmarketTaskSpec(
        "stock_minute",
        "chain_context",
        shard_key="chain_representatives",
        depends_on=("chain_heat_snapshots:all", "postmarket_chain_rebuild:all"),
        env={
            "STOCK_MINUTE_SCOPE": "postmarket_candidates",
            "STOCK_MINUTE_FREQS": "5min,15min",
            "STOCK_MINUTE_POSTMARKET_MAX_CODES": "160",
            "STOCK_MINUTE_POSTMARKET_CHAIN_LIMIT": "80",
            "STOCK_MINUTE_POSTMARKET_ROLLUP_LIMIT": "40",
            "STOCK_MINUTE_WORKERS": "4",
            "STOCK_MINUTE_CALL_INTERVAL": "0.15",
        },
    ),
    PostmarketTaskSpec("weekly_rollup", "derived", depends_on=(*_STOCK_DAILY_DEPS, "index_daily:all")),
    PostmarketTaskSpec(
        "technical_signal_scan",
        "derived",
        depends_on=(*_STOCK_DAILY_DEPS, "weekly_rollup:all"),
        env={
            "TECHNICAL_SIGNAL_SCAN_MARKETS": "A",
            "TECHNICAL_SIGNAL_SCAN_REQUIRED_FREQS": "日线,周线",
            "TECHNICAL_SIGNAL_SCAN_OPTIONAL_FREQS": "30分钟,15分钟,5分钟",
        },
    ),
    PostmarketTaskSpec("knowledge_market_views", "derived"),
    PostmarketTaskSpec(
        "concept_relationship_graph",
        "derived",
        depends_on=("board_ranking:all", *_BOARD_CONS_DEPS, "chain_heat_snapshots:all", "knowledge_market_views:all"),
    ),
    PostmarketTaskSpec("signal_pool", "derived", depends_on=("technical_signal_scan:all",)),
    PostmarketTaskSpec(
        "terminal_realtime_pool",
        "terminal",
        depends_on=("technical_signal_scan:all", "knowledge_market_views:all", "postmarket_chain_rebuild:all", "chain_heat_snapshots:all", "concept_relationship_graph:all"),
        env={"TERMINAL_POOL_STRICT_SOURCES": "true"},
    ),
    PostmarketTaskSpec("strategy_snapshot", "terminal", depends_on=("terminal_realtime_pool:all",)),
    PostmarketTaskSpec("cache_preheat", "terminal", depends_on=("terminal_realtime_pool:all",)),
    PostmarketTaskSpec(
        "stock_minute",
        "minute_preheat",
        depends_on=("terminal_realtime_pool:all",),
        env={
            "STOCK_MINUTE_SCOPE": "postmarket_candidates",
            "STOCK_MINUTE_FREQS": "5min,15min",
            "STOCK_MINUTE_POSTMARKET_MAX_CODES": "240",
            "STOCK_MINUTE_WORKERS": "6",
            "STOCK_MINUTE_CALL_INTERVAL": "0.15",
        },
    ),
    PostmarketTaskSpec("index_minute", "minute_preheat", depends_on=("terminal_realtime_pool:all",)),
    PostmarketTaskSpec(
        "minute_readiness_probe",
        "minute_preheat",
        depends_on=("stock_minute:all", "index_minute:all"),
        blocks_run=False,
    ),
    *_STOCK_30M_TASKS,
    *_HK_STOCK_DAILY_TASKS,
)

POSTMARKET_PHASES: tuple[str, ...] = tuple(dict.fromkeys(task.phase for task in POSTMARKET_TASKS))


def default_run_id(trade_date: str) -> str:
    return f"postmarket:{trade_date}"


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, default)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, *, minimum: float = 0.0, maximum: float = 100.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _now_bj() -> datetime:
    return datetime.now(TZ_BEIJING)


def _naive_bj() -> datetime:
    return _now_bj().replace(tzinfo=None)


def _local_bj(now: datetime | None = None) -> datetime:
    now = now or _now_bj()
    return now.astimezone(TZ_BEIJING).replace(tzinfo=None) if now.tzinfo else now


def _is_a_share_trading_day(now: datetime | None = None) -> bool:
    local = _local_bj(now)
    try:
        from signals.core.calendar.engine import get_calendar

        return bool(get_calendar().is_trading_day("SSE", local.date()))
    except Exception:
        return local.weekday() < 5


def _postmarket_trade_date(now: datetime | None = None) -> str:
    local = _local_bj(now)
    try:
        from signals.core.calendar.engine import get_calendar

        cal = get_calendar()
        d = local.date()
        if not cal.is_trading_day("SSE", d) or local.time() < dt_time(9, 30):
            d -= timedelta(days=1)
        while not cal.is_trading_day("SSE", d):
            d -= timedelta(days=1)
        return d.isoformat()
    except Exception:
        try:
            from signals.data.mongo_fallback import get_last_trading_day

            return str(get_last_trading_day("A"))[:10]
        except Exception:
            return local.date().isoformat()


def _previous_trading_date(now: datetime | None = None) -> str:
    local = _local_bj(now)
    d = local.date() - timedelta(days=1)
    try:
        from signals.core.calendar.engine import get_calendar

        cal = get_calendar()
        while not cal.is_trading_day("SSE", d):
            d -= timedelta(days=1)
        return d.isoformat()
    except Exception:
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.isoformat()


def _coerce_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=None) if parsed.tzinfo is None else parsed.astimezone(TZ_BEIJING).replace(tzinfo=None)
    return None


def _parse_hm(value: str, default: dt_time) -> dt_time:
    try:
        hour, minute = value.strip().split(":", 1)
        return dt_time(int(hour), int(minute))
    except Exception:
        return default


def _summarize_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"value": str(result)[:500]}
    allow = {
        "module",
        "status",
        "elapsed",
        "market",
        "lane",
        "inserted",
        "updated",
        "count",
        "stocks",
        "symbols",
        "spot_snapshot",
        "requested",
        "candidates",
        "skipped",
        "errors",
        "deferred",
        "cooling_down",
        "processed",
        "total",
        "expected_codes",
        "covered_codes",
        "coverage_pct",
        "progress_pct",
        "remaining",
        "next_cursor",
        "total_groups",
        "processed_groups",
        "sample_errors",
        "sample_deferred",
        "source_counts",
        "markets",
        "unmapped",
        "skipped_fresh",
        "skip_reason_counts",
        "original_groups",
        "incremental",
        "reason_counts",
        "result",
    }
    summary: dict[str, Any] = {}
    for key, value in result.items():
        if key not in allow:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value
        elif isinstance(value, dict):
            summary[key] = {str(k): v for k, v in list(value.items())[:20] if isinstance(v, (str, int, float, bool, type(None)))}
        elif isinstance(value, list):
            summary[key] = value[:20]
        else:
            summary[key] = str(value)[:300]
    return summary


def _result_sources(task_doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    summary = task_doc.get("result_summary") if isinstance(task_doc.get("result_summary"), dict) else {}
    nested = summary.get("result") if isinstance(summary.get("result"), dict) else {}
    return summary, nested


def _result_number(task_doc: dict[str, Any], key: str, default: float = 0.0) -> float:
    summary, nested = _result_sources(task_doc)
    value = nested.get(key, summary.get(key, default))
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _stock_daily_dependency_ok(task_doc: dict[str, Any]) -> bool:
    status = str(task_doc.get("status") or "pending")
    summary, nested = _result_sources(task_doc)
    result_status = str(nested.get("status") or summary.get("status") or "").lower()
    processed = _result_number(task_doc, "processed")
    total = _result_number(task_doc, "total")
    progress_pct = _result_number(task_doc, "progress_pct")
    errors = _result_number(task_doc, "errors")
    deferred = _result_number(task_doc, "deferred")
    coverage_pct = _result_number(task_doc, "coverage_pct")
    min_coverage = _env_float("SIGNALS_POSTMARKET_STOCK_DAILY_PARTIAL_MIN_COVERAGE", 40.0)
    max_errors = _env_int("SIGNALS_POSTMARKET_STOCK_DAILY_PARTIAL_MAX_ERRORS", 25, minimum=0)
    max_error_pct = _env_float("SIGNALS_POSTMARKET_STOCK_DAILY_PARTIAL_MAX_ERROR_PCT", 6.0)
    processed_all = bool(total > 0 and processed >= total)
    progress_done = bool(progress_pct >= 99.9)
    error_pct = (errors / total * 100.0) if total > 0 else 0.0
    sparse_errors_ok = bool(errors <= max_errors and error_pct <= max_error_pct)
    if status == "degraded" and (result_status != "ok" or deferred > 0):
        return False
    return sparse_errors_ok and (processed_all or progress_done) and coverage_pct >= min_coverage


def _dependency_status_ok(task_doc: dict[str, Any]) -> bool:
    status = str(task_doc.get("status") or "pending")
    if status in TASK_OK_STATUSES:
        return True
    if status not in {"partial", "degraded"}:
        return False
    if str(task_doc.get("module") or "") in {"stock_daily", "hk_stock_daily"}:
        return _stock_daily_dependency_ok(task_doc)
    return False


def _task_effectively_done(task_doc: dict[str, Any]) -> bool:
    """True when a previous task attempt is good enough to resume past."""
    return _dependency_status_ok(task_doc)


class PostmarketRunner:
    """Run the postmarket DAG and store task/run state in Mongo."""

    def __init__(self, engine, *, max_workers: int | None = None):
        self.engine = engine
        self.db = engine.db
        self.max_workers = max_workers or _env_int("SIGNALS_POSTMARKET_WORKERS", 8, minimum=1)
        self.module_semaphores = {
            "stock_daily": threading.BoundedSemaphore(_env_int("SIGNALS_POSTMARKET_STOCK_DAILY_WORKERS", 2, minimum=1)),
            "hk_stock_daily": threading.BoundedSemaphore(_env_int("SIGNALS_POSTMARKET_HK_STOCK_DAILY_WORKERS", 2, minimum=1)),
            "stock_30m_fullmarket": threading.BoundedSemaphore(_env_int("SIGNALS_POSTMARKET_STOCK_30M_WORKERS", 4, minimum=1)),
            "board_cons": threading.BoundedSemaphore(_env_int("SIGNALS_POSTMARKET_BOARD_CONS_WORKERS", 2, minimum=1)),
        }
        self.owner_pid = os.getpid()
        self.heartbeat_seconds = _env_int("SIGNALS_POSTMARKET_HEARTBEAT_SECONDS", 60, minimum=10)
        self.task_stale_seconds = _env_int("SIGNALS_POSTMARKET_TASK_STALE_SECONDS", 15 * 60, minimum=60)

    @staticmethod
    def _pid_alive(pid: Any) -> bool:
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            return False
        if pid_int <= 0:
            return False
        try:
            os.kill(pid_int, 0)
        except OSError:
            return False
        return True

    @staticmethod
    def _task_id(run_id: str, spec: PostmarketTaskSpec) -> str:
        return f"{run_id}:{spec.task_key}"

    def _get_task(self, run_id: str, spec: PostmarketTaskSpec) -> dict[str, Any]:
        return self.db["sync_tasks"].find_one({"_id": self._task_id(run_id, spec)}) or {}

    def _task_status(self, run_id: str, task_key: str) -> str:
        doc = self.db["sync_tasks"].find_one({"_id": f"{run_id}:{task_key}"}, {"status": 1}) or {}
        return str(doc.get("status") or "pending")

    def _dependencies_ok(self, run_id: str, spec: PostmarketTaskSpec) -> bool:
        for dep in spec.depends_on:
            doc = self.db["sync_tasks"].find_one({"_id": f"{run_id}:{dep}"}) or {}
            if not _dependency_status_ok(doc):
                return False
        return True

    def _init_run(self, run_id: str, trade_date: str) -> None:
        now = _naive_bj()
        self.db["sync_runs"].update_one(
            {"_id": run_id},
            {
                "$set": {
                    "run_id": run_id,
                    "trade_date": trade_date,
                    "status": "running",
                    "owner_pid": self.owner_pid,
                    "heartbeat_at": now,
                    "updated_at": now,
                    "task_count": len(POSTMARKET_TASKS),
                },
                "$setOnInsert": {
                    "started_at": now,
                    "phase": "",
                    "finished_at": None,
                },
            },
            upsert=True,
        )

    def _init_tasks(self, run_id: str, trade_date: str) -> None:
        now = _naive_bj()
        current_ids: set[str] = set()
        for order, spec in enumerate(POSTMARKET_TASKS):
            task_id = self._task_id(run_id, spec)
            current_ids.add(task_id)
            current = self.db["sync_tasks"].find_one(
                {"_id": task_id},
                {"phase": 1, "depends_on": 1, "status": 1},
            ) or {}
            spec_changed = bool(current) and (
                str(current.get("phase") or "") != spec.phase
                or tuple(current.get("depends_on") or ()) != tuple(spec.depends_on)
                or bool(current.get("blocks_run", True)) != spec.blocks_run
            )
            set_values: dict[str, Any] = {
                "run_id": run_id,
                "trade_date": trade_date,
                "module": spec.module,
                "task_key": spec.task_key,
                "phase": spec.phase,
                "shard_key": spec.shard_key,
                "depends_on": list(spec.depends_on),
                "blocks_run": spec.blocks_run,
                "order": order,
                "updated_at": now,
            }
            if spec_changed:
                set_values.update({
                    "status": "pending",
                    "owner_pid": "",
                    "cursor": {},
                    "result_summary": {},
                    "error_msg": "task_spec_changed",
                    "started_at": None,
                    "finished_at": None,
                })
            update_doc: dict[str, Any] = {"$set": set_values}
            if not current:
                update_doc["$setOnInsert"] = {
                    "status": "pending",
                    "attempts": 0,
                    "cursor": {},
                    "result_summary": {},
                    "error_msg": "",
                    "started_at": None,
                    "finished_at": None,
                }
            self.db["sync_tasks"].update_one({"_id": task_id}, update_doc, upsert=True)
        for doc in self.db["sync_tasks"].find({"run_id": run_id}, {"_id": 1, "status": 1}):
            task_id = str(doc.get("_id") or "")
            if task_id in current_ids:
                continue
            if str(doc.get("status") or "") == "obsolete":
                continue
            self.db["sync_tasks"].update_one(
                {"_id": task_id},
                {"$set": {
                    "status": "obsolete",
                    "owner_pid": "",
                    "error_msg": "superseded_by_sharded_postmarket_dag",
                    "updated_at": now,
                }},
            )

    def _heartbeat(self, run_id: str, phase: str = "") -> None:
        now = _naive_bj()
        update = {
            "owner_pid": self.owner_pid,
            "heartbeat_at": now,
            "updated_at": now,
        }
        if phase:
            update["phase"] = phase
        self.db["sync_runs"].update_one({"_id": run_id}, {"$set": update}, upsert=True)

    def _heartbeat_running_tasks(self, run_id: str, phase: str = "") -> int:
        now = _naive_bj()
        query = {"run_id": run_id, "status": "running", "owner_pid": self.owner_pid}
        if phase:
            query["phase"] = phase
        task_ids = [
            doc.get("_id")
            for doc in self.db["sync_tasks"].find(query, {"_id": 1})
            if doc.get("_id")
        ]
        for task_id in task_ids:
            self.db["sync_tasks"].update_one(
                {"_id": task_id},
                {"$set": {"heartbeat_at": now, "updated_at": now}},
            )
        return len(task_ids)

    def _release_stale_running_tasks(self, run_id: str) -> int:
        now = _naive_bj()
        released = 0
        for doc in self.db["sync_tasks"].find({"run_id": run_id, "status": "running"}):
            heartbeat = _coerce_dt(doc.get("heartbeat_at") or doc.get("started_at"))
            owner_dead = bool(doc.get("owner_pid")) and not self._pid_alive(doc.get("owner_pid"))
            too_old = bool(heartbeat and (now - heartbeat).total_seconds() > self.task_stale_seconds)
            if not (owner_dead or too_old):
                continue
            reason = "running_owner_dead" if owner_dead else "stale_running_task"
            self.db["sync_tasks"].update_one(
                {"_id": doc["_id"]},
                {"$set": {
                    "status": "stale",
                    "owner_pid": "",
                    "error_msg": reason,
                    "updated_at": now,
                }},
            )
            released += 1
        return released

    def _mark_task_started(self, run_id: str, spec: PostmarketTaskSpec) -> None:
        now = _naive_bj()
        self.db["sync_tasks"].update_one(
            {"_id": self._task_id(run_id, spec)},
            {
                "$set": {
                    "status": "running",
                    "owner_pid": self.owner_pid,
                    "started_at": now,
                    "heartbeat_at": now,
                    "updated_at": now,
                    "error_msg": "",
                },
                "$inc": {"attempts": 1},
            },
        )

    def _mark_task_finished(self, run_id: str, spec: PostmarketTaskSpec, result: dict[str, Any]) -> None:
        now = _naive_bj()
        status = str(result.get("status") or "ok")
        result_summary = _summarize_result(result)
        cursor = {}
        nested = result.get("result") if isinstance(result.get("result"), dict) else {}
        for source in (result, nested):
            if source.get("next_cursor") is not None:
                cursor["next_cursor"] = source.get("next_cursor")
            if source.get("remaining") is not None:
                cursor["remaining"] = source.get("remaining")
            if source.get("processed") is not None:
                cursor["processed"] = source.get("processed")
            if source.get("total") is not None:
                cursor["total"] = source.get("total")
            if source.get("total_groups") is not None:
                cursor["total_groups"] = source.get("total_groups")
            if source.get("progress_pct") is not None:
                cursor["progress_pct"] = source.get("progress_pct")
        self.db["sync_tasks"].update_one(
            {"_id": self._task_id(run_id, spec)},
            {"$set": {
                "status": status,
                "owner_pid": "",
                "heartbeat_at": now,
                "finished_at": now,
                "updated_at": now,
                "cursor": cursor,
                "result_summary": result_summary,
                "error_msg": str(result.get("error") or result.get("error_msg") or "")[:1000],
            }},
        )

    def _with_env(self, env: dict[str, str]):
        class EnvGuard:
            def __init__(self, values: dict[str, str]):
                self.values = values
                self.guard = None

            def __enter__(self):
                self.guard = task_env(self.values)
                return self.guard.__enter__()

            def __exit__(self, exc_type, exc, tb):
                if self.guard is not None:
                    return self.guard.__exit__(exc_type, exc, tb)
                return None

        return EnvGuard(env)

    def _run_task(self, run_id: str, spec: PostmarketTaskSpec) -> dict[str, Any]:
        if spec.module not in self.engine.module_map:
            self._mark_task_started(run_id, spec)
            result = {"module": spec.module, "status": "error", "error": "module_missing"}
            self._mark_task_finished(run_id, spec, result)
            return result
        fn, _schedule = self.engine.module_map[spec.module]
        plan = LANE_MAINTENANCE_PLANS.get(spec.module)
        semaphore = self.module_semaphores.get(spec.module)
        with self._with_env(spec.env):
            if semaphore is None:
                self._mark_task_started(run_id, spec)
                result = self.engine.run_module(spec.module, fn, plan=plan)
            else:
                with semaphore:
                    self._mark_task_started(run_id, spec)
                    result = self.engine.run_module(spec.module, fn, plan=plan)
        self._mark_task_finished(run_id, spec, result)
        return result

    def _phase_specs(self, phase: str) -> list[PostmarketTaskSpec]:
        return [task for task in POSTMARKET_TASKS if task.phase == phase]

    def _has_retryable_incomplete_tasks(self, run_id: str, *, include_optional: bool = False) -> bool:
        blocking_task_keys = {task.task_key for task in POSTMARKET_TASKS if task.blocks_run}
        for doc in self.db["sync_tasks"].find({"run_id": run_id}, {"task_key": 1, "status": 1}):
            if not include_optional and str(doc.get("task_key") or "") not in blocking_task_keys:
                continue
            status = str(doc.get("status") or "pending")
            if status in RETRYABLE_TASK_STATUSES and not _task_effectively_done(doc):
                return True
        return False

    def _minute_preheat_pending_count(self, trade_date: str) -> int:
        try:
            rows = self.db["minute_preheat_universe"].find({"trade_date": trade_date}, {"status": 1})
        except Exception:
            return 0
        return sum(1 for row in rows if str(row.get("status") or "pending") in {"pending", "error", "stale"})

    def _continue_minute_preheat_universe(self, trade_date: str, run_id: str) -> int:
        if not _env_bool("SIGNALS_POSTMARKET_CONTINUE_MINUTE_PREHEAT", True):
            return 0
        before = self._minute_preheat_pending_count(trade_date)
        if before <= 0 or "stock_minute" not in self.engine.module_map:
            return 0

        spec = next(
            (task for task in POSTMARKET_TASKS if task.module == "stock_minute" and task.shard_key == "all"),
            None,
        )
        env = dict(spec.env if spec else {})
        env.setdefault("STOCK_MINUTE_SCOPE", "postmarket_candidates")
        env.setdefault("STOCK_MINUTE_FREQS", "5min,15min")
        env.setdefault("STOCK_MINUTE_POSTMARKET_MAX_CODES", "240")
        env.setdefault("STOCK_MINUTE_WORKERS", "6")
        env.setdefault("STOCK_MINUTE_CALL_INTERVAL", "0.15")
        continue_cap = os.getenv("SIGNALS_POSTMARKET_CONTINUE_MINUTE_MAX_CODES")
        if continue_cap:
            env["STOCK_MINUTE_POSTMARKET_MAX_CODES"] = continue_cap

        batches = _env_int("SIGNALS_POSTMARKET_CONTINUE_MINUTE_BATCHES", 1, minimum=1)
        fn, _schedule = self.engine.module_map["stock_minute"]
        completed = 0
        for batch in range(batches):
            pending_before = self._minute_preheat_pending_count(trade_date)
            if pending_before <= 0:
                break
            logger.info(
                "postmarket continue minute preheat run=%s batch=%d pending=%d",
                run_id,
                batch + 1,
                pending_before,
            )
            with self._with_env(env):
                self.engine.run_module("stock_minute", fn, plan=LANE_MAINTENANCE_PLANS.get("stock_minute"))
            pending_after = self._minute_preheat_pending_count(trade_date)
            completed += max(0, pending_before - pending_after)
            self.db["sync_runs"].update_one(
                {"_id": run_id},
                {"$set": {
                    "minute_preheat_pending": pending_after,
                    "minute_preheat_continued_at": _naive_bj(),
                    "updated_at": _naive_bj(),
                }},
                upsert=True,
            )
            if pending_after <= 0 or pending_after >= pending_before:
                break
        return completed

    def run_once(self, *, resume_run_id: str | None = None, trade_date: str | None = None, force: bool = False) -> dict[str, Any]:
        trade_date = trade_date or _postmarket_trade_date()
        run_id = resume_run_id or default_run_id(trade_date)
        run_doc = self.db["sync_runs"].find_one({"_id": run_id}) or {}
        if run_doc.get("trade_date"):
            trade_date = str(run_doc["trade_date"])
        continue_terminal_optional = (
            _env_bool("SIGNALS_POSTMARKET_RUN_OPTIONAL_TASKS", False)
            and self._has_retryable_incomplete_tasks(run_id, include_optional=True)
        )
        if run_doc.get("status") in RUN_TERMINAL_STATUSES and not force and not continue_terminal_optional:
            return {"run_id": run_id, "trade_date": trade_date, "status": run_doc.get("status"), "skipped": True}

        self._init_run(run_id, trade_date)
        self._init_tasks(run_id, trade_date)
        released = self._release_stale_running_tasks(run_id)
        if released:
            logger.warning("postmarket released stale tasks: run=%s released=%d", run_id, released)

        results: list[dict[str, Any]] = []
        run_optional_tasks = _env_bool("SIGNALS_POSTMARKET_RUN_OPTIONAL_TASKS", False)
        blocked: set[str] = set()
        for phase in POSTMARKET_PHASES:
            phase_specs = self._phase_specs(phase)
            if not run_optional_tasks:
                phase_specs = [spec for spec in phase_specs if spec.blocks_run]
            if not phase_specs:
                continue
            attempted: set[str] = set()
            while True:
                self._heartbeat(run_id, phase)
                runnable: list[PostmarketTaskSpec] = []
                waiting: set[str] = set()
                for spec in phase_specs:
                    current = self._get_task(run_id, spec)
                    status = str(current.get("status") or "pending")
                    task_id = self._task_id(run_id, spec)
                    if _task_effectively_done(current) and not force:
                        continue
                    if task_id in attempted:
                        continue
                    if not self._dependencies_ok(run_id, spec):
                        waiting.add(spec.task_key)
                        continue
                    if status not in RETRYABLE_TASK_STATUSES and not force:
                        continue
                    runnable.append(spec)

                if not runnable:
                    blocked.update(waiting)
                    break

                logger.info("postmarket phase=%s tasks=%s workers=%d", phase, [item.task_key for item in runnable], self.max_workers)
                workers = min(self.max_workers, max(1, len(runnable)))
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"postmarket-{phase}") as executor:
                    future_map = {executor.submit(self._run_task, run_id, spec): spec for spec in runnable}
                    pending = set(future_map)
                    while pending:
                        done, pending = wait(
                            pending,
                            timeout=self.heartbeat_seconds,
                            return_when=FIRST_COMPLETED,
                        )
                        self._heartbeat(run_id, phase)
                        self._heartbeat_running_tasks(run_id, phase)
                        if not done:
                            continue
                        for future in done:
                            spec = future_map[future]
                            attempted.add(self._task_id(run_id, spec))
                            blocked.discard(spec.task_key)
                            try:
                                results.append(future.result())
                            except Exception as exc:
                                result = {"module": spec.module, "status": "error", "error": f"{exc.__class__.__name__}: {exc}"}
                                self._mark_task_finished(run_id, spec, result)
                                results.append(result)

        task_docs = list(self.db["sync_tasks"].find(
            {"run_id": run_id},
            {"module": 1, "task_key": 1, "status": 1, "phase": 1, "updated_at": 1, "result_summary": 1},
        ))
        blocking_task_keys = {task.task_key for task in POSTMARKET_TASKS if task.blocks_run}
        incomplete = [
            doc
            for doc in task_docs
            if str(doc.get("task_key") or "") in blocking_task_keys and not _task_effectively_done(doc)
        ]
        optional_incomplete = [
            doc
            for doc in task_docs
            if str(doc.get("task_key") or "") not in blocking_task_keys and not _task_effectively_done(doc)
        ]
        blocked_critical = {task_key for task_key in blocked if task_key in blocking_task_keys}
        blocked_optional = {task_key for task_key in blocked if task_key not in blocking_task_keys}
        status = "ok" if not incomplete and not blocked_critical else "partial"
        now = _naive_bj()
        self.db["sync_runs"].update_one(
            {"_id": run_id},
            {"$set": {
                "status": status,
                "owner_pid": "",
                "heartbeat_at": now,
                "updated_at": now,
                "finished_at": now,
                "blocked_tasks": sorted(blocked_critical),
                "optional_blocked_tasks": sorted(blocked_optional),
                "ok_tasks": len(task_docs) - len(incomplete),
                "incomplete_tasks": len(incomplete),
                "optional_incomplete_tasks": len(optional_incomplete),
            }},
        )
        return {
            "run_id": run_id,
            "trade_date": trade_date,
            "status": status,
            "results": results,
            "blocked_tasks": sorted(blocked_critical),
            "optional_blocked_tasks": sorted(blocked_optional),
            "ok_tasks": len(task_docs) - len(incomplete),
            "incomplete_tasks": len(incomplete),
            "optional_incomplete_tasks": len(optional_incomplete),
        }

    @staticmethod
    def should_run_now(now: datetime | None = None) -> bool:
        now = now or _now_bj()
        if not _is_a_share_trading_day(now):
            return False
        start = _parse_hm(os.getenv("SIGNALS_POSTMARKET_START_TIME", "16:10"), dt_time(16, 10))
        end = _parse_hm(os.getenv("SIGNALS_POSTMARKET_END_TIME", "23:50"), dt_time(23, 50))
        current = _local_bj(now).time()
        return start <= current <= end

    @staticmethod
    def should_catchup_now(now: datetime | None = None) -> bool:
        if not _env_bool("SIGNALS_POSTMARKET_CATCHUP_ENABLED", True):
            return False
        start = _parse_hm(os.getenv("SIGNALS_POSTMARKET_CATCHUP_START_TIME", "00:00"), dt_time(0, 0))
        end = _parse_hm(os.getenv("SIGNALS_POSTMARKET_CATCHUP_END_TIME", "15:00"), dt_time(15, 0))
        current = _local_bj(now).time()
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end

    def catchup_target(self, now: datetime | None = None) -> tuple[str, str, str] | None:
        if not self.should_catchup_now(now):
            return None
        trade_date = _previous_trading_date(now)
        run_id = default_run_id(trade_date)
        run_doc = self.db["sync_runs"].find_one({"_id": run_id}, {"status": 1, "trade_date": 1}) or {}
        status = str(run_doc.get("status") or "missing")
        if status in RUN_TERMINAL_STATUSES:
            return None
        return run_id, trade_date, status

    def run_daemon(self, *, check_seconds: int | None = None) -> None:
        check_seconds = check_seconds or _env_int("SIGNALS_POSTMARKET_CHECK_SECONDS", 300, minimum=30)
        logger.info("postmarket daemon started workers=%d check_seconds=%d", self.max_workers, check_seconds)
        while True:
            now = _now_bj()
            if self.should_run_now(now):
                trade_date = _postmarket_trade_date(now)
                run_id = default_run_id(trade_date)
                run_doc = self.db["sync_runs"].find_one({"_id": run_id}, {"status": 1}) or {}
                status = run_doc.get("status")
                if status not in RUN_TERMINAL_STATUSES or _env_bool("SIGNALS_POSTMARKET_FORCE", False):
                    logger.info("postmarket trigger run=%s previous_status=%s", run_id, status or "missing")
                    self.run_once(trade_date=trade_date, force=_env_bool("SIGNALS_POSTMARKET_FORCE", False))
                else:
                    self._continue_minute_preheat_universe(trade_date, run_id)
                    if (
                        _env_bool("SIGNALS_POSTMARKET_CONTINUE_OPTIONAL_TASKS", True)
                        and self._has_retryable_incomplete_tasks(run_id, include_optional=True)
                    ):
                        logger.info("postmarket continue optional tasks run=%s", run_id)
                        with self._with_env({"SIGNALS_POSTMARKET_RUN_OPTIONAL_TASKS": "true"}):
                            self.run_once(resume_run_id=run_id, trade_date=trade_date)
            else:
                target = self.catchup_target(now)
                if target:
                    run_id, trade_date, status = target
                    logger.info("postmarket catchup trigger run=%s previous_status=%s", run_id, status)
                    self.run_once(resume_run_id=run_id, trade_date=trade_date)
            time.sleep(check_seconds)
