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


def test_schedule_window_is_only_due_inside_window():
    assert SyncEngine._schedule_due("16:00-17:30 weekday", datetime(2026, 4, 24, 16, 1)) is True
    assert SyncEngine._schedule_due("16:00-17:30 weekday", datetime(2026, 4, 24, 17, 31)) is False


def test_weekday_schedule_skips_cn_labor_day_holiday():
    assert SyncEngine._schedule_due("16:00-17:30 weekday", datetime(2026, 5, 1, 16, 1)) is False
    assert SyncEngine._schedule_due("16:00-17:30 weekday", datetime(2026, 5, 9, 16, 1)) is True


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
        if self.docs and isinstance(query, dict):
            for doc in self.docs.values():
                if all(doc.get(key) == value for key, value in query.items()):
                    return doc
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
    sync_log.docs["board_heat_minute:A:_meta"] = {
        "status": "ok",
        "last_run": now - timedelta(minutes=10),
    }
    engine = object.__new__(SyncEngine)
    engine.db = _FakeDb({"sync_log": sync_log})

    assert engine._has_run_recent("quote_snapshots", "A", now, interval_seconds=60) is False
    assert engine._has_run_recent("board_heat_minute", "A", now, interval_seconds=30 * 60) is True


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

    def ulist_fn(db, proxy_url=None):
        calls.append("eastmoney_ulist_quote")
        return {"inserted": 1}

    def fullmarket_fn(db, proxy_url=None):
        calls.append("fullmarket_spot_snapshot")
        return {"inserted": 1}

    def quote_fn(db, proxy_url=None):
        calls.append("quote_snapshots")
        return {"inserted": 1}

    def index_fn(db, proxy_url=None):
        calls.append("index_minute")
        return {"inserted": 1}

    engine.module_map = {
        "eastmoney_ulist_quote": (ulist_fn, ""),
        "fullmarket_spot_snapshot": (fullmarket_fn, ""),
        "quote_snapshots": (quote_fn, ""),
        "index_minute": (index_fn, ""),
    }
    engine.proxy_url = None

    results = engine._run_intraday_bundle({Market.A}, datetime(2026, 4, 27, 10, 0))

    assert calls == ["eastmoney_ulist_quote", "fullmarket_spot_snapshot", "quote_snapshots"]
    assert [item["module"] for item in results] == calls


def test_signal_lane_intraday_runs_readiness_probe():
    engine = object.__new__(SyncEngine)
    engine.enabled_lanes = {"signal_lane"}
    engine.db = _FakeDb({"sync_log": _FakeCollection(), "minute_readiness": _FakeCollection(count=1)})
    calls = []

    def stock_fn(db, proxy_url=None):
        calls.append("stock_minute")
        return {"inserted": 1}

    def index_fn(db, proxy_url=None):
        calls.append("index_minute")
        return {"inserted": 1}

    def readiness_fn(db, proxy_url=None):
        calls.append("minute_readiness_probe")
        return {"inserted": 1}

    engine.module_map = {
        "stock_minute": (stock_fn, ""),
        "index_minute": (index_fn, ""),
        "minute_readiness_probe": (readiness_fn, ""),
    }
    engine.proxy_url = None

    results = engine._run_intraday_bundle({Market.A}, datetime(2026, 4, 27, 10, 0))

    assert calls == ["index_minute", "stock_minute", "minute_readiness_probe"]
    assert [item["module"] for item in results] == calls


def test_quote_snapshots_preserves_writer_freshness_summary():
    engine = object.__new__(SyncEngine)
    quote_snapshots = _FakeCollection(count=98, doc={"dt": datetime(2026, 4, 29), "freshness": "stale"})
    freshness = _FakeCollection()
    freshness.docs["quote"] = {
        "domain": "quote",
        "market": "A",
        "mode": "realtime",
        "collection": "quote_snapshots",
        "freshness": "partial",
        "live_count": 97,
        "stale_count": 1,
    }
    engine.db = _FakeDb({
        "quote_snapshots": quote_snapshots,
        "data_freshness": freshness,
    })

    engine._write_module_freshness("quote_snapshots", "ok", None, market="A", lane="quote_lane")

    assert freshness.docs["quote"]["freshness"] == "partial"
    assert freshness.docs["quote"]["live_count"] == 97
    assert "count" not in freshness.docs["quote"]


def test_fullmarket_spot_preserves_writer_freshness_summary():
    engine = object.__new__(SyncEngine)
    spot_snapshots = _FakeCollection(count=11695, doc={"dt": datetime(2026, 4, 29), "freshness": "fresh"})
    freshness = _FakeCollection()
    freshness.docs["spot"] = {
        "domain": "spot",
        "market": "A",
        "mode": "realtime",
        "collection": "fullmarket_spot_snapshots",
        "freshness": "fresh",
        "latest_dt": "2026-04-30",
        "count": 5849,
        "elapsed_seconds": 4.449,
    }
    engine.db = _FakeDb({
        "fullmarket_spot_snapshots": spot_snapshots,
        "data_freshness": freshness,
    })

    engine._write_module_freshness("fullmarket_spot_snapshot", "ok", None, market="A", lane="quote_lane")

    assert freshness.docs["spot"]["count"] == 5849
    assert freshness.docs["spot"]["elapsed_seconds"] == 4.449
    assert freshness.docs["spot"]["latest_dt"] == "2026-04-30"


def test_board_lane_intraday_runs_heat_not_board_cons():
    engine = object.__new__(SyncEngine)
    engine.enabled_lanes = {"board_lane"}
    engine.db = _FakeDb({"sync_log": _FakeCollection(), "board_heat_ticks": _FakeCollection(count=1)})
    calls = []

    def board_heat(db, proxy_url=None):
        calls.append("board_heat_minute")
        return {"inserted": 1}

    def concept_heat(db, proxy_url=None):
        calls.append("concept_heat_minute")
        return {"inserted": 1}

    def chain_heat(db, proxy_url=None):
        calls.append("chain_heat_snapshots")
        return {"inserted": 1}

    def board_cons(db, proxy_url=None):
        calls.append("board_cons")
        return {"inserted": 1}

    engine.module_map = {
        "board_heat_minute": (board_heat, ""),
        "concept_heat_minute": (concept_heat, ""),
        "chain_heat_snapshots": (chain_heat, ""),
        "board_cons": (board_cons, ""),
    }
    engine.proxy_url = None

    results = engine._run_intraday_bundle({Market.A}, datetime(2026, 4, 27, 10, 0))

    assert calls == ["board_heat_minute", "concept_heat_minute", "chain_heat_snapshots"]
    assert [item["module"] for item in results] == calls


def test_workbench_lane_intraday_rebuilds_terminal_stock_pool():
    engine = object.__new__(SyncEngine)
    engine.enabled_lanes = {"workbench_lane"}
    engine.db = _FakeDb({"sync_log": _FakeCollection(), "strategy_snapshots": _FakeCollection(count=1)})
    calls = []

    def market_pools(db, proxy_url=None):
        calls.append("market_pools")
        return {"inserted": 1}

    def strategy_snapshot(db, proxy_url=None):
        calls.append("strategy_snapshot")
        return {"inserted": 1}

    def terminal_realtime_pool(db, proxy_url=None):
        calls.append("terminal_realtime_pool")
        return {"inserted": 1}

    engine.module_map = {
        "market_pools": (market_pools, ""),
        "strategy_snapshot": (strategy_snapshot, ""),
        "terminal_realtime_pool": (terminal_realtime_pool, ""),
    }
    engine.proxy_url = None

    results = engine._run_intraday_bundle({Market.A}, datetime(2026, 4, 27, 10, 0))

    assert calls == ["market_pools", "strategy_snapshot", "terminal_realtime_pool"]
    assert [item["module"] for item in results] == calls


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


def test_unfiltered_daemon_runs_scheduled_daily_maintenance():
    engine = object.__new__(SyncEngine)
    engine.enabled_lanes = None
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

    def weekly_rollup(db, proxy_url=None):
        calls.append("weekly_rollup")
        return {"inserted": 1}

    engine.modules = [
        ("stock_daily", stock_daily, "16:00-17:30 weekday"),
        ("weekly_rollup", weekly_rollup, "17:30-18:00 weekday"),
    ]
    engine.module_map = {name: (fn, schedule) for name, fn, schedule in engine.modules}

    results = engine._run_scheduled_modules(datetime(2026, 4, 27, 16, 45), "2026-04-27")

    assert calls == ["stock_daily"]
    assert results[0]["lane"] == "workbench_lane"
    assert engine.db["sync_log"].docs["stock_daily:_meta"]["lane"] == "workbench_lane"


def test_live_lane_daemon_does_not_run_scheduled_maintenance_directly():
    engine = object.__new__(SyncEngine)
    engine.enabled_lanes = {"workbench_lane"}
    engine.db = _FakeDb({"sync_log": _FakeCollection(), "data_freshness": _FakeCollection()})
    engine.proxy_url = None
    calls = []

    def stock_daily(db, proxy_url=None):
        calls.append("stock_daily")
        return {"inserted": 1}

    engine.modules = [("stock_daily", stock_daily, "16:00-17:30 weekday")]
    engine.module_map = {"stock_daily": (stock_daily, "16:00-17:30 weekday")}

    results = engine._run_scheduled_modules(datetime(2026, 4, 27, 16, 45), "2026-04-27")

    assert calls == []
    assert results == []


def test_board_lane_runs_board_cons_as_partial_status():
    engine = object.__new__(SyncEngine)
    engine.enabled_lanes = None
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
