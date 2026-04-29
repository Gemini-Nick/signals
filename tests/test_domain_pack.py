# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
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
    assert dashboard["operator_actions"][0]["metadata"] == {}
    assert dashboard["cache_status"]["mode"] == "test"


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
