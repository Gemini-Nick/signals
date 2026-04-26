# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient


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
