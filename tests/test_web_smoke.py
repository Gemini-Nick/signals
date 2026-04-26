# -*- coding: utf-8 -*-
"""
Web API 冒烟测试 — 验证所有端点可达且返回有效 JSON。

用法:
    pytest tests/test_web_smoke.py -v

注意: 需要先启动 Web 服务器 (python run.py --mode web)，
或使用 TestClient 直接测试（无需启动服务器）。
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    """创建 FastAPI TestClient（不启动真实服务器）"""
    from signals.web.app import create_app
    app = create_app()
    return TestClient(app)


class TestIndexAPI:
    """指数相关端点"""

    def test_status(self, client):
        r = client.get("/api/index/status")
        assert r.status_code == 200
        data = r.json()
        assert "ready" in data

    def test_context(self, client):
        r = client.get("/api/index/context")
        # 可能 503（引擎未就绪）或 200
        assert r.status_code in (200, 503)
        if r.status_code == 200:
            data = r.json()
            assert "overall_direction" in data

    def test_reports(self, client):
        r = client.get("/api/index/reports")
        assert r.status_code in (200, 503)
        if r.status_code == 200:
            data = r.json()
            assert isinstance(data, list)

    def test_summary(self, client):
        r = client.get("/api/index/summary")
        assert r.status_code in (200, 503)


class TestIndustryAPI:
    """行业相关端点"""

    def test_ranking(self, client):
        r = client.get("/api/industry/ranking")
        assert r.status_code in (200, 503)
        if r.status_code == 200:
            data = r.json()
            # 可能包含 loading=true 或正常数据
            assert isinstance(data, dict)


class TestScreenerAPI:
    """标的筛选端点"""

    def test_results(self, client):
        r = client.get("/api/screener/results")
        assert r.status_code in (200, 503)


class TestSocialAPI:
    """社交舆情端点（依赖外部 AKShare API，超时不算失败）"""

    @pytest.mark.timeout(15)
    def test_brief(self, client):
        try:
            r = client.get("/api/social/brief")
            assert r.status_code in (200, 503)
        except Exception:
            pytest.skip("Social API timed out (external dependency)")


class TestReviewAPI:
    """复盘端点"""

    def test_presets(self, client):
        r = client.get("/api/review/presets")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    def test_status(self, client):
        r = client.get("/api/review/status")
        assert r.status_code == 200
        data = r.json()
        assert "is_running" in data


class TestBacktestAPI:
    """回测端点"""

    def test_summary(self, client):
        r = client.get("/api/backtest/summary")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data or "error" in data

    def test_analyze_canonical_contract(self, client, monkeypatch):
        from signals.services import backtest as backtest_service

        async def fake_analyze(**kwargs):
            return {
                "symbol": "SZ.002759",
                "code": kwargs["code"],
                "freq": "日线",
                "data_source": "bars",
                "ohlcv": [],
                "signals": [],
                "sim_kpi": {},
            }

        monkeypatch.setattr(backtest_service, "backtest_analyze", fake_analyze)

        r = client.get("/api/backtest/analyze?code=002759&freq=daily")
        assert r.status_code == 200
        data = r.json()
        assert data["code"] == "002759"
        assert data["data_source"] == "bars"
        assert "ohlcv" in data
        assert "signals" in data
        assert "sim_kpi" in data


class TestStrategyAPI:
    """策略快照端点"""

    def test_snapshot(self, client, monkeypatch):
        from signals.web.api import strategy as strategy_api

        monkeypatch.setattr(strategy_api, "get_strategy_snapshot", lambda persist=False: {
            "as_of": "2026-04-24",
            "daily_brief": {"summary": "今日关注半导体", "changed_since_last": {}},
            "candidates": [{"symbol": "SZ.002759"}],
            "warnings": [],
            "decision_queue": [{"symbol": "SZ.002759", "action": "review_entry"}],
            "strategy_kpis": {"signals_total": 1},
            "source_confidence": {"overall": 0.9, "sources": []},
        })

        r = client.get("/api/strategy/snapshot")
        assert r.status_code == 200
        data = r.json()
        assert data["daily_brief"]["summary"] == "今日关注半导体"
        assert data["decision_queue"][0]["symbol"] == "SZ.002759"
        assert data["strategy_kpis"]["signals_total"] == 1


class TestPlanAPI:
    """计划端点"""

    def test_weekly(self, client):
        r = client.get("/api/plan/weekly")
        assert r.status_code == 200
        data = r.json()
        assert "week_label" in data

    def test_generate(self, client):
        r = client.post("/api/plan/generate")
        # 503 if engine not ready, 200 if ready
        assert r.status_code in (200, 503)


class TestTradeAPI:
    """交易日志端点"""

    def test_list(self, client):
        r = client.get("/api/trade/list")
        assert r.status_code == 200
        data = r.json()
        assert "trades" in data

    def test_summary(self, client):
        r = client.get("/api/trade/summary")
        assert r.status_code == 200
        data = r.json()
        assert "total_trades" in data

    def test_missed(self, client):
        r = client.get("/api/trade/missed")
        assert r.status_code == 200
        data = r.json()
        assert "missed" in data

    def test_add_and_delete(self, client):
        # Add
        r = client.post("/api/trade/add", json={
            "symbol": "SZ.000001",
            "name": "平安银行",
            "entry_date": "2026-03-10",
            "entry_price": 12.50,
        })
        assert r.status_code == 200
        trade_id = r.json()["id"]
        # Delete
        r = client.delete(f"/api/trade/{trade_id}")
        assert r.status_code == 200


class TestStaticFiles:
    """静态资源"""

    def test_index_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "隆小侠" in r.text or "LONG CLAW" in r.text

    def test_app_js(self, client):
        r = client.get("/static/js/app.js")
        assert r.status_code == 200

    def test_app_css(self, client):
        r = client.get("/static/css/app.css")
        assert r.status_code == 200
