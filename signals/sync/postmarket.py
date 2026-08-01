# -*- coding: utf-8 -*-
"""Postmarket DAG runner with Mongo-backed resume/checkpoint state."""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
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
FULLMARKET_SPOT_TASK_KEY = "fullmarket_spot_snapshot:all"
ETF_SPOT_TASK_KEY = "etf_spot_snapshot:all"
SOURCE_FALLBACK_MODULES = {"quote_snapshots", "stock_daily"}
SOURCE_BLOCKED_STATUSES = {"degraded", "error", "stale"}
RUNTIME_USABLE_DEGRADED_MODULES = {
    "market_pools",
    "stock_minute",
    "index_minute",
    "chain_heat_snapshots",
    "board_ranking",
    "board_cons",
}
RUNTIME_USABLE_OUTPUT_KEYS = (
    "count",
    "written",
    "modified",
    "inserted",
    "updated",
    "compat_written",
    "refreshed",
    "deleted",
    "ticks",
    "nodes",
    "checked",
    "processed",
    "processed_groups",
)


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
            depends_on=(FULLMARKET_SPOT_TASK_KEY, ETF_SPOT_TASK_KEY),
            env={
                "SIGNALS_SYNC_FULL_STOCK_DAILY": "true",
                "STOCK_DAILY_SCOPE": "all",
                "STOCK_DAILY_TODAY_ONLY": "true",
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
            # Start each 30m shard as soon as its matching daily shard is
            # usable.  Waiting for all 16 daily shards lets one slow/orphaned
            # shard block the entire full-market minute cache.
            depends_on=(f"stock_daily:shard_{idx:02d}",),
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
    PostmarketTaskSpec("etf_spot_snapshot", "market_data"),
    PostmarketTaskSpec("market_pools", "market_data"),
    PostmarketTaskSpec("market_limit_pools", "market_data"),
    PostmarketTaskSpec("board_heat_minute", "market_data"),
    PostmarketTaskSpec("concept_heat_minute", "market_data"),
    PostmarketTaskSpec("index_minute", "market_data"),
    PostmarketTaskSpec("quote_snapshots", "market_data", depends_on=(FULLMARKET_SPOT_TASK_KEY, ETF_SPOT_TASK_KEY)),
    PostmarketTaskSpec("index_daily", "market_data", depends_on=("quote_snapshots:all",)),
    *_STOCK_DAILY_TASKS,
    PostmarketTaskSpec("board_ranking", "market_data"),
    *_BOARD_CONS_TASKS,
    PostmarketTaskSpec("chain_heat_snapshots", "chain_context", depends_on=("board_ranking:all",)),
    PostmarketTaskSpec(
        "security_business_facts",
        "chain_context",
        depends_on=("fullmarket_spot_snapshot:all", "board_ranking:all", *_BOARD_CONS_DEPS),
        env={
            "SECURITY_BUSINESS_FACT_MAX_CODES": "80",
            "SECURITY_BUSINESS_FACT_CALL_INTERVAL": "0.8",
        },
        blocks_run=False,
    ),
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
    PostmarketTaskSpec(
        "weekly_rollup",
        "derived",
        depends_on=(*_STOCK_DAILY_DEPS, "index_daily:all"),
        env={
            "WEEKLY_ROLLUP_SCOPE": "postmarket_candidates",
            "WEEKLY_ROLLUP_MAX_SYMBOLS": "300",
        },
    ),
    PostmarketTaskSpec(
        "ma_climb_scan",
        "derived",
        depends_on=(*_STOCK_DAILY_DEPS,),
    ),
    PostmarketTaskSpec(
        "technical_signal_scan",
        "derived",
        depends_on=(*_STOCK_DAILY_DEPS, "weekly_rollup:all"),
        env={
            "TECHNICAL_SIGNAL_SCAN_SCOPE": "postmarket_candidates",
            "TECHNICAL_SIGNAL_SCAN_MARKETS": "A",
            "TECHNICAL_SIGNAL_SCAN_REQUIRED_FREQS": "日线,周线",
            "TECHNICAL_SIGNAL_SCAN_OPTIONAL_FREQS": "30分钟,15分钟,5分钟",
            "TECHNICAL_SIGNAL_POSTMARKET_MAX_SYMBOLS": "300",
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
        "hot_rank_clues",
        "derived",
        depends_on=("technical_signal_scan:all", "ma_climb_scan:all"),
    ),
    PostmarketTaskSpec(
        "sector_transition_rollup",
        "derived",
        depends_on=(
            "technical_signal_scan:all",
            "ma_climb_scan:all",
            "chain_heat_snapshots:all",
            "board_heat_minute:all",
            "concept_heat_minute:all",
            ETF_SPOT_TASK_KEY,
        ),
    ),
    PostmarketTaskSpec(
        "terminal_realtime_pool",
        "terminal",
        depends_on=("technical_signal_scan:all", "ma_climb_scan:all", "knowledge_market_views:all", "postmarket_chain_rebuild:all", "chain_heat_snapshots:all", "concept_relationship_graph:all", "sector_transition_rollup:all", "hot_rank_clues:all"),
        env={"TERMINAL_POOL_STRICT_SOURCES": "true"},
    ),
    PostmarketTaskSpec("strategy_snapshot", "terminal", depends_on=("terminal_realtime_pool:all", ETF_SPOT_TASK_KEY)),
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
    PostmarketTaskSpec(
        "minute_readiness_probe",
        "minute_preheat",
        depends_on=("stock_minute:all", "index_minute:all"),
        env={"MINUTE_READINESS_SELECTION_META_ID": "stock_minute:postmarket_selection:_meta"},
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
        "modified",
        "updated",
        "written",
        "compat_written",
        "refreshed",
        "deleted",
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
        "planned_calls",
        "empty",
        "empty_calls",
        "failed_calls",
        "not_ready",
        "source_counts",
        "markets",
        "unmapped",
        "skipped_fresh",
        "skip_reason_counts",
        "original_groups",
        "incremental",
        "reason_counts",
        "source_fallback",
        "partial_usable",
        "degraded_dependencies",
        "recovery_state",
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


def _number(value: Any, default: float = 0.0) -> float:
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


def _quote_snapshots_dependency_ok(task_doc: dict[str, Any]) -> bool:
    summary, nested = _result_sources(task_doc)
    count = _result_number(task_doc, "count")
    live = _result_number(task_doc, "live")
    errors = _result_number(task_doc, "errors")
    if count <= 0:
        return False
    coverage_pct = live / count * 100.0
    min_coverage = _env_float("SIGNALS_POSTMARKET_QUOTE_PARTIAL_MIN_COVERAGE", 50.0)
    max_error_pct = _env_float("SIGNALS_POSTMARKET_QUOTE_PARTIAL_MAX_ERROR_PCT", 50.0)
    error_pct = errors / count * 100.0
    return coverage_pct >= min_coverage and error_pct <= max_error_pct


def _runtime_degraded_result_usable(task_doc: dict[str, Any]) -> bool:
    module = str(task_doc.get("module") or "")
    if module not in RUNTIME_USABLE_DEGRADED_MODULES:
        return False
    if _result_number(task_doc, "failed_calls") > 0 or _result_number(task_doc, "errors") > 0:
        return False
    if _result_number(task_doc, "not_ready") > 0:
        return False
    planned = _result_number(task_doc, "planned_calls")
    empty = _result_number(task_doc, "empty_calls") or _result_number(task_doc, "empty")
    if planned > 0 and empty >= planned:
        return False
    return any(_result_number(task_doc, key) > 0 for key in RUNTIME_USABLE_OUTPUT_KEYS)


def _stale_finished_result_usable(task_doc: dict[str, Any]) -> bool:
    if str(task_doc.get("status") or "") != "stale":
        return False
    module = str(task_doc.get("module") or "")
    if module in {"stock_daily", "hk_stock_daily"}:
        return _stock_daily_dependency_ok({**task_doc, "status": "partial"})
    summary, nested = _result_sources(task_doc)
    result_status = str(nested.get("status") or summary.get("status") or "").lower()
    if result_status not in TASK_OK_STATUSES:
        return False
    processed = _result_number(task_doc, "processed")
    total = _result_number(task_doc, "total")
    progress_pct = _result_number(task_doc, "progress_pct")
    coverage_pct = _result_number(task_doc, "coverage_pct")
    output_seen = any(_result_number(task_doc, key) > 0 for key in RUNTIME_USABLE_OUTPUT_KEYS)
    done = bool((total > 0 and processed >= total) or progress_pct >= 99.9 or coverage_pct >= 99.9)
    return output_seen and done


def _dependency_status_ok(task_doc: dict[str, Any]) -> bool:
    status = str(task_doc.get("status") or "pending")
    module = str(task_doc.get("module") or "")
    if module in {"stock_daily", "hk_stock_daily"} and status in {"partial", "degraded", "stale", "running"}:
        effective_status = status if status in {"partial", "degraded"} else "partial"
        return _stock_daily_dependency_ok({**task_doc, "status": effective_status})
    if status in TASK_OK_STATUSES:
        return True
    if _stale_finished_result_usable(task_doc):
        return True
    if status not in {"partial", "degraded"}:
        return False
    if module == "quote_snapshots":
        return _quote_snapshots_dependency_ok(task_doc)
    if _runtime_degraded_result_usable(task_doc):
        return True
    return False


def _task_effectively_done(task_doc: dict[str, Any]) -> bool:
    """True when a previous task attempt is good enough to resume past."""
    return _dependency_status_ok(task_doc)


def _source_dependency_blocked(task_doc: dict[str, Any]) -> bool:
    status = str(task_doc.get("status") or "pending")
    if status in TASK_OK_STATUSES or status in {"pending", "running"}:
        return False
    return status in SOURCE_BLOCKED_STATUSES or bool(task_doc.get("error_msg"))


def _source_fallback_usable(spec: PostmarketTaskSpec, result: dict[str, Any]) -> bool:
    result_summary = _summarize_result(result)
    task_doc = {"module": spec.module, "status": "partial", "result_summary": result_summary}
    if spec.module == "stock_daily":
        return _stock_daily_dependency_ok(task_doc)
    if spec.module == "quote_snapshots":
        return _quote_snapshots_dependency_ok({
            **task_doc,
            "result_summary": {
                **result_summary,
                "partial_usable": True,
            },
        })
    return False


def _source_blocker_from_task(task_doc: dict[str, Any]) -> dict[str, Any]:
    summary, nested = _result_sources(task_doc)
    error_msg = (
        str(task_doc.get("error_msg") or "")
        or str(summary.get("reason") or summary.get("error") or summary.get("error_msg") or "")
        or str(nested.get("reason") or nested.get("error") or nested.get("error_msg") or "")
    )
    return {
        "scope": "postmarket_backfill",
        "module": "fullmarket_spot_snapshot",
        "provider": "eastmoney",
        "endpoint": "fullmarket_spot_snapshot",
        "task_key": FULLMARKET_SPOT_TASK_KEY,
        "status": str(task_doc.get("status") or "pending"),
        "error_msg": error_msg[:1000],
    }


class PostmarketRunner:
    """Run the postmarket DAG and store task/run state in Mongo."""

    def __init__(self, engine, *, max_workers: int | None = None):
        self.engine = engine
        self.db = engine.db
        default_workers = min(16, max(8, (os.cpu_count() or 8)))
        self.max_workers = max_workers or _env_int("SIGNALS_POSTMARKET_WORKERS", default_workers, minimum=1)
        self.module_semaphores = {
            "stock_daily": threading.BoundedSemaphore(_env_int("SIGNALS_POSTMARKET_STOCK_DAILY_WORKERS", 2, minimum=1)),
            "hk_stock_daily": threading.BoundedSemaphore(_env_int("SIGNALS_POSTMARKET_HK_STOCK_DAILY_WORKERS", 2, minimum=1)),
            "stock_30m_fullmarket": threading.BoundedSemaphore(_env_int("SIGNALS_POSTMARKET_STOCK_30M_WORKERS", 4, minimum=1)),
            "board_cons": threading.BoundedSemaphore(_env_int("SIGNALS_POSTMARKET_BOARD_CONS_WORKERS", 2, minimum=1)),
        }
        self.owner_pid = os.getpid()
        self.heartbeat_seconds = _env_int("SIGNALS_POSTMARKET_HEARTBEAT_SECONDS", 60, minimum=10)
        self.task_stale_seconds = _env_int("SIGNALS_POSTMARKET_TASK_STALE_SECONDS", 15 * 60, minimum=60)
        self.close_seal = None
        if _env_bool("SIGNALS_CLOSE_SEAL_ENABLED", False):
            from .close_seal import CloseSealRunner

            self.close_seal = CloseSealRunner(self.db, self._run_close_seal_module, owner=f"pid:{self.owner_pid}")

    def _run_close_seal_module(self, module: str, trade_date: str) -> dict[str, Any]:
        if module not in self.engine.module_map:
            return {"module": module, "status": "error", "error": "module_missing"}
        fn, _schedule = self.engine.module_map[module]
        plan = LANE_MAINTENANCE_PLANS.get(module)
        with self._with_env(
            {
                "SIGNALS_CLOSE_SEAL_RUN_ID": f"close_seal:{trade_date}",
                "SIGNALS_POSTMARKET_TRADE_DATE": trade_date,
            }
        ):
            # Keep the A-market sync watermark aligned with the close-seal
            # task.  The formal gate reads the close-seal run itself, while
            # operations dashboards should not retain an older A metadata
            # row from an isolated historical backfill.
            return self.engine.run_module(module, fn, market="A", plan=plan)

    def _stable_close_seal_ready(self, trade_date: str) -> bool:
        from .close_seal import SEAL_MODULES, seal_result_usable

        seal_run_id = f"close_seal:{trade_date}"
        seal = self.db["sync_runs"].find_one(
            {"_id": seal_run_id},
            {"status": 1, "close_finality": 1},
        ) or {}
        if seal.get("status") != "sealed" or seal.get("close_finality") != "stable_close":
            return False
        for module in SEAL_MODULES:
            task = self.db["sync_tasks"].find_one(
                {"_id": f"{seal_run_id}:{module}:all", "status": "ok"},
                {"result_summary": 1},
            ) or {}
            usable, _reason = seal_result_usable(module, task.get("result_summary") or {}, trade_date)
            if not usable:
                return False
        return True

    def _apply_close_seal_handoff(self, run_id: str, trade_date: str) -> int:
        from .close_seal import HANDOFF_MODULES, seal_result_usable

        seal_run_id = f"close_seal:{trade_date}"
        seal = self.db["sync_runs"].find_one(
            {"_id": seal_run_id},
            {"status": 1, "close_finality": 1, "sealed_at": 1},
        ) or {}
        if seal.get("status") != "sealed" or seal.get("close_finality") != "stable_close":
            return 0
        reused = 0
        now = _naive_bj()
        for spec in POSTMARKET_TASKS:
            if spec.module not in HANDOFF_MODULES:
                continue
            source = self.db["sync_tasks"].find_one(
                {"_id": f"{seal_run_id}:{spec.module}:all", "status": "ok"},
                {"result_summary": 1, "finished_at": 1},
            ) or {}
            if not source:
                continue
            usable, _reason = seal_result_usable(spec.module, source.get("result_summary") or {}, trade_date)
            if not usable:
                continue
            task_id = self._task_id(run_id, spec)
            current = self.db["sync_tasks"].find_one({"_id": task_id}, {"status": 1}) or {}
            if _task_effectively_done({**current, "module": spec.module}):
                continue
            update_result = self.db["sync_tasks"].update_one(
                {
                    "_id": task_id,
                    "status": {"$in": ["pending", "stale", "partial", "degraded", "error", "deferred"]},
                    "owner_pid": {"$in": ["", None]},
                },
                {
                    "$set": {
                        "status": "ok",
                        "result_summary": {
                            **(source.get("result_summary") or {}),
                            "reused_from_close_seal": True,
                            "close_seal_run_id": seal_run_id,
                        },
                        "owner_pid": "",
                        "started_at": source.get("finished_at") or seal.get("sealed_at"),
                        "finished_at": source.get("finished_at") or seal.get("sealed_at"),
                        "updated_at": now,
                        "error_msg": "",
                    }
                },
            )
            modified = getattr(update_result, "modified_count", None)
            if modified is None:
                verified = self.db["sync_tasks"].find_one({"_id": task_id}, {"result_summary": 1}) or {}
                modified = bool((verified.get("result_summary") or {}).get("reused_from_close_seal"))
            reused += int(bool(modified))
        if reused:
            self.db["sync_runs"].update_one(
                {"_id": seal_run_id},
                {"$set": {"postmarket_handoff_at": now, "postmarket_run_id": run_id, "updated_at": now}},
            )
        return reused

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
        # ``kill(pid, 0)`` also succeeds for a zombie on macOS.  A finished
        # detached minute child can therefore block every later rotation
        # unless its process state is checked explicitly.
        try:
            state = subprocess.check_output(
                ["ps", "-o", "stat=", "-p", str(pid_int)],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            return False
        return bool(state) and not state.startswith("Z")

    @staticmethod
    def _task_id(run_id: str, spec: PostmarketTaskSpec) -> str:
        return f"{run_id}:{spec.task_key}"

    def _get_task(self, run_id: str, spec: PostmarketTaskSpec) -> dict[str, Any]:
        return self.db["sync_tasks"].find_one({"_id": self._task_id(run_id, spec)}) or {}

    def _task_status(self, run_id: str, task_key: str) -> str:
        doc = self.db["sync_tasks"].find_one({"_id": f"{run_id}:{task_key}"}, {"status": 1}) or {}
        return str(doc.get("status") or "pending")

    def _soft_dependency_allowed(self, spec: PostmarketTaskSpec, dep: str, dep_doc: dict[str, Any]) -> bool:
        if (
            spec.module == "stock_30m_fullmarket"
            and dep.startswith("stock_daily:")
            and str(dep_doc.get("status") or "") in {"partial", "degraded", "stale"}
        ):
            # A partial daily shard still provides a usable universe and the
            # 30m lane can resume missing symbols independently.  Treating it
            # as a hard blocker recreates the old all-shards deadlock.
            return True
        return (
            dep == FULLMARKET_SPOT_TASK_KEY
            and spec.module in SOURCE_FALLBACK_MODULES
            and _source_dependency_blocked(dep_doc)
        )

    def _soft_failed_dependencies(self, run_id: str, spec: PostmarketTaskSpec) -> list[dict[str, Any]]:
        failed: list[dict[str, Any]] = []
        for dep in spec.depends_on:
            doc = self.db["sync_tasks"].find_one({"_id": f"{run_id}:{dep}"}) or {}
            if _dependency_status_ok(doc):
                continue
            if self._soft_dependency_allowed(spec, dep, doc):
                failed.append({"task_key": dep, "status": str(doc.get("status") or "pending")})
        return failed

    def _dependencies_ok(self, run_id: str, spec: PostmarketTaskSpec) -> bool:
        for dep in spec.depends_on:
            doc = self.db["sync_tasks"].find_one({"_id": f"{run_id}:{dep}"}) or {}
            if not _dependency_status_ok(doc):
                if not self._soft_dependency_allowed(spec, dep, doc):
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
                    "finished_at": None,
                    "blocked_tasks": [],
                    "optional_blocked_tasks": [],
                    "critical_blocker": {},
                    "recovery_state": "running",
                },
                "$setOnInsert": {
                    "started_at": now,
                    "phase": "",
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
                {"phase": 1, "depends_on": 1, "blocks_run": 1, "status": 1},
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

    def _stock_daily_global_coverage_summary(self, trade_date: str | None) -> dict[str, Any]:
        date_key = str(trade_date or "").replace("-", "")[:8]
        if not date_key:
            return {}
        try:
            trade_dt = datetime.strptime(date_key, "%Y%m%d")
        except ValueError:
            return {}
        valid_query = {
            "date_key": date_key,
            "code": {"$regex": r"^\d{6}$"},
            "price": {"$gt": 0},
            "open": {"$gt": 0},
            "high": {"$gt": 0},
            "low": {"$gt": 0},
        }
        try:
            valid_count = len(self.db["fullmarket_spot_snapshots"].distinct("code", valid_query, maxTimeMS=3000))
            cached_count = len(self.db["bars"].distinct("meta.symbol", {"meta.freq": "日线", "dt": trade_dt}, maxTimeMS=3000))
        except TypeError:
            try:
                valid_count = len(self.db["fullmarket_spot_snapshots"].distinct("code", valid_query))
                cached_count = len(self.db["bars"].distinct("meta.symbol", {"meta.freq": "日线", "dt": trade_dt}))
            except Exception:
                return {}
        except Exception:
            return {}
        if valid_count <= 0:
            return {}
        coverage_pct = cached_count / valid_count * 100.0
        return {
            "total": int(valid_count),
            "processed": int(valid_count),
            "covered_codes": int(cached_count),
            "missing_symbols": max(0, int(valid_count) - int(cached_count)),
            "coverage_pct": round(coverage_pct, 2),
            "source": "fullmarket_spot_snapshots.valid_universe + bars.daily",
        }

    def _upgrade_stock_daily_after_close_seal(self, trade_date: str) -> int:
        """Upgrade already-written daily shards once a stable seal exists."""
        run_id = default_run_id(trade_date)
        repaired = 0
        try:
            pending = self.db["sync_tasks"].count_documents(
                {
                    "run_id": run_id,
                    "module": "stock_daily",
                    "result_summary.result.quality": "provisional_close",
                },
                maxTimeMS=2000,
            )
        except TypeError:
            pending = self.db["sync_tasks"].count_documents(
                {
                    "run_id": run_id,
                    "module": "stock_daily",
                    "result_summary.result.quality": "provisional_close",
                }
            )
        except Exception:
            pending = 0
        if not self._stable_close_seal_ready(trade_date):
            return 0
        if pending:
            repaired += self._reconcile_stock_daily_tasks_from_bars(run_id, trade_date)
        # Keep the legacy sync_log watermarks aligned with the authoritative
        # dated bars after a late/recovered close seal.  These documents are
        # still consumed by older dashboard/API paths even though the DAG
        # task rows are now the primary source of truth.
        repaired += self._reconcile_stock_daily_legacy_progress(trade_date)
        return repaired

    def _reconcile_stock_daily_legacy_progress(self, trade_date: str) -> int:
        """Repair legacy stock_daily progress watermarks from final-close bars.

        A previous provider attempt can leave ``sync_log`` at an old universe
        size or ``orphaned_running_module`` even after all dated daily bars are
        present.  Promote only when the close seal is stable and every symbol
        in the persisted full-market universe has a dated daily bar; preserve
        the old attempt fields as audit history in the task documents.
        """
        if not self._stable_close_seal_ready(trade_date):
            return 0
        date_key = str(trade_date or "").replace("-", "")[:8]
        try:
            valid_codes = sorted({
                self._canonical_a_code(code)
                for code in self.db["fullmarket_spot_snapshots"].distinct(
                    "code",
                    {
                        "date_key": date_key,
                        "code": {"$regex": r"^\d{6}$"},
                        "price": {"$gt": 0},
                        "open": {"$gt": 0},
                        "high": {"$gt": 0},
                        "low": {"$gt": 0},
                    },
                )
                if self._canonical_a_code(code)
            })
            trade_dt = datetime.strptime(date_key, "%Y%m%d")
            cached_codes = {
                self._canonical_a_code(code)
                for code in self.db["bars"].distinct(
                    "meta.symbol",
                    {"meta.freq": "日线", "dt": trade_dt},
                )
                if self._canonical_a_code(code)
            }
        except Exception:
            logger.debug("legacy stock_daily progress reconciliation unavailable", exc_info=True)
            return 0
        if not valid_codes or not set(valid_codes) <= cached_codes:
            return 0

        now = _naive_bj()
        shard_count = len([spec for spec in POSTMARKET_TASKS if spec.module == "stock_daily"]) or 16
        repaired = 0

        def publish(doc_id: str, result: dict[str, Any], *, module: str = "stock_daily") -> None:
            nonlocal repaired
            current = self.db["sync_log"].find_one(
                {"_id": doc_id},
                {"status": 1, "result": 1, "quality": 1, "error_msg": 1, "owner_pid": 1},
            ) or {}
            current_result = current.get("result") if isinstance(current.get("result"), dict) else {}
            already_final = (
                str(current.get("status") or "") == "ok"
                and str(current.get("quality") or current_result.get("quality") or "") == "final_close"
                and int(current_result.get("covered_codes") or current.get("covered_codes") or 0) == int(result["covered_codes"])
                and int(current_result.get("expected_codes") or current.get("expected_codes") or 0) == int(result["expected_codes"])
            )
            if already_final and not current.get("error_msg") and not current.get("owner_pid"):
                return
            self.db["sync_log"].update_one(
                {"_id": doc_id},
                {"$set": {
                    "module": module,
                    "status": "ok",
                    "quality": "final_close",
                    "trade_date": str(trade_date)[:10],
                    "expected_codes": result["expected_codes"],
                    "covered_codes": result["covered_codes"],
                    "processed": result["processed"],
                    "total": result["total"],
                    "progress_pct": 100.0,
                    "coverage_pct": 100.0,
                    "errors": 0,
                    "deferred": 0,
                    "missing_symbols": 0,
                    "remaining": 0,
                    "owner_pid": "",
                    "error_msg": "",
                    "reconciled_existing_bars": True,
                    "reconciliation_source": "fullmarket_spot_snapshots.valid_universe + bars.daily",
                    "reconciled_at": now,
                    "last_checked_at": now,
                    "updated_at": now,
                    "last_run": now,
                    "result": result,
                }},
                upsert=True,
            )
            repaired += 1

        global_result = {
            "status": "ok",
            "quality": "final_close",
            "trade_date": str(trade_date)[:10],
            "scope": "all",
            "global_total": len(valid_codes),
            "expected_codes": len(valid_codes),
            "covered_codes": len(valid_codes),
            "processed": len(valid_codes),
            "total": len(valid_codes),
            "progress_pct": 100.0,
            "coverage_pct": 100.0,
            "errors": 0,
            "deferred": 0,
            "missing_symbols": 0,
            "remaining": 0,
            "reconciled_existing_bars": True,
            "reconciliation_source": "fullmarket_spot_snapshots.valid_universe + bars.daily",
        }
        publish("stock_daily:progress:_meta", global_result)
        publish("stock_daily:_meta", global_result)

        for shard_index in range(shard_count):
            shard_codes = valid_codes[shard_index::shard_count]
            shard_result = {
                **global_result,
                "shard_key": f"shard_{shard_index:02d}",
                "shard_index": shard_index,
                "shard_count": shard_count,
                "global_total": len(valid_codes),
                "expected_codes": len(shard_codes),
                "covered_codes": len(shard_codes),
                "processed": len(shard_codes),
                "total": len(shard_codes),
            }
            publish(f"stock_daily:progress:shard_{shard_index:02d}", shard_result)
        return repaired

    def _reconcile_minute_readiness_task(self, trade_date: str) -> int:
        """Publish a successful readiness probe into the historical DAG row."""
        spec = next((item for item in POSTMARKET_TASKS if item.module == "minute_readiness_probe"), None)
        if spec is None:
            return 0
        run_id = default_run_id(trade_date)
        meta = self.db["sync_log"].find_one(
            {"_id": "minute_readiness_probe:A:_meta"},
            {"status": 1, "result": 1},
        ) or {}
        result = meta.get("result") if isinstance(meta.get("result"), dict) else {}
        if str(meta.get("status") or "") != "ok" or int(result.get("not_ready") or 0) > 0:
            return 0
        task_id = self._task_id(run_id, spec)
        current = self.db["sync_tasks"].find_one({"_id": task_id}, {"status": 1, "result_summary": 1}) or {}
        summary = current.get("result_summary") if isinstance(current.get("result_summary"), dict) else {}
        if str(current.get("status") or "") == "ok" and summary.get("reconciled_existing_probe"):
            return 0
        now = _naive_bj()
        self.db["sync_tasks"].update_one(
            {"_id": task_id},
            {"$set": {
                "status": "ok",
                "owner_pid": "",
                "heartbeat_at": now,
                "finished_at": now,
                "updated_at": now,
                "cursor": {"processed": int(result.get("checked") or 0), "total": int(result.get("checked") or 0), "progress_pct": 100.0},
                "result_summary": {
                    "module": "minute_readiness_probe",
                    "status": "ok",
                    "result": result,
                    "reconciled_existing_probe": True,
                    "reconciliation_source": "sync_log:minute_readiness_probe:A:_meta",
                },
                "error_msg": "",
                "reconciled_existing_probe": True,
            }},
            upsert=True,
        )
        return 1

    @staticmethod
    def _canonical_a_code(value: Any) -> str:
        raw = str(value or "").strip().upper()
        if "." in raw:
            raw = raw.rsplit(".", 1)[-1]
        for prefix in ("SH", "SZ", "BJ"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
        return raw if raw.isdigit() and len(raw) == 6 else ""

    def _reconcile_stock_daily_tasks_from_bars(self, run_id: str, trade_date: str) -> int:
        """Refresh daily task watermarks from the actual dated bars collection.

        A historical run can finish writing today's bars while its old shard
        summaries still describe the provider failures that occurred during
        the original attempt.  Rebuilding the shard watermarks from Mongo
        keeps the UI and dependency graph truthful, while retaining
        ``provisional_close`` so this cannot open the formal close gate.
        """
        specs = [spec for spec in POSTMARKET_TASKS if spec.module == "stock_daily"]
        if not specs:
            return 0
        date_key = str(trade_date or "").replace("-", "")[:8]
        try:
            valid_codes = sorted({
                self._canonical_a_code(code)
                for code in self.db["fullmarket_spot_snapshots"].distinct(
                    "code",
                    {
                        "date_key": date_key,
                        "code": {"$regex": r"^\d{6}$"},
                        "price": {"$gt": 0},
                        "open": {"$gt": 0},
                        "high": {"$gt": 0},
                        "low": {"$gt": 0},
                    },
                )
                if self._canonical_a_code(code)
            })
            trade_dt = datetime.strptime(date_key, "%Y%m%d")
            cached_codes = {
                self._canonical_a_code(code)
                for code in self.db["bars"].distinct(
                    "meta.symbol",
                    {"meta.freq": "日线", "dt": trade_dt},
                )
                if self._canonical_a_code(code)
            }
        except Exception:
            logger.debug("stock_daily task reconciliation unavailable", exc_info=True)
            return 0
        if not valid_codes:
            return 0

        now = _naive_bj()
        formal_close_ready = self._stable_close_seal_ready(trade_date)
        repaired = 0
        shard_count = len(specs)
        for spec in specs:
            task_id = self._task_id(run_id, spec)
            shard_index = int(spec.shard_key.rsplit("_", 1)[-1])
            shard_codes = valid_codes[shard_index::shard_count]
            covered = len(set(shard_codes) & cached_codes)
            missing = max(0, len(shard_codes) - covered)
            quality = "final_close" if formal_close_ready and missing == 0 else "provisional_close"
            task_status = "ok" if quality == "final_close" else "partial"
            result = {
                "status": task_status,
                "quality": quality,
                "scope": "all",
                "shard_key": spec.shard_key,
                "shard_index": shard_index,
                "shard_count": shard_count,
                "global_total": len(valid_codes),
                "codes": len(shard_codes),
                "processed": len(shard_codes),
                "total": len(shard_codes),
                "expected_codes": len(shard_codes),
                "covered_codes": covered,
                "coverage_pct": round(covered / len(shard_codes) * 100, 2) if shard_codes else 100.0,
                "progress_pct": 100.0,
                "errors": missing,
                "deferred": 0,
                "missing_symbols": missing,
                "reconciled_existing_bars": True,
                "reconciliation_source": "fullmarket_spot_snapshots.valid_universe + bars.daily",
            }
            current = self.db["sync_tasks"].find_one({"_id": task_id}) or {}
            if str(current.get("status") or "") == "ok" and not current.get("reconciled_existing_bars"):
                continue
            current_summary = dict(current.get("result_summary") or {})
            current_summary.update({
                "module": "stock_daily",
                "status": result["status"],
                "result": result,
                "reconciled_existing_bars": True,
            })
            self.db["sync_tasks"].update_one(
                {"_id": task_id, "status": {"$in": [*RETRYABLE_TASK_STATUSES, "ok"]}},
                {"$set": {
                    "status": result["status"],
                    "owner_pid": "",
                    "heartbeat_at": now,
                    "finished_at": now,
                    "updated_at": now,
                    "cursor": {"processed": len(shard_codes), "total": len(shard_codes), "progress_pct": 100.0},
                    "result_summary": current_summary,
                    "error_msg": "",
                    "reconciled_existing_bars": True,
                }},
            )
            repaired += 1
        return repaired

    def _reconcile_index_daily_task_from_bars(self, run_id: str, trade_date: str) -> int:
        """Publish the actual index-day coverage without upgrading quality."""
        task_id = self._task_id(
            run_id,
            next((spec for spec in POSTMARKET_TASKS if spec.module == "index_daily"), None),
        ) if any(spec.module == "index_daily" for spec in POSTMARKET_TASKS) else ""
        if not task_id:
            return 0
        current = self.db["sync_tasks"].find_one({"_id": task_id}) or {}
        if str(current.get("status") or "") == "ok":
            return 0
        try:
            from signals.replay.market_replay import _MAJOR_INDEX_TARGETS

            start = datetime.fromisoformat(str(trade_date)[:10])
            end = start + timedelta(days=1)
            expected = len(_MAJOR_INDEX_TARGETS)
            covered = official = provisional = 0
            for _name, symbol in _MAJOR_INDEX_TARGETS:
                row = self.db["index_bars"].find_one(
                    {"meta.symbol": symbol, "meta.freq": "日线", "dt": {"$gte": start, "$lt": end}},
                    {"meta": 1},
                )
                if not row:
                    continue
                covered += 1
                quality = str((row.get("meta") or {}).get("quality") or "").lower()
                if quality in {"official", "final_close"}:
                    official += 1
                else:
                    provisional += 1
        except Exception:
            logger.debug("index_daily task reconciliation unavailable", exc_info=True)
            return 0
        # Full symbol coverage is not enough for a formal close: provisional
        # bars must remain retryable until every target has an official/final
        # close value.  This prevents a historical fallback from silently
        # becoming the production close seal.
        result = {
            "status": "ok" if covered >= expected and official >= expected else "partial",
            "trade_date": str(trade_date)[:10],
            "expected": expected,
            "covered": covered,
            "official": official,
            "provisional": provisional,
            "missing": max(0, expected - covered),
            "coverage_pct": round(covered / expected * 100, 2) if expected else 100.0,
            "official_coverage_pct": round(official / expected * 100, 2) if expected else 100.0,
            "reconciled_existing_bars": True,
            "reconciliation_source": "index_bars:日线",
        }
        now = _naive_bj()
        summary = dict(current.get("result_summary") or {})
        summary.update({"module": "index_daily", "status": result["status"], "result": result, "reconciled_existing_bars": True})
        self.db["sync_tasks"].update_one(
            {"_id": task_id, "status": {"$in": list(RETRYABLE_TASK_STATUSES)}},
            {"$set": {
                "status": result["status"],
                "owner_pid": "",
                "heartbeat_at": now,
                "finished_at": now,
                "updated_at": now,
                "cursor": {"processed": covered, "total": expected, "progress_pct": result["coverage_pct"]},
                "result_summary": summary,
                "error_msg": "" if not result["missing"] else f"missing_index_symbols={result['missing']}",
                "reconciled_existing_bars": True,
            }},
        )
        return 1

    def _stock_daily_aggregate_summary(self, trade_date: str | None = None) -> dict[str, Any]:
        try:
            progress = self.db["sync_log"].find_one({"_id": "stock_daily:progress:_meta"}) or {}
        except Exception:
            progress = {}
        if not isinstance(progress, dict) or not progress:
            return {}

        global_coverage = self._stock_daily_global_coverage_summary(trade_date)
        total = _number(global_coverage.get("total") or progress.get("total") or progress.get("expected_codes") or progress.get("global_total"))
        processed = _number(progress.get("processed"))
        inserted = _number(global_coverage.get("covered_codes") or progress.get("covered_codes") or progress.get("inserted") or processed)
        if global_coverage.get("processed"):
            processed = _number(global_coverage.get("processed"))
        progress_pct = _number(progress.get("progress_pct"))
        if progress_pct <= 0 and total > 0:
            progress_pct = processed / total * 100.0
        elif total > 0 and processed > 0:
            progress_pct = max(progress_pct, processed / total * 100.0)
        coverage_pct = _number(global_coverage.get("coverage_pct") or progress.get("coverage_pct"))
        has_explicit_coverage = bool(global_coverage or progress.get("coverage_pct") or progress.get("covered_codes"))
        if coverage_pct <= 0 and total > 0 and (has_explicit_coverage or _number(progress.get("deferred") or progress.get("deferred_symbols")) <= 0):
            coverage_pct = min(100.0, inserted / total * 100.0)
        errors = _number(progress.get("errors"))
        missing = _number(global_coverage.get("missing_symbols") if global_coverage else progress.get("missing_symbols"))
        deferred_symbols = _number(progress.get("deferred_symbols"))
        deferred = _number(progress.get("deferred") or deferred_symbols)

        min_coverage = _env_float("SIGNALS_POSTMARKET_STOCK_DAILY_PARTIAL_MIN_COVERAGE", 40.0)
        max_errors = _env_int("SIGNALS_POSTMARKET_STOCK_DAILY_PARTIAL_MAX_ERRORS", 25, minimum=0)
        max_error_pct = _env_float("SIGNALS_POSTMARKET_STOCK_DAILY_PARTIAL_MAX_ERROR_PCT", 6.0)
        processed_all = bool(total > 0 and processed >= total)
        progress_done = bool(progress_pct >= 99.9)
        error_pct = (errors / total * 100.0) if total > 0 else 0.0
        sparse_errors_ok = bool(errors <= max_errors and error_pct <= max_error_pct)
        if not ((processed_all or progress_done) and sparse_errors_ok and coverage_pct >= min_coverage):
            return {}
        if (deferred > 0 or deferred_symbols > 0) and not has_explicit_coverage:
            return {}

        return {
            "status": str(progress.get("status") or ("partial" if errors or missing or deferred else "ok")),
            "processed": int(processed),
            "total": int(total),
            "inserted": int(inserted),
            "covered_codes": int(global_coverage.get("covered_codes") or progress.get("covered_codes") or inserted),
            "errors": int(errors),
            "deferred": int(deferred),
            "missing_symbols": int(missing),
            "deferred_symbols": int(deferred_symbols),
            "progress_pct": round(progress_pct, 2),
            "coverage_pct": round(coverage_pct, 2),
            "source": str(global_coverage.get("source") or "sync_log:stock_daily:progress:_meta"),
        }

    def _repair_effective_stock_daily_tasks(self, run_id: str, trade_date: str | None = None) -> int:
        aggregate_summary = self._stock_daily_aggregate_summary(trade_date)
        if not aggregate_summary:
            return 0
        repaired = 0
        now = _naive_bj()
        for doc in self.db["sync_tasks"].find(
            {"run_id": run_id, "module": "stock_daily"},
            {"_id": 1, "status": 1, "result_summary": 1},
        ):
            task_id = str(doc.get("_id") or "")
            if not task_id:
                continue
            status = str(doc.get("status") or "pending")
            if status == "ok" or status not in RETRYABLE_TASK_STATUSES:
                continue
            current = dict(doc.get("result_summary") or {})
            if current and _stock_daily_dependency_ok(dict(doc)):
                continue
            merged = {**current, **aggregate_summary}
            self.db["sync_tasks"].update_one(
                {"_id": task_id},
                {"$set": {
                    "result_summary": merged,
                    "cursor": {
                        "processed": merged["processed"],
                        "total": merged["total"],
                        "progress_pct": merged["progress_pct"],
                    },
                    "updated_at": now,
                }},
            )
            repaired += 1
        return repaired

    def _reconcile_stock_30m_tasks_from_bars(self, run_id: str, trade_date: str) -> int:
        """Publish already-complete 30m bars into the DAG checkpoint.

        A controlled historical backfill may intentionally run the module
        outside the live DAG.  Without this reconciliation, Mongo contains a
        complete shard while the old task document remains ``pending``
        forever.  Only shards whose every eligible symbol has a current-day
        bar are promoted, and the provenance is recorded explicitly.
        """
        specs = [spec for spec in POSTMARKET_TASKS if spec.module == "stock_30m_fullmarket"]
        if not specs:
            return 0
        try:
            from .modules.stock_30m_fullmarket import (
                _latest_30m_state,
                _needs_refresh,
                _shard_symbols,
                _symbols_with_daily,
            )

            universe = _symbols_with_daily(self.db)
        except Exception:
            logger.debug("stock_30m task reconciliation unavailable", exc_info=True)
            return 0
        if not universe:
            return 0

        now = _naive_bj()
        repaired = 0
        for spec in specs:
            task_id = self._task_id(run_id, spec)
            current = self.db["sync_tasks"].find_one(
                {"_id": task_id},
                {"status": 1, "result_summary": 1},
            ) or {}
            if str(current.get("status") or "") == "ok":
                continue
            shard_index = int(spec.shard_key.rsplit("_", 1)[-1])
            shard_symbols = _shard_symbols(universe, shard_index, len(specs))
            state = _latest_30m_state(self.db, shard_symbols)
            due = [
                symbol
                for symbol in shard_symbols
                if _needs_refresh(
                    state.get(symbol, {}),
                    min_bars=260,
                    trade_date=trade_date,
                    require_today=True,
                )
            ]
            if due:
                continue
            summary = {
                "module": "stock_30m_fullmarket",
                "status": "ok",
                "trade_date": trade_date,
                "shard_key": spec.shard_key,
                "total": len(shard_symbols),
                "processed": len(shard_symbols),
                "selected": 0,
                "remaining": 0,
                "coverage_pct": 100.0,
                "progress_pct": 100.0,
                "errors": 0,
                "reconciled_existing_bars": True,
                "reconciliation_source": "bars:30分钟",
            }
            self.db["sync_tasks"].update_one(
                {"_id": task_id, "status": {"$in": list(RETRYABLE_TASK_STATUSES)}},
                {"$set": {
                    "status": "ok",
                    "owner_pid": "",
                    "heartbeat_at": now,
                    "finished_at": now,
                    "updated_at": now,
                    "cursor": {"processed": len(shard_symbols), "total": len(shard_symbols), "progress_pct": 100.0},
                    "result_summary": summary,
                    "error_msg": "",
                    "depends_on": list(spec.depends_on),
                    "reconciled_existing_bars": True,
                }},
            )
            repaired += 1
        return repaired

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

    def _orphan_reason(
        self,
        doc: dict[str, Any],
        now: datetime,
        *,
        preserve_live_owner: bool = False,
    ) -> str:
        heartbeat = _coerce_dt(doc.get("heartbeat_at") or doc.get("started_at"))
        has_owner = bool(doc.get("owner_pid"))
        owner_dead = has_owner and not self._pid_alive(doc.get("owner_pid"))
        too_old = bool(heartbeat and (now - heartbeat).total_seconds() > self.task_stale_seconds)
        if owner_dead:
            return "running_owner_dead"
        if has_owner and preserve_live_owner:
            return ""
        if too_old:
            return "stale_running_task"
        return ""

    def _release_stale_running_tasks(self, run_id: str) -> int:
        now = _naive_bj()
        released = 0
        for doc in self.db["sync_tasks"].find({"run_id": run_id, "status": "running"}):
            reason = self._orphan_reason(doc, now)
            if not reason:
                continue
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

    def _reconcile_orphaned_state(self) -> dict[str, int]:
        """Close owner-dead postmarket state without replaying an old trade date."""
        now = _naive_bj()
        task_updates = 0
        run_updates = 0

        for doc in self.db["sync_tasks"].find(
            {"run_id": {"$regex": r"^postmarket:"}, "status": "running"},
            {"_id": 1, "run_id": 1, "owner_pid": 1, "heartbeat_at": 1, "started_at": 1},
        ):
            reason = self._orphan_reason(doc, now, preserve_live_owner=True)
            if not reason:
                continue
            query: dict[str, Any] = {
                "_id": doc["_id"],
                "status": "running",
                "owner_pid": doc.get("owner_pid"),
            }
            if doc.get("heartbeat_at") is not None:
                query["heartbeat_at"] = doc.get("heartbeat_at")
            result = self.db["sync_tasks"].update_one(
                query,
                {"$set": {
                    "status": "stale",
                    "owner_pid": "",
                    "error_msg": reason,
                    "interrupted_at": now,
                    "updated_at": now,
                }},
            )
            if getattr(result, "modified_count", None) == 0:
                continue
            task_updates += 1

        for run_doc in self.db["sync_runs"].find(
            {"_id": {"$regex": r"^postmarket:"}, "status": "running"},
            {"_id": 1, "owner_pid": 1, "heartbeat_at": 1, "started_at": 1, "task_count": 1},
        ):
            reason = self._orphan_reason(run_doc, now, preserve_live_owner=True)
            if not reason:
                continue
            run_id = str(run_doc.get("_id") or "")
            if not run_id:
                continue
            if self.db["sync_tasks"].find_one({"run_id": run_id, "status": "running"}, {"_id": 1}):
                continue

            task_docs = list(self.db["sync_tasks"].find(
                {"run_id": run_id},
                {"module": 1, "task_key": 1, "status": 1, "blocks_run": 1, "result_summary": 1},
            ))
            blocking_docs = [doc for doc in task_docs if bool(doc.get("blocks_run", True))]
            incomplete = [
                doc
                for doc in blocking_docs
                if not _task_effectively_done(doc)
            ]
            optional_incomplete = [
                doc
                for doc in task_docs
                if not bool(doc.get("blocks_run", True)) and not _task_effectively_done(doc)
            ]
            expected_task_count = int(run_doc.get("task_count") or len(task_docs))
            missing_task_count = max(0, expected_task_count - len(task_docs))
            blocked_tasks = sorted(
                {str(doc.get("task_key") or "") for doc in incomplete if doc.get("task_key")}
            )
            status = "ok" if task_docs and not blocked_tasks and not missing_task_count else "partial"
            recovery_state = "ok" if status == "ok" else "interrupted"
            update_query: dict[str, Any] = {
                "_id": run_id,
                "status": "running",
                "owner_pid": run_doc.get("owner_pid"),
            }
            if run_doc.get("heartbeat_at") is not None:
                update_query["heartbeat_at"] = run_doc.get("heartbeat_at")
            result = self.db["sync_runs"].update_one(
                update_query,
                {"$set": {
                    "status": status,
                    "owner_pid": "",
                    "updated_at": now,
                    "finished_at": now,
                    "reconciled_at": now,
                    "interruption_reason": reason if status == "partial" else "",
                    "blocked_tasks": blocked_tasks,
                    "recovery_state": recovery_state,
                    "ok_tasks": len(task_docs) - len(incomplete),
                    "incomplete_tasks": len(incomplete) + missing_task_count,
                    "optional_incomplete_tasks": len(optional_incomplete),
                }},
            )
            if getattr(result, "modified_count", None) == 0:
                continue
            run_updates += 1

        return {"tasks": task_updates, "runs": run_updates}

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
                    "finished_at": None,
                    "cursor": {},
                    "result_summary": {},
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
        soft_failed_deps = self._soft_failed_dependencies(run_id, spec)
        if spec.module not in self.engine.module_map:
            self._mark_task_started(run_id, spec)
            result = {"module": spec.module, "status": "error", "error": "module_missing"}
            self._mark_task_finished(run_id, spec, result)
            return result
        fn, _schedule = self.engine.module_map[spec.module]
        plan = LANE_MAINTENANCE_PLANS.get(spec.module)
        semaphore = self.module_semaphores.get(spec.module)
        # Keep the quote lane's legacy A-market watermark synchronized with
        # the postmarket retry.  Without this explicit market, the task can
        # be healthy while ``quote_snapshots:A:_meta`` remains degraded from
        # an older live attempt.
        module_market = "A" if spec.module == "quote_snapshots" else None
        task_values = {
            "SIGNALS_POSTMARKET_RUN_ID": run_id,
            "SIGNALS_POSTMARKET_TRADE_DATE": run_id.split(":", 1)[1] if ":" in run_id else "",
            **spec.env,
        }
        if spec.module == "stock_daily":
            trade_date = task_values["SIGNALS_POSTMARKET_TRADE_DATE"]
            task_values["STOCK_DAILY_CLOSE_FINALITY"] = (
                "final_close" if self._stable_close_seal_ready(trade_date) else "provisional_close"
            )
        with self._with_env(task_values):
            if semaphore is None:
                self._mark_task_started(run_id, spec)
                result = self.engine.run_module(spec.module, fn, market=module_market, plan=plan)
            else:
                with semaphore:
                    self._mark_task_started(run_id, spec)
                    result = self.engine.run_module(spec.module, fn, market=module_market, plan=plan)
        expected_trade_date = str(task_values.get("SIGNALS_POSTMARKET_TRADE_DATE") or "")[:10]
        if isinstance(result, dict) and expected_trade_date:
            actual_trade_date = str(result.get("trade_date") or result.get("date_key") or "")[:10]
            if actual_trade_date and actual_trade_date.replace("-", "")[:8] != expected_trade_date.replace("-", "")[:8]:
                result = {
                    **result,
                    "status": "error",
                    "error_msg": f"trade_date_mismatch: expected={expected_trade_date} actual={actual_trade_date}",
                    "trade_date_mismatch": True,
                }
        if soft_failed_deps and isinstance(result, dict):
            result = dict(result)
            nested = result.get("result") if isinstance(result.get("result"), dict) else {}
            partial_usable = _source_fallback_usable(spec, result)
            if str(result.get("status") or "ok") == "ok":
                result["status"] = "partial"
            result["source_fallback"] = True
            result["partial_usable"] = partial_usable
            result["degraded_dependencies"] = soft_failed_deps
            result["recovery_state"] = "source_fallback"
            if nested:
                result["result"] = {
                    **nested,
                    "source_fallback": True,
                    "partial_usable": partial_usable,
                    "degraded_dependencies": soft_failed_deps,
                }
        self._mark_task_finished(run_id, spec, result)
        return result

    def _phase_specs(self, phase: str) -> list[PostmarketTaskSpec]:
        return [task for task in POSTMARKET_TASKS if task.phase == phase]

    def _has_retryable_incomplete_tasks(self, run_id: str, *, include_optional: bool = False) -> bool:
        blocking_task_keys = {task.task_key for task in POSTMARKET_TASKS if task.blocks_run}
        for doc in self.db["sync_tasks"].find(
            {"run_id": run_id},
            {"task_key": 1, "module": 1, "status": 1, "result_summary": 1},
        ):
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

    def _reconcile_minute_preheat_coverage(self, trade_date: str) -> int:
        """Reconcile status rows with actual current-trade-date minute bars."""
        try:
            from .modules import stock_minute

            rows = list(self.db["minute_preheat_universe"].find(
                {"trade_date": trade_date, "status": {"$ne": "dropped"}},
                {"symbol": 1, "status": 1, "freq_status": 1},
            ))
            symbols = [str(row.get("symbol") or "") for row in rows if row.get("symbol")]
            states = {
                str(row.get("symbol")): row
                for row in rows
                if row.get("symbol")
            }
            stock_minute._reconcile_minute_universe_coverage(
                self.db,
                symbols,
                states,
                ["5分钟", "15分钟"],
            )
        except Exception:
            logger.debug("minute preheat coverage reconciliation failed", exc_info=True)
        return self._minute_preheat_pending_count(trade_date)

    def _reset_minute_preheat_running(
        self,
        trade_date: str,
        *,
        owner_pid: str | None = None,
        reason: str = "minute_preheat_owner_dead",
    ) -> int:
        query: dict[str, Any] = {"trade_date": trade_date, "status": "running"}
        if owner_pid:
            query["owner_pid"] = str(owner_pid)
        now = _naive_bj()
        result = self.db["minute_preheat_universe"].update_many(
            query,
            {"$set": {
                "status": "pending",
                "owner_pid": "",
                "selected_current_run": False,
                "recovery_reason": reason,
                "updated_at": now,
            }},
        )
        return int(getattr(result, "modified_count", 0) or 0)

    def _reconcile_minute_preheat_running(self, trade_date: str) -> int:
        """Release minute candidates left by a killed daemon/child."""
        released = 0
        try:
            rows = self.db["minute_preheat_universe"].find(
                {"trade_date": trade_date, "status": "running"},
                {"owner_pid": 1},
            )
            for row in rows:
                owner_pid = str(row.get("owner_pid") or "")
                if owner_pid and self._pid_alive(owner_pid):
                    continue
                released += self._reset_minute_preheat_running(
                    trade_date,
                    owner_pid=owner_pid or None,
                    reason="minute_preheat_owner_dead" if owner_pid else "minute_preheat_owner_missing",
                )
        except Exception:
            logger.debug("minute preheat orphan reconciliation failed", exc_info=True)
        return released

    def _reconcile_historical_minute_preheat_orphans(self, active_trade_date: str) -> int:
        """Mark ownerless minute rows from old runs as stale, not running.

        The minute universe is retained across trade dates for auditability.
        Older daemon versions could leave rows in ``running`` without an
        owner PID, which made global health counters report a permanently
        active job even though no process could resume it.  Only rows outside
        the active trade date are handled here; the live current-date child is
        reconciled by ``_reconcile_minute_preheat_running``.
        """
        recovered = 0
        now = _naive_bj()
        try:
            rows = self.db["minute_preheat_universe"].find(
                {"status": "running", "trade_date": {"$ne": active_trade_date}},
                {"_id": 1, "owner_pid": 1},
            )
            for row in rows:
                owner_pid = str(row.get("owner_pid") or "")
                if owner_pid and self._pid_alive(owner_pid):
                    continue
                result = self.db["minute_preheat_universe"].update_one(
                    {"_id": row.get("_id"), "status": "running"},
                    {"$set": {
                        "status": "stale",
                        "owner_pid": "",
                        "selected_current_run": False,
                        "recovery_reason": "historical_orphan_reconciled",
                        "updated_at": now,
                    }},
                )
                recovered += int(getattr(result, "modified_count", 0) or 0)
        except Exception:
            logger.debug("historical minute preheat orphan reconciliation failed", exc_info=True)
        return recovered

    def _launch_minute_preheat_child(
        self,
        trade_date: str,
        run_id: str,
        env: dict[str, str],
    ) -> bool:
        """Launch one bounded minute batch outside the long-lived daemon.

        Public minute endpoints occasionally leave a socket worker alive after
        the Mongo receipt has been written.  Running that batch in a child
        keeps the formal postmarket carrier responsive; the owner PID in
        Mongo prevents duplicate rotations and allows the next heartbeat to
        reclaim a dead or timed-out child.
        """
        run_doc = self.db["sync_runs"].find_one(
            {"_id": run_id},
            {"minute_preheat_owner_pid": 1, "minute_preheat_started_at": 1},
        ) or {}
        owner_pid = str(run_doc.get("minute_preheat_owner_pid") or "")
        if owner_pid:
            if self._pid_alive(owner_pid):
                owned_running = self.db["minute_preheat_universe"].count_documents(
                    {
                        "trade_date": trade_date,
                        "status": "running",
                        "owner_pid": owner_pid,
                    }
                )
                meta_status = str(
                    (self.db["sync_log"].find_one(
                        {"_id": "stock_minute:_meta"},
                        {"status": 1},
                    ) or {}).get("status")
                    or ""
                ).lower()
                # The module writes all per-symbol receipts before a provider
                # socket thread can linger.  Once no owned row is running and
                # the module receipt is terminal, stop that child and allow
                # the next rotation on the following daemon tick.
                if owned_running == 0 and meta_status in {"ok", "partial", "degraded", "error"}:
                    try:
                        os.kill(int(owner_pid), signal.SIGTERM)
                    except (OSError, ValueError):
                        pass
                    self.db["sync_runs"].update_one(
                        {"_id": run_id},
                        {"$set": {
                            "minute_preheat_owner_pid": "",
                            "minute_preheat_finished_at": _naive_bj(),
                            "minute_preheat_recovery": "child_receipt_complete",
                            "updated_at": _naive_bj(),
                        }},
                    )
                    owner_pid = ""
                else:
                    started = _coerce_dt(run_doc.get("minute_preheat_started_at"))
                    timeout = _env_int(
                        "SIGNALS_POSTMARKET_MINUTE_BATCH_TIMEOUT_SECONDS",
                        600,
                        minimum=60,
                    )
                    if started is None or (_naive_bj() - started).total_seconds() <= timeout:
                        return False
                    try:
                        os.kill(int(owner_pid), signal.SIGTERM)
                    except (OSError, ValueError):
                        pass
                    self._reset_minute_preheat_running(
                        trade_date,
                        owner_pid=owner_pid,
                        reason="minute_preheat_child_timeout",
                    )
                    self.db["sync_log"].update_one(
                        {"_id": "stock_minute:_meta"},
                        {"$set": {
                            "status": "degraded",
                            "error_msg": "minute_preheat_child_timeout",
                            "updated_at": _naive_bj(),
                        }},
                    )
                    self.db["sync_runs"].update_one(
                        {"_id": run_id},
                        {"$set": {
                            "minute_preheat_owner_pid": "",
                            "minute_preheat_finished_at": _naive_bj(),
                            "minute_preheat_recovery": "timeout",
                            "updated_at": _naive_bj(),
                        }},
                    )
                    return False
            if owner_pid:
                self._reset_minute_preheat_running(
                    trade_date,
                    owner_pid=owner_pid,
                    reason="minute_preheat_owner_dead",
                )
                self.db["sync_runs"].update_one(
                    {"_id": run_id},
                    {"$set": {
                        "minute_preheat_owner_pid": "",
                        "minute_preheat_finished_at": _naive_bj(),
                        "updated_at": _naive_bj(),
                    }},
                )

        child_env = os.environ.copy()
        child_env.update({str(key): str(value) for key, value in env.items()})
        child_env["SIGNALS_POSTMARKET_MINUTE_CHILD"] = "1"
        child_env["SIGNALS_POSTMARKET_MINUTE_PARENT_RUN_ID"] = run_id
        script = (
            "from signals.sync.engine import SyncEngine, LANE_MAINTENANCE_PLANS; "
            "from signals.sync.postmarket import PostmarketRunner; "
            "e=SyncEngine(); f,_=e.module_map['stock_minute']; "
            "e.run_module('stock_minute', f, plan=LANE_MAINTENANCE_PLANS.get('stock_minute'))"
        )
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        child_log_path = os.getenv(
            "SIGNALS_POSTMARKET_MINUTE_CHILD_LOG",
            "/tmp/longclaw-guardian/signals.minute-preheat-child.log",
        )
        try:
            with open(child_log_path, "ab", buffering=0) as child_log:
                child = subprocess.Popen(
                    [sys.executable, "-c", script],
                    cwd=repo_root,
                    env=child_env,
                    stdout=child_log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except Exception as exc:
            logger.warning("minute preheat child launch failed: %s", exc)
            return False
        now = _naive_bj()
        self.db["sync_runs"].update_one(
            {"_id": run_id},
            {"$set": {
                "minute_preheat_owner_pid": str(child.pid),
                "minute_preheat_started_at": now,
                "minute_preheat_recovery": "child_started",
                "updated_at": now,
            }},
            upsert=True,
        )
        logger.info("postmarket minute preheat child started run=%s pid=%s", run_id, child.pid)
        return True

    def _continue_minute_preheat_universe(self, trade_date: str, run_id: str) -> int:
        if not _env_bool("SIGNALS_POSTMARKET_CONTINUE_MINUTE_PREHEAT", True):
            return 0
        before = self._reconcile_minute_preheat_coverage(trade_date)
        if before <= 0:
            run_doc = self.db["sync_runs"].find_one(
                {"_id": run_id},
                {"minute_preheat_owner_pid": 1},
            ) or {}
            owner_pid = str(run_doc.get("minute_preheat_owner_pid") or "")
            if owner_pid and not self._pid_alive(owner_pid):
                self.db["sync_runs"].update_one(
                    {"_id": run_id},
                    {"$set": {
                        "minute_preheat_owner_pid": "",
                        "minute_preheat_finished_at": _naive_bj(),
                        "minute_preheat_recovery": "child_receipt_complete",
                        "updated_at": _naive_bj(),
                    }},
                )
            self.db["sync_runs"].update_one(
                {"_id": run_id},
                {"$set": {"minute_preheat_pending": 0, "updated_at": _naive_bj()}},
                upsert=True,
            )
            return 0
        if "stock_minute" not in self.engine.module_map:
            return 0

        released = self._reconcile_minute_preheat_running(trade_date)
        if released:
            logger.warning(
                "postmarket released orphaned minute preheat candidates run=%s released=%d",
                run_id,
                released,
            )

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

        isolated_default = not bool(os.getenv("PYTEST_CURRENT_TEST"))
        isolated = _env_bool("SIGNALS_POSTMARKET_MINUTE_ISOLATED", isolated_default)
        if isolated and os.getenv("SIGNALS_POSTMARKET_MINUTE_CHILD") != "1":
            pending_before = self._minute_preheat_pending_count(trade_date)
            started = self._launch_minute_preheat_child(trade_date, run_id, env)
            pending_after = self._minute_preheat_pending_count(trade_date)
            self.db["sync_runs"].update_one(
                {"_id": run_id},
                {"$set": {
                    "minute_preheat_pending": pending_after,
                    "minute_preheat_continued_at": _naive_bj(),
                    "minute_preheat_last_launch": bool(started),
                    "updated_at": _naive_bj(),
                }},
                upsert=True,
            )
            return max(0, pending_before - pending_after) if not started else 0

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

    def _has_retryable_hk_daily_tasks(self, run_id: str) -> bool:
        for doc in self.db["sync_tasks"].find(
            {"run_id": run_id, "module": "hk_stock_daily"},
            {"status": 1, "result_summary": 1},
        ):
            status = str(doc.get("status") or "pending")
            if status in RETRYABLE_TASK_STATUSES and not _task_effectively_done(doc):
                return True
        return False

    def _continue_hk_daily(self, trade_date: str, run_id: str) -> int:
        """Run a bounded, low-priority HK due-symbol rotation.

        HK is optional for the A-share close path.  Keep it out of the formal
        DAG critical path, but do not let a pre-cap universe prefix starve the
        missing tail.  Each shard records ``remaining_due`` and can advance
        on the next cooldown window.
        """
        if not _env_bool("SIGNALS_POSTMARKET_CONTINUE_HK_DAILY", True):
            return 0
        if not self._has_retryable_hk_daily_tasks(run_id):
            return 0
        now = _naive_bj()
        run_doc = self.db["sync_runs"].find_one(
            {"_id": run_id},
            {"hk_daily_last_continued_at": 1},
        ) or {}
        marker = _coerce_dt(run_doc.get("hk_daily_last_continued_at"))
        cooldown = _env_int("SIGNALS_POSTMARKET_HK_DAILY_COOLDOWN_SECONDS", 300, minimum=60)
        if marker and (now - marker).total_seconds() < cooldown:
            return 0
        self.db["sync_runs"].update_one(
            {"_id": run_id},
            {"$set": {"hk_daily_last_continued_at": now, "hk_daily_batch_cap": min(200, _env_int("SIGNALS_POSTMARKET_HK_DAILY_BATCH_CODES", 24, minimum=1))}},
            upsert=True,
        )
        specs = [
            spec for spec in POSTMARKET_TASKS
            if spec.module == "hk_stock_daily"
            and (self._get_task(run_id, spec).get("status") in RETRYABLE_TASK_STATUSES)
            and not _task_effectively_done(self._get_task(run_id, spec))
        ]
        if not specs:
            return 0
        cap = min(200, _env_int("SIGNALS_POSTMARKET_HK_DAILY_BATCH_CODES", 24, minimum=1))
        workers = min(4, _env_int("SIGNALS_POSTMARKET_HK_STOCK_DAILY_WORKERS", 2, minimum=1))

        def run_one(spec: PostmarketTaskSpec) -> dict[str, Any]:
            with self._with_env({"HK_STOCK_DAILY_MAX_CODES": str(cap)}):
                return self._run_task(run_id, spec)

        completed = 0
        with ThreadPoolExecutor(max_workers=min(workers, len(specs)), thread_name_prefix="postmarket-hk") as executor:
            futures = {executor.submit(run_one, spec): spec for spec in specs}
            for future, spec in list(futures.items()):
                try:
                    result = future.result()
                    if isinstance(result, dict) and str(result.get("status") or "") in {"ok", "partial"}:
                        completed += 1
                except Exception:
                    logger.exception("bounded HK daily rotation failed task=%s", spec.task_key)
        self.db["sync_runs"].update_one(
            {"_id": run_id},
            {"$set": {"hk_daily_last_batch_completed": completed, "hk_daily_updated_at": _naive_bj()}},
            upsert=True,
        )
        return completed

    def _continue_terminal_run(self, trade_date: str, run_id: str) -> bool:
        continued = self._continue_minute_preheat_universe(trade_date, run_id) > 0
        continued = self._continue_hk_daily(trade_date, run_id) > 0 or continued
        if (
            # Optional tails (notably the per-symbol HK history lane) may be
            # provider-bound and can outlive the formal A-share postmarket
            # window.  Keep them opt-in so a slow optional lane cannot make a
            # completed critical run look perpetually ``running``.  Operators
            # can still enable the lane explicitly for a controlled catch-up.
            _env_bool("SIGNALS_POSTMARKET_CONTINUE_OPTIONAL_TASKS", False)
            and self._has_retryable_incomplete_tasks(run_id, include_optional=True)
        ):
            logger.info("postmarket continue optional tasks run=%s", run_id)
            self.run_once(resume_run_id=run_id, trade_date=trade_date, run_optional_tasks=True)
            continued = True
        return continued

    def run_once(
        self,
        *,
        resume_run_id: str | None = None,
        trade_date: str | None = None,
        force: bool = False,
        run_optional_tasks: bool | None = None,
    ) -> dict[str, Any]:
        trade_date = trade_date or _postmarket_trade_date()
        run_id = resume_run_id or default_run_id(trade_date)
        run_doc = self.db["sync_runs"].find_one({"_id": run_id}) or {}
        if run_doc.get("trade_date"):
            trade_date = str(run_doc["trade_date"])
        run_optional_tasks_enabled = (
            _env_bool("SIGNALS_POSTMARKET_RUN_OPTIONAL_TASKS", False)
            if run_optional_tasks is None
            else bool(run_optional_tasks)
        )
        continue_terminal_optional = (
            run_optional_tasks_enabled
            and self._has_retryable_incomplete_tasks(run_id, include_optional=True)
        )
        if run_doc.get("status") in RUN_TERMINAL_STATUSES and not force and not continue_terminal_optional:
            return {"run_id": run_id, "trade_date": trade_date, "status": run_doc.get("status"), "skipped": True}

        self._init_run(run_id, trade_date)
        self._init_tasks(run_id, trade_date)
        reconciled_index = self._reconcile_index_daily_task_from_bars(run_id, trade_date)
        if reconciled_index:
            logger.info("postmarket reconciled existing index bars run=%s", run_id)
        reconciled_daily = self._reconcile_stock_daily_tasks_from_bars(run_id, trade_date)
        if reconciled_daily:
            logger.info("postmarket reconciled existing daily bars run=%s tasks=%d", run_id, reconciled_daily)
        reconciled_30m = self._reconcile_stock_30m_tasks_from_bars(run_id, trade_date)
        if reconciled_30m:
            logger.info("postmarket reconciled existing 30m bars run=%s tasks=%d", run_id, reconciled_30m)
        reused = self._apply_close_seal_handoff(run_id, trade_date)
        if reused:
            logger.info("postmarket reused close seal modules: run=%s reused=%d", run_id, reused)
        repaired = self._repair_effective_stock_daily_tasks(run_id, trade_date)
        if repaired:
            logger.info("postmarket repaired effective stock_daily task summaries: run=%s repaired=%d", run_id, repaired)
        released = self._release_stale_running_tasks(run_id)
        if released:
            logger.warning("postmarket released stale tasks: run=%s released=%d", run_id, released)

        results: list[dict[str, Any]] = []
        blocked: set[str] = set()
        for phase in POSTMARKET_PHASES:
            phase_specs = self._phase_specs(phase)
            if not run_optional_tasks_enabled:
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
            {"module": 1, "task_key": 1, "status": 1, "phase": 1, "updated_at": 1, "result_summary": 1, "error_msg": 1},
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
        source_blocker_task = next(
            (
                doc for doc in task_docs
                if str(doc.get("task_key") or "") == FULLMARKET_SPOT_TASK_KEY
                and not _task_effectively_done(doc)
                and _source_dependency_blocked(doc)
            ),
            None,
        )
        critical_blocker = _source_blocker_from_task(source_blocker_task) if source_blocker_task else {}
        if critical_blocker:
            blocked_critical.add(FULLMARKET_SPOT_TASK_KEY)
        source_fallback_used = False
        for doc in task_docs:
            summary = doc.get("result_summary") if isinstance(doc.get("result_summary"), dict) else {}
            nested = summary.get("result") if isinstance(summary.get("result"), dict) else {}
            if summary.get("source_fallback") or nested.get("source_fallback"):
                source_fallback_used = True
                break
        status = "ok" if not incomplete and not blocked_critical else "partial"
        if critical_blocker:
            recovery_state = "partial/source_blocked" if source_fallback_used else "waiting_for_source"
        else:
            recovery_state = "ok" if status == "ok" else "partial"
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
                "critical_blocker": critical_blocker,
                "recovery_state": recovery_state,
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
            "critical_blocker": critical_blocker,
            "recovery_state": recovery_state,
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
        del now
        return False

    def catchup_target(self, now: datetime | None = None, *, force: bool = False) -> tuple[str, str, str] | None:
        del now, force
        # Old-trade-date catch-up can overwrite a newer event-date snapshot.
        # A same-day rerun must call run_once explicitly through the ops path.
        return None

    def run_daemon(self, *, check_seconds: int | None = None) -> None:
        # The minute-preheat rotation runs in a bounded child.  A five-minute
        # parent sleep can leave a finished child idle for almost the entire
        # interval before the next 240-symbol batch is launched.  Polling once
        # per minute only performs lightweight Mongo/PID checks; it does not
        # change provider concurrency or request volume.
        check_seconds = check_seconds or _env_int("SIGNALS_POSTMARKET_CHECK_SECONDS", 60, minimum=30)
        logger.info("postmarket daemon started workers=%d check_seconds=%d", self.max_workers, check_seconds)
        try:
            reconciled = self._reconcile_orphaned_state()
            if reconciled["tasks"] or reconciled["runs"]:
                logger.warning(
                    "postmarket reconciled orphaned state tasks=%d runs=%d",
                    reconciled["tasks"],
                    reconciled["runs"],
                )
        except Exception:
            logger.exception("postmarket orphan reconciliation failed")
        try:
            active_trade_date = _postmarket_trade_date(_now_bj())
            recovered = self._reconcile_historical_minute_preheat_orphans(active_trade_date)
            if recovered:
                logger.warning(
                    "postmarket reconciled historical minute-preheat orphans active_date=%s recovered=%d",
                    active_trade_date,
                    recovered,
                )
        except Exception:
            logger.exception("historical minute-preheat orphan reconciliation failed")
        try:
            active_trade_date = _postmarket_trade_date(_now_bj())
            reconciled_probe = self._reconcile_minute_readiness_task(active_trade_date)
            if reconciled_probe:
                logger.info("postmarket reconciled minute readiness task trade_date=%s", active_trade_date)
        except Exception:
            logger.exception("minute readiness task reconciliation failed")
        while True:
            now = _now_bj()
            if self.close_seal is not None and _is_a_share_trading_day(now):
                trade_date = _postmarket_trade_date(now)
                try:
                    seal_result = self.close_seal.tick(trade_date, _local_bj(now))
                    if seal_result.get("status") in {"sealed", "partial"} and not seal_result.get("skipped"):
                        logger.info("close seal tick result=%s", seal_result.get("status"))
                    if seal_result.get("status") == "sealed":
                        upgraded = self._upgrade_stock_daily_after_close_seal(trade_date)
                        if upgraded:
                            logger.info("postmarket upgraded daily shards after close seal trade_date=%s tasks=%d", trade_date, upgraded)
                except Exception:
                    logger.exception("close seal tick failed")
            elif self.close_seal is not None and not _is_a_share_trading_day(now):
                # If the daemon was started/restarted after the normal
                # 15:00-18:30 window, the old implementation never created a
                # close-seal run.  Recover only the most recent trade date,
                # with the same two-probe stability rule and a dedicated
                # close-seal run id.  This does not touch today's formal lane
                # or promote replay backfill snapshots.
                trade_date = _postmarket_trade_date(now)
                seal_id = f"close_seal:{trade_date}"
                seal_doc = self.db["sync_runs"].find_one(
                    {"_id": seal_id},
                    {"status": 1, "terminal_partial": 1},
                ) or {}
                if seal_doc.get("status") != "sealed":
                    try:
                        seal_result = self.close_seal.tick(
                            trade_date,
                            _local_bj(now),
                            allow_late_recovery=True,
                        )
                        if seal_result.get("status") in {"sealed", "partial"} and not seal_result.get("skipped"):
                            logger.info(
                                "late close seal recovery trade_date=%s result=%s",
                                trade_date,
                                seal_result.get("status"),
                            )
                        if seal_result.get("status") == "sealed":
                            upgraded = self._upgrade_stock_daily_after_close_seal(trade_date)
                            if upgraded:
                                logger.info("postmarket upgraded daily shards after late close seal trade_date=%s tasks=%d", trade_date, upgraded)
                    except Exception:
                        logger.exception("late close seal recovery failed")
                else:
                    upgraded = self._upgrade_stock_daily_after_close_seal(trade_date)
                    if upgraded:
                        logger.info("postmarket upgraded daily shards after existing close seal trade_date=%s tasks=%d", trade_date, upgraded)
                    reconciled_probe = self._reconcile_minute_readiness_task(trade_date)
                    if reconciled_probe:
                        logger.info("postmarket reconciled minute readiness task trade_date=%s", trade_date)
            if self.should_run_now(now):
                trade_date = _postmarket_trade_date(now)
                run_id = default_run_id(trade_date)
                run_doc = self.db["sync_runs"].find_one({"_id": run_id}, {"status": 1}) or {}
                status = run_doc.get("status")
                if status not in RUN_TERMINAL_STATUSES or _env_bool("SIGNALS_POSTMARKET_FORCE", False):
                    logger.info("postmarket trigger run=%s previous_status=%s", run_id, status or "missing")
                    self.run_once(trade_date=trade_date, force=_env_bool("SIGNALS_POSTMARKET_FORCE", False))
                else:
                    self._continue_terminal_run(trade_date, run_id)
            else:
                target = self.catchup_target(now)
                if target:
                    run_id, trade_date, status = target
                    if status in RUN_TERMINAL_STATUSES:
                        self._continue_terminal_run(trade_date, run_id)
                    else:
                        logger.info("postmarket catchup trigger run=%s previous_status=%s", run_id, status)
                        self.run_once(resume_run_id=run_id, trade_date=trade_date)
                elif not _is_a_share_trading_day(now):
                    # A completed postmarket DAG may still have a bounded,
                    # low-priority minute-preheat rotation outstanding.  The
                    # normal catch-up path intentionally stays disabled for
                    # old trade dates, but this rotation is safe to continue:
                    # it only advances the existing universe pointer and does
                    # not rebuild formal close data or reports.  Without this
                    # branch, a Friday backlog can remain frozen all weekend.
                    trade_date = _postmarket_trade_date(now)
                    run_id = default_run_id(trade_date)
                    run_doc = self.db["sync_runs"].find_one(
                        {"_id": run_id},
                        {"status": 1, "minute_preheat_owner_pid": 1},
                    ) or {}
                    if (
                        run_doc.get("status") in RUN_TERMINAL_STATUSES
                        and (
                            self._minute_preheat_pending_count(trade_date) > 0
                            or bool(run_doc.get("minute_preheat_owner_pid"))
                            or self._has_retryable_hk_daily_tasks(run_id)
                        )
                    ):
                        logger.info(
                            "postmarket continue minute rotation on non-trading day run=%s",
                            run_id,
                        )
                        self._continue_terminal_run(trade_date, run_id)
            sleep_seconds = check_seconds
            if self.close_seal is not None:
                try:
                    sleep_seconds = min(
                        check_seconds,
                        self.close_seal.seconds_until_next_event(_postmarket_trade_date(now), _local_bj(now)),
                    )
                except Exception:
                    logger.debug("close seal next wake unavailable", exc_info=True)
            time.sleep(max(1, sleep_seconds))
