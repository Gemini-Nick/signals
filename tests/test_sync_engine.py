# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timedelta

from signals.core.market_hours import Market
from signals.sync.engine import SyncEngine


def test_schedule_due_after_scheduled_weekday():
    now = datetime(2026, 4, 24, 16, 45)  # Friday
    assert SyncEngine._schedule_due("16:30 weekday", now) is True


def test_schedule_not_due_before_scheduled_weekday():
    now = datetime(2026, 4, 24, 16, 15)  # Friday
    assert SyncEngine._schedule_due("16:30 weekday", now) is False


def test_sunday_schedule_only_on_sunday():
    sunday = datetime(2026, 4, 26, 10, 1)
    friday = datetime(2026, 4, 24, 10, 1)
    assert SyncEngine._schedule_due("Sunday 10:00", sunday) is True
    assert SyncEngine._schedule_due("Sunday 10:00", friday) is False


class _FakeCollection:
    def __init__(self, count=0, doc=None):
        self.count = count
        self.doc = doc or {}
        self.docs = {}

    def estimated_document_count(self):
        return self.count

    def find_one(self, query=None, *args, **kwargs):
        if self.docs and isinstance(query, dict) and "_id" in query:
            return self.docs.get(query["_id"])
        return self.doc

    def find(self, *args, **kwargs):
        return []

    def update_one(self, query=None, update=None, upsert=False, **kwargs):
        key = None
        if isinstance(query, dict):
            key = query.get("_id") or query.get("domain") or query.get("collection")
        if key:
            doc = self.docs.setdefault(key, {"_id": key})
            for key, value in (update or {}).get("$set", {}).items():
                doc[key] = value
        return None


class _FakeDb(dict):
    def __missing__(self, key):
        self[key] = _FakeCollection()
        return self[key]


def test_zero_insert_empty_target_is_degraded():
    engine = object.__new__(SyncEngine)
    engine.db = _FakeDb({"quote_snapshots": _FakeCollection(count=0)})

    status, error = engine._classify_result("quote_snapshots", {"inserted": 0})

    assert status == "degraded"
    assert error == "target_empty_after_zero_insert"


def test_stale_running_module_can_rerun():
    engine = object.__new__(SyncEngine)
    engine.db = _FakeDb({
        "sync_log": _FakeCollection(doc={
            "status": "running",
            "last_run": datetime.now() - timedelta(hours=3),
        })
    })

    assert engine._has_run_today("stock_daily", datetime.now().strftime("%Y-%m-%d")) is False


def test_intraday_recent_throttle_is_market_specific():
    now = datetime(2026, 4, 27, 10, 0)
    sync_log = _FakeCollection()
    sync_log.docs["quote_snapshots:A:_meta"] = {
        "status": "ok",
        "last_run": now - timedelta(minutes=10),
    }
    sync_log.docs["quote_snapshots:HK:_meta"] = {
        "status": "ok",
        "last_run": now - timedelta(minutes=31),
    }
    engine = object.__new__(SyncEngine)
    engine.db = _FakeDb({"sync_log": sync_log})

    assert engine._has_run_recent("quote_snapshots", "A", now) is True
    assert engine._has_run_recent("quote_snapshots", "HK", now) is False


def test_run_module_writes_market_specific_sync_log():
    sync_log = _FakeCollection()
    freshness = _FakeCollection()
    engine = object.__new__(SyncEngine)
    engine.db = _FakeDb({
        "sync_log": sync_log,
        "data_freshness": freshness,
        "quote_snapshots": _FakeCollection(count=1, doc={"updated_at": datetime(2026, 4, 27, 10, 0)}),
    })
    engine.proxy_url = None

    def module_fn(db, proxy_url=None):
        return {"inserted": 1}

    result = engine.run_module("quote_snapshots", module_fn, market="A")

    assert result["status"] == "ok"
    assert result["market"] == "A"
    assert sync_log.docs["quote_snapshots:A:_meta"]["market"] == "A"
    assert sync_log.docs["quote_snapshots:A:_meta"]["status"] == "ok"
    assert sync_log.docs["quote_snapshots:A:_meta"]["lane"] == "quote_lane"
    assert sync_log.docs["quote_snapshots:A:_meta"]["next_due_at"] is not None


def test_live_plan_uses_lane_specific_interval():
    now = datetime(2026, 4, 27, 10, 0)
    sync_log = _FakeCollection()
    sync_log.docs["quote_snapshots:A:_meta"] = {
        "status": "ok",
        "last_run": now - timedelta(seconds=65),
    }
    sync_log.docs["board_ranking:A:_meta"] = {
        "status": "ok",
        "last_run": now - timedelta(minutes=10),
    }
    engine = object.__new__(SyncEngine)
    engine.db = _FakeDb({"sync_log": sync_log})

    assert engine._has_run_recent("quote_snapshots", "A", now, interval_seconds=60) is False
    assert engine._has_run_recent("board_ranking", "A", now, interval_seconds=30 * 60) is True


def test_mark_market_unavailable_is_explicit():
    sync_log = _FakeCollection()
    freshness = _FakeCollection()
    engine = object.__new__(SyncEngine)
    engine.db = _FakeDb({"sync_log": sync_log, "data_freshness": freshness})
    now = datetime(2026, 4, 27, 10, 0)

    engine._mark_market_unavailable("HK", "HK source unavailable", now)

    assert sync_log.docs["live_bundle:HK:_meta"]["status"] == "unavailable"
    assert sync_log.docs["live_bundle:HK:_meta"]["market"] == "HK"
    doc = freshness.docs["live_bundle"]
    assert doc["market"] == "HK"
    assert doc["freshness"] == "unavailable"


def test_lane_filtered_daemon_only_runs_matching_live_plans():
    engine = object.__new__(SyncEngine)
    engine.enabled_lanes = {"quote_lane"}
    engine.db = _FakeDb({"sync_log": _FakeCollection()})
    calls = []

    def quote_fn(db, proxy_url=None):
        calls.append("quote_snapshots")
        return {"inserted": 1}

    def index_fn(db, proxy_url=None):
        calls.append("index_minute")
        return {"inserted": 1}

    engine.module_map = {
        "quote_snapshots": (quote_fn, ""),
        "index_minute": (index_fn, ""),
    }
    engine.proxy_url = None

    results = engine._run_intraday_bundle({Market.A}, datetime(2026, 4, 27, 10, 0))

    assert calls == ["quote_snapshots"]
    assert [item["module"] for item in results] == ["quote_snapshots"]


def test_lane_unavailable_state_is_throttled_per_lane():
    sync_log = _FakeCollection()
    freshness = _FakeCollection()
    engine = object.__new__(SyncEngine)
    engine.enabled_lanes = {"quote_lane"}
    engine.db = _FakeDb({"sync_log": sync_log, "data_freshness": freshness})
    now = datetime(2026, 4, 27, 15, 20)

    engine._run_intraday_bundle({Market.HK}, now)

    key = "live_bundle:quote_lane:HK:_meta"
    assert sync_log.docs[key]["module"] == "live_bundle:quote_lane"
    assert sync_log.docs[key]["status"] == "unavailable"
    assert engine._has_run_recent("live_bundle:quote_lane", "HK", now + timedelta(seconds=60)) is True


def test_workbench_lane_runs_scheduled_daily_maintenance():
    engine = object.__new__(SyncEngine)
    engine.enabled_lanes = {"workbench_lane"}
    engine.db = _FakeDb({
        "sync_log": _FakeCollection(),
        "data_freshness": _FakeCollection(),
        "bars": _FakeCollection(count=1, doc={"dt": datetime(2026, 4, 24)}),
    })
    engine.proxy_url = None
    calls = []

    def stock_daily(db, proxy_url=None):
        calls.append("stock_daily")
        return {"inserted": 1}

    def board_cons(db, proxy_url=None):
        calls.append("board_cons")
        return {"status": "partial", "inserted": 0}

    engine.modules = [
        ("stock_daily", stock_daily, "16:30 weekday"),
        ("board_cons", board_cons, "16:30 weekday"),
    ]
    engine.module_map = {name: (fn, schedule) for name, fn, schedule in engine.modules}

    results = engine._run_scheduled_modules(datetime(2026, 4, 27, 16, 45), "2026-04-27")

    assert calls == ["stock_daily"]
    assert results[0]["lane"] == "workbench_lane"
    assert engine.db["sync_log"].docs["stock_daily:_meta"]["lane"] == "workbench_lane"


def test_board_lane_runs_board_cons_as_partial_status():
    engine = object.__new__(SyncEngine)
    engine.enabled_lanes = {"board_lane"}
    engine.db = _FakeDb({
        "sync_log": _FakeCollection(),
        "data_freshness": _FakeCollection(),
        "board_constituents": _FakeCollection(count=12, doc={"updated_at": datetime(2026, 4, 27)}),
    })
    engine.proxy_url = None

    def board_cons(db, proxy_url=None):
        return {"status": "partial", "remaining": 10, "error_msg": "remaining=10"}

    engine.modules = [("board_cons", board_cons, "16:30 weekday")]
    engine.module_map = {"board_cons": (board_cons, "16:30 weekday")}

    results = engine._run_scheduled_modules(datetime(2026, 4, 27, 16, 45), "2026-04-27")

    assert results[0]["status"] == "partial"
    assert results[0]["lane"] == "board_lane"
    assert engine.db["sync_log"].docs["board_cons:_meta"]["status"] == "partial"
