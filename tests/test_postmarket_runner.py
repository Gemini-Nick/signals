# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.sync import postmarket as pm


def test_default_postmarket_tasks_split_long_market_data_tasks():
    stock_daily = [task for task in pm.POSTMARKET_TASKS if task.module == "stock_daily"]
    board_cons = [task for task in pm.POSTMARKET_TASKS if task.module == "board_cons"]
    weekly = next(task for task in pm.POSTMARKET_TASKS if task.module == "weekly_rollup")
    chain = next(task for task in pm.POSTMARKET_TASKS if task.module == "chain_heat_snapshots")

    assert len(stock_daily) == 16
    assert {task.shard_key for task in stock_daily} == {f"shard_{idx:02d}" for idx in range(16)}
    assert all(task.env["STOCK_DAILY_SCOPE"] == "all" for task in stock_daily)
    assert {task.shard_key for task in board_cons} == {"board", "concept"}
    assert set(task.task_key for task in stock_daily).issubset(set(weekly.depends_on))
    assert set(task.task_key for task in board_cons).issubset(set(chain.depends_on))


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
