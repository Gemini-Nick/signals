# -*- coding: utf-8 -*-
"""
数据同步引擎 — 调度器 + CLI 入口

借鉴 Akshare-Sync 的 ProcessPoolExecutor 模式，
使用 ThreadPoolExecutor（I/O 密集无需多进程）。

用法:
    # 一次性执行所有模块
    python -m signals.sync.engine --once

    # 只执行指定模块
    python -m signals.sync.engine --once --module index_daily

    # 常驻调度模式（cron 外的备选方案）
    python -m signals.sync.engine --daemon

    # 第二屏低延时 lane：独立进程运行，避免 stock_minute 阻塞 quote
    python -m signals.sync.engine --daemon --lane quote_lane
"""
import argparse
import atexit
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from signals.core.market_hours import Market, TZ_BEIJING, TZ_US_EAST, TZ_UTC, get_active_markets, next_live_check_seconds
from .db import get_db, close as close_db
from .task_context import task_env

logger = logging.getLogger("signals.sync")


def _sector_transition_enabled() -> bool:
    return os.getenv("SECTOR_TRANSITION_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


MODULE_TARGETS = {
    "cache_preheat": ("bars",),
    "signal_pool": ("signals",),
    "market_pools": ("market_pools",),
    "market_limit_pools": ("market_limit_pools",),
    "fullmarket_spot_snapshot": ("fullmarket_spot_snapshots",),
    "etf_spot_snapshot": ("etf_spot_snapshots",),
    "eastmoney_ulist_quote": ("quote_snapshots",),
    "quote_snapshots": ("quote_snapshots",),
    "strategy_snapshot": ("strategy_snapshots",),
    "stock_daily": ("bars",),
    "hk_stock_daily": ("bars",),
    "stock_30m_fullmarket": ("bars",),
    "index_daily": ("index_bars",),
    "global_market_foundation": ("security_master", "market_universe_membership", "market_daily_snapshots"),
    "stock_minute": ("bars",),
    "index_minute": ("index_bars",),
    "board_ranking": ("board_ranking", "concept_ranking"),
    "board_heat_minute": ("board_heat_ticks",),
    "concept_heat_minute": ("board_heat_ticks",),
    "security_business_facts": ("security_business_facts",),
    "postmarket_chain_rebuild": (
        "source_board_catalog",
        "source_board_chain_mappings",
        "security_concept_evidence",
        "security_master",
        "security_chain_memberships",
        "chain_node_security_rollups",
        "chain_coverage_reports",
    ),
    "chain_heat_snapshots": ("chain_heat_snapshots",),
    "concept_relationship_graph": ("concept_relationship_graph",),
    "ma_climb_scan": ("terminal_technical_signals",),
    "technical_signal_scan": ("terminal_technical_signals",),
    "intraday_technical_signal_scan": ("terminal_technical_signals",),
    "knowledge_market_views": ("knowledge_market_views",),
    "hot_rank_clues": ("hot_rank_clues",),
    "minute_readiness_probe": ("minute_readiness",),
    "weekly_rollup": ("bars", "index_bars"),
    "terminal_realtime_pool": ("terminal_stock_pool",),
    "sector_transition_scan": (
        "sector_transition_states",
        "sector_transition_events",
        "sector_liquidity_snapshots",
    ),
    "sector_transition_rollup": ("sector_transition_daily", "sector_transition_events"),
    "board_cons": ("board_constituents", "concept_constituents"),
}

COLLECTION_DOMAINS = {
    "bars": "kline",
    "kline_cache": "kline",
    "index_bars": "index",
    "board_ranking": "board",
    "concept_ranking": "concept",
    "board_heat_ticks": "board_heat",
    "security_business_facts": "business_facts",
    "source_board_catalog": "chain_rebuild",
    "source_board_chain_mappings": "chain_rebuild",
    "security_concept_evidence": "chain_rebuild",
    "security_master": "security_master",
    "market_universe_membership": "security_master",
    "market_daily_snapshots": "market_snapshot",
    "security_chain_memberships": "chain_rebuild",
    "chain_node_security_rollups": "chain_rebuild",
    "chain_coverage_reports": "chain_rebuild",
    "chain_heat_snapshots": "chain_heat",
    "concept_relationship_graph": "concept_graph",
    "terminal_technical_signals": "technical_signal",
    "knowledge_market_views": "knowledge",
    "hot_rank_clues": "hot_rank_clue",
    "minute_readiness": "readiness",
    "terminal_stock_pool": "terminal_pool",
    "sector_transition_states": "sector_transition",
    "sector_transition_events": "sector_transition",
    "sector_transition_daily": "sector_transition",
    "sector_liquidity_snapshots": "sector_transition",
    "board_constituents": "constituents",
    "concept_constituents": "constituents",
    "quote_snapshots": "quote",
    "market_pools": "market_pool",
    "market_limit_pools": "market_limit_pool",
    "fullmarket_spot_snapshots": "spot",
    "etf_spot_snapshots": "etf_spot",
    "signals": "signal",
    "strategy_snapshots": "strategy",
}

WRITER_FRESHNESS_FIELDS = {
    "quote_snapshots": ("live_count", "stale_count"),
    "eastmoney_ulist_quote": ("live_count", "stale_count"),
    "fullmarket_spot_snapshot": ("elapsed_seconds", "latest_dt", "count"),
    "etf_spot_snapshot": ("elapsed_seconds", "latest_dt", "count"),
}

REALTIME_MODULES = {
    "market_pools",
    "market_limit_pools",
    "fullmarket_spot_snapshot",
    "etf_spot_snapshot",
    "eastmoney_ulist_quote",
    "quote_snapshots",
    "stock_minute",
    "index_minute",
    "board_heat_minute",
    "concept_heat_minute",
    "chain_heat_snapshots",
    "minute_readiness_probe",
    "board_ranking",
    "strategy_snapshot",
    "intraday_technical_signal_scan",
    "sector_transition_scan",
}

EMPTY_OK_MODULES = {
    "ma_climb_scan",
    "technical_signal_scan",
    "intraday_technical_signal_scan",
    "sector_transition_scan",
    "sector_transition_rollup",
    "knowledge_market_views",
    "concept_relationship_graph",
}

SYNC_TZ = ZoneInfo(os.getenv("SIGNALS_SYNC_TIMEZONE", "Asia/Shanghai"))
QUOTE_PREOPEN_START = dt_time(9, 15)
QUOTE_PREOPEN_END = dt_time(9, 30)
PREOPEN_LIVE_LANES = {"quote_lane", "signal_lane", "workbench_lane", "board_lane"}


def _env_seconds(name: str, default: int, *, minimum: int = 60) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, default)


INTRADAY_INTERVAL_SECONDS = max(
    60,
    int(os.getenv("SIGNALS_INTRADAY_SYNC_INTERVAL_SECONDS", str(30 * 60))),
)
INTRADAY_STALE_SECONDS = max(
    INTRADAY_INTERVAL_SECONDS,
    int(os.getenv("SIGNALS_INTRADAY_STALE_SECONDS", str(2 * 60 * 60))),
)
QUOTE_LANE_INTERVAL_SECONDS = _env_seconds("SIGNALS_LIVE_QUOTE_INTERVAL_SECONDS", 60)
EASTMONEY_ULIST_QUOTE_INTERVAL_SECONDS = _env_seconds("SIGNALS_EASTMONEY_ULIST_QUOTE_INTERVAL_SECONDS", 10, minimum=5)
FULLMARKET_SPOT_INTERVAL_SECONDS = _env_seconds("SIGNALS_LIVE_FULLMARKET_SPOT_INTERVAL_SECONDS", 90, minimum=30)
ETF_SPOT_INTERVAL_SECONDS = _env_seconds("SIGNALS_LIVE_ETF_SPOT_INTERVAL_SECONDS", 180, minimum=60)
SIGNAL_LANE_INTERVAL_SECONDS = _env_seconds("SIGNALS_LIVE_SIGNAL_INTERVAL_SECONDS", 5 * 60)
WORKBENCH_LANE_INTERVAL_SECONDS = _env_seconds("SIGNALS_LIVE_WORKBENCH_INTERVAL_SECONDS", 10 * 60)
BOARD_LANE_INTERVAL_SECONDS = _env_seconds("SIGNALS_LIVE_BOARD_INTERVAL_SECONDS", 60)
LIMIT_POOL_INTERVAL_SECONDS = _env_seconds("SIGNALS_LIVE_LIMIT_POOL_INTERVAL_SECONDS", 3 * 60)


@dataclass(frozen=True)
class LiveSyncPlan:
    module: str
    lane: str
    interval_seconds: int
    stale_seconds: int
    max_runtime_seconds: int
    priority: int = 100


def _lane_stale(interval_seconds: int, multiplier: int = 3) -> int:
    return _env_seconds("SIGNALS_LIVE_STALE_SECONDS", interval_seconds * multiplier)


LIVE_SYNC_PLANS = {
    Market.A: (
        LiveSyncPlan("eastmoney_ulist_quote", "quote_lane", EASTMONEY_ULIST_QUOTE_INTERVAL_SECONDS, _lane_stale(EASTMONEY_ULIST_QUOTE_INTERVAL_SECONDS, 6), 20, 5),
        LiveSyncPlan("fullmarket_spot_snapshot", "quote_lane", FULLMARKET_SPOT_INTERVAL_SECONDS, _lane_stale(FULLMARKET_SPOT_INTERVAL_SECONDS, 3), 30, 8),
        LiveSyncPlan("etf_spot_snapshot", "quote_lane", ETF_SPOT_INTERVAL_SECONDS, _lane_stale(ETF_SPOT_INTERVAL_SECONDS, 3), 60, 9),
        LiveSyncPlan("quote_snapshots", "quote_lane", QUOTE_LANE_INTERVAL_SECONDS, _lane_stale(QUOTE_LANE_INTERVAL_SECONDS, 3), 20, 10),
        LiveSyncPlan("index_minute", "signal_lane", SIGNAL_LANE_INTERVAL_SECONDS, _lane_stale(SIGNAL_LANE_INTERVAL_SECONDS, 3), 120, 20),
        LiveSyncPlan("stock_minute", "signal_lane", SIGNAL_LANE_INTERVAL_SECONDS, _lane_stale(SIGNAL_LANE_INTERVAL_SECONDS, 3), 240, 30),
        LiveSyncPlan("minute_readiness_probe", "signal_lane", SIGNAL_LANE_INTERVAL_SECONDS, _lane_stale(SIGNAL_LANE_INTERVAL_SECONDS, 3), 60, 35),
        LiveSyncPlan("intraday_technical_signal_scan", "signal_lane", SIGNAL_LANE_INTERVAL_SECONDS, _lane_stale(SIGNAL_LANE_INTERVAL_SECONDS, 3), 240, 38),
        LiveSyncPlan("market_pools", "workbench_lane", WORKBENCH_LANE_INTERVAL_SECONDS, _lane_stale(WORKBENCH_LANE_INTERVAL_SECONDS, 3), 60, 40),
        LiveSyncPlan("market_limit_pools", "workbench_lane", LIMIT_POOL_INTERVAL_SECONDS, _lane_stale(LIMIT_POOL_INTERVAL_SECONDS, 4), 90, 42),
        LiveSyncPlan("strategy_snapshot", "workbench_lane", WORKBENCH_LANE_INTERVAL_SECONDS, _lane_stale(WORKBENCH_LANE_INTERVAL_SECONDS, 3), 90, 55),
        LiveSyncPlan("board_heat_minute", "board_lane", BOARD_LANE_INTERVAL_SECONDS, _lane_stale(BOARD_LANE_INTERVAL_SECONDS, 3), 180, 60),
        LiveSyncPlan("concept_heat_minute", "board_lane", BOARD_LANE_INTERVAL_SECONDS, _lane_stale(BOARD_LANE_INTERVAL_SECONDS, 3), 180, 65),
        LiveSyncPlan("chain_heat_snapshots", "board_lane", BOARD_LANE_INTERVAL_SECONDS, _lane_stale(BOARD_LANE_INTERVAL_SECONDS, 3), 90, 70),
        LiveSyncPlan("sector_transition_scan", "board_lane", BOARD_LANE_INTERVAL_SECONDS, _lane_stale(BOARD_LANE_INTERVAL_SECONDS, 3), 120, 75),
    ),
    # HK/US slots are explicit and independently throttled. Data-source modules
    # can be plugged in here without affecting the A-share live bundle.
    Market.HK: (),
    Market.US: (),
}

LIVE_SYNC_STAGE_BY_MODULE = {
    "eastmoney_ulist_quote": 0,
    "fullmarket_spot_snapshot": 0,
    "etf_spot_snapshot": 0,
    "quote_snapshots": 0,
    "index_minute": 0,
    "stock_minute": 0,
    "market_pools": 0,
    "market_limit_pools": 0,
    "board_heat_minute": 0,
    "concept_heat_minute": 0,
    "chain_heat_snapshots": 0,
    "minute_readiness_probe": 1,
    "intraday_technical_signal_scan": 1,
    "strategy_snapshot": 2,
    "sector_transition_scan": 1,
}

INTRADAY_BUNDLES = {
    market: tuple(plan.module for plan in plans)
    for market, plans in LIVE_SYNC_PLANS.items()
}
LIVE_PLAN_BY_MODULE = {
    plan.module: plan
    for plans in LIVE_SYNC_PLANS.values()
    for plan in plans
}

LANE_MAINTENANCE_PLANS = {
    "stock_minute": LiveSyncPlan("stock_minute", "signal_lane", 24 * 60 * 60, 60 * 60, 360, 5),
    "index_minute": LiveSyncPlan("index_minute", "signal_lane", 24 * 60 * 60, 60 * 60, 120, 6),
    "board_heat_minute": LiveSyncPlan("board_heat_minute", "board_lane", 24 * 60 * 60, 60 * 60, 180, 7),
    "concept_heat_minute": LiveSyncPlan("concept_heat_minute", "board_lane", 24 * 60 * 60, 60 * 60, 180, 8),
    "chain_heat_snapshots": LiveSyncPlan("chain_heat_snapshots", "board_lane", 24 * 60 * 60, 60 * 60, 90, 9),
    "sector_transition_scan": LiveSyncPlan("sector_transition_scan", "board_lane", 24 * 60 * 60, 60 * 60, 120, 10),
    "minute_readiness_probe": LiveSyncPlan("minute_readiness_probe", "signal_lane", 24 * 60 * 60, 60 * 60, 60, 9),
    "etf_spot_snapshot": LiveSyncPlan("etf_spot_snapshot", "quote_lane", 24 * 60 * 60, 2 * 60 * 60, 600, 20),
    "stock_daily": LiveSyncPlan("stock_daily", "workbench_lane", 24 * 60 * 60, 4 * 60 * 60, 3600, 30),
    "hk_stock_daily": LiveSyncPlan("hk_stock_daily", "workbench_lane", 24 * 60 * 60, 4 * 60 * 60, 3600, 32),
    "stock_30m_fullmarket": LiveSyncPlan("stock_30m_fullmarket", "workbench_lane", 24 * 60 * 60, 4 * 60 * 60, 5400, 35),
    "market_limit_pools": LiveSyncPlan("market_limit_pools", "workbench_lane", 24 * 60 * 60, 2 * 60 * 60, 180, 38),
    "index_daily": LiveSyncPlan("index_daily", "workbench_lane", 24 * 60 * 60, 2 * 60 * 60, 300, 40),
    "weekly_rollup": LiveSyncPlan("weekly_rollup", "workbench_lane", 24 * 60 * 60, 2 * 60 * 60, 600, 45),
    "board_ranking": LiveSyncPlan("board_ranking", "board_lane", 24 * 60 * 60, 2 * 60 * 60, 300, 60),
    "board_cons": LiveSyncPlan("board_cons", "board_lane", 24 * 60 * 60, 6 * 60 * 60, 900, 70),
    "security_business_facts": LiveSyncPlan("security_business_facts", "workbench_lane", 24 * 60 * 60, 12 * 60 * 60, 1200, 76),
    "postmarket_chain_rebuild": LiveSyncPlan("postmarket_chain_rebuild", "workbench_lane", 24 * 60 * 60, 2 * 60 * 60, 900, 78),
    "signal_pool": LiveSyncPlan("signal_pool", "workbench_lane", 24 * 60 * 60, 2 * 60 * 60, 300, 80),
    "ma_climb_scan": LiveSyncPlan("ma_climb_scan", "workbench_lane", 24 * 60 * 60, 4 * 60 * 60, 900, 81),
    "technical_signal_scan": LiveSyncPlan("technical_signal_scan", "workbench_lane", 24 * 60 * 60, 4 * 60 * 60, 1800, 82),
    "knowledge_market_views": LiveSyncPlan("knowledge_market_views", "workbench_lane", 24 * 60 * 60, 2 * 60 * 60, 300, 84),
    "concept_relationship_graph": LiveSyncPlan("concept_relationship_graph", "workbench_lane", 24 * 60 * 60, 2 * 60 * 60, 300, 86),
    "sector_transition_rollup": LiveSyncPlan("sector_transition_rollup", "workbench_lane", 24 * 60 * 60, 2 * 60 * 60, 300, 88),
    "strategy_snapshot": LiveSyncPlan("strategy_snapshot", "workbench_lane", 24 * 60 * 60, 2 * 60 * 60, 120, 90),
    "cache_preheat": LiveSyncPlan("cache_preheat", "workbench_lane", 24 * 60 * 60, 2 * 60 * 60, 180, 100),
}

BOOTSTRAP_LANE_MODULES = {
    "eastmoney_ulist_quote": {"quote_lane"},
    "fullmarket_spot_snapshot": {"quote_lane"},
    "etf_spot_snapshot": {"quote_lane"},
    "quote_snapshots": {"quote_lane"},
    "market_pools": {"workbench_lane"},
    "market_limit_pools": {"workbench_lane"},
    "cache_preheat": {"workbench_lane"},
    "signal_pool": {"workbench_lane"},
    "ma_climb_scan": {"workbench_lane"},
    "technical_signal_scan": {"workbench_lane"},
    "intraday_technical_signal_scan": {"signal_lane"},
    "knowledge_market_views": {"workbench_lane"},
    "concept_relationship_graph": {"workbench_lane"},
    "stock_30m_fullmarket": {"workbench_lane"},
    "hk_stock_daily": {"workbench_lane"},
    "index_daily": {"workbench_lane"},
    "weekly_rollup": {"workbench_lane"},
    "strategy_snapshot": {"workbench_lane"},
    "board_ranking": {"board_lane"},
    "board_heat_minute": {"board_lane"},
    "concept_heat_minute": {"board_lane"},
    "chain_heat_snapshots": {"board_lane"},
    "sector_transition_scan": {"board_lane"},
    "minute_readiness_probe": {"signal_lane"},
    "board_cons": {"board_lane"},
    "security_business_facts": {"workbench_lane"},
    "postmarket_chain_rebuild": {"workbench_lane"},
    "sector_transition_rollup": {"workbench_lane"},
}


class SyncEngine:
    """
    数据同步调度器。

    管理 6 个同步模块的执行：
    - stock_daily:   ~5000 A股日线（增量）
    - index_daily:   11 只指数日线（全量）
    - stock_minute:  活跃标的 30M/15M（增量）
    - index_minute:  宏观观察指数 5M/15M/30M
    - board_ranking: 行业排行快照
    - board_cons:    行业成分股（周日全量）
    """

    def __init__(
        self,
        mongo_url: str = None,
        proxy_url: str = None,
        max_workers: int = 4,
        enabled_lanes: set[str] | None = None,
    ):
        self.db = get_db(mongo_url)
        self.proxy_url = proxy_url
        self.max_workers = max_workers
        self.enabled_lanes = set(enabled_lanes or []) or None
        atexit.register(close_db)
        from .storage import ensure_storage_model

        ensure_storage_model(self.db)
        self.mark_stale_running_modules()

        # 延迟导入避免循环
        from .modules import ALL_MODULES
        self.modules = ALL_MODULES
        self.module_map = {name: (fn, schedule) for name, fn, schedule in self.modules}

    @staticmethod
    def _now() -> datetime:
        """Return scheduler time as naive Beijing time for Mongo compatibility."""
        return datetime.now(SYNC_TZ).replace(tzinfo=None)

    @staticmethod
    def _now_utc() -> datetime:
        return datetime.now(TZ_UTC)

    @staticmethod
    def _meta_id(module_name: str, market: str | None = None) -> str:
        return f"{module_name}:{market}:_meta" if market else f"{module_name}:_meta"

    @staticmethod
    def _coerce_local_datetime(value) -> datetime | None:
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                return value.astimezone(SYNC_TZ).replace(tzinfo=None)
            return value
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is not None:
                return parsed.astimezone(SYNC_TZ).replace(tzinfo=None)
            return parsed
        return None

    @staticmethod
    def _pid_alive(pid: object) -> bool:
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

    def mark_stale_running_modules(
        self,
        max_age: timedelta = timedelta(hours=2),
        *,
        release_orphans: bool = True,
    ) -> int:
        """Release modules stuck in `running` so the daemon can retry them."""
        sync_log = self.db["sync_log"]
        now = self._now()
        cutoff = now - max_age
        stale_docs = list(sync_log.find(
            {
                "$or": [
                    {"_id": {"$regex": ":_meta$"}},
                    {"_id": {"$regex": ":progress:"}},
                ],
                "status": "running",
            },
            {"_id": 1, "module": 1, "last_run": 1, "owner_pid": 1, "heartbeat_at": 1, "updated_at": 1},
        ))

        released = 0
        for doc in stale_docs:
            last_run = doc.get("heartbeat_at") or doc.get("last_run") or doc.get("updated_at")
            last_run_dt = self._coerce_local_datetime(last_run)
            owner_pid = doc.get("owner_pid")
            owner_dead = bool(owner_pid) and not self._pid_alive(owner_pid)
            legacy_orphan = release_orphans and not owner_pid
            too_old = bool(last_run_dt and last_run_dt < cutoff)
            if not (owner_dead or legacy_orphan or too_old):
                continue
            elapsed = None
            if last_run_dt:
                elapsed = (now - last_run_dt).total_seconds()
            sync_log.update_one(
                {"_id": doc["_id"]},
                {"$set": {
                    "status": "degraded",
                    "last_checked_at": now,
                    "elapsed_seconds": elapsed,
                    "owner_pid": "",
                    "error_msg": "orphaned_running_module" if owner_dead or legacy_orphan else "stale_running_timeout",
                }},
            )
            released += 1
        # provider_health has no owner PID, so only release entries whose
        # running marker is older than the same bounded timeout.  This keeps
        # the dashboard from advertising a dead provider lane forever.
        provider_released = 0
        try:
            for doc in self.db["provider_health"].find(
                {"status": "running"},
                {"_id": 1, "updated_at": 1, "last_success_at": 1, "endpoint": 1},
            ):
                marker = doc.get("updated_at") or doc.get("last_success_at")
                marker_dt = self._coerce_local_datetime(marker)
                if marker_dt and marker_dt >= cutoff:
                    continue
                self.db["provider_health"].update_one(
                    {"_id": doc.get("_id"), "status": "running"},
                    {"$set": {
                        "status": "degraded",
                        "stale_reason": "orphaned_running_provider",
                        "updated_at": now,
                    }},
                )
                provider_released += 1
        except Exception:
            logger.debug("provider health orphan reconciliation unavailable", exc_info=True)
        if released or provider_released:
            logger.warning(
                "released stale/orphaned state sync_modules=%d providers=%d",
                released,
                provider_released,
            )
        return released + provider_released

    def run_module(
        self,
        name: str,
        module_fn,
        market: str | None = None,
        plan: LiveSyncPlan | None = None,
    ) -> dict:
        """
        执行单个同步模块，返回执行结果。
        """
        sync_log = self.db["sync_log"]
        start_time = self._now()
        meta_id = self._meta_id(name, market)
        if name == "stock_daily" and os.getenv("STOCK_DAILY_ONLY_CODES", "").strip() and not market:
            meta_id = self._meta_id("stock_daily:manual_only_codes", market)
        plan = plan or LIVE_PLAN_BY_MODULE.get(name)
        lane = plan.lane if plan else ""

        # 标记开始
        sync_log.update_one(
            {"_id": meta_id},
            {"$set": {
                "module": name,
                "market": market or "",
                "lane": lane,
                "owner_pid": os.getpid(),
                "status": "running",
                "last_run": start_time,
                "elapsed_seconds": 0,
                "runtime_seconds": 0,
                "error_msg": "",
                "degraded_reason": "",
            }},
            upsert=True,
        )

        env_values = {}
        if lane:
            env_values["SIGNALS_CURRENT_SYNC_LANE"] = lane
        if market:
            env_values["SIGNALS_CURRENT_SYNC_MARKET"] = market
        try:
            with task_env(env_values):
                result = module_fn(self.db, proxy_url=self.proxy_url)
            elapsed = (self._now() - start_time).total_seconds()

            status, error_msg = self._classify_result(name, result)
            if plan and elapsed > plan.max_runtime_seconds and status == "ok":
                status = "degraded"
                error_msg = f"runtime_exceeded_{plan.max_runtime_seconds}s"
            finished_at = self._now()
            next_due_at = (
                finished_at + timedelta(seconds=plan.interval_seconds)
                if plan else None
            )

            # 标记完成
            sync_log.update_one(
                {"_id": meta_id},
                {"$set": {
                    "module": name,
                    "market": market or "",
                    "lane": lane,
                    "owner_pid": "",
                    "status": status,
                    "last_run": finished_at,
                    "next_due_at": next_due_at,
                    "elapsed_seconds": elapsed,
                    "runtime_seconds": elapsed,
                    "max_runtime_seconds": plan.max_runtime_seconds if plan else 0,
                    "error_msg": error_msg,
                    "degraded_reason": error_msg or "",
                    "result": result,
                }},
            )

            self._write_module_freshness(name, status, error_msg, market=market, lane=lane)
            if status == "ok":
                logger.info("✓ %s%s 完成 (%.1fs)", name, f"[{market}]" if market else "", elapsed)
            elif status == "partial":
                logger.warning("~ %s%s partial (%.1fs): %s", name, f"[{market}]" if market else "", elapsed, error_msg)
            else:
                logger.warning("! %s%s degraded (%.1fs): %s", name, f"[{market}]" if market else "", elapsed, error_msg)
            return {"module": name, "status": status, "elapsed": elapsed,
                    "market": market or "", "lane": lane, "result": result}

        except Exception as e:
            elapsed = (self._now() - start_time).total_seconds()
            finished_at = self._now()
            next_due_at = (
                finished_at + timedelta(seconds=plan.interval_seconds)
                if plan else None
            )

            sync_log.update_one(
                {"_id": meta_id},
                {"$set": {
                    "module": name,
                    "market": market or "",
                    "lane": lane,
                    "owner_pid": "",
                    "status": "error",
                    "last_run": finished_at,
                    "next_due_at": next_due_at,
                    "elapsed_seconds": elapsed,
                    "runtime_seconds": elapsed,
                    "max_runtime_seconds": plan.max_runtime_seconds if plan else 0,
                    "error_msg": str(e)[:500],
                    "degraded_reason": str(e)[:500],
                }},
            )

            self._write_module_freshness(name, "error", str(e)[:500], market=market, lane=lane)
            logger.error("✗ %s%s 失败 (%.1fs): %s", name, f"[{market}]" if market else "", elapsed, e)
            return {"module": name, "status": "error", "elapsed": elapsed,
                    "market": market or "", "lane": lane, "error": str(e)}
    def run_all(self) -> list:
        """
        一次性执行所有模块（--once 模式）。

        使用 ThreadPoolExecutor 并行执行，max_workers 控制并发。
        """
        logger.info(f"🔄 开始全量同步 ({len(self.modules)} 模块, "
                     f"{self.max_workers} 并发)")
        results = []

        parallel_modules = [
            module for module in self.modules
            if module[0] != "strategy_snapshot"
        ]

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self.run_module, name, fn): name
                for name, fn, _ in parallel_modules
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    logger.error(f"✗ {name} 未预期错误: {e}")
                    results.append({
                        "module": name, "status": "error", "error": str(e)
                    })

        if "strategy_snapshot" in self.module_map:
            results.append(self.run_one("strategy_snapshot"))

        ok = sum(1 for r in results if r["status"] == "ok")
        logger.info(f"🏁 同步完成: {ok}/{len(results)} 成功")
        return results

    def run_one(self, module_name: str) -> dict:
        """执行指定模块"""
        if module_name in self.module_map:
            fn, _ = self.module_map[module_name]
            return self.run_module(module_name, fn)
        raise ValueError(f"未知模块: {module_name}，"
                         f"可选: {[m[0] for m in self.modules]}")

    def _has_run_today(self, module_name: str, today: str, market: str | None = None) -> bool:
        doc = self.db["sync_log"].find_one(
            {"_id": self._meta_id(module_name, market)},
            {"last_run": 1, "status": 1},
        )
        if not doc:
            return False
        status = doc.get("status")
        if status != "ok":
            if status == "running":
                last_run = doc.get("last_run")
                last_run_dt = self._coerce_local_datetime(last_run)
                if last_run_dt and self._now() - last_run_dt < timedelta(hours=2):
                    return True
            return False
        last_run = doc.get("last_run")
        if not last_run:
            return False
        last_run_dt = self._coerce_local_datetime(last_run)
        if last_run_dt:
            ran_today = last_run_dt.strftime("%Y-%m-%d") == today
        elif isinstance(last_run, str):
            ran_today = last_run[:10] == today
        else:
            ran_today = False
        return ran_today

    def _has_run_recent(
        self,
        module_name: str,
        market: str,
        now: datetime,
        interval_seconds: int = INTRADAY_INTERVAL_SECONDS,
        stale_seconds: int = INTRADAY_STALE_SECONDS,
    ) -> bool:
        """Throttle live sync independently by market while honoring legacy meta."""
        sync_log = self.db["sync_log"]
        for meta_id in (self._meta_id(module_name, market), self._meta_id(module_name)):
            doc = sync_log.find_one({"_id": meta_id}, {"last_run": 1, "status": 1})
            if not doc:
                continue
            last_run = self._coerce_local_datetime(doc.get("last_run"))
            if not last_run:
                continue
            age = (now - last_run).total_seconds()
            if doc.get("status") == "running":
                return age < stale_seconds
            if age < interval_seconds:
                return True
        return False

    @staticmethod
    def _result_inserted(result: object) -> int:
        if isinstance(result, dict):
            value = result.get("inserted", 0)
            try:
                return int(value or 0)
            except Exception:
                return 0
        return 0

    def _target_counts(self, module_name: str) -> dict[str, int]:
        counts = {}
        for collection in MODULE_TARGETS.get(module_name, ()):
            try:
                counts[collection] = int(self.db[collection].estimated_document_count())
            except Exception:
                counts[collection] = 0
        return counts

    def _classify_result(self, module_name: str, result: object) -> tuple[str, str | None]:
        counts = self._target_counts(module_name)
        if isinstance(result, dict):
            result_status = str(result.get("status") or "").lower()
            if result.get("backfill_isolated"):
                return "partial", str(
                    result.get("backfill_reason")
                    or result.get("reason")
                    or "backfill_isolated"
                )
            if result_status in {"partial", "degraded", "error"}:
                return result_status, str(
                    result.get("error_msg")
                    or result.get("degraded_reason")
                    or result.get("backfill_reason")
                    or result.get("reason")
                    or ""
                )
        if not counts:
            return "ok", None
        inserted = self._result_inserted(result)
        if module_name in EMPTY_OK_MODULES:
            return "ok", None
        if inserted == 0 and all(count <= 0 for count in counts.values()):
            return "degraded", "target_empty_after_zero_insert"
        if isinstance(result, dict) and result.get("errors"):
            return "degraded", f"errors={result.get('errors')}"
        return "ok", None

    def _module_allowed_for_lanes(self, module_name: str) -> bool:
        if self.enabled_lanes is None:
            return True
        allowed = BOOTSTRAP_LANE_MODULES.get(module_name)
        if not allowed:
            plan = LIVE_PLAN_BY_MODULE.get(module_name) or LANE_MAINTENANCE_PLANS.get(module_name)
            allowed = {plan.lane} if plan else set()
        return bool(allowed & self.enabled_lanes)

    def _write_module_freshness(
        self,
        module_name: str,
        status: str,
        error_msg: str | None,
        *,
        market: str | None = None,
        lane: str = "",
    ) -> None:
        now = self._now()
        mode = "realtime" if market or module_name in REALTIME_MODULES else "historical"
        market_value = market or "A"
        for collection, count in self._target_counts(module_name).items():
            domain = COLLECTION_DOMAINS.get(collection, module_name)
            freshness_query = {"domain": domain, "market": market_value, "mode": mode, "collection": collection}
            writer_fields = WRITER_FRESHNESS_FIELDS.get(module_name)
            # Writers such as fullmarket/ETF snapshots own their freshness
            # watermark even when a historical ad-hoc request is isolated into
            # the backfill collection.  Preserve that explicit state for
            # partial results; otherwise the engine would replace it with the
            # wall-clock timestamp of the maintenance attempt.
            if writer_fields and status in {"ok", "partial"}:
                try:
                    existing = self.db["data_freshness"].find_one(
                        freshness_query,
                        {field: 1 for field in writer_fields},
                    )
                except Exception:
                    existing = None
                if existing and any(field in existing for field in writer_fields):
                    continue
            latest_dt = None
            try:
                latest = self.db[collection].find_one(
                    {},
                    {"dt": 1, "latest_dt": 1, "updated_at": 1, "signal_date": 1, "snapshot_at": 1, "freshness": 1, "stale_reason": 1},
                    sort=[("dt", -1), ("latest_dt", -1), ("signal_date", -1), ("snapshot_at", -1), ("updated_at", -1)],
                ) or {}
                value = latest.get("dt") or latest.get("latest_dt") or latest.get("signal_date") or latest.get("snapshot_at") or latest.get("updated_at")
                latest_dt = str(value.date()) if hasattr(value, "date") else (str(value)[:10] if value else None)
            except Exception:
                latest_dt = None
                latest = {}
            if count <= 0:
                freshness = "empty"
            elif status == "partial":
                freshness = "partial"
            elif status != "ok":
                freshness = "stale"
            elif latest.get("freshness") in {"fresh", "stale", "partial", "empty", "pending", "unknown"}:
                freshness = latest["freshness"]
            else:
                freshness = "fresh"
            stale_reason = error_msg or latest.get("stale_reason") or ""
            self.db["data_freshness"].update_one(
                freshness_query,
                {"$set": {
                    "domain": domain,
                    "market": market_value,
                    "mode": mode,
                    "lane": lane,
                    "collection": collection,
                    "freshness": freshness,
                    "latest_dt": latest_dt,
                    "as_of": latest_dt,
                    "updated_at": now,
                    "stale_reason": stale_reason,
                    "count": count,
                }},
                upsert=True,
            )

    def _mark_market_unavailable(self, market: str, reason: str, now: datetime, *, module: str = "live_bundle") -> None:
        self.db["sync_log"].update_one(
            {"_id": self._meta_id(module, market)},
            {"$set": {
                "module": module,
                "market": market,
                "lane": "unavailable",
                "status": "unavailable",
                "last_run": now,
                "error_msg": reason,
            }},
            upsert=True,
        )
        self.db["data_freshness"].update_one(
            {"domain": "live_bundle", "market": market, "mode": "realtime", "collection": "intraday_bundle"},
            {"$set": {
                "domain": "live_bundle",
                "market": market,
                "mode": "realtime",
                "lane": "unavailable",
                "collection": "intraday_bundle",
                "freshness": "unavailable",
                "latest_dt": now.date().isoformat(),
                "as_of": now.date().isoformat(),
                "updated_at": now,
                "stale_reason": reason,
                "count": 0,
            }},
            upsert=True,
        )

    def _module_running_recent(
        self,
        module_name: str,
        market: str,
        now: datetime,
        stale_seconds: int,
    ) -> bool:
        """True when a live module is already owned by a fresh worker."""
        sync_log = self.db["sync_log"]
        for meta_id in (self._meta_id(module_name, market), self._meta_id(module_name)):
            doc = sync_log.find_one({"_id": meta_id}, {"last_run": 1, "status": 1})
            if not doc or doc.get("status") != "running":
                continue
            last_run = self._coerce_local_datetime(doc.get("last_run"))
            if last_run and (now - last_run).total_seconds() < stale_seconds:
                return True
        return False

    def _run_intraday_bundle(self, active_markets: set[Market], now: datetime, *, force: bool = False) -> list[dict]:
        results: list[dict] = []
        for market in sorted(active_markets, key=lambda item: item.value):
            market_key = market.value
            plans = LIVE_SYNC_PLANS.get(market, ())
            if self.enabled_lanes is not None:
                plans = tuple(plan for plan in plans if plan.lane in self.enabled_lanes)
            if not plans:
                unavailable_id = "live_bundle"
                if self.enabled_lanes is not None:
                    unavailable_id = f"live_bundle:{','.join(sorted(self.enabled_lanes))}"
                if not self._has_run_recent(unavailable_id, market_key, now):
                    reason = f"{market_key} live data source unavailable"
                    logger.warning("%s live bundle unavailable: %s", market_key, reason)
                    self._mark_market_unavailable(market_key, reason, now, module=unavailable_id)
                continue

            runnable: list[tuple[int, LiveSyncPlan, object]] = []
            for plan in sorted(plans, key=lambda item: item.priority):
                module_name = plan.module
                if module_name == "sector_transition_scan" and not _sector_transition_enabled():
                    continue
                if module_name not in self.module_map:
                    results.append({"module": module_name, "market": market_key, "status": "missing"})
                    logger.warning("live bundle module missing: %s[%s]", module_name, market_key)
                    continue
                if self._module_running_recent(module_name, market_key, now, plan.stale_seconds):
                    continue
                if not force and self._has_run_recent(
                    module_name,
                    market_key,
                    now,
                    interval_seconds=plan.interval_seconds,
                    stale_seconds=plan.stale_seconds,
                ):
                    continue
                fn, _ = self.module_map[module_name]
                runnable.append((LIVE_SYNC_STAGE_BY_MODULE.get(module_name, plan.priority), plan, fn))
            for stage in sorted({item[0] for item in runnable}):
                stage_items = [item for item in runnable if item[0] == stage]
                if not stage_items:
                    continue
                workers = min(getattr(self, "max_workers", 4), max(1, len(stage_items)))
                logger.info(
                    "⏱ live stage=%s market=%s modules=%s workers=%d",
                    stage,
                    market_key,
                    [item[1].module for item in stage_items],
                    workers,
                )
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"live-{market_key}-s{stage}") as executor:
                    future_map = {
                        executor.submit(self.run_module, plan.module, fn, market=market_key, plan=plan): plan
                        for _stage, plan, fn in stage_items
                    }
                    for future in as_completed(future_map):
                        plan = future_map[future]
                        try:
                            results.append(future.result())
                        except Exception as exc:
                            logger.error("✗ live %s[%s] unexpected error: %s", plan.module, market_key, exc)
                            results.append({
                                "module": plan.module,
                                "market": market_key,
                                "lane": plan.lane,
                                "status": "error",
                                "error": str(exc),
                            })
        return results

    def run_live_once(self, *, force: bool = False) -> list[dict]:
        """Run one live-lane pass for currently active markets."""
        now = self._now()
        active_markets = get_active_markets(self._now_utc())
        quote_preopen = self._quote_preopen_enabled() and self._a_quote_preopen_active(now)
        if not active_markets and quote_preopen:
            active_markets = {Market.A}
        # A manual dashboard refresh may pass force_live=True.  Do not turn a
        # weekend/holiday click into a provider storm or write a prior trading
        # day's data into realtime caches.  Keep the useful weekday after-hours
        # force behavior for operators who intentionally request a close pass.
        if not active_markets and force and self._is_a_trading_day(now.date()):
            active_markets = {Market.A}
        if not active_markets:
            return []
        return self._run_intraday_bundle(active_markets, now, force=force)

    def _run_scheduled_modules(self, now: datetime, today: str) -> list[dict]:
        results: list[dict] = []
        if self.enabled_lanes is not None:
            return results
        for name, fn, schedule in self.modules:
            if schedule in {"live only", "postmarket only"}:
                continue
            if not self._module_allowed_for_lanes(name):
                continue
            plan = LANE_MAINTENANCE_PLANS.get(name)
            if self._has_run_today(name, today):
                continue

            if self._schedule_due(schedule, now):
                lane_label = f" lane={plan.lane}" if plan else ""
                logger.info("⏰ 触发 %s%s (schedule: %s)", name, lane_label, schedule)
                results.append(self.run_module(name, fn, plan=plan))
        return results

    @staticmethod
    def _parse_schedule_time(schedule: str) -> dt_time:
        for token in schedule.split():
            if ":" in token:
                if "-" in token:
                    token = token.split("-", 1)[0]
                hour, minute = token.split(":", 1)
                return dt_time(int(hour), int(minute))
        return dt_time(0, 0)

    @staticmethod
    def _parse_schedule_end_time(schedule: str) -> dt_time | None:
        for token in schedule.split():
            if ":" in token and "-" in token:
                _, end_token = token.split("-", 1)
                hour, minute = end_token.split(":", 1)
                return dt_time(int(hour), int(minute))
        return None

    @classmethod
    def _schedule_due(cls, schedule: str, now: datetime) -> bool:
        # Lane markers are dispatched by their dedicated live/postmarket
        # runners and are never due in the generic scheduled lane.
        if schedule in {"live only", "postmarket only"}:
            return False
        weekday = now.weekday()
        if "weekday" in schedule:
            try:
                from signals.core.trading_dates import is_trading_day

                if not is_trading_day("A", now.date()):
                    return False
            except Exception:
                if weekday >= 5:
                    return False
        if "Sunday" in schedule and weekday != 6:
            return False
        start = cls._parse_schedule_time(schedule)
        end = cls._parse_schedule_end_time(schedule)
        if end is not None and now.time() > end:
            return False
        return now.time() >= start

    @staticmethod
    def _is_a_trading_day(day) -> bool:
        try:
            from signals.core.trading_dates import is_trading_day

            return bool(is_trading_day("A", day))
        except Exception:
            return day.weekday() < 5

    @classmethod
    def _a_quote_preopen_active(cls, now: datetime) -> bool:
        if not cls._is_a_trading_day(now.date()):
            return False
        return QUOTE_PREOPEN_START <= now.time() < QUOTE_PREOPEN_END

    @classmethod
    def _seconds_until_a_quote_preopen(cls, now: datetime) -> int:
        if not cls._is_a_trading_day(now.date()):
            return 24 * 60 * 60
        start = datetime.combine(now.date(), QUOTE_PREOPEN_START)
        if now < start:
            return max(0, int((start - now).total_seconds()))
        return 24 * 60 * 60

    def _quote_preopen_enabled(self) -> bool:
        return self.enabled_lanes is not None and self.enabled_lanes <= PREOPEN_LIVE_LANES

    def bootstrap_preheat(self) -> list[dict]:
        """Run conservative startup preheat for empty critical collections."""
        checks = [
            ("cache_preheat", "bars"),
            ("signal_pool", "signals"),
            ("market_pools", "market_pools"),
            ("quote_snapshots", "quote_snapshots"),
            ("board_ranking", "board_ranking"),
            ("board_cons", "board_constituents"),
            ("index_daily", "index_bars"),
            ("strategy_snapshot", "strategy_snapshots"),
        ]
        results = []
        for module_name, collection in checks:
            if not self._module_allowed_for_lanes(module_name):
                continue
            if module_name not in self.module_map:
                continue
            try:
                count = self.db[collection].estimated_document_count()
            except Exception:
                count = 0
            if count > 0:
                continue
            logger.info("bootstrap preheat: %s empty, running %s", collection, module_name)
            results.append(self.run_one(module_name))
        return results

    def run_daemon(self, check_interval: int = 60):
        """
        常驻调度模式（--daemon）。

        每分钟检查一次。到达预设时间且当天未成功运行时触发对应模块；
        daemon 重启或错过精确分钟后仍可补跑。
        """
        lanes_label = ",".join(sorted(self.enabled_lanes)) if self.enabled_lanes else "all"
        logger.info("🐲 同步守护进程启动 lane=%s", lanes_label)
        self.bootstrap_preheat()

        while True:
            now = self._now()
            today = now.strftime("%Y-%m-%d")
            active_markets = get_active_markets(self._now_utc())
            quote_preopen = self._quote_preopen_enabled() and self._a_quote_preopen_active(now)
            if not active_markets and quote_preopen:
                active_markets = {Market.A}

            if self.enabled_lanes is None:
                self._run_scheduled_modules(now, today)

            if active_markets:
                active_label = ",".join(market.value for market in sorted(active_markets, key=lambda item: item.value))
                logger.info("live markets active=%s BJ=%s ET=%s", active_label, now.strftime("%Y-%m-%d %H:%M:%S"), datetime.now(TZ_US_EAST).strftime("%Y-%m-%d %H:%M:%S %Z"))
                self._run_intraday_bundle(active_markets, now)
                sleep_seconds = check_interval
            else:
                next_seconds = next_live_check_seconds(self._now_utc())
                if self._quote_preopen_enabled():
                    next_seconds = min(next_seconds, self._seconds_until_a_quote_preopen(now))
                sleep_seconds = min(max(next_seconds, check_interval), 3600)

            time.sleep(sleep_seconds)


def _setup_logging(level: str = "INFO"):
    """配置日志"""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    parser = argparse.ArgumentParser(description="隆小侠数据同步引擎")
    parser.add_argument("--once", action="store_true",
                        help="一次性执行所有模块")
    parser.add_argument("--daemon", action="store_true",
                        help="常驻调度模式")
    parser.add_argument("--postmarket-daemon", action="store_true",
                        help="盘后常驻 DAG worker，按北京时间触发并支持断点续跑")
    parser.add_argument("--postmarket-once", action="store_true",
                        help="立即执行一次盘后 DAG")
    parser.add_argument("--resume", type=str, default=None,
                        help="恢复指定 postmarket run_id，例如 postmarket:2026-04-28")
    parser.add_argument("--module", type=str, default=None,
                        help="只执行指定模块（配合 --once）")
    parser.add_argument("--lane", action="append", default=[],
                        help="只运行指定第二屏 lane，可重复或逗号分隔：quote_lane/signal_lane/workbench_lane/board_lane")
    parser.add_argument("--workers", type=int, default=4,
                        help="并行工人数（默认 4）")
    parser.add_argument("--check-interval", type=int, default=int(os.getenv("SIGNALS_SYNC_CHECK_INTERVAL_SECONDS", "60")),
                        help="daemon 检查间隔秒数")
    parser.add_argument("--log-level", type=str, default="INFO",
                        help="日志级别")
    args = parser.parse_args()

    _setup_logging(args.log_level)

    import config
    if not config.MONGO_URL:
        logger.error("MONGO_URL 未配置，请设置环境变量 MONGO_URL")
        sys.exit(1)

    allowed_lanes = {
        "quote_lane",
        "signal_lane",
        "workbench_lane",
        "board_lane",
    }
    enabled_lanes: set[str] = set()
    for raw_lane in args.lane or []:
        for lane in str(raw_lane).split(","):
            lane = lane.strip()
            if lane:
                enabled_lanes.add(lane)
    unknown_lanes = enabled_lanes - allowed_lanes
    if unknown_lanes:
        logger.error("未知 lane: %s，可选: %s", sorted(unknown_lanes), sorted(allowed_lanes))
        sys.exit(2)

    engine = SyncEngine(
        mongo_url=config.MONGO_URL,
        proxy_url=config.EM_PROXY_URL if config.EM_PROXY_ENABLED else None,
        max_workers=args.workers,
        enabled_lanes=enabled_lanes or None,
    )

    if args.once:
        if args.module:
            result = engine.run_one(args.module)
            print(f"\n{result}")
        else:
            results = engine.run_all()
            for r in results:
                status = "✓" if r["status"] == "ok" else "✗"
                print(f"  {status} {r['module']}: {r.get('elapsed', 0):.1f}s")
    elif args.postmarket_once:
        from .postmarket import PostmarketRunner

        result = PostmarketRunner(engine, max_workers=args.workers).run_once(resume_run_id=args.resume)
        print(f"\n{result}")
    elif args.postmarket_daemon:
        from .postmarket import PostmarketRunner

        PostmarketRunner(engine, max_workers=args.workers).run_daemon()
    elif args.daemon:
        engine.run_daemon(check_interval=max(5, args.check_interval))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
