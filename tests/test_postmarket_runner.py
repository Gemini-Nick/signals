# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
import time
from concurrent.futures import ThreadPoolExecutor

from signals.sync import postmarket as pm


def test_default_postmarket_tasks_split_long_market_data_tasks():
    spot = next(task for task in pm.POSTMARKET_TASKS if task.module == "fullmarket_spot_snapshot")
    stock_daily = [task for task in pm.POSTMARKET_TASKS if task.module == "stock_daily"]
    stock_30m = [task for task in pm.POSTMARKET_TASKS if task.module == "stock_30m_fullmarket"]
    board_cons = [task for task in pm.POSTMARKET_TASKS if task.module == "board_cons"]
    index_daily = next(task for task in pm.POSTMARKET_TASKS if task.module == "index_daily")
    weekly = next(task for task in pm.POSTMARKET_TASKS if task.module == "weekly_rollup")
    technical_scan = next(task for task in pm.POSTMARKET_TASKS if task.module == "technical_signal_scan")
    chain = next(task for task in pm.POSTMARKET_TASKS if task.module == "chain_heat_snapshots")

    assert spot.phase == "market_data"
    assert len(stock_daily) == 16
    assert len(stock_30m) == 16
    assert {task.shard_key for task in stock_daily} == {f"shard_{idx:02d}" for idx in range(16)}
    assert {task.shard_key for task in stock_30m} == {f"shard_{idx:02d}" for idx in range(16)}
    assert all(task.env["STOCK_DAILY_SCOPE"] == "all" for task in stock_daily)
    assert all(task.depends_on == ("fullmarket_spot_snapshot:all",) for task in stock_daily)
    assert index_daily.depends_on == ("quote_snapshots:all",)
    assert all(task.task_key in technical_scan.depends_on for task in stock_30m)
    assert {task.shard_key for task in board_cons} == {"board", "concept"}
    assert all(task.depends_on == ("board_ranking:all",) for task in board_cons)
    assert set(task.task_key for task in stock_daily).issubset(set(weekly.depends_on))
    assert not (set(task.task_key for task in board_cons) & set(chain.depends_on))
    assert chain.depends_on == ("board_ranking:all",)
    stock_minute = next(task for task in pm.POSTMARKET_TASKS if task.module == "stock_minute")
    assert stock_minute.env["STOCK_MINUTE_FREQS"] == "5min,15min"


def test_postmarket_trade_date_skips_cn_labor_day_holiday():
    assert pm._postmarket_trade_date(datetime(2026, 5, 1, 16, 30)) == "2026-04-30"


def test_postmarket_should_not_run_on_cn_labor_day_holiday():
    assert pm.PostmarketRunner.should_run_now(datetime(2026, 5, 1, 16, 30)) is False


def test_postmarket_should_run_after_cn_trading_day_close():
    assert pm.PostmarketRunner.should_run_now(datetime(2026, 4, 30, 16, 30)) is True


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
