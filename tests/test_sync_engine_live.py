# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
import time
from datetime import datetime


def test_run_module_uses_thread_local_lane_context(monkeypatch):
    from signals.sync.engine import LiveSyncPlan, SyncEngine
    from signals.sync.task_context import get_task_env

    class _Collection:
        def update_one(self, *args, **kwargs):
            return None

    class _Db(dict):
        def __missing__(self, key):
            self[key] = _Collection()
            return self[key]

    engine = object.__new__(SyncEngine)
    engine.db = _Db()
    engine.proxy_url = None
    engine._now = lambda: datetime(2026, 6, 22, 10, 0, 0)
    engine._classify_result = lambda name, result: ("ok", "")
    engine._write_module_freshness = lambda *args, **kwargs: None
    observed = []

    def module_fn(db, proxy_url=None):
        observed.append((
            get_task_env("SIGNALS_CURRENT_SYNC_LANE"),
            get_task_env("SIGNALS_CURRENT_SYNC_MARKET"),
        ))
        return {"inserted": 1}

    monkeypatch.delenv("SIGNALS_CURRENT_SYNC_LANE", raising=False)
    monkeypatch.delenv("SIGNALS_CURRENT_SYNC_MARKET", raising=False)

    result = engine.run_module(
        "stock_minute",
        module_fn,
        market="A",
        plan=LiveSyncPlan("stock_minute", "signal_lane", 60, 180, 240),
    )

    assert result["status"] == "ok"
    assert observed == [("signal_lane", "A")]


def test_live_bundle_runs_independent_stage_in_parallel_and_snapshot_last(monkeypatch):
    from signals.core.market_hours import Market
    from signals.sync.engine import LIVE_SYNC_PLANS, LIVE_SYNC_STAGE_BY_MODULE, SyncEngine

    a_share_modules = [plan.module for plan in LIVE_SYNC_PLANS[Market.A]]
    assert "etf_spot_snapshot" in a_share_modules
    assert LIVE_SYNC_STAGE_BY_MODULE["etf_spot_snapshot"] == 0
    monkeypatch.setenv("SECTOR_TRANSITION_ENABLED", "true")

    engine = object.__new__(SyncEngine)
    engine.max_workers = 8
    engine.enabled_lanes = None
    engine.module_map = {plan.module: (lambda db, proxy_url=None: {"inserted": 1}, "live only") for plan in LIVE_SYNC_PLANS[Market.A]}

    active = 0
    max_active = 0
    calls: list[tuple[str, int]] = []
    lock = threading.Lock()

    def fake_run_module(name, fn, market=None, plan=None):
        nonlocal active, max_active
        stage = LIVE_SYNC_STAGE_BY_MODULE.get(name, plan.priority)
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.01 if stage == 0 else 0.002)
        with lock:
            active -= 1
            calls.append((name, stage))
        return {"module": name, "status": "ok", "market": market, "lane": plan.lane}

    monkeypatch.setattr(engine, "_module_running_recent", lambda *args, **kwargs: False)
    monkeypatch.setattr(engine, "_has_run_recent", lambda *args, **kwargs: False)
    monkeypatch.setattr(engine, "run_module", fake_run_module)

    results = engine._run_intraday_bundle({Market.A}, datetime(2026, 6, 22, 10, 0, 0), force=True)

    assert len(results) == len(LIVE_SYNC_PLANS[Market.A])
    assert max_active > 1
    assert max(stage for _name, stage in calls[:-1]) <= 1
    assert calls[-1] == ("strategy_snapshot", 2)


def test_force_live_once_runs_a_share_bundle_outside_market_hours(monkeypatch):
    from signals.core.market_hours import Market
    from signals.sync import engine as sync_engine
    from signals.sync.engine import SyncEngine

    instance = object.__new__(SyncEngine)
    instance._now = lambda: datetime(2026, 6, 22, 16, 30, 0)
    instance._now_utc = lambda: datetime(2026, 6, 22, 8, 30, 0)
    instance._quote_preopen_enabled = lambda: False
    instance._a_quote_preopen_active = lambda now: False
    calls = []

    monkeypatch.setattr(sync_engine, "get_active_markets", lambda now: set())

    def fake_bundle(active_markets, now, *, force=False):
        calls.append((active_markets, force))
        return [{"module": "market_pools", "status": "ok"}]

    monkeypatch.setattr(instance, "_run_intraday_bundle", fake_bundle)

    assert instance.run_live_once(force=False) == []
    assert instance.run_live_once(force=True) == [{"module": "market_pools", "status": "ok"}]
    assert calls == [({Market.A}, True)]


def test_force_live_once_skips_non_trading_day(monkeypatch):
    from signals.sync import engine as sync_engine
    from signals.sync.engine import SyncEngine

    instance = object.__new__(SyncEngine)
    instance._now = lambda: datetime(2026, 8, 1, 16, 30, 0)  # Saturday
    instance._now_utc = lambda: datetime(2026, 8, 1, 8, 30, 0)
    instance._quote_preopen_enabled = lambda: False
    instance._a_quote_preopen_active = lambda now: False
    called = []
    monkeypatch.setattr(sync_engine, "get_active_markets", lambda now: set())
    monkeypatch.setattr(instance, "_run_intraday_bundle", lambda *args, **kwargs: called.append(True))

    assert instance.run_live_once(force=True) == []
    assert called == []
