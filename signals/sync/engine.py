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

from signals.core.market_hours import Market, TZ_BEIJING, TZ_US_EAST, TZ_UTC, get_active_markets
from .db import get_db, close as close_db

logger = logging.getLogger("signals.sync")


MODULE_TARGETS = {
    "cache_preheat": ("bars",),
    "signal_pool": ("signals",),
    "market_pools": ("market_pools",),
    "quote_snapshots": ("quote_snapshots",),
    "strategy_snapshot": ("strategy_snapshots",),
    "stock_daily": ("bars",),
    "index_daily": ("index_bars",),
    "stock_minute": ("bars",),
    "index_minute": ("index_bars",),
    "board_ranking": ("board_ranking", "concept_ranking"),
    "board_heat_minute": ("board_heat_ticks",),
    "concept_heat_minute": ("board_heat_ticks",),
    "minute_readiness_probe": ("minute_readiness",),
    "weekly_rollup": ("bars", "index_bars"),
    "terminal_realtime_pool": ("terminal_realtime_pool",),
    "board_cons": ("board_constituents", "concept_constituents"),
}

COLLECTION_DOMAINS = {
    "bars": "kline",
    "kline_cache": "kline",
    "index_bars": "index",
    "board_ranking": "board",
    "concept_ranking": "concept",
    "board_heat_ticks": "board_heat",
    "minute_readiness": "readiness",
    "terminal_realtime_pool": "terminal_pool",
    "board_constituents": "constituents",
    "concept_constituents": "constituents",
    "quote_snapshots": "quote",
    "market_pools": "market_pool",
    "signals": "signal",
    "strategy_snapshots": "strategy",
}

REALTIME_MODULES = {
    "market_pools",
    "quote_snapshots",
    "stock_minute",
    "index_minute",
    "board_heat_minute",
    "concept_heat_minute",
    "minute_readiness_probe",
    "board_ranking",
    "strategy_snapshot",
}

SYNC_TZ = ZoneInfo(os.getenv("SIGNALS_SYNC_TIMEZONE", "Asia/Shanghai"))


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
SIGNAL_LANE_INTERVAL_SECONDS = _env_seconds("SIGNALS_LIVE_SIGNAL_INTERVAL_SECONDS", 5 * 60)
WORKBENCH_LANE_INTERVAL_SECONDS = _env_seconds("SIGNALS_LIVE_WORKBENCH_INTERVAL_SECONDS", 10 * 60)
BOARD_LANE_INTERVAL_SECONDS = _env_seconds("SIGNALS_LIVE_BOARD_INTERVAL_SECONDS", 5 * 60)


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
        LiveSyncPlan("quote_snapshots", "quote_lane", QUOTE_LANE_INTERVAL_SECONDS, _lane_stale(QUOTE_LANE_INTERVAL_SECONDS, 3), 45, 10),
        LiveSyncPlan("index_minute", "signal_lane", SIGNAL_LANE_INTERVAL_SECONDS, _lane_stale(SIGNAL_LANE_INTERVAL_SECONDS, 3), 120, 20),
        LiveSyncPlan("stock_minute", "signal_lane", SIGNAL_LANE_INTERVAL_SECONDS, _lane_stale(SIGNAL_LANE_INTERVAL_SECONDS, 3), 240, 30),
        LiveSyncPlan("minute_readiness_probe", "signal_lane", SIGNAL_LANE_INTERVAL_SECONDS, _lane_stale(SIGNAL_LANE_INTERVAL_SECONDS, 3), 60, 35),
        LiveSyncPlan("market_pools", "workbench_lane", WORKBENCH_LANE_INTERVAL_SECONDS, _lane_stale(WORKBENCH_LANE_INTERVAL_SECONDS, 3), 60, 40),
        LiveSyncPlan("strategy_snapshot", "workbench_lane", WORKBENCH_LANE_INTERVAL_SECONDS, _lane_stale(WORKBENCH_LANE_INTERVAL_SECONDS, 3), 90, 50),
        LiveSyncPlan("board_heat_minute", "board_lane", BOARD_LANE_INTERVAL_SECONDS, _lane_stale(BOARD_LANE_INTERVAL_SECONDS, 3), 180, 60),
        LiveSyncPlan("concept_heat_minute", "board_lane", BOARD_LANE_INTERVAL_SECONDS, _lane_stale(BOARD_LANE_INTERVAL_SECONDS, 3), 180, 65),
    ),
    # HK/US slots are explicit and independently throttled. Data-source modules
    # can be plugged in here without affecting the A-share live bundle.
    Market.HK: (),
    Market.US: (),
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
    "stock_minute": LiveSyncPlan("stock_minute", "signal_lane", 24 * 60 * 60, 60 * 60, 240, 5),
    "index_minute": LiveSyncPlan("index_minute", "signal_lane", 24 * 60 * 60, 60 * 60, 120, 6),
    "board_heat_minute": LiveSyncPlan("board_heat_minute", "board_lane", 24 * 60 * 60, 60 * 60, 180, 7),
    "concept_heat_minute": LiveSyncPlan("concept_heat_minute", "board_lane", 24 * 60 * 60, 60 * 60, 180, 8),
    "minute_readiness_probe": LiveSyncPlan("minute_readiness_probe", "signal_lane", 24 * 60 * 60, 60 * 60, 60, 9),
    "stock_daily": LiveSyncPlan("stock_daily", "workbench_lane", 24 * 60 * 60, 4 * 60 * 60, 900, 30),
    "index_daily": LiveSyncPlan("index_daily", "workbench_lane", 24 * 60 * 60, 2 * 60 * 60, 300, 40),
    "weekly_rollup": LiveSyncPlan("weekly_rollup", "workbench_lane", 24 * 60 * 60, 2 * 60 * 60, 600, 45),
    "board_ranking": LiveSyncPlan("board_ranking", "board_lane", 24 * 60 * 60, 2 * 60 * 60, 300, 60),
    "board_cons": LiveSyncPlan("board_cons", "board_lane", 24 * 60 * 60, 6 * 60 * 60, 900, 70),
    "signal_pool": LiveSyncPlan("signal_pool", "workbench_lane", 24 * 60 * 60, 2 * 60 * 60, 300, 80),
    "strategy_snapshot": LiveSyncPlan("strategy_snapshot", "workbench_lane", 24 * 60 * 60, 2 * 60 * 60, 120, 90),
    "terminal_realtime_pool": LiveSyncPlan("terminal_realtime_pool", "workbench_lane", 24 * 60 * 60, 2 * 60 * 60, 120, 95),
    "cache_preheat": LiveSyncPlan("cache_preheat", "workbench_lane", 24 * 60 * 60, 2 * 60 * 60, 180, 100),
}

BOOTSTRAP_LANE_MODULES = {
    "quote_snapshots": {"quote_lane"},
    "market_pools": {"workbench_lane"},
    "cache_preheat": {"workbench_lane"},
    "signal_pool": {"workbench_lane"},
    "index_daily": {"workbench_lane"},
    "weekly_rollup": {"workbench_lane"},
    "terminal_realtime_pool": {"workbench_lane"},
    "strategy_snapshot": {"workbench_lane"},
    "board_ranking": {"board_lane"},
    "board_heat_minute": {"board_lane"},
    "concept_heat_minute": {"board_lane"},
    "minute_readiness_probe": {"signal_lane"},
    "board_cons": {"board_lane"},
}


class SyncEngine:
    """
    数据同步调度器。

    管理 6 个同步模块的执行：
    - stock_daily:   ~5000 A股日线（增量）
    - index_daily:   11 只指数日线（全量）
    - stock_minute:  活跃标的 30M/15M（增量）
    - index_minute:  11 只指数 30M/15M
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
            {"_id": {"$regex": ":_meta$"}, "status": "running"},
            {"_id": 1, "module": 1, "last_run": 1, "owner_pid": 1},
        ))
        if not stale_docs:
            return 0

        released = 0
        for doc in stale_docs:
            last_run = doc.get("last_run")
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
        if released:
            logger.warning("released %d stale/orphaned running sync modules", released)
        return released

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

        env_before = {
            "SIGNALS_CURRENT_SYNC_LANE": os.environ.get("SIGNALS_CURRENT_SYNC_LANE"),
            "SIGNALS_CURRENT_SYNC_MARKET": os.environ.get("SIGNALS_CURRENT_SYNC_MARKET"),
        }
        if lane:
            os.environ["SIGNALS_CURRENT_SYNC_LANE"] = lane
        if market:
            os.environ["SIGNALS_CURRENT_SYNC_MARKET"] = market
        try:
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
        finally:
            for key, value in env_before.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

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
            if result_status in {"partial", "degraded", "error"}:
                return result_status, str(result.get("error_msg") or result.get("degraded_reason") or result.get("reason") or "")
        if not counts:
            return "ok", None
        inserted = self._result_inserted(result)
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
                {"domain": domain, "market": market_value, "mode": mode, "collection": collection},
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

    def _run_intraday_bundle(self, active_markets: set[Market], now: datetime) -> list[dict]:
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

            for plan in sorted(plans, key=lambda item: item.priority):
                module_name = plan.module
                if module_name not in self.module_map:
                    results.append({"module": module_name, "market": market_key, "status": "missing"})
                    logger.warning("live bundle module missing: %s[%s]", module_name, market_key)
                    continue
                if self._has_run_recent(
                    module_name,
                    market_key,
                    now,
                    interval_seconds=plan.interval_seconds,
                    stale_seconds=plan.stale_seconds,
                ):
                    continue
                fn, _ = self.module_map[module_name]
                logger.info("⏱ %s trigger %s[%s]", plan.lane, module_name, market_key)
                results.append(self.run_module(module_name, fn, market=market_key, plan=plan))
        return results

    def _run_scheduled_modules(self, now: datetime, today: str) -> list[dict]:
        results: list[dict] = []
        for name, fn, schedule in self.modules:
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
        weekday = now.weekday()
        if "weekday" in schedule and weekday >= 5:
            return False
        if "Sunday" in schedule and weekday != 6:
            return False
        start = cls._parse_schedule_time(schedule)
        end = cls._parse_schedule_end_time(schedule)
        if end is not None and now.time() > end:
            return False
        return now.time() >= start

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

            self._run_scheduled_modules(now, today)

            if active_markets:
                active_label = ",".join(market.value for market in sorted(active_markets, key=lambda item: item.value))
                logger.info("live markets active=%s BJ=%s ET=%s", active_label, now.strftime("%Y-%m-%d %H:%M:%S"), datetime.now(TZ_US_EAST).strftime("%Y-%m-%d %H:%M:%S %Z"))
                self._run_intraday_bundle(active_markets, now)

            time.sleep(check_interval)


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
    parser.add_argument("--module", type=str, default=None,
                        help="只执行指定模块（配合 --once）")
    parser.add_argument("--lane", action="append", default=[],
                        help="只运行指定第二屏 lane，可重复或逗号分隔：quote_lane/signal_lane/workbench_lane/board_lane")
    parser.add_argument("--workers", type=int, default=4,
                        help="并行工人数（默认 4）")
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
    elif args.daemon:
        engine.run_daemon()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
