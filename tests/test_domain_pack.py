# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import threading
from datetime import datetime

from fastapi.testclient import TestClient


class _Cursor(list):
    def sort(self, *args, **kwargs):
        return self

    def limit(self, n):
        return _Cursor(self[:n])


class _Collection:
    def __init__(self, docs=None):
        self.docs = docs or []

    def _match(self, doc, query):
        for key, value in (query or {}).items():
            actual = doc.get(key)
            if isinstance(value, dict) and "$ne" in value:
                if actual == value["$ne"]:
                    return False
                continue
            if actual != value:
                return False
        return True

    def find_one(self, query=None, projection=None, sort=None):
        for doc in self.docs:
            if self._match(doc, query):
                return dict(doc)
        return None

    def find(self, query=None, projection=None):
        return _Cursor([dict(doc) for doc in self.docs if self._match(doc, query)])


class _Db(dict):
    def __missing__(self, key):
        self[key] = _Collection()
        return self[key]


def _reset_pack_refresh_state(domain_pack):
    with domain_pack._PACK_REFRESH_LOCK:
        domain_pack._PACK_REFRESH_STATE.update({
            "status": "idle",
            "thread": None,
            "started_at": None,
            "finished_at": None,
            "last_requested_at": None,
            "last_reason": "",
            "last_result": {},
            "last_error": "",
        })


def test_signals_pack_dashboard_matches_electron_contract(tmp_path, monkeypatch):
    from signals.domain_pack import SignalsPack

    pack = SignalsPack(repo_root=tmp_path, state_root=tmp_path / "state")
    monkeypatch.setattr(pack, "_backtest_summary", lambda: {"total": 3, "evaluated": 2, "pending": 1})
    monkeypatch.setattr(pack, "_pending_backlog_preview", lambda _limit: [])
    monkeypatch.setattr(pack, "_strategy_snapshot", lambda: {
        "as_of": "2026-04-24",
        "generated_at": "2026-04-24T16:00:00",
        "market_regime": {"label": "偏进攻", "candidate_count": 1},
        "themes": [{"name": "半导体", "domain": "board", "strength": 2.3, "change_pct": 2.3}],
        "candidates": [{
            "symbol": "SZ.002759",
            "name": "测试股份",
            "score": 78,
            "direction": "buy",
            "reason": "趋势增强",
            "status": "open",
            "metadata": {"thesis": "测试 thesis", "next_action": "打开图表复核"},
        }],
        "warnings": [],
        "chart_context": {"symbol": "SZ.002759", "freq": "daily"},
        "daily_brief": {"summary": "今日关注半导体", "changed_since_last": {}},
        "decision_queue": [{"symbol": "SZ.002759", "action": "review_entry"}],
        "strategy_kpis": {"signals_total": 3, "signals_pending": 1},
        "source_confidence": {"overall": 0.9, "sources": []},
    })
    monkeypatch.setattr(pack, "_rank_snapshot", lambda domain: {
        "items": [{"label": "半导体", "change_pct": 2.3, "leader": "测试股份"}],
        "source": f"{domain}_snapshot",
        "freshness": "fresh",
        "warning": "",
    })
    monkeypatch.setattr(pack, "_cache_status", lambda: {
        "available": False,
        "mode": "test",
        "live_low_latency": {"modules": [], "summary": {}},
        "postmarket_backfill": {"run": None, "tasks": [], "summary": {}},
        "mongo_stock_cache": {"freqs": [], "summary": {}},
        "terminal_outputs": [],
        "blockers": [],
    })
    monkeypatch.setattr(pack, "_terminal_clue_candidates", lambda: [])

    dashboard = asyncio.run(pack.dashboard())

    assert dashboard["pack_id"] == "signals"
    assert dashboard["status"] in {"healthy", "degraded"}
    assert dashboard["overview"]["cluster_summary"]["industry_top"]
    assert dashboard["backtest_summary"]["pending"] == 1
    assert dashboard["buy_candidates"][0]["symbol"] == "SZ.002759"
    assert dashboard["daily_brief"]["summary"] == "今日关注半导体"
    assert dashboard["decision_queue"]
    assert dashboard["strategy_kpis"]["signals_total"] == 3
    assert dashboard["source_confidence"]["overall"] == 0.9
    assert dashboard["recent_runs"][0]["capability"] == "strategy_snapshot"
    assert isinstance(dashboard["backtest_jobs"], list)
    assert isinstance(dashboard["deep_links"], list)
    assert any(action["action_id"] == "pack:signals:refresh" for action in dashboard["operator_actions"])
    assert any(action["metadata"] == {} for action in dashboard["operator_actions"])
    assert dashboard["cache_status"]["mode"] == "test"


def test_signals_pack_dashboard_runs_independent_modules_concurrently(tmp_path, monkeypatch):
    from signals.domain_pack import SignalsPack

    pack = SignalsPack(repo_root=tmp_path, state_root=tmp_path / "state")
    strategy_started = threading.Event()
    cache_started = threading.Event()

    def strategy_snapshot():
        strategy_started.set()
        assert cache_started.wait(1)
        return {
            "as_of": "2026-04-24",
            "generated_at": "2026-04-24T16:00:00",
            "themes": [{"name": "半导体", "domain": "board", "strength": 2.3}],
            "candidates": [],
            "warnings": [],
            "daily_brief": {"summary": "并发 dashboard"},
            "decision_queue": [],
            "strategy_kpis": {},
            "source_confidence": {"overall": 0.9, "sources": []},
        }

    def cache_status():
        cache_started.set()
        assert strategy_started.wait(1)
        return {
            "available": True,
            "mode": "test",
            "live_low_latency": {"modules": [], "summary": {}},
            "postmarket_backfill": {"run": None, "tasks": [], "summary": {}},
            "mongo_stock_cache": {"freqs": [], "summary": {}},
            "terminal_outputs": [],
            "blockers": [],
        }

    monkeypatch.setattr(pack, "_strategy_snapshot", strategy_snapshot)
    monkeypatch.setattr(pack, "_cache_status", cache_status)
    monkeypatch.setattr(pack, "_terminal_clue_candidates", lambda: [])
    monkeypatch.setattr(pack, "_connector_health", lambda: [])
    monkeypatch.setattr(pack, "_backtest_summary", lambda: {"total": 0, "evaluated": 0, "pending": 0})
    monkeypatch.setattr(pack, "_pending_backlog_preview", lambda _limit: [])

    dashboard = asyncio.run(pack.dashboard())

    assert dashboard["daily_brief"]["summary"] == "并发 dashboard"
    assert dashboard["cache_status"]["available"] is True


def test_signals_pack_dashboard_prepends_terminal_clue_candidates(tmp_path, monkeypatch):
    from signals.domain_pack import SignalsPack

    pack = SignalsPack(repo_root=tmp_path, state_root=tmp_path / "state")
    monkeypatch.setattr(pack, "_backtest_summary", lambda: {"total": 0, "evaluated": 0, "pending": 0})
    monkeypatch.setattr(pack, "_pending_backlog_preview", lambda _limit: [])
    monkeypatch.setattr(pack, "_cache_status", lambda: {
        "available": True,
        "mode": "test",
        "live_low_latency": {"modules": [], "summary": {}},
        "postmarket_backfill": {"run": None, "tasks": [], "summary": {}},
        "mongo_stock_cache": {"freqs": [], "summary": {}},
        "terminal_outputs": [],
        "blockers": [],
    })
    monkeypatch.setattr(pack, "_strategy_snapshot", lambda: {
        "as_of": "2026-06-30",
        "generated_at": "2026-06-30T10:30:00",
        "themes": [],
        "candidates": [
            {"symbol": "SH.600172", "name": "黄河旋风", "source": "strategy_snapshot"},
            {"symbol": "SZ.300308", "name": "中际旭创", "source": "strategy_snapshot"},
        ],
        "warnings": [],
        "daily_brief": {"summary": "线索池测试"},
        "decision_queue": [],
        "strategy_kpis": {},
        "source_confidence": {"overall": 0.9, "sources": []},
    })
    monkeypatch.setattr(pack, "_terminal_clue_candidates", lambda: [
        {
            "symbol": "SH.600172",
            "name": "黄河旋风",
            "source": "terminal_stock_pool.clue_stocks",
            "source_collections": ["hot_rank_clues"],
            "stage_label": "线索池",
        },
        {
            "symbol": "SH.603290",
            "name": "斯达半导",
            "source": "terminal_stock_pool.clue_stocks",
            "source_collections": ["hot_rank_clues"],
            "stage_label": "线索池",
        },
    ])

    dashboard = asyncio.run(pack.dashboard())

    assert [row["symbol"] for row in dashboard["buy_candidates"][:3]] == [
        "SH.600172",
        "SH.603290",
        "SZ.300308",
    ]
    assert dashboard["buy_candidates"][0]["source"] == "terminal_stock_pool.clue_stocks"


def test_pack_refresh_endpoint_triggers_pack_refresh(monkeypatch):
    from signals.web.app import create_app
    from signals import domain_pack

    calls = []

    def fake_trigger(self, **kwargs):
        calls.append(kwargs)
        return {"triggered": True, "status": "running", "message": "refresh_started"}

    monkeypatch.setattr(domain_pack.SignalsPack, "trigger_refresh", fake_trigger)
    client = TestClient(create_app())

    response = client.post("/api/pack/refresh", json={"reason": "manual", "force_live": True, "wait": False})

    assert response.status_code == 200
    assert response.json()["triggered"] is True
    assert calls == [{
        "reason": "manual",
        "force_live": True,
        "force_postmarket": False,
        "run_optional_tasks": True,
        "wait": False,
    }]


def test_pack_trigger_refresh_wait_records_result(monkeypatch):
    from signals import domain_pack
    from signals.domain_pack import SignalsPack

    _reset_pack_refresh_state(domain_pack)
    pack = SignalsPack()

    def fake_run(**kwargs):
        return {"status": "ok", "reason": kwargs["reason"], "live_result_count": 3}

    monkeypatch.setattr(pack, "_run_refresh_job", fake_run)

    result = pack.trigger_refresh(reason="manual", force_live=True, wait=True)

    assert result["triggered"] is True
    assert result["message"] == "refresh_completed"
    assert result["status"] == "completed"
    assert result["last_result"]["live_result_count"] == 3
    assert result["last_error"] == ""


def test_pack_trigger_refresh_throttles_auto_requests(monkeypatch):
    from signals import domain_pack
    from signals.domain_pack import SignalsPack

    _reset_pack_refresh_state(domain_pack)
    monkeypatch.setenv("SIGNALS_PACK_AUTO_REFRESH_MIN_SECONDS", "300")
    pack = SignalsPack()
    monkeypatch.setattr(pack, "_run_refresh_job", lambda **kwargs: {"status": "ok"})

    first = pack.trigger_refresh(reason="watchdog", wait=True)
    second = pack.trigger_refresh(reason="watchdog", wait=True)

    assert first["triggered"] is True
    assert second["triggered"] is False
    assert second["message"] == "refresh_throttled"


def test_pack_trigger_refresh_startup_uses_dedicated_throttle(monkeypatch):
    from signals import domain_pack
    from signals.domain_pack import SignalsPack

    _reset_pack_refresh_state(domain_pack)
    monkeypatch.setenv("SIGNALS_PACK_AUTO_REFRESH_MIN_SECONDS", "300")
    monkeypatch.setenv("SIGNALS_PACK_STARTUP_REFRESH_MIN_SECONDS", "0")
    pack = SignalsPack()
    monkeypatch.setattr(pack, "_run_refresh_job", lambda **kwargs: {"status": "ok"})

    first = pack.trigger_refresh(reason="startup", wait=True)
    second = pack.trigger_refresh(reason="startup", wait=True)

    assert first["triggered"] is True
    assert second["triggered"] is True
    assert second["message"] == "refresh_completed"


def test_pack_postmarket_live_owner_guard(monkeypatch):
    from signals import domain_pack
    from signals.domain_pack import SignalsPack

    db = _Db({
        "sync_runs": _Collection([
            {"_id": "postmarket:2026-06-22", "owner_pid": 12345},
        ]),
    })
    pack = SignalsPack()
    monkeypatch.setattr(domain_pack.os, "getpid", lambda: 999)
    monkeypatch.setattr(domain_pack.os, "kill", lambda pid, sig: None)

    assert pack._postmarket_live_owner_pid(db, "postmarket:2026-06-22") == 12345

    def dead_process(pid, sig):
        raise OSError("no such process")

    monkeypatch.setattr(domain_pack.os, "kill", dead_process)
    assert pack._postmarket_live_owner_pid(db, "postmarket:2026-06-22") == 0


def test_pack_dashboard_endpoint_smoke():
    from signals.web.app import create_app

    client = TestClient(create_app())
    response = client.get("/api/pack/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["pack_id"] == "signals"
    assert "connector_health" in payload
    assert "overview" in payload
    assert "daily_brief" in payload
    assert "decision_queue" in payload
    assert "strategy_kpis" in payload
    assert "source_confidence" in payload
    assert "cache_status" in payload


def test_cache_freshness_uses_beijing_market_time(monkeypatch):
    from signals import domain_pack
    from signals.domain_pack import SignalsPack

    monkeypatch.setattr(domain_pack, "naive_market_now", lambda _market: datetime(2026, 4, 29, 10, 5))

    pack = SignalsPack()

    assert pack._freshness_seconds(datetime(2026, 4, 29, 9, 55)) == 600


def test_cache_trade_date_prefers_current_market_day(monkeypatch):
    from signals.data import mongo_fallback
    from signals.domain_pack import SignalsPack

    monkeypatch.setattr(mongo_fallback, "get_last_trading_day", lambda _market="A": "2026-04-30")
    db = _Db({
        "sync_runs": _Collection([
            {"trade_date": "2026-04-29", "started_at": datetime(2026, 4, 29, 19, 0)}
        ])
    })

    assert SignalsPack()._cache_trade_date(db) == "2026-04-30"


def test_mongo_stock_cache_uses_latest_daily_bar_date_for_daily_coverage(monkeypatch):
    from signals.domain_pack import SignalsPack

    pack = SignalsPack()
    requested_dates = []

    def fake_find_one(db, collection, query, projection=None, sort=None):
        if collection == "sync_log" and query.get("_id") == "stock_daily:progress:_meta":
            return {"total": 5515, "processed": 5515, "inserted": 5490, "progress_pct": 100.0}
        if collection == "bars" and query.get("meta.freq") == "日线":
            return {"dt": datetime(2026, 5, 12), "meta": {"symbol": "920957"}}
        return None

    def fake_daily_snapshot_coverage(db, trade_date):
        requested_dates.append(trade_date)
        return {
            "valid_universe": 5490,
            "cached_today": 5490,
            "invalid_rows": 358,
            "source": "fullmarket_spot_snapshots.valid_universe + bars.daily",
        }

    monkeypatch.setattr(pack, "_find_one", fake_find_one)
    monkeypatch.setattr(pack, "_cache_daily_snapshot_coverage", fake_daily_snapshot_coverage)
    monkeypatch.setattr(pack, "_cache_minute_universe", lambda db, trade_date: {})

    result = pack._cache_mongo_stock_cache(_Db(), "2026-05-13")

    assert requested_dates == ["2026-05-12"]
    assert result["summary"]["daily_coverage_date"] == "2026-05-12"
    assert result["summary"]["daily_symbols"] == 5490
    assert result["summary"]["daily_today_symbols"] == 5490
    assert result["summary"]["daily_missing_symbols"] == 0
    assert result["freqs"][0]["coverage_date"] == "2026-05-12"


def test_live_low_latency_strict_status_and_stock_selection_merge(monkeypatch):
    from signals import domain_pack
    from signals.domain_pack import SignalsPack

    now = datetime(2026, 4, 29, 10, 0)
    monkeypatch.setattr(domain_pack, "naive_market_now", lambda _market: now)
    db = _Db({
        "sync_log": _Collection([
            {"_id": "quote_snapshots:A:_meta", "module": "quote_snapshots", "status": "ok", "last_run": now},
            {
                "_id": "stock_minute:A:_meta",
                "module": "stock_minute",
                "status": "degraded",
                "last_run": now,
                "error_msg": "orphaned_running_module",
            },
            {
                "_id": "stock_minute:selection:_meta",
                "module": "stock_minute",
                "status": "partial",
                "selected_symbols": ["688802", "300575"],
                "error_msg": "",
            },
            {"_id": "index_minute:A:_meta", "module": "index_minute", "status": "ok", "last_run": now},
            {
                "_id": "minute_readiness_probe:A:_meta",
                "module": "minute_readiness_probe",
                "status": "ok",
                "last_run": now,
                "result": {"checked": 36, "not_ready": 2},
            },
            {"_id": "market_pools:A:_meta", "module": "market_pools", "status": "running", "last_run": now},
            {"_id": "board_heat_minute:A:_meta", "module": "board_heat_minute", "status": "ok", "last_run": now},
            {"_id": "concept_heat_minute:A:_meta", "module": "concept_heat_minute", "status": "partial", "last_run": now},
            {"_id": "chain_heat_snapshots:A:_meta", "module": "chain_heat_snapshots", "status": "ok", "last_run": now},
        ]),
        "minute_readiness": _Collection([
            {
                "trade_date": "2026-04-29",
                "domain": "index",
                "symbol": "sh000680",
                "freq": "5分钟",
                "status": "not_ready",
                "root_cause_class": "index_minute_not_ready",
            }
        ]),
    })
    pack = SignalsPack()

    live = pack._cache_live_low_latency(db)

    assert live["summary"]["ok_modules"] == 4
    assert live["summary"]["strict_status"] == "degraded"
    assert live["summary"]["minute_not_ready"] == 2
    assert live["summary"]["minute_not_ready_samples"][0]["symbol"] == "sh000680"
    stock = next(item for item in live["modules"] if item["module"] == "stock_minute")
    assert stock["status"] == "degraded"
    assert stock["error_msg"] == "orphaned_running_module"
    assert stock["selected_symbols"] == ["688802", "300575"]
    blockers = pack._cache_blockers(live, {"tasks": []}, [])
    assert {item["module"] for item in blockers} >= {"stock_minute", "minute_readiness_probe", "market_pools", "concept_heat_minute"}


def test_live_low_latency_keeps_closed_a_share_snapshots_usable(monkeypatch):
    from signals import domain_pack
    from signals.domain_pack import SignalsPack

    now = datetime(2026, 4, 29, 16, 20)
    last_a_tick = datetime(2026, 4, 29, 14, 58)
    monkeypatch.setattr(domain_pack, "naive_market_now", lambda _market: now)
    db = _Db({
        "sync_log": _Collection([
            {
                "_id": "quote_snapshots:A:_meta",
                "module": "quote_snapshots",
                "status": "degraded",
                "last_run": now,
                "result": {"live": 105, "count": 109, "missing_current": 4},
            },
            {"_id": "stock_minute:A:_meta", "module": "stock_minute", "status": "ok", "last_run": last_a_tick},
            {"_id": "index_minute:A:_meta", "module": "index_minute", "status": "ok", "last_run": last_a_tick},
            {
                "_id": "minute_readiness_probe:A:_meta",
                "module": "minute_readiness_probe",
                "status": "ok",
                "last_run": last_a_tick,
                "result": {"checked": 373, "not_ready": 0},
            },
            {"_id": "market_pools:A:_meta", "module": "market_pools", "status": "ok", "last_run": last_a_tick},
            {
                "_id": "board_heat_minute:A:_meta",
                "module": "board_heat_minute",
                "status": "ok",
                "last_run": last_a_tick,
                "result": {"latest_minute": "2026-04-29T14:59:00"},
            },
            {
                "_id": "concept_heat_minute:A:_meta",
                "module": "concept_heat_minute",
                "status": "ok",
                "last_run": last_a_tick,
                "result": {"latest_minute": "2026-04-29T14:59:00"},
            },
            {
                "_id": "chain_heat_snapshots:A:_meta",
                "module": "chain_heat_snapshots",
                "status": "ok",
                "last_run": last_a_tick,
                "result": {"latest_minute": "2026-04-29T14:59:00"},
            },
        ]),
    })

    live = SignalsPack()._cache_live_low_latency(db)

    statuses = {item["module"]: item["status"] for item in live["modules"]}
    assert statuses["quote_snapshots"] == "degraded"
    assert statuses["index_minute"] == "ok"
    assert statuses["minute_readiness_probe"] == "ok"
    assert statuses["board_heat_minute"] == "ok"
    assert statuses["concept_heat_minute"] == "ok"
    assert statuses["chain_heat_snapshots"] == "ok"
    assert live["summary"]["ok_modules"] == 7
    assert live["summary"]["problem_modules"] == ["quote_snapshots"]


def test_live_low_latency_treats_runtime_exceeded_with_outputs_as_usable(monkeypatch):
    from signals import domain_pack
    from signals.domain_pack import SignalsPack

    now = datetime(2026, 4, 29, 10, 0)
    monkeypatch.setattr(domain_pack, "naive_market_now", lambda _market: now)
    db = _Db({
        "sync_log": _Collection([
            {"_id": "quote_snapshots:A:_meta", "module": "quote_snapshots", "status": "ok", "last_run": now},
            {"_id": "stock_minute:A:_meta", "module": "stock_minute", "status": "ok", "last_run": now},
            {
                "_id": "index_minute:A:_meta",
                "module": "index_minute",
                "status": "degraded",
                "last_run": now,
                "error_msg": "runtime_exceeded_120s",
                "degraded_reason": "runtime_exceeded_120s",
                "result": {"written": 27, "planned_calls": 36, "empty": 9, "errors": 0},
            },
            {
                "_id": "minute_readiness_probe:A:_meta",
                "module": "minute_readiness_probe",
                "status": "ok",
                "last_run": now,
                "result": {"checked": 492, "not_ready": 0},
            },
            {
                "_id": "market_pools:A:_meta",
                "module": "market_pools",
                "status": "degraded",
                "last_run": now,
                "error_msg": "runtime_exceeded_60s",
                "degraded_reason": "runtime_exceeded_60s",
                "result": {"count": 50, "modified": 1},
            },
            {"_id": "board_heat_minute:A:_meta", "module": "board_heat_minute", "status": "ok", "last_run": now},
            {"_id": "concept_heat_minute:A:_meta", "module": "concept_heat_minute", "status": "ok", "last_run": now},
            {"_id": "chain_heat_snapshots:A:_meta", "module": "chain_heat_snapshots", "status": "ok", "last_run": now},
        ]),
    })

    live = SignalsPack()._cache_live_low_latency(db)

    statuses = {item["module"]: item["status"] for item in live["modules"]}
    assert statuses["index_minute"] == "ok"
    assert statuses["market_pools"] == "ok"
    assert live["summary"]["strict_status"] == "ok"
    assert live["summary"]["problem_modules"] == []


def test_live_low_latency_prefers_a_market_meta_over_postmarket_generic(monkeypatch):
    from signals import domain_pack
    from signals.domain_pack import SignalsPack

    now = datetime(2026, 4, 29, 10, 0)
    later = datetime(2026, 4, 29, 10, 5)
    monkeypatch.setattr(domain_pack, "naive_market_now", lambda _market: now)
    db = _Db({
        "sync_log": _Collection([
            {"_id": "quote_snapshots:A:_meta", "module": "quote_snapshots", "status": "ok", "last_run": now},
            {"_id": "stock_minute:A:_meta", "module": "stock_minute", "status": "ok", "last_run": now},
            {
                "_id": "stock_minute:_meta",
                "module": "stock_minute",
                "status": "running",
                "last_run": later,
                "result": {"selected": 240},
            },
            {
                "_id": "stock_minute:selection:_meta",
                "module": "stock_minute",
                "selected_symbols": ["688802", "300575"],
                "minute_scope": "terminal_stock_pool",
            },
            {"_id": "index_minute:A:_meta", "module": "index_minute", "status": "ok", "last_run": now},
            {
                "_id": "minute_readiness_probe:A:_meta",
                "module": "minute_readiness_probe",
                "status": "ok",
                "last_run": now,
                "result": {"checked": 36, "not_ready": 0},
            },
            {
                "_id": "minute_readiness_probe:_meta",
                "module": "minute_readiness_probe",
                "status": "partial",
                "last_run": later,
                "result": {"checked": 500, "not_ready": 3},
            },
            {"_id": "market_pools:A:_meta", "module": "market_pools", "status": "ok", "last_run": now},
            {"_id": "board_heat_minute:A:_meta", "module": "board_heat_minute", "status": "ok", "last_run": now},
            {"_id": "concept_heat_minute:A:_meta", "module": "concept_heat_minute", "status": "ok", "last_run": now},
            {"_id": "chain_heat_snapshots:A:_meta", "module": "chain_heat_snapshots", "status": "ok", "last_run": now},
        ]),
    })

    live = SignalsPack()._cache_live_low_latency(db)

    stock = next(item for item in live["modules"] if item["module"] == "stock_minute")
    minute = next(item for item in live["modules"] if item["module"] == "minute_readiness_probe")
    assert stock["status"] == "ok"
    assert stock["raw_status"] == "ok"
    assert stock["selected_symbols"] == ["688802", "300575"]
    assert minute["status"] == "ok"
    assert live["summary"]["strict_status"] == "ok"
    assert live["summary"]["problem_modules"] == []


def test_live_low_latency_treats_orphaned_result_with_outputs_as_usable(monkeypatch):
    from signals import domain_pack
    from signals.domain_pack import SignalsPack

    now = datetime(2026, 4, 29, 10, 0)
    monkeypatch.setattr(domain_pack, "naive_market_now", lambda _market: now)
    db = _Db({
        "sync_log": _Collection([
            {"_id": "quote_snapshots:A:_meta", "module": "quote_snapshots", "status": "ok", "last_run": now},
            {"_id": "stock_minute:A:_meta", "module": "stock_minute", "status": "ok", "last_run": now},
            {"_id": "index_minute:A:_meta", "module": "index_minute", "status": "ok", "last_run": now},
            {
                "_id": "minute_readiness_probe:A:_meta",
                "module": "minute_readiness_probe",
                "status": "ok",
                "last_run": now,
                "result": {"checked": 492, "not_ready": 0},
            },
            {
                "_id": "market_pools:A:_meta",
                "module": "market_pools",
                "status": "degraded",
                "last_run": now,
                "error_msg": "orphaned_running_module",
                "result": {"count": 50, "modified": 1},
            },
            {"_id": "board_heat_minute:A:_meta", "module": "board_heat_minute", "status": "ok", "last_run": now},
            {"_id": "concept_heat_minute:A:_meta", "module": "concept_heat_minute", "status": "ok", "last_run": now},
            {"_id": "chain_heat_snapshots:A:_meta", "module": "chain_heat_snapshots", "status": "ok", "last_run": now},
        ]),
    })

    live = SignalsPack()._cache_live_low_latency(db)

    statuses = {item["module"]: item["status"] for item in live["modules"]}
    assert statuses["market_pools"] == "ok"
    assert live["summary"]["strict_status"] == "ok"
    assert live["summary"]["problem_modules"] == []
    assert SignalsPack()._cache_blockers(live, {"tasks": []}, []) == []


def test_live_low_latency_treats_running_result_with_outputs_as_usable(monkeypatch):
    from signals import domain_pack
    from signals.domain_pack import SignalsPack

    now = datetime(2026, 4, 29, 10, 0)
    monkeypatch.setattr(domain_pack, "naive_market_now", lambda _market: now)
    db = _Db({
        "sync_log": _Collection([
            {"_id": "quote_snapshots:A:_meta", "module": "quote_snapshots", "status": "ok", "last_run": now},
            {"_id": "stock_minute:A:_meta", "module": "stock_minute", "status": "ok", "last_run": now},
            {
                "_id": "index_minute:A:_meta",
                "module": "index_minute",
                "status": "running",
                "last_run": now,
                "result": {"written": 29, "planned_calls": 36, "empty": 15, "errors": 0},
            },
            {
                "_id": "minute_readiness_probe:A:_meta",
                "module": "minute_readiness_probe",
                "status": "ok",
                "last_run": now,
                "result": {"checked": 492, "not_ready": 0},
            },
            {"_id": "market_pools:A:_meta", "module": "market_pools", "status": "ok", "last_run": now},
            {"_id": "board_heat_minute:A:_meta", "module": "board_heat_minute", "status": "ok", "last_run": now},
            {"_id": "concept_heat_minute:A:_meta", "module": "concept_heat_minute", "status": "ok", "last_run": now},
            {
                "_id": "chain_heat_snapshots:A:_meta",
                "module": "chain_heat_snapshots",
                "status": "running",
                "last_run": now,
                "result": {"status": "ok", "nodes": 45, "latest_minute": "2026-04-29 09:59:00"},
            },
        ]),
    })

    live = SignalsPack()._cache_live_low_latency(db)

    statuses = {item["module"]: item["status"] for item in live["modules"]}
    assert statuses["index_minute"] == "ok"
    assert statuses["chain_heat_snapshots"] == "ok"
    assert live["summary"]["strict_status"] == "ok"
    assert live["summary"]["problem_modules"] == []


def test_live_low_latency_uses_effective_trade_day_on_holiday(monkeypatch):
    from signals import domain_pack
    from signals.data import mongo_fallback
    from signals.domain_pack import SignalsPack

    now = datetime(2026, 5, 1, 10, 0)
    last_trade_tick = datetime(2026, 4, 30, 15, 0)
    monkeypatch.setattr(domain_pack, "naive_market_now", lambda _market: now)
    monkeypatch.setattr(mongo_fallback, "get_last_trading_day", lambda _market="A": "2026-04-30")
    db = _Db({
        "sync_log": _Collection([
            {
                "_id": "quote_snapshots:A:_meta",
                "module": "quote_snapshots",
                "status": "degraded",
                "last_run": now,
                "result": {"count": 310, "live": 305, "missing_current": 5, "errors": 0},
            },
            {"_id": "stock_minute:A:_meta", "module": "stock_minute", "status": "ok", "last_run": last_trade_tick},
            {"_id": "index_minute:A:_meta", "module": "index_minute", "status": "ok", "last_run": last_trade_tick},
            {
                "_id": "minute_readiness_probe:A:_meta",
                "module": "minute_readiness_probe",
                "status": "ok",
                "last_run": now,
                "result": {"checked": 36, "not_ready": 0, "trade_date": "2026-04-30"},
            },
            {"_id": "market_pools:A:_meta", "module": "market_pools", "status": "ok", "last_run": last_trade_tick},
            {
                "_id": "board_heat_minute:A:_meta",
                "module": "board_heat_minute",
                "status": "ok",
                "last_run": datetime(2026, 5, 1, 9, 0),
                "result": {"latest_minute": "2026-04-30T15:00:00"},
            },
            {
                "_id": "concept_heat_minute:A:_meta",
                "module": "concept_heat_minute",
                "status": "ok",
                "last_run": datetime(2026, 5, 1, 9, 0),
                "result": {"as_of": "2026-04-30"},
            },
            {
                "_id": "chain_heat_snapshots:A:_meta",
                "module": "chain_heat_snapshots",
                "status": "ok",
                "last_run": datetime(2026, 5, 1, 9, 0),
                "result": {"trade_date": "2026-04-30"},
            },
        ]),
    })

    live = SignalsPack()._cache_live_low_latency(db)

    statuses = {item["module"]: item["status"] for item in live["modules"]}
    assert statuses["quote_snapshots"] == "ok"
    assert statuses["stock_minute"] == "ok"
    assert statuses["index_minute"] == "ok"
    assert statuses["minute_readiness_probe"] == "ok"
    assert statuses["board_heat_minute"] == "ok"
    assert statuses["concept_heat_minute"] == "ok"
    assert statuses["chain_heat_snapshots"] == "ok"
    assert live["summary"]["ok_modules"] == 8
    assert live["summary"]["strict_status"] == "ok"
    assert live["summary"]["problem_modules"] == []
    assert SignalsPack()._cache_blockers(live, {"tasks": []}, []) == []


def test_provider_health_blocker_ignores_degraded_source_with_healthy_peer():
    from signals.domain_pack import SignalsPack

    pack = SignalsPack()
    blockers = pack._cache_blockers(
        {"modules": []},
        {"tasks": []},
        [
            {
                "provider": "sina",
                "endpoint": "stock_minute",
                "domain": "minute",
                "status": "degraded",
                "last_error_type": "ReadTimeout",
                "updated_at": "2026-04-30T10:47:52",
            },
            {
                "provider": "tencent",
                "endpoint": "stock_minute",
                "domain": "minute",
                "status": "running",
                "last_success_at": "2026-04-30T10:47:51",
                "updated_at": "2026-04-30T10:47:53",
            },
        ],
    )

    assert blockers == []


def test_cache_recovery_state_reports_old_daily_cache():
    from signals.domain_pack import SignalsPack

    state = SignalsPack()._cache_recovery_state(
        trade_date="2026-05-13",
        daily_coverage_date="2026-05-12",
        terminal_ready_date="",
        postmarket={"run": {"status": "running"}},
        critical_blocker={},
    )

    assert state == "old_cache_readable"


def test_cache_recovery_state_accepts_previous_daily_cache_intraday(monkeypatch):
    from signals import domain_pack
    from signals.domain_pack import SignalsPack

    monkeypatch.setattr(domain_pack, "naive_market_now", lambda _market: datetime(2026, 5, 13, 10, 0))

    state = SignalsPack()._cache_recovery_state(
        trade_date="2026-05-13",
        daily_coverage_date="2026-05-12",
        terminal_ready_date="2026-05-13",
        postmarket={"run": {"status": "ok"}},
        critical_blocker={},
    )

    assert state == "terminal_ready"


def test_fullmarket_provider_blocker_is_prioritized():
    from signals.domain_pack import SignalsPack

    pack = SignalsPack()
    provider_health = [
        {
            "provider": "sina",
            "endpoint": "stock_minute",
            "domain": "minute",
            "status": "degraded",
            "last_error_type": "ReadTimeout",
            "last_error_at": "2026-05-13T09:01:00",
            "updated_at": "2026-05-13T09:01:00",
        },
        {
            "provider": "eastmoney",
            "endpoint": "fullmarket_spot_snapshot",
            "domain": "market_data",
            "status": "degraded",
            "last_error_type": "SSLError",
            "last_error_at": "2026-05-13T09:02:00",
            "last_success_at": "2026-05-12T16:12:00",
            "updated_at": "2026-05-13T09:02:00",
        },
    ]

    blocker = pack._cache_critical_blocker({"run": {}, "tasks": []}, provider_health)
    blockers = pack._cache_blockers({"modules": []}, {"tasks": []}, provider_health)

    assert blocker["provider"] == "eastmoney"
    assert blocker["endpoint"] == "fullmarket_spot_snapshot"
    assert blocker["last_success_at"] == "2026-05-12T16:12:00"
    assert blockers[0]["endpoint"] == "fullmarket_spot_snapshot"


def test_fullmarket_provider_recovery_hides_stale_run_blocker():
    from signals.domain_pack import SignalsPack

    pack = SignalsPack()
    postmarket = {
        "run": {
            "status": "partial",
            "recovery_state": "waiting_for_source",
            "critical_blocker": {
                "provider": "eastmoney",
                "endpoint": "fullmarket_spot_snapshot",
                "status": "degraded",
            },
        },
        "tasks": [
            {"module": "fullmarket_spot_snapshot", "status": "degraded", "error_msg": "old SSL error"},
        ],
    }
    provider_health = [
        {
            "provider": "eastmoney",
            "endpoint": "fullmarket_spot_snapshot",
            "domain": "market_data",
            "status": "ok",
            "last_success_at": "2026-05-13T09:10:00",
            "updated_at": "2026-05-13T09:10:00",
        }
    ]

    blocker = pack._cache_critical_blocker(postmarket, provider_health)
    state = pack._cache_recovery_state(
        trade_date="2026-05-13",
        daily_coverage_date="2026-05-13",
        terminal_ready_date="",
        postmarket=postmarket,
        critical_blocker=blocker,
    )

    assert blocker == {}
    assert state == "postmarket_running"


def test_completed_postmarket_progress_is_done_even_with_stale_task_progress():
    from signals.domain_pack import SignalsPack

    db = _Db({
        "sync_runs": _Collection([
            {
                "_id": "postmarket:2026-04-29",
                "run_id": "postmarket:2026-04-29",
                "trade_date": "2026-04-29",
                "status": "ok",
                "started_at": datetime(2026, 4, 29, 15, 35),
                "finished_at": datetime(2026, 4, 29, 19, 14),
            }
        ]),
        "sync_tasks": _Collection([
            {
                "_id": "postmarket:2026-04-29:stock_daily:shard_00",
                "run_id": "postmarket:2026-04-29",
                "module": "stock_daily",
                "phase": "market_data",
                "shard_key": "shard_00",
                "status": "ok",
                "order": 1,
                "cursor": {"progress_pct": 20.0},
            }
        ]),
    })
    pack = SignalsPack()

    postmarket = pack._cache_postmarket_backfill(db)

    assert postmarket["summary"]["completed"] == 1
    assert postmarket["summary"]["progress_pct"] == 100.0
    assert postmarket["summary"]["critical_progress_pct"] == 100.0
    assert postmarket["summary"]["critical_status"] == "ok"
    assert postmarket["summary"]["eta_seconds"] == 0


def test_postmarket_effective_done_tasks_are_done_and_not_blockers():
    from signals.domain_pack import SignalsPack

    db = _Db({
        "sync_runs": _Collection([
            {
                "_id": "postmarket:2026-06-22",
                "run_id": "postmarket:2026-06-22",
                "trade_date": "2026-06-22",
                "status": "running",
                "started_at": datetime(2026, 6, 22, 16, 10),
            }
        ]),
        "sync_tasks": _Collection([
            {
                "_id": "postmarket:2026-06-22:quote_snapshots:all",
                "run_id": "postmarket:2026-06-22",
                "module": "quote_snapshots",
                "phase": "market_data",
                "task_key": "quote_snapshots:all",
                "shard_key": "all",
                "blocks_run": True,
                "status": "degraded",
                "order": 1,
                "result_summary": {"status": "degraded", "result": {"count": 363, "live": 362, "errors": 1}},
            },
            {
                "_id": "postmarket:2026-06-22:stock_minute:all",
                "run_id": "postmarket:2026-06-22",
                "module": "stock_minute",
                "phase": "minute_preheat",
                "task_key": "stock_minute:all",
                "shard_key": "all",
                "blocks_run": True,
                "status": "degraded",
                "order": 2,
                "result_summary": {
                    "status": "degraded",
                    "result": {"written": 28427, "planned_calls": 320, "empty": 91, "errors": 0},
                },
            },
            {
                "_id": "postmarket:2026-06-22:hk_stock_daily:shard_00",
                "run_id": "postmarket:2026-06-22",
                "module": "hk_stock_daily",
                "phase": "hk_market_data",
                "task_key": "hk_stock_daily:shard_00",
                "shard_key": "shard_00",
                "blocks_run": False,
                "status": "stale",
                "order": 3,
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
            },
            {
                "_id": "postmarket:2026-06-22:stock_daily:shard_01",
                "run_id": "postmarket:2026-06-22",
                "module": "stock_daily",
                "phase": "market_data",
                "task_key": "stock_daily:shard_01",
                "shard_key": "shard_01",
                "blocks_run": True,
                "status": "stale",
                "order": 4,
                "error_msg": "running_owner_dead",
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
            },
            {
                "_id": "postmarket:2026-06-22:stock_daily:shard_02",
                "run_id": "postmarket:2026-06-22",
                "module": "stock_daily",
                "phase": "market_data",
                "task_key": "stock_daily:shard_02",
                "shard_key": "shard_02",
                "blocks_run": True,
                "status": "running",
                "order": 5,
                "result_summary": {
                    "status": "partial",
                    "processed": 5510,
                    "total": 5510,
                    "covered_codes": 5482,
                    "errors": 16,
                    "deferred": 3504,
                    "progress_pct": 81.19,
                    "coverage_pct": 99.49,
                    "source": "fullmarket_spot_snapshots.valid_universe + bars.daily",
                },
                "cursor": {"processed": 5510, "total": 5510, "progress_pct": 81.19},
            },
            {
                "_id": "postmarket:2026-06-22:minute_readiness_probe:all",
                "run_id": "postmarket:2026-06-22",
                "module": "minute_readiness_probe",
                "phase": "minute_preheat",
                "task_key": "minute_readiness_probe:all",
                "shard_key": "all",
                "blocks_run": False,
                "status": "partial",
                "order": 6,
                "result_summary": {
                    "status": "partial",
                    "result": {"checked": 516, "not_ready": 3, "inserted": 516},
                },
            },
        ]),
    })
    pack = SignalsPack()

    postmarket = pack._cache_postmarket_backfill(db)
    rows = {row["task_id"]: row for row in postmarket["tasks"]}

    assert postmarket["summary"]["completed"] == 6
    assert postmarket["summary"]["critical_completed"] == 4
    assert postmarket["summary"]["critical_status"] == "ok"
    assert postmarket["summary"]["optional_completed"] == 2
    assert postmarket["summary"]["optional_progress_pct"] == 100.0
    assert postmarket["summary"]["status_counts"] == {"ok": 6}
    assert rows["postmarket:2026-06-22:quote_snapshots:all"]["status"] == "ok"
    assert rows["postmarket:2026-06-22:quote_snapshots:all"]["raw_status"] == "degraded"
    assert rows["postmarket:2026-06-22:stock_daily:shard_01"]["raw_status"] == "stale"
    assert rows["postmarket:2026-06-22:stock_daily:shard_02"]["raw_status"] == "running"
    assert rows["postmarket:2026-06-22:hk_stock_daily:shard_00"]["raw_status"] == "stale"
    assert rows["postmarket:2026-06-22:minute_readiness_probe:all"]["raw_status"] == "partial"
    assert pack._cache_blockers({"modules": []}, postmarket, []) == []


def test_postmarket_critical_progress_ignores_optional_hk_tail():
    from signals.domain_pack import SignalsPack

    db = _Db({
        "sync_runs": _Collection([
            {
                "_id": "postmarket:2026-05-11",
                "run_id": "postmarket:2026-05-11",
                "trade_date": "2026-05-11",
                "status": "running",
                "phase": "hk_market_data",
                "started_at": datetime(2026, 5, 11, 16, 10),
            }
        ]),
        "sync_tasks": _Collection([
            {
                "_id": "postmarket:2026-05-11:terminal_realtime_pool:all",
                "run_id": "postmarket:2026-05-11",
                "module": "terminal_realtime_pool",
                "phase": "terminal",
                "task_key": "terminal_realtime_pool:all",
                "shard_key": "all",
                "blocks_run": True,
                "status": "ok",
                "order": 1,
            },
            {
                "_id": "postmarket:2026-05-11:hk_stock_daily:shard_00",
                "run_id": "postmarket:2026-05-11",
                "module": "hk_stock_daily",
                "phase": "hk_market_data",
                "task_key": "hk_stock_daily:shard_00",
                "shard_key": "shard_00",
                "blocks_run": False,
                "status": "running",
                "order": 2,
                "cursor": {"processed": 100, "total": 400},
            },
        ]),
    })
    pack = SignalsPack()

    postmarket = pack._cache_postmarket_backfill(db)

    assert postmarket["summary"]["progress_pct"] == 62.5
    assert postmarket["summary"]["critical_progress_pct"] == 100.0
    assert postmarket["summary"]["critical_status"] == "ok"
    assert postmarket["summary"]["optional_progress_pct"] == 25.0
    assert postmarket["summary"]["optional_status_counts"] == {"running": 1}
