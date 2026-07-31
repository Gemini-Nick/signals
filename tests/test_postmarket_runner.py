# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from signals.sync import postmarket as pm


def test_default_postmarket_tasks_split_long_market_data_tasks():
    spot = next(task for task in pm.POSTMARKET_TASKS if task.module == "fullmarket_spot_snapshot")
    etf_spot = next(task for task in pm.POSTMARKET_TASKS if task.module == "etf_spot_snapshot")
    quote = next(task for task in pm.POSTMARKET_TASKS if task.module == "quote_snapshots")
    stock_daily = [task for task in pm.POSTMARKET_TASKS if task.module == "stock_daily"]
    hk_stock_daily = [task for task in pm.POSTMARKET_TASKS if task.module == "hk_stock_daily"]
    stock_30m = [task for task in pm.POSTMARKET_TASKS if task.module == "stock_30m_fullmarket"]
    board_cons = [task for task in pm.POSTMARKET_TASKS if task.module == "board_cons"]
    index_daily = next(task for task in pm.POSTMARKET_TASKS if task.module == "index_daily")
    weekly = next(task for task in pm.POSTMARKET_TASKS if task.module == "weekly_rollup")
    ma_climb_scan = next(task for task in pm.POSTMARKET_TASKS if task.module == "ma_climb_scan")
    technical_scan = next(task for task in pm.POSTMARKET_TASKS if task.module == "technical_signal_scan")
    hot_rank = next(task for task in pm.POSTMARKET_TASKS if task.module == "hot_rank_clues")
    chain = next(task for task in pm.POSTMARKET_TASKS if task.module == "chain_heat_snapshots")
    chain_rebuild = next(task for task in pm.POSTMARKET_TASKS if task.module == "postmarket_chain_rebuild")
    strategy_snapshot = next(task for task in pm.POSTMARKET_TASKS if task.module == "strategy_snapshot")
    terminal_pool = next(task for task in pm.POSTMARKET_TASKS if task.module == "terminal_realtime_pool")

    assert spot.phase == "market_data"
    assert etf_spot.phase == "market_data"
    assert etf_spot.depends_on == ()
    assert len(stock_daily) == 16
    assert len(hk_stock_daily) == 8
    assert len(stock_30m) == 16
    assert {task.shard_key for task in stock_daily} == {f"shard_{idx:02d}" for idx in range(16)}
    assert {task.shard_key for task in hk_stock_daily} == {f"shard_{idx:02d}" for idx in range(8)}
    assert {task.shard_key for task in stock_30m} == {f"shard_{idx:02d}" for idx in range(16)}
    assert all(task.env["STOCK_DAILY_SCOPE"] == "all" for task in stock_daily)
    assert all(task.env["STOCK_DAILY_TODAY_ONLY"] == "true" for task in stock_daily)
    assert all(task.env["HK_STOCK_DAILY_SCOPE"] == "all" for task in hk_stock_daily)
    assert all(task.phase == "hk_market_data" for task in hk_stock_daily)
    assert all(task.blocks_run is False for task in hk_stock_daily)
    assert all(task.blocks_run is False for task in stock_30m)
    assert quote.depends_on == ("fullmarket_spot_snapshot:all", "etf_spot_snapshot:all")
    assert all(task.depends_on == ("fullmarket_spot_snapshot:all", "etf_spot_snapshot:all") for task in stock_daily)
    assert index_daily.depends_on == ("quote_snapshots:all",)
    assert "etf_spot_snapshot:all" in strategy_snapshot.depends_on
    assert all(task.task_key not in technical_scan.depends_on for task in stock_30m)
    assert not (set(task.task_key for task in hk_stock_daily) & set(technical_scan.depends_on))
    assert {task.shard_key for task in board_cons} == {"board", "concept"}
    assert all(task.depends_on == ("board_ranking:all",) for task in board_cons)
    assert set(task.task_key for task in stock_daily).issubset(set(weekly.depends_on))
    assert set(task.task_key for task in stock_daily).issubset(set(ma_climb_scan.depends_on))
    assert "weekly_rollup:all" not in ma_climb_scan.depends_on
    assert not (set(task.task_key for task in hk_stock_daily) & set(weekly.depends_on))
    assert weekly.env["WEEKLY_ROLLUP_SCOPE"] == "postmarket_candidates"
    assert weekly.env["WEEKLY_ROLLUP_MAX_SYMBOLS"] == "300"
    assert technical_scan.env["TECHNICAL_SIGNAL_SCAN_SCOPE"] == "postmarket_candidates"
    assert technical_scan.env["TECHNICAL_SIGNAL_SCAN_MARKETS"] == "A"
    assert technical_scan.env["TECHNICAL_SIGNAL_SCAN_REQUIRED_FREQS"] == "日线,周线"
    assert technical_scan.env["TECHNICAL_SIGNAL_POSTMARKET_MAX_SYMBOLS"] == "300"
    assert hot_rank.depends_on == ("technical_signal_scan:all", "ma_climb_scan:all")
    assert "ma_climb_scan:all" in terminal_pool.depends_on
    assert "hot_rank_clues:all" in terminal_pool.depends_on
    assert not (set(task.task_key for task in board_cons) & set(chain.depends_on))
    assert chain.phase == "chain_context"
    assert chain.depends_on == ("board_ranking:all",)
    assert chain_rebuild.phase == "chain_context"
    assert "security_business_facts:all" not in chain_rebuild.depends_on
    optional_task_keys = {task.task_key for task in pm.POSTMARKET_TASKS if not task.blocks_run}
    assert all(
        dep not in optional_task_keys
        for task in pm.POSTMARKET_TASKS
        if task.blocks_run
        for dep in task.depends_on
    )
    assert pm.POSTMARKET_PHASES.index("chain_context") < pm.POSTMARKET_PHASES.index("derived")
    assert pm.POSTMARKET_PHASES.index("minute_preheat") < pm.POSTMARKET_PHASES.index("minute_fullmarket")
    assert pm.POSTMARKET_PHASES.index("minute_fullmarket") < pm.POSTMARKET_PHASES.index("hk_market_data")
    stock_minute_tasks = [task for task in pm.POSTMARKET_TASKS if task.module == "stock_minute"]
    chain_minute = next(task for task in stock_minute_tasks if task.shard_key == "chain_representatives")
    terminal_minute = next(task for task in stock_minute_tasks if task.shard_key == "all")
    readiness = next(task for task in pm.POSTMARKET_TASKS if task.module == "minute_readiness_probe")
    close_index_minute = next(task for task in pm.POSTMARKET_TASKS if task.module == "index_minute")
    assert chain_minute.phase == "chain_context"
    assert chain_minute.depends_on == ("chain_heat_snapshots:all", "postmarket_chain_rebuild:all")
    assert chain_minute.env["STOCK_MINUTE_POSTMARKET_MAX_CODES"] == "160"
    assert chain_minute.env["STOCK_MINUTE_POSTMARKET_ROLLUP_LIMIT"] == "40"
    assert chain_minute.env["STOCK_MINUTE_WORKERS"] == "4"
    assert chain_minute.env["STOCK_MINUTE_CALL_INTERVAL"] == "0.15"
    assert terminal_minute.phase == "minute_preheat"
    assert terminal_minute.env["STOCK_MINUTE_FREQS"] == "5min,15min"
    assert terminal_minute.env["STOCK_MINUTE_POSTMARKET_MAX_CODES"] == "240"
    assert terminal_minute.env["STOCK_MINUTE_WORKERS"] == "6"
    assert terminal_minute.env["STOCK_MINUTE_CALL_INTERVAL"] == "0.15"
    assert readiness.env["MINUTE_READINESS_SELECTION_META_ID"] == "stock_minute:postmarket_selection:_meta"
    assert readiness.blocks_run is False
    assert close_index_minute.phase == "market_data"


def test_postmarket_trade_date_skips_cn_labor_day_holiday():
    assert pm._postmarket_trade_date(datetime(2026, 5, 1, 16, 30)) == "2026-04-30"


def test_postmarket_should_not_run_on_cn_labor_day_holiday():
    assert pm.PostmarketRunner.should_run_now(datetime(2026, 5, 1, 16, 30)) is False


def test_postmarket_should_run_after_cn_trading_day_close():
    assert pm.PostmarketRunner.should_run_now(datetime(2026, 4, 30, 16, 30)) is True


def test_postmarket_waits_for_hk_close_by_default():
    assert pm.PostmarketRunner.should_run_now(datetime(2026, 4, 30, 15, 50)) is False


class _Cursor(list):
    def sort(self, keys, *args, **kwargs):
        if isinstance(keys, list):
            for key, direction in reversed(keys):
                super().sort(key=lambda item: item.get(key) or "", reverse=direction < 0)
        else:
            direction = args[0] if args else 1
            super().sort(key=lambda item: item.get(keys) or "", reverse=direction < 0)
        return self

    def limit(self, n):
        return _Cursor(self[:n])


class _Collection:
    def __init__(self):
        self.docs = {}

    def find_one(self, query=None, projection=None, sort=None):
        rows = self.find(query or {}, projection)
        if sort:
            rows = rows.sort(sort)
        return rows[0] if rows else None

    def find(self, query=None, projection=None):
        query = query or {}
        rows = _Cursor()
        for doc in self.docs.values():
            if self._matches(doc, query):
                rows.append(self._project(doc, projection))
        return rows

    def update_one(self, query=None, update=None, upsert=False, **kwargs):
        query = query or {}
        update = update or {}
        doc_id = query.get("_id")
        if doc_id is None:
            doc_id = query.get("run_id") or str(len(self.docs) + 1)
        doc = self.docs.get(doc_id)
        inserted = False
        if doc is None:
            if not upsert and "_id" in query:
                doc = {"_id": doc_id}
                self.docs[doc_id] = doc
            elif upsert:
                doc = {"_id": doc_id}
                self.docs[doc_id] = doc
                inserted = True
            else:
                return None
        if inserted:
            doc.update(update.get("$setOnInsert", {}))
        for key, value in update.get("$set", {}).items():
            doc[key] = value
        for key, value in update.get("$inc", {}).items():
            doc[key] = doc.get(key, 0) + value
        return None

    def _matches(self, doc, query):
        for key, expected in query.items():
            actual = doc.get(key)
            if isinstance(expected, dict):
                if "$regex" in expected:
                    import re

                    if not re.search(expected["$regex"], str(actual or "")):
                        return False
                else:
                    return False
            elif actual != expected:
                return False
        return True

    def _project(self, doc, projection):
        if not projection:
            return dict(doc)
        out = {}
        include_id = projection.get("_id", 1)
        if include_id and "_id" in doc:
            out["_id"] = doc["_id"]
        for key, enabled in projection.items():
            if key == "_id" or not enabled:
                continue
            if key in doc:
                out[key] = doc[key]
        return out


class _Db(dict):
    def __missing__(self, key):
        self[key] = _Collection()
        return self[key]


class _Engine:
    def __init__(self, db, module_map):
        self.db = db
        self.module_map = module_map
        self.proxy_url = None

    def run_module(self, name, module_fn, market=None, plan=None):
        result = module_fn(self.db, proxy_url=self.proxy_url)
        return {"module": name, "status": result.get("status", "ok"), "result": result}


def test_postmarket_runner_resumes_only_unfinished_tasks(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("alpha", "data"),
        pm.PostmarketTaskSpec("beta", "derived", depends_on=("alpha:all",)),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("data", "derived"))

    calls = []
    beta_attempts = {"count": 0}

    def alpha(db, proxy_url=None):
        calls.append("alpha")
        return {"status": "ok"}

    def beta(db, proxy_url=None):
        calls.append("beta")
        beta_attempts["count"] += 1
        if beta_attempts["count"] == 1:
            return {"status": "error", "error_msg": "boom"}
        return {"status": "ok"}

    db = _Db()
    engine = _Engine(db, {"alpha": (alpha, ""), "beta": (beta, "")})
    runner = pm.PostmarketRunner(engine, max_workers=1)

    first = runner.run_once(trade_date="2026-04-28")
    second = runner.run_once(resume_run_id="postmarket:2026-04-28")

    assert first["status"] == "partial"
    assert second["status"] == "ok"
    assert calls == ["alpha", "beta", "beta"]
    assert db["sync_tasks"].docs["postmarket:2026-04-28:alpha:all"]["status"] == "ok"
    assert db["sync_tasks"].docs["postmarket:2026-04-28:beta:all"]["attempts"] == 2


def test_postmarket_runner_marks_superseded_tasks_obsolete(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("stock_daily", "market_data", shard_key="shard_00"),
        pm.PostmarketTaskSpec("stock_daily", "market_data", shard_key="shard_01"),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)

    db = _Db()
    db["sync_tasks"].docs["postmarket:2026-04-28:stock_daily:all"] = {
        "_id": "postmarket:2026-04-28:stock_daily:all",
        "run_id": "postmarket:2026-04-28",
        "module": "stock_daily",
        "shard_key": "all",
        "status": "stale",
    }
    runner = pm.PostmarketRunner(_Engine(db, {}), max_workers=1)

    runner._init_tasks("postmarket:2026-04-28", "2026-04-28")

    legacy = db["sync_tasks"].docs["postmarket:2026-04-28:stock_daily:all"]
    current = db["sync_tasks"].docs["postmarket:2026-04-28:stock_daily:shard_00"]
    assert legacy["status"] == "obsolete"
    assert legacy["error_msg"] == "superseded_by_sharded_postmarket_dag"
    assert current["status"] == "pending"
    assert current["task_key"] == "stock_daily:shard_00"


def test_postmarket_resets_completed_task_when_dependencies_change(monkeypatch):
    tasks_v1 = (
        pm.PostmarketTaskSpec("stock_daily", "market_data", shard_key="shard_00"),
        pm.PostmarketTaskSpec("weekly_rollup", "derived", depends_on=("stock_daily:shard_00",)),
    )
    tasks_v2 = (
        pm.PostmarketTaskSpec("stock_daily", "market_data", shard_key="shard_00"),
        pm.PostmarketTaskSpec("hk_stock_daily", "market_data", shard_key="shard_00"),
        pm.PostmarketTaskSpec("weekly_rollup", "derived", depends_on=("stock_daily:shard_00", "hk_stock_daily:shard_00")),
    )
    db = _Db()
    runner = pm.PostmarketRunner(_Engine(db, {}), max_workers=1)

    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks_v1)
    runner._init_tasks("postmarket:2026-04-28", "2026-04-28")
    db["sync_tasks"].update_one(
        {"_id": "postmarket:2026-04-28:weekly_rollup:all"},
        {"$set": {"status": "ok", "result_summary": {"status": "ok"}, "error_msg": ""}},
    )

    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks_v2)
    runner._init_tasks("postmarket:2026-04-28", "2026-04-28")

    weekly = db["sync_tasks"].docs["postmarket:2026-04-28:weekly_rollup:all"]
    assert weekly["status"] == "pending"
    assert weekly["depends_on"] == ["stock_daily:shard_00", "hk_stock_daily:shard_00"]
    assert weekly["result_summary"] == {}
    assert weekly["error_msg"] == "task_spec_changed"


def test_postmarket_completed_run_is_not_repeated(monkeypatch):
    tasks = (pm.PostmarketTaskSpec("alpha", "data"),)
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("data",))

    calls = []

    def alpha(db, proxy_url=None):
        calls.append("alpha")
        return {"status": "ok"}

    db = _Db()
    engine = _Engine(db, {"alpha": (alpha, "")})
    runner = pm.PostmarketRunner(engine, max_workers=1)

    first = runner.run_once(trade_date="2026-04-28")
    second = runner.run_once(trade_date="2026-04-28")

    assert first["status"] == "ok"
    assert second["skipped"] is True
    assert calls == ["alpha"]


def test_postmarket_completed_run_can_continue_optional_tasks(monkeypatch):
    monkeypatch.setenv("SIGNALS_POSTMARKET_RUN_OPTIONAL_TASKS", "true")
    tasks = (
        pm.PostmarketTaskSpec("alpha", "data"),
        pm.PostmarketTaskSpec("beta", "optional", blocks_run=False),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("data", "optional"))

    calls = []

    def beta(db, proxy_url=None):
        calls.append("beta")
        return {"status": "ok"}

    db = _Db()
    db["sync_runs"].docs["postmarket:2026-04-28"] = {
        "_id": "postmarket:2026-04-28",
        "run_id": "postmarket:2026-04-28",
        "trade_date": "2026-04-28",
        "status": "ok",
    }
    db["sync_tasks"].docs["postmarket:2026-04-28:alpha:all"] = {
        "_id": "postmarket:2026-04-28:alpha:all",
        "run_id": "postmarket:2026-04-28",
        "task_key": "alpha:all",
        "module": "alpha",
        "phase": "data",
        "status": "ok",
    }
    db["sync_tasks"].docs["postmarket:2026-04-28:beta:all"] = {
        "_id": "postmarket:2026-04-28:beta:all",
        "run_id": "postmarket:2026-04-28",
        "task_key": "beta:all",
        "module": "beta",
        "phase": "optional",
        "status": "pending",
        "blocks_run": False,
    }
    engine = _Engine(db, {"beta": (beta, "")})
    runner = pm.PostmarketRunner(engine, max_workers=1)

    result = runner.run_once(resume_run_id="postmarket:2026-04-28")

    assert result["status"] == "ok"
    assert calls == ["beta"]
    assert db["sync_tasks"].docs["postmarket:2026-04-28:beta:all"]["status"] == "ok"


def test_postmarket_daemon_continues_optional_tasks_for_terminal_run(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("alpha", "data"),
        pm.PostmarketTaskSpec("beta", "optional", blocks_run=False),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("data", "optional"))
    monkeypatch.setattr(pm.PostmarketRunner, "should_run_now", staticmethod(lambda now=None: True))
    monkeypatch.setattr(pm, "_postmarket_trade_date", lambda now=None: "2026-04-28")
    monkeypatch.setenv("SIGNALS_POSTMARKET_CONTINUE_MINUTE_PREHEAT", "false")

    calls = []

    def beta(db, proxy_url=None):
        calls.append("beta")
        return {"status": "ok"}

    db = _Db()
    db["sync_runs"].docs["postmarket:2026-04-28"] = {
        "_id": "postmarket:2026-04-28",
        "run_id": "postmarket:2026-04-28",
        "trade_date": "2026-04-28",
        "status": "ok",
    }
    db["sync_tasks"].docs["postmarket:2026-04-28:alpha:all"] = {
        "_id": "postmarket:2026-04-28:alpha:all",
        "run_id": "postmarket:2026-04-28",
        "task_key": "alpha:all",
        "module": "alpha",
        "phase": "data",
        "shard_key": "all",
        "depends_on": [],
        "blocks_run": True,
        "status": "ok",
    }
    db["sync_tasks"].docs["postmarket:2026-04-28:beta:all"] = {
        "_id": "postmarket:2026-04-28:beta:all",
        "run_id": "postmarket:2026-04-28",
        "task_key": "beta:all",
        "module": "beta",
        "phase": "optional",
        "shard_key": "all",
        "depends_on": [],
        "blocks_run": False,
        "status": "pending",
    }
    runner = pm.PostmarketRunner(_Engine(db, {"beta": (beta, "")}), max_workers=1)

    def stop_daemon(seconds):
        raise RuntimeError("stop-daemon")

    monkeypatch.setattr(pm.time, "sleep", stop_daemon)

    with pytest.raises(RuntimeError, match="stop-daemon"):
        runner.run_daemon(check_seconds=30)

    assert calls == ["beta"]
    assert db["sync_tasks"].docs["postmarket:2026-04-28:beta:all"]["status"] == "ok"


def test_postmarket_daemon_does_not_catch_up_old_terminal_run(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("alpha", "data"),
        pm.PostmarketTaskSpec("beta", "optional", blocks_run=False),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("data", "optional"))
    monkeypatch.setattr(pm.PostmarketRunner, "should_run_now", staticmethod(lambda now=None: False))
    monkeypatch.setattr(pm.PostmarketRunner, "should_catchup_now", staticmethod(lambda now=None: True))
    monkeypatch.setattr(pm, "_previous_trading_date", lambda now=None: "2026-04-28")
    monkeypatch.setenv("SIGNALS_POSTMARKET_CONTINUE_MINUTE_PREHEAT", "false")

    calls = []

    def beta(db, proxy_url=None):
        calls.append("beta")
        return {"status": "ok"}

    db = _Db()
    db["sync_runs"].docs["postmarket:2026-04-28"] = {
        "_id": "postmarket:2026-04-28",
        "run_id": "postmarket:2026-04-28",
        "trade_date": "2026-04-28",
        "status": "ok",
    }
    db["sync_tasks"].docs["postmarket:2026-04-28:alpha:all"] = {
        "_id": "postmarket:2026-04-28:alpha:all",
        "run_id": "postmarket:2026-04-28",
        "task_key": "alpha:all",
        "module": "alpha",
        "phase": "data",
        "shard_key": "all",
        "depends_on": [],
        "blocks_run": True,
        "status": "ok",
    }
    db["sync_tasks"].docs["postmarket:2026-04-28:beta:all"] = {
        "_id": "postmarket:2026-04-28:beta:all",
        "run_id": "postmarket:2026-04-28",
        "task_key": "beta:all",
        "module": "beta",
        "phase": "optional",
        "shard_key": "all",
        "depends_on": [],
        "blocks_run": False,
        "status": "pending",
    }
    runner = pm.PostmarketRunner(_Engine(db, {"beta": (beta, "")}), max_workers=1)

    def stop_daemon(seconds):
        raise RuntimeError("stop-daemon")

    monkeypatch.setattr(pm.time, "sleep", stop_daemon)

    with pytest.raises(RuntimeError, match="stop-daemon"):
        runner.run_daemon(check_seconds=30)

    assert calls == []
    assert db["sync_tasks"].docs["postmarket:2026-04-28:beta:all"]["status"] == "pending"


def test_postmarket_init_tasks_preserves_optional_completion(monkeypatch):
    tasks = (pm.PostmarketTaskSpec("beta", "optional", blocks_run=False),)
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)

    db = _Db()
    db["sync_tasks"].docs["postmarket:2026-04-28:beta:all"] = {
        "_id": "postmarket:2026-04-28:beta:all",
        "run_id": "postmarket:2026-04-28",
        "task_key": "beta:all",
        "module": "beta",
        "phase": "optional",
        "shard_key": "all",
        "depends_on": [],
        "blocks_run": False,
        "status": "ok",
    }
    runner = pm.PostmarketRunner(_Engine(db, {}), max_workers=1)

    runner._init_tasks("postmarket:2026-04-28", "2026-04-28")

    task = db["sync_tasks"].docs["postmarket:2026-04-28:beta:all"]
    assert task["status"] == "ok"
    assert task.get("error_msg", "") == ""


def test_postmarket_same_phase_dependency_runs_after_parent_finishes(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("alpha", "derived"),
        pm.PostmarketTaskSpec("beta", "derived", depends_on=("alpha:all",)),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("derived",))

    calls = []

    def alpha(db, proxy_url=None):
        calls.append("alpha")
        return {"status": "ok"}

    def beta(db, proxy_url=None):
        calls.append("beta")
        return {"status": "ok"}

    db = _Db()
    engine = _Engine(db, {"alpha": (alpha, ""), "beta": (beta, "")})
    runner = pm.PostmarketRunner(engine, max_workers=1)

    result = runner.run_once(trade_date="2026-04-28")

    assert result["status"] == "ok"
    assert calls == ["alpha", "beta"]
    assert db["sync_tasks"].docs["postmarket:2026-04-28:beta:all"]["status"] == "ok"


def test_postmarket_fullmarket_degraded_sets_source_blocker_and_fallback_tasks(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("fullmarket_spot_snapshot", "market_data"),
        pm.PostmarketTaskSpec("quote_snapshots", "market_data", depends_on=("fullmarket_spot_snapshot:all",)),
        pm.PostmarketTaskSpec("stock_daily", "market_data", shard_key="shard_00", depends_on=("fullmarket_spot_snapshot:all",)),
        pm.PostmarketTaskSpec("weekly_rollup", "derived", depends_on=("stock_daily:shard_00",)),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("market_data", "derived"))

    calls = []

    def fullmarket(db, proxy_url=None):
        calls.append("fullmarket_spot_snapshot")
        return {"status": "degraded", "reason": "SSLError: certificate verify failed", "count": 0}

    def quote_snapshots(db, proxy_url=None):
        calls.append("quote_snapshots")
        return {"status": "ok", "count": 10, "live": 8, "errors": 0}

    def stock_daily(db, proxy_url=None):
        calls.append("stock_daily")
        return {
            "status": "ok",
            "processed": 100,
            "total": 100,
            "progress_pct": 100.0,
            "coverage_pct": 100.0,
            "errors": 0,
            "deferred": 0,
        }

    def weekly_rollup(db, proxy_url=None):
        calls.append("weekly_rollup")
        return {"status": "ok"}

    db = _Db()
    engine = _Engine(db, {
        "fullmarket_spot_snapshot": (fullmarket, ""),
        "quote_snapshots": (quote_snapshots, ""),
        "stock_daily": (stock_daily, ""),
        "weekly_rollup": (weekly_rollup, ""),
    })
    runner = pm.PostmarketRunner(engine, max_workers=1)

    result = runner.run_once(trade_date="2026-04-28")

    assert result["status"] == "partial"
    assert result["recovery_state"] == "partial/source_blocked"
    assert result["critical_blocker"]["provider"] == "eastmoney"
    assert result["critical_blocker"]["endpoint"] == "fullmarket_spot_snapshot"
    assert result["blocked_tasks"] == ["fullmarket_spot_snapshot:all"]
    assert calls == ["fullmarket_spot_snapshot", "quote_snapshots", "stock_daily", "weekly_rollup"]
    quote_task = db["sync_tasks"].docs["postmarket:2026-04-28:quote_snapshots:all"]
    stock_task = db["sync_tasks"].docs["postmarket:2026-04-28:stock_daily:shard_00"]
    assert quote_task["status"] == "partial"
    assert quote_task["result_summary"]["source_fallback"] is True
    assert quote_task["result_summary"]["partial_usable"] is True
    assert stock_task["status"] == "partial"
    assert stock_task["result_summary"]["source_fallback"] is True
    assert stock_task["result_summary"]["partial_usable"] is True
    assert db["sync_tasks"].docs["postmarket:2026-04-28:weekly_rollup:all"]["status"] == "ok"


def test_postmarket_quote_degraded_with_high_coverage_unblocks_index_daily(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("quote_snapshots", "market_data"),
        pm.PostmarketTaskSpec("index_daily", "market_data", depends_on=("quote_snapshots:all",)),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("market_data",))

    calls = []

    def quote_snapshots(db, proxy_url=None):
        calls.append("quote_snapshots")
        return {"status": "degraded", "count": 363, "live": 362, "errors": 1}

    def index_daily(db, proxy_url=None):
        calls.append("index_daily")
        return {"status": "ok"}

    db = _Db()
    engine = _Engine(db, {"quote_snapshots": (quote_snapshots, ""), "index_daily": (index_daily, "")})
    runner = pm.PostmarketRunner(engine, max_workers=1)

    result = runner.run_once(trade_date="2026-04-28")

    assert result["status"] == "ok"
    assert calls == ["quote_snapshots", "index_daily"]
    assert db["sync_tasks"].docs["postmarket:2026-04-28:index_daily:all"]["status"] == "ok"


def test_postmarket_runtime_degraded_stock_minute_with_outputs_unblocks_downstream(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("stock_minute", "minute_preheat"),
        pm.PostmarketTaskSpec("minute_readiness_probe", "minute_preheat", depends_on=("stock_minute:all",)),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("minute_preheat",))

    calls = []

    def stock_minute(db, proxy_url=None):
        calls.append("stock_minute")
        return {
            "status": "degraded",
            "written": 28427,
            "planned_calls": 320,
            "empty": 91,
            "errors": 0,
        }

    def minute_readiness_probe(db, proxy_url=None):
        calls.append("minute_readiness_probe")
        return {"status": "ok"}

    db = _Db()
    engine = _Engine(db, {
        "stock_minute": (stock_minute, ""),
        "minute_readiness_probe": (minute_readiness_probe, ""),
    })
    runner = pm.PostmarketRunner(engine, max_workers=1)

    result = runner.run_once(trade_date="2026-04-28")

    assert result["status"] == "ok"
    assert calls == ["stock_minute", "minute_readiness_probe"]
    assert db["sync_tasks"].docs["postmarket:2026-04-28:stock_minute:all"]["status"] == "degraded"


def test_postmarket_runtime_degraded_market_pools_with_outputs_completes_run(monkeypatch):
    tasks = (pm.PostmarketTaskSpec("market_pools", "market_data"),)
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("market_data",))

    def market_pools(db, proxy_url=None):
        return {"status": "degraded", "count": 50, "modified": 1, "errors": 0}

    db = _Db()
    engine = _Engine(db, {"market_pools": (market_pools, "")})
    runner = pm.PostmarketRunner(engine, max_workers=1)

    result = runner.run_once(trade_date="2026-04-28")

    assert result["status"] == "ok"
    assert db["sync_tasks"].docs["postmarket:2026-04-28:market_pools:all"]["status"] == "degraded"


def test_postmarket_stale_task_with_finished_ok_result_is_effectively_done():
    assert pm._task_effectively_done({
        "module": "hk_stock_daily",
        "status": "stale",
        "result_summary": {
            "status": "ok",
            "result": {
                "status": "ok",
                "processed": 347,
                "total": 347,
                "coverage_pct": 100.0,
                "errors": 0,
            },
        },
    })


def test_postmarket_stale_stock_daily_with_high_global_coverage_is_effectively_done():
    assert pm._task_effectively_done({
        "module": "stock_daily",
        "status": "stale",
        "result_summary": {
            "status": "partial",
            "processed": 348,
            "total": 348,
            "covered_codes": 5482,
            "errors": 2,
            "deferred": 239,
            "progress_pct": 100.0,
            "coverage_pct": 99.49,
            "source": "fullmarket_spot_snapshots.valid_universe + bars.daily",
        },
    })


def test_postmarket_running_stock_daily_with_high_global_coverage_is_effectively_done():
    assert pm._task_effectively_done({
        "module": "stock_daily",
        "status": "running",
        "result_summary": {
            "status": "partial",
            "processed": 5510,
            "total": 5510,
            "covered_codes": 5482,
            "errors": 16,
            "deferred": 3504,
            "progress_pct": 100.0,
            "coverage_pct": 99.49,
            "source": "fullmarket_spot_snapshots.valid_universe + bars.daily",
        },
        "cursor": {"processed": 5510, "total": 5510, "progress_pct": 100.0},
    })


def test_postmarket_init_run_clears_stale_terminal_blocker_fields():
    db = _Db()
    db["sync_runs"].docs["postmarket:2026-04-28"] = {
        "_id": "postmarket:2026-04-28",
        "run_id": "postmarket:2026-04-28",
        "trade_date": "2026-04-28",
        "status": "partial",
        "finished_at": datetime(2026, 4, 28, 18, 0),
        "blocked_tasks": ["fullmarket_spot_snapshot:all"],
        "optional_blocked_tasks": ["hk_stock_daily:shard_00"],
        "critical_blocker": {"endpoint": "fullmarket_spot_snapshot"},
        "recovery_state": "waiting_for_source",
    }
    runner = pm.PostmarketRunner(_Engine(db, {}), max_workers=1)

    runner._init_run("postmarket:2026-04-28", "2026-04-28")

    run = db["sync_runs"].docs["postmarket:2026-04-28"]
    assert run["status"] == "running"
    assert run["finished_at"] is None
    assert run["blocked_tasks"] == []
    assert run["optional_blocked_tasks"] == []
    assert run["critical_blocker"] == {}
    assert run["recovery_state"] == "running"


def test_postmarket_stock_daily_cooling_partial_unlocks_downstream(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("stock_daily", "market_data", shard_key="shard_00"),
        pm.PostmarketTaskSpec("weekly_rollup", "derived", depends_on=("stock_daily:shard_00",)),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("market_data", "derived"))

    calls = []

    def stock_daily(db, proxy_url=None):
        calls.append("stock_daily")
        return {
            "status": "partial",
            "processed": 10,
            "total": 10,
            "progress_pct": 100.0,
            "coverage_pct": 45.0,
            "errors": 0,
            "deferred": 5,
            "cooling_down": 5,
        }

    def weekly_rollup(db, proxy_url=None):
        calls.append("weekly_rollup")
        return {"status": "ok"}

    db = _Db()
    engine = _Engine(db, {"stock_daily": (stock_daily, ""), "weekly_rollup": (weekly_rollup, "")})
    runner = pm.PostmarketRunner(engine, max_workers=1)

    result = runner.run_once(trade_date="2026-04-28")

    assert result["status"] == "ok"
    assert calls == ["stock_daily", "weekly_rollup"]
    assert db["sync_tasks"].docs["postmarket:2026-04-28:weekly_rollup:all"]["status"] == "ok"


def test_postmarket_stock_daily_sparse_errors_unlock_downstream(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("stock_daily", "market_data", shard_key="shard_00"),
        pm.PostmarketTaskSpec("weekly_rollup", "derived", depends_on=("stock_daily:shard_00",)),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("market_data", "derived"))

    calls = []

    def stock_daily(db, proxy_url=None):
        calls.append("stock_daily")
        return {
            "status": "partial",
            "processed": 100,
            "total": 100,
            "progress_pct": 100.0,
            "coverage_pct": 96.0,
            "errors": 4,
            "deferred": 0,
            "cooling_down": 0,
        }

    def weekly_rollup(db, proxy_url=None):
        calls.append("weekly_rollup")
        return {"status": "ok"}

    db = _Db()
    engine = _Engine(db, {"stock_daily": (stock_daily, ""), "weekly_rollup": (weekly_rollup, "")})
    runner = pm.PostmarketRunner(engine, max_workers=1)

    result = runner.run_once(trade_date="2026-04-28")

    assert result["status"] == "ok"
    assert calls == ["stock_daily", "weekly_rollup"]
    assert db["sync_tasks"].docs["postmarket:2026-04-28:weekly_rollup:all"]["status"] == "ok"


def test_postmarket_repairs_stock_daily_aggregate_progress_on_resume(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("stock_daily", "market_data", shard_key="shard_00"),
        pm.PostmarketTaskSpec("weekly_rollup", "derived", depends_on=("stock_daily:shard_00",)),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("market_data", "derived"))

    calls = []

    def weekly_rollup(db, proxy_url=None):
        calls.append("weekly_rollup")
        return {"status": "ok"}

    db = _Db()
    db["sync_log"].docs["stock_daily:progress:_meta"] = {
        "_id": "stock_daily:progress:_meta",
        "status": "partial",
        "processed": 100,
        "total": 100,
        "inserted": 99,
        "progress_pct": 100.0,
        "errors": 1,
        "missing_symbols": 0,
        "deferred_symbols": 0,
    }
    db["sync_runs"].docs["postmarket:2026-04-28"] = {
        "_id": "postmarket:2026-04-28",
        "run_id": "postmarket:2026-04-28",
        "trade_date": "2026-04-28",
        "status": "partial",
    }
    db["sync_tasks"].docs["postmarket:2026-04-28:stock_daily:shard_00"] = {
        "_id": "postmarket:2026-04-28:stock_daily:shard_00",
        "run_id": "postmarket:2026-04-28",
        "trade_date": "2026-04-28",
        "module": "stock_daily",
        "task_key": "stock_daily:shard_00",
        "phase": "market_data",
        "shard_key": "shard_00",
        "depends_on": [],
        "blocks_run": True,
        "status": "partial",
        "attempts": 1,
        "result_summary": {},
    }
    engine = _Engine(db, {"weekly_rollup": (weekly_rollup, "")})
    runner = pm.PostmarketRunner(engine, max_workers=1)

    result = runner.run_once(resume_run_id="postmarket:2026-04-28")

    assert result["status"] == "ok"
    assert calls == ["weekly_rollup"]
    stock_doc = db["sync_tasks"].docs["postmarket:2026-04-28:stock_daily:shard_00"]
    assert stock_doc["result_summary"]["source"] == "sync_log:stock_daily:progress:_meta"
    assert stock_doc["cursor"]["progress_pct"] == 100.0
    assert db["sync_tasks"].docs["postmarket:2026-04-28:weekly_rollup:all"]["status"] == "ok"


def test_postmarket_repairs_stock_daily_shards_from_high_global_coverage(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("stock_daily", "market_data", shard_key="shard_00"),
        pm.PostmarketTaskSpec("weekly_rollup", "derived", depends_on=("stock_daily:shard_00",)),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("market_data", "derived"))

    calls = []

    def weekly_rollup(db, proxy_url=None):
        calls.append("weekly_rollup")
        return {"status": "ok"}

    db = _Db()
    db["sync_log"].docs["stock_daily:progress:_meta"] = {
        "_id": "stock_daily:progress:_meta",
        "status": "partial",
        "processed": 5510,
        "total": 5510,
        "covered_codes": 5478,
        "progress_pct": 100.0,
        "coverage_pct": 99.42,
        "errors": 14,
        "missing_symbols": 32,
        "deferred_symbols": 3672,
    }
    task_id = "postmarket:2026-04-28:stock_daily:shard_00"
    db["sync_tasks"].docs[task_id] = {
        "_id": task_id,
        "run_id": "postmarket:2026-04-28",
        "trade_date": "2026-04-28",
        "module": "stock_daily",
        "task_key": "stock_daily:shard_00",
        "phase": "market_data",
        "shard_key": "shard_00",
        "depends_on": [],
        "blocks_run": True,
        "status": "partial",
        "attempts": 1,
        "result_summary": {"coverage_pct": 13.01, "errors": 2, "deferred": 299},
    }
    engine = _Engine(db, {"weekly_rollup": (weekly_rollup, "")})
    runner = pm.PostmarketRunner(engine, max_workers=1)

    result = runner.run_once(resume_run_id="postmarket:2026-04-28")

    assert result["status"] == "ok"
    assert calls == ["weekly_rollup"]
    stock_doc = db["sync_tasks"].docs[task_id]
    assert stock_doc["result_summary"]["coverage_pct"] == 99.42
    assert stock_doc["result_summary"]["covered_codes"] == 5478


def test_postmarket_stock_daily_many_errors_blocks_downstream(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("stock_daily", "market_data", shard_key="shard_00"),
        pm.PostmarketTaskSpec("weekly_rollup", "derived", depends_on=("stock_daily:shard_00",)),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("market_data", "derived"))

    calls = []

    def stock_daily(db, proxy_url=None):
        calls.append("stock_daily")
        return {
            "status": "partial",
            "processed": 100,
            "total": 100,
            "progress_pct": 100.0,
            "coverage_pct": 60.0,
            "errors": 40,
            "deferred": 0,
            "cooling_down": 0,
        }

    def weekly_rollup(db, proxy_url=None):
        calls.append("weekly_rollup")
        return {"status": "ok"}

    db = _Db()
    engine = _Engine(db, {"stock_daily": (stock_daily, ""), "weekly_rollup": (weekly_rollup, "")})
    runner = pm.PostmarketRunner(engine, max_workers=1)

    result = runner.run_once(trade_date="2026-04-28")

    assert result["status"] == "partial"
    assert calls == ["stock_daily"]
    assert db["sync_tasks"].docs["postmarket:2026-04-28:weekly_rollup:all"]["status"] == "pending"


def test_postmarket_hk_daily_error_does_not_block_a_scan(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("stock_daily", "market_data", shard_key="shard_00"),
        pm.PostmarketTaskSpec("weekly_rollup", "derived", depends_on=("stock_daily:shard_00",)),
        pm.PostmarketTaskSpec("technical_signal_scan", "derived", depends_on=("stock_daily:shard_00", "weekly_rollup:all")),
        pm.PostmarketTaskSpec("hk_stock_daily", "hk_market_data", shard_key="shard_00", blocks_run=False),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("market_data", "derived", "hk_market_data"))
    monkeypatch.setenv("SIGNALS_POSTMARKET_RUN_OPTIONAL_TASKS", "true")

    calls = []

    def stock_daily(db, proxy_url=None):
        calls.append("stock_daily")
        return {"status": "ok"}

    def hk_stock_daily(db, proxy_url=None):
        calls.append("hk_stock_daily")
        return {"status": "error", "error_msg": "hk_universe_empty"}

    def weekly_rollup(db, proxy_url=None):
        calls.append("weekly_rollup")
        return {"status": "ok"}

    def technical_signal_scan(db, proxy_url=None):
        calls.append("technical_signal_scan")
        return {"status": "ok"}

    db = _Db()
    engine = _Engine(db, {
        "stock_daily": (stock_daily, ""),
        "hk_stock_daily": (hk_stock_daily, ""),
        "weekly_rollup": (weekly_rollup, ""),
        "technical_signal_scan": (technical_signal_scan, ""),
    })
    runner = pm.PostmarketRunner(engine, max_workers=2)

    result = runner.run_once(trade_date="2026-04-28")

    assert result["status"] == "ok"
    assert "technical_signal_scan" in calls
    assert "weekly_rollup" in calls
    assert set(calls) == {"stock_daily", "weekly_rollup", "technical_signal_scan", "hk_stock_daily"}
    assert db["sync_tasks"].docs["postmarket:2026-04-28:hk_stock_daily:shard_00"]["status"] == "error"
    assert result["incomplete_tasks"] == 0
    assert result["optional_incomplete_tasks"] == 1


def test_postmarket_skips_optional_tasks_by_default(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("alpha", "data"),
        pm.PostmarketTaskSpec("optional_probe", "data", blocks_run=False),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("data",))
    monkeypatch.delenv("SIGNALS_POSTMARKET_RUN_OPTIONAL_TASKS", raising=False)
    calls = []

    def ok(name):
        def inner(db, proxy_url=None):
            calls.append(name)
            return {"status": "ok"}
        return inner

    db = _Db()
    engine = _Engine(db, {"alpha": (ok("alpha"), ""), "optional_probe": (ok("optional_probe"), "")})
    runner = pm.PostmarketRunner(engine, max_workers=1)

    result = runner.run_once(trade_date="2026-04-28")

    assert result["status"] == "ok"
    assert calls == ["alpha"]
    assert result["incomplete_tasks"] == 0
    assert result["optional_incomplete_tasks"] == 1


def test_postmarket_runs_optional_tasks_when_enabled(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("alpha", "data"),
        pm.PostmarketTaskSpec("optional_probe", "data", blocks_run=False),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("data",))
    monkeypatch.setenv("SIGNALS_POSTMARKET_RUN_OPTIONAL_TASKS", "true")
    calls = []

    def ok(name):
        def inner(db, proxy_url=None):
            calls.append(name)
            return {"status": "ok"}
        return inner

    db = _Db()
    engine = _Engine(db, {"alpha": (ok("alpha"), ""), "optional_probe": (ok("optional_probe"), "")})
    runner = pm.PostmarketRunner(engine, max_workers=1)

    result = runner.run_once(trade_date="2026-04-28")

    assert result["status"] == "ok"
    assert calls == ["alpha", "optional_probe"]
    assert result["optional_incomplete_tasks"] == 0


def test_postmarket_30m_fullmarket_runs_after_signal_pools(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("stock_daily", "market_data", shard_key="shard_00"),
        pm.PostmarketTaskSpec("weekly_rollup", "derived", depends_on=("stock_daily:shard_00",)),
        pm.PostmarketTaskSpec("technical_signal_scan", "derived", depends_on=("stock_daily:shard_00", "weekly_rollup:all")),
        pm.PostmarketTaskSpec("signal_pool", "derived", depends_on=("technical_signal_scan:all",)),
        pm.PostmarketTaskSpec("stock_30m_fullmarket", "minute_fullmarket", shard_key="shard_00", depends_on=("stock_daily:shard_00",)),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("market_data", "derived", "minute_fullmarket"))

    calls = []

    def ok(name):
        def inner(db, proxy_url=None):
            calls.append(name)
            return {"status": "ok"}
        return inner

    db = _Db()
    engine = _Engine(db, {
        "stock_daily": (ok("stock_daily"), ""),
        "weekly_rollup": (ok("weekly_rollup"), ""),
        "technical_signal_scan": (ok("technical_signal_scan"), ""),
        "signal_pool": (ok("signal_pool"), ""),
        "stock_30m_fullmarket": (ok("stock_30m_fullmarket"), ""),
    })
    runner = pm.PostmarketRunner(engine, max_workers=1)

    result = runner.run_once(trade_date="2026-04-28")

    assert result["status"] == "ok"
    assert calls.index("signal_pool") < calls.index("stock_30m_fullmarket")


def test_postmarket_effectively_done_stock_daily_degraded_is_not_repeated(monkeypatch):
    tasks = (
        pm.PostmarketTaskSpec("stock_daily", "market_data", shard_key="shard_00"),
        pm.PostmarketTaskSpec("weekly_rollup", "derived", depends_on=("stock_daily:shard_00",)),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("market_data", "derived"))

    calls = []

    def stock_daily(db, proxy_url=None):
        calls.append("stock_daily")
        return {"status": "ok", "processed": 10, "total": 10, "progress_pct": 100.0, "coverage_pct": 100.0, "errors": 0, "deferred": 0}

    def weekly_rollup(db, proxy_url=None):
        calls.append("weekly_rollup")
        return {"status": "ok"}

    db = _Db()
    task_id = "postmarket:2026-04-28:stock_daily:shard_00"
    db["sync_tasks"].docs[task_id] = {
        "_id": task_id,
        "run_id": "postmarket:2026-04-28",
        "module": "stock_daily",
        "task_key": "stock_daily:shard_00",
        "phase": "market_data",
        "shard_key": "shard_00",
        "status": "degraded",
        "attempts": 1,
        "result_summary": {
            "status": "degraded",
            "result": {
                "status": "ok",
                "processed": 10,
                "total": 10,
                "progress_pct": 100.0,
                "coverage_pct": 100.0,
                "errors": 0,
                "deferred": 0,
            },
        },
    }
    engine = _Engine(db, {"stock_daily": (stock_daily, ""), "weekly_rollup": (weekly_rollup, "")})
    runner = pm.PostmarketRunner(engine, max_workers=1)

    result = runner.run_once(resume_run_id="postmarket:2026-04-28")

    assert result["status"] == "ok"
    assert calls == ["weekly_rollup"]
    assert db["sync_tasks"].docs[task_id]["attempts"] == 1


def test_postmarket_semaphore_waiting_task_is_not_marked_running(monkeypatch):
    tasks = (pm.PostmarketTaskSpec("stock_daily", "market_data", shard_key="shard_00"),)
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("market_data",))
    monkeypatch.setenv("SIGNALS_POSTMARKET_STOCK_DAILY_WORKERS", "1")

    def stock_daily(db, proxy_url=None):
        return {"status": "ok"}

    db = _Db()
    engine = _Engine(db, {"stock_daily": (stock_daily, "")})
    runner = pm.PostmarketRunner(engine, max_workers=1)
    runner._init_run("postmarket:2026-04-28", "2026-04-28")
    runner._init_tasks("postmarket:2026-04-28", "2026-04-28")
    sem = runner.module_semaphores["stock_daily"]
    sem.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(runner._run_task, "postmarket:2026-04-28", tasks[0])
            task = db["sync_tasks"].docs["postmarket:2026-04-28:stock_daily:shard_00"]
            assert task["status"] == "pending"
            sem.release()
            assert future.result()["status"] == "ok"
    finally:
        try:
            sem.release()
        except ValueError:
            pass


def test_postmarket_mark_task_started_clears_stale_finished_payload(monkeypatch):
    task = pm.PostmarketTaskSpec("alpha", "market_data")
    db = _Db()
    task_id = "postmarket:2026-04-28:alpha:all"
    db["sync_tasks"].docs[task_id] = {
        "_id": task_id,
        "run_id": "postmarket:2026-04-28",
        "module": "alpha",
        "task_key": "alpha:all",
        "phase": "market_data",
        "shard_key": "all",
        "status": "ok",
        "attempts": 1,
        "finished_at": datetime(2026, 4, 28, 18, 0),
        "cursor": {"processed": 10},
        "result_summary": {"status": "ok"},
    }
    runner = pm.PostmarketRunner(_Engine(db, {}), max_workers=1)

    runner._mark_task_started("postmarket:2026-04-28", task)

    row = db["sync_tasks"].docs[task_id]
    assert row["status"] == "running"
    assert row["finished_at"] is None
    assert row["cursor"] == {}
    assert row["result_summary"] == {}
    assert row["attempts"] == 2


def test_postmarket_heartbeats_running_tasks_during_long_phase(monkeypatch):
    tasks = (pm.PostmarketTaskSpec("alpha", "market_data"),)
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)
    monkeypatch.setattr(pm, "POSTMARKET_PHASES", ("market_data",))

    def alpha(db, proxy_url=None):
        task_id = "postmarket:2026-04-28:alpha:all"
        deadline = time.time() + 1
        while db["sync_tasks"].docs.get(task_id, {}).get("status") != "running" and time.time() < deadline:
            time.sleep(0.001)
        time.sleep(0.15)
        return {"status": "ok"}

    db = _Db()
    engine = _Engine(db, {"alpha": (alpha, "")})
    runner = pm.PostmarketRunner(engine, max_workers=1)
    runner.heartbeat_seconds = 0.01
    heartbeats = []
    original = runner._heartbeat_running_tasks

    def spy(run_id, phase=""):
        count = original(run_id, phase)
        heartbeats.append((run_id, phase, count))
        return count

    runner._heartbeat_running_tasks = spy

    result = runner.run_once(trade_date="2026-04-28")

    assert result["status"] == "ok"
    assert any(count == 1 for _run_id, _phase, count in heartbeats)
    assert db["sync_tasks"].docs["postmarket:2026-04-28:alpha:all"]["status"] == "ok"


def test_postmarket_catchup_target_does_not_resume_previous_trading_day_partial():
    db = _Db()
    db["sync_runs"].docs["postmarket:2026-05-07"] = {
        "_id": "postmarket:2026-05-07",
        "status": "partial",
        "trade_date": "2026-05-07",
    }
    runner = pm.PostmarketRunner(_Engine(db, {}), max_workers=1)

    target = runner.catchup_target(datetime(2026, 5, 8, 9, 10))

    assert target is None


def test_postmarket_catchup_target_does_not_start_missing_previous_trading_day():
    runner = pm.PostmarketRunner(_Engine(_Db(), {}), max_workers=1)

    target = runner.catchup_target(datetime(2026, 5, 8, 9, 10))

    assert target is None


def test_postmarket_catchup_target_force_cannot_restore_old_trade_date(monkeypatch):
    monkeypatch.setattr(pm.PostmarketRunner, "should_catchup_now", staticmethod(lambda now=None: False))
    monkeypatch.setattr(pm, "_previous_trading_date", lambda now=None: "2026-05-07")
    runner = pm.PostmarketRunner(_Engine(_Db(), {}), max_workers=1)

    assert runner.catchup_target(datetime(2026, 5, 8, 16, 10)) is None
    target = runner.catchup_target(datetime(2026, 5, 8, 16, 10), force=True)

    assert target is None


def test_postmarket_catchup_target_ignores_ok_previous_trading_day():
    db = _Db()
    db["sync_runs"].docs["postmarket:2026-05-07"] = {
        "_id": "postmarket:2026-05-07",
        "status": "ok",
        "trade_date": "2026-05-07",
    }
    runner = pm.PostmarketRunner(_Engine(db, {}), max_workers=1)

    target = runner.catchup_target(datetime(2026, 5, 8, 9, 10))

    assert target is None


def test_postmarket_catchup_target_does_not_resume_optional_tail_from_old_trade_date():
    db = _Db()
    db["sync_runs"].docs["postmarket:2026-05-07"] = {
        "_id": "postmarket:2026-05-07",
        "status": "ok",
        "trade_date": "2026-05-07",
    }
    db["sync_tasks"].docs["postmarket:2026-05-07:optional_probe:all"] = {
        "_id": "postmarket:2026-05-07:optional_probe:all",
        "run_id": "postmarket:2026-05-07",
        "task_key": "optional_probe:all",
        "module": "optional_probe",
        "status": "pending",
        "blocks_run": False,
    }
    runner = pm.PostmarketRunner(_Engine(db, {}), max_workers=1)

    target = runner.catchup_target(datetime(2026, 5, 8, 9, 10))

    assert target is None


def test_postmarket_continues_minute_preheat_universe(monkeypatch):
    monkeypatch.setenv("SIGNALS_POSTMARKET_CONTINUE_MINUTE_BATCHES", "1")
    tasks = (
        pm.PostmarketTaskSpec(
            "stock_minute",
            "minute_preheat",
            env={
                "STOCK_MINUTE_SCOPE": "postmarket_candidates",
                "STOCK_MINUTE_FREQS": "5min,15min",
                "STOCK_MINUTE_POSTMARKET_MAX_CODES": "240",
            },
        ),
    )
    monkeypatch.setattr(pm, "POSTMARKET_TASKS", tasks)

    calls = []

    def stock_minute(db, proxy_url=None):
        calls.append("stock_minute")
        for doc in db["minute_preheat_universe"].docs.values():
            if doc.get("trade_date") == "2026-05-08" and doc.get("status") == "pending":
                doc["status"] = "cached"
        return {"status": "ok"}

    db = _Db()
    db["minute_preheat_universe"].docs["2026-05-08:300001"] = {
        "_id": "2026-05-08:300001",
        "trade_date": "2026-05-08",
        "status": "pending",
    }
    db["minute_preheat_universe"].docs["2026-05-08:300002"] = {
        "_id": "2026-05-08:300002",
        "trade_date": "2026-05-08",
        "status": "cached",
    }
    runner = pm.PostmarketRunner(_Engine(db, {"stock_minute": (stock_minute, "")}), max_workers=1)

    completed = runner._continue_minute_preheat_universe("2026-05-08", "postmarket:2026-05-08")

    assert completed == 1
    assert calls == ["stock_minute"]
    assert db["sync_runs"].docs["postmarket:2026-05-08"]["minute_preheat_pending"] == 0
