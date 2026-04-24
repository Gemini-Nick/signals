# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient


def test_signals_pack_dashboard_matches_electron_contract(tmp_path, monkeypatch):
    from signals.domain_pack import SignalsPack

    pack = SignalsPack(repo_root=tmp_path, state_root=tmp_path / "state")
    monkeypatch.setattr(pack, "_backtest_summary", lambda: {"total": 3, "evaluated": 2, "pending": 1})
    monkeypatch.setattr(pack, "_pending_backlog_preview", lambda _limit: [])
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
