# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class _Cursor(list):
    def sort(self, sort_spec=None, *args, **kwargs):
        rows = list(self)
        for key, direction in reversed(sort_spec or []):
            rows.sort(key=lambda item: str(item.get(key) or ""), reverse=direction < 0)
        return _Cursor(rows)

    def limit(self, n):
        return _Cursor(self[:n])


@dataclass
class _Result:
    modified_count: int = 1
    upserted_id: str | None = None


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def _match(self, doc, query):
        for key, value in (query or {}).items():
            if doc.get(key) != value:
                return False
        return True

    def find_one(self, query=None, projection=None, sort=None):
        rows = [doc for doc in self.docs if self._match(doc, query)]
        if sort:
            rows = _Cursor(rows).sort(sort)
        return dict(rows[0]) if rows else None

    def find(self, query=None, projection=None):
        return _Cursor([dict(doc) for doc in self.docs if self._match(doc, query)])

    def update_one(self, query=None, update=None, upsert=False, **kwargs):
        query = dict(query or {})
        patch = dict((update or {}).get("$set", {}))
        for doc in self.docs:
            if self._match(doc, query):
                doc.update(patch)
                return _Result(modified_count=1)
        if upsert:
            doc = {**query, **patch}
            self.docs.append(doc)
            return _Result(modified_count=0, upserted_id=str(doc.get("_id") or doc.get("factor_id") or ""))
        return _Result(modified_count=0)


class _Db(dict):
    def __missing__(self, key):
        self[key] = _Collection()
        return self[key]


def _samples() -> list[dict[str, Any]]:
    return [
        {
            "factor_id": "us_ai_hardware_to_cn_optical_cpo_memory_v1",
            "us_signal_date": "2026-04-01",
            "cn_trade_date": "2026-04-02",
            "symbol": "SZ.300308",
            "name": "中际旭创",
            "group": "CPO",
            "factor_value": 0.10,
            "return_t1": -0.01,
            "return_t5": -0.03,
            "return_t10": -0.02,
            "return_t20": 0.00,
            "mfe": 0.02,
            "mae": -0.06,
            "failure_reason": "高开低走，A股未承接。",
        },
        {
            "factor_id": "us_ai_hardware_to_cn_optical_cpo_memory_v1",
            "us_signal_date": "2026-04-02",
            "cn_trade_date": "2026-04-03",
            "symbol": "SZ.300394",
            "name": "天孚通信",
            "group": "CPO",
            "factor_value": 0.25,
            "return_t1": 0.00,
            "return_t5": -0.01,
            "return_t10": 0.00,
            "return_t20": 0.01,
            "mfe": 0.03,
            "mae": -0.04,
        },
        {
            "factor_id": "us_ai_hardware_to_cn_optical_cpo_memory_v1",
            "us_signal_date": "2026-04-03",
            "cn_trade_date": "2026-04-07",
            "symbol": "SH.688041",
            "name": "海光信息",
            "group": "算力",
            "factor_value": 0.50,
            "return_t1": 0.01,
            "return_t5": 0.02,
            "return_t10": 0.03,
            "return_t20": 0.04,
            "mfe": 0.07,
            "mae": -0.03,
        },
        {
            "factor_id": "us_ai_hardware_to_cn_optical_cpo_memory_v1",
            "us_signal_date": "2026-04-07",
            "cn_trade_date": "2026-04-08",
            "symbol": "SH.688498",
            "name": "源杰科技",
            "group": "光模块",
            "factor_value": 0.80,
            "return_t1": 0.03,
            "return_t5": 0.05,
            "return_t10": 0.07,
            "return_t20": 0.10,
            "mfe": 0.12,
            "mae": -0.02,
        },
        {
            "factor_id": "us_ai_hardware_to_cn_optical_cpo_memory_v1",
            "us_signal_date": "2026-04-08",
            "cn_trade_date": "2026-04-09",
            "symbol": "SZ.000938",
            "name": "紫光股份",
            "group": "存储/HBM",
            "factor_value": 0.92,
            "return_t1": 0.04,
            "return_t5": 0.08,
            "return_t10": 0.09,
            "return_t20": 0.12,
            "mfe": 0.15,
            "mae": -0.01,
        },
        {
            "factor_id": "us_ai_hardware_to_cn_optical_cpo_memory_v1",
            "us_signal_date": "2026-04-10",
            "cn_trade_date": "2026-04-10",
            "symbol": "SZ.999999",
            "name": "未来函数样本",
            "group": "bad",
            "factor_value": 0.99,
            "return_t5": 0.30,
        },
    ]


def test_factor_draft_writes_qlib_style_ledger_without_fake_metrics():
    from signals.strategy.ai_factor_factory import build_ai_factor_factory, create_factor_draft

    db = _Db()
    draft = create_factor_draft(
        idea="美股 AI 硬件上涨后，A股光模块/CPO 是否有次日联动",
        db=db,
    )

    assert draft["status"] == "specified"
    assert draft["metrics"] == {}
    assert draft["validation"]["verified"] is False
    assert draft["ledger"]["experiment_id"]
    assert draft["ledger"]["recorder_id"]
    assert draft["ledger"]["tags"]["qlib_recorder_style"] is True
    assert draft["portfolio_construction"]["us_trigger_basket"][0]["symbols"] == ["NVDA", "AMD"]
    assert draft["portfolio_construction"]["cn_reaction_basket"][0]["group"] == "光模块/CPO"
    assert "T+1" in draft["portfolio_construction"]["mapping_rule"]
    workflow = draft["research_workflow"]
    assert workflow["czsc_signal_event_trade"]["event"]["signals_all"] == [
        "us_ai_hardware_strength",
        "cn_opening_acceptance",
    ]
    assert workflow["vnpy_lifecycle"][1]["state"] == "inited"
    assert "paper factor account" in workflow["quantaxis_local_simulation"]["account"]

    factory = build_ai_factor_factory(db=db, include_sample=False)

    assert factory["summary"]["requires_validation"] == 1
    assert factory["summary"]["live_enabled"] == 0
    assert factory["data_lineage"]["no_auto_order"] is True


def test_dynamic_factor_draft_and_demo_validation_artifact_without_observations():
    from signals.strategy.ai_factor_factory import create_factor_draft, run_factor_validation

    db = _Db()
    draft = create_factor_draft(
        idea="低空经济政策催化后，A股 eVTOL 和无人机是否扩散",
        db=db,
    )

    assert draft["title"].startswith("低空经济")
    assert "低空经济" in draft["research"]["target_universe"]
    assert draft["portfolio_construction"]["cn_reaction_basket"][0]["group"] == "低空经济"
    assert "event:低空经济" in draft["portfolio_construction"]["us_trigger_basket"][0]["symbols"]
    assert draft["development"]["factor_definition"]["mapped_universe"][0] == "低空经济"

    result = run_factor_validation(
        factor_id=draft["factor_id"],
        db=db,
        demo_mode=True,
    )
    artifact = result["validation"]["artifact"]

    assert result["status"] == "validated"
    assert result["validation"]["mode"] == "demo"
    assert artifact["sample_count"] > 0
    assert artifact["win_rate"] > 0
    assert artifact["T+5"] > 0
    assert artifact["rank_ic"] > 0.9
    assert artifact["long_short_quantile_spread"] > 0
    assert artifact["mae"] < 0
    assert artifact["failure_samples"]
    assert artifact["rejected_future_leak"]["count"] == 1
    assert artifact["rejected_future_leak"]["samples"][0]["reason"] == "rejected_future_leak_boundary"
    assert result["paper_account"]["equity_curve"]
    assert result["paper_account"]["positions"]
    assert result["paper_account"]["exposure"]["gross"] > 0


def test_single_factor_validation_computes_alphalens_metrics_and_rejects_future_leak():
    from signals.strategy.ai_factor_factory import run_factor_validation

    db = _Db()
    result = run_factor_validation(
        factor_id="us_ai_hardware_to_cn_optical_cpo_memory_v1",
        observations=_samples(),
        db=db,
    )
    metrics = result["metrics"]

    assert result["status"] == "validated"
    assert result["validation"]["rejected_sample_count"] == 1
    assert metrics["sample_count"] == 5
    assert metrics["verified"] is True
    assert metrics["rank_ic"] > 0.9
    assert metrics["long_short_return"] > 0
    assert metrics["quantile_returns"]["q5"] > metrics["quantile_returns"]["q1"]
    assert result["failure_samples"][0]["symbol"] == "SZ.300308"
    assert result["reproducibility"]["rejected_future_leak_rows"] == 1
    assert result["paper_account"]["no_auto_order"] is True


def test_publish_gate_controls_strategy_snapshot_pollution():
    from signals.strategy.ai_factor_factory import publish_factor, run_factor_validation
    from signals.strategy.snapshot import build_strategy_snapshot

    db = _Db()
    rejected = publish_factor(
        factor_id="us_ai_hardware_to_cn_optical_cpo_memory_v1",
        db=db,
    )

    assert rejected["status"] == "rejected"

    run_factor_validation(
        factor_id="us_ai_hardware_to_cn_optical_cpo_memory_v1",
        observations=_samples(),
        db=db,
    )
    publication = publish_factor(
        factor_id="us_ai_hardware_to_cn_optical_cpo_memory_v1",
        db=db,
        live_enabled=True,
    )

    assert publication["status"] == "published"
    assert publication["approval_status"] == "approved"
    assert publication["live_enabled"] is True

    snapshot = build_strategy_snapshot(
        db=db,
        responses={
            "board": None,
            "concept": None,
            "market_pool": None,
            "quote": None,
            "signal": None,
        },
        journal_summary={"total": 0, "evaluated": 0, "pending": 0},
    )

    ai_candidates = [
        item for item in snapshot["candidates"]
        if item.get("metadata", {}).get("source") == "ai_factor_factory"
    ]
    assert ai_candidates
    assert ai_candidates[0]["metadata"]["next_action"] == "等待盘中触发，不自动下单"


def test_strategy_ai_factor_factory_api_smoke():
    from fastapi.testclient import TestClient
    from signals.web.app import create_app

    client = TestClient(create_app())

    draft = client.post(
        "/api/strategy/ai-factor-factory/draft",
        json={"persist": False, "idea": "低空经济政策催化后，A股 eVTOL 和无人机是否扩散"},
    )
    assert draft.status_code == 200
    assert draft.json()["metrics"] == {}
    assert draft.json()["portfolio_construction"]["cn_reaction_basket"][0]["group"] == "低空经济"

    validation = client.post(
        "/api/strategy/ai-factor-factory/validate",
        json={
            "persist": False,
            "idea": "低空经济政策催化后，A股 eVTOL 和无人机是否扩散",
            "demo_mode": True,
        },
    )
    assert validation.status_code == 200
    body = validation.json()
    assert body["validation"]["mode"] == "demo"
    assert body["metrics"]["sample_count"] > 0
    assert body["validation"]["artifact"]["paper_account"]["positions"]
