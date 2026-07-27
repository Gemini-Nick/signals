# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class _Cursor(list):
    def sort(self, sort_spec=None, *args, **kwargs):
        rows = list(self)
        if isinstance(sort_spec, str):
            sort_spec = [(sort_spec, args[0] if args else 1)]
        for key, direction in reversed(sort_spec or []):
            rows.sort(key=lambda item: str(_doc_get(item, key) or ""), reverse=direction < 0)
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
            actual = _doc_get(doc, key)
            if isinstance(value, dict):
                if "$exists" in value and bool(actual is not None) is not bool(value["$exists"]):
                    return False
                if "$in" in value and actual not in set(value["$in"]):
                    return False
                continue
            if actual != value:
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


def _doc_get(doc, key):
    current = doc
    for part in str(key).split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


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
    assert draft["research_mode"] == "research_first"
    assert draft["factor_origin"] == "industry_research"
    assert draft["factor_family"]["family_id"] == "industry_factor.ai_hardware"
    assert draft["industry_beta"]["name"] == "ai_hardware_industry_chain"
    assert draft["expectation_alpha"]["name"] == "ai_expectation_revision"
    assert draft["strategy_integration"]["outputs"] == [
        "factor_exposure",
        "factor_origin",
        "validation_status",
        "industry_beta_score",
        "expectation_alpha_score",
        "technical_confirmation_score",
        "risk_overlay_flags",
    ]
    assert draft["risk_overlay_flags"]
    assert draft["metrics"] == {}
    assert draft["validation"]["verified"] is False
    assert draft["ledger"]["experiment_id"]
    assert draft["ledger"]["recorder_id"]
    assert draft["ledger"]["tags"]["qlib_recorder_style"] is True
    assert draft["portfolio_construction"]["us_trigger_basket"][0]["node_id"] == "optical_interconnect"
    assert {"COHR", "LITE", "FN"}.issubset(
        set(draft["portfolio_construction"]["us_trigger_basket"][0]["symbols"])
    )
    assert draft["portfolio_construction"]["cn_reaction_basket"][0]["group"] == "光模块/CPO"
    assert "SZ.300308" in draft["portfolio_construction"]["cn_reaction_basket"][0]["symbols"]
    assert draft["portfolio_construction"]["cn_mapping_nodes"][0]["top_candidates"][0]["symbol"] == "SZ.300308"
    assert draft["portfolio_construction"]["us_driver_nodes"][0]["role"] == "primary_driver"
    assert draft["rhythm"]["status"] == "pending_kline_fusion"
    assert "T+1" in draft["portfolio_construction"]["mapping_rule"]
    assert draft["development"]["factor_definition"]["components"] == [
        "industry_beta",
        "expectation_alpha",
        "cross_market_lead_lag",
        "a_share_acceptance_confirmation",
    ]
    assert "macd" in draft["development"]["factor_definition"]["a_share_acceptance_confirmation"]
    workflow = draft["research_workflow"]
    assert workflow["czsc_signal_event_trade"]["event"]["signals_all"] == [
        "us_ai_hardware_strength",
        "cn_opening_acceptance",
    ]
    assert workflow["vnpy_lifecycle"][1]["state"] == "inited"
    assert "paper factor account" in workflow["quantaxis_local_simulation"]["account"]

    factory = build_ai_factor_factory(db=db, include_sample=False)

    assert factory["summary"]["requires_validation"] == 1
    assert factory["summary"]["research_modes"]["research_first"] == 1
    assert factory["summary"]["live_enabled"] == 0
    assert factory["factor_registry"]["industry_factor.ai_hardware"]["alpha"] == "ai_expectation_revision"
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


def test_cross_market_optical_liquid_title_uses_chain_specific_language():
    from signals.strategy.ai_factor_factory import create_factor_draft

    draft = create_factor_draft(
        idea="Lumentum/Coherent/Fabrinet 光器件链走强，同时 Vertiv/Eaton/nVent 数据中心液冷订单上修后，A股光模块与液冷是否存在 T+1 分化联动？",
        persist=False,
    )

    assert draft["title"] == "美股光器件/液冷链 -> A股光模块/液冷联动因子"


def test_rhythm_demo_keeps_metrics_empty_and_marks_demo_path():
    from signals.strategy.ai_factor_factory import create_factor_draft, run_factor_rhythm_demo

    db = _Db()
    draft = create_factor_draft(
        idea="Lumentum/Coherent/Fabrinet 光器件链走强，同时 Vertiv/Eaton/nVent 数据中心液冷订单上修后，A股光模块与液冷是否存在 T+1 分化联动？",
        db=db,
    )
    result = run_factor_rhythm_demo(factor_id=draft["factor_id"], db=db)

    assert result["metrics"] == {}
    assert result["validation"]["verified"] is False
    assert result["rhythm"]["mode"] == "demo"
    assert result["rhythm"]["demo"] is True
    assert result["rhythm"]["windows"][0]["kline_marker"]["label"] == "美股尾盘加速"
    assert result["rhythm"]["path_samples"][0]["demo"] is True
    assert result["rhythm"]["selected_cn_mapping"]["symbol"].startswith(("SZ.", "SH."))


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


def test_signal_first_technical_scan_builds_candidate_factor_ideas_without_live_pollution():
    from signals.strategy.ai_factor_factory import (
        build_ai_factor_factory,
        build_ai_factor_strategy_candidates,
    )

    db = _Db()
    db["terminal_technical_signals"] = _Collection(docs=[
        {
            "market": "A",
            "as_of": "2026-05-08",
            "symbol": "SZ.300024",
            "name": "机器人A",
            "concept": "机器人执行器",
            "signal_side": "buy",
            "signal_type": "三买",
            "freq": "日线",
            "total_score": 82,
            "confidence": 0.84,
        },
        {
            "market": "A",
            "as_of": "2026-05-08",
            "symbol": "SH.688017",
            "name": "机器人B",
            "concept": "机器人执行器",
            "signal_side": "buy",
            "signal_type": "中枢突破",
            "freq": "30分钟",
            "total_score": 76,
            "confidence": 0.78,
        },
        {
            "market": "A",
            "as_of": "2026-05-08",
            "symbol": "SZ.002472",
            "name": "机器人C",
            "concept": "机器人执行器",
            "signal_side": "buy",
            "signal_type": "MACD面积收缩",
            "freq": "周线",
            "total_score": 71,
            "confidence": 0.72,
        },
        {
            "market": "A",
            "as_of": "2026-05-08",
            "symbol": "SZ.000001",
            "concept": "银行",
            "signal_side": "sell",
            "signal_type": "顶背驰卖点",
            "freq": "日线",
            "total_score": 90,
            "confidence": 0.9,
        },
    ])

    factory = build_ai_factor_factory(db=db, include_sample=False)
    ideas = factory["candidate_factor_ideas"]

    assert ideas
    idea = ideas[0]
    assert idea["research_mode"] == "signal_first"
    assert idea["factor_origin"] == "technical_discovery"
    assert idea["status"] == "idea"
    assert idea["live_enabled"] is False
    assert idea["validation"]["verified"] is False
    assert idea["beta_alpha_assessment"]["classification"] == "industry_beta"
    assert idea["industry_beta"]["symbol_count"] == 3
    assert idea["technical_confirmation"]["unique_symbol_count"] == 3
    assert "unvalidated_factor_idea" in idea["risk_overlay_flags"]
    assert "机器人执行器" in idea["title"]
    assert factory["summary"]["candidate_factor_ideas"] == 1
    assert factory["summary"]["research_modes"]["signal_first"] == 1
    assert build_ai_factor_strategy_candidates(db=db) == []


def test_signal_first_rl_environments_split_fallback_technical_bucket():
    from signals.strategy.ai_factor_factory import build_ai_factor_factory

    db = _Db()
    db["terminal_technical_signals"] = _Collection(docs=[
        {
            "market": "A",
            "as_of": "2026-05-08",
            "symbol": "SZ.300001",
            "signal_side": "buy",
            "signal_type": "MACD绿柱缩小_零下",
            "signal_family": "hard_technical",
            "freq": "周线",
            "scan_scope": "postmarket",
            "total_score": 80,
            "confidence": 0.8,
            "resonance_context": {"grade": "strong_resonance"},
        },
        {
            "market": "A",
            "as_of": "2026-05-08",
            "symbol": "SZ.300002",
            "signal_side": "buy",
            "signal_type": "三买",
            "signal_family": "hard_technical",
            "freq": "日线",
            "scan_scope": "postmarket",
            "total_score": 78,
            "confidence": 0.76,
            "resonance_context": {"grade": "single_period"},
        },
        {
            "market": "A",
            "as_of": "2026-05-08",
            "symbol": "SZ.300003",
            "signal_side": "buy",
            "signal_type": "三买",
            "signal_family": "hard_technical",
            "freq": "30分钟",
            "scan_scope": "postmarket",
            "total_score": 74,
            "confidence": 0.73,
            "resonance_context": {"grade": "multi_period"},
        },
    ])

    factory = build_ai_factor_factory(db=db, include_sample=False)
    environments = factory["rl_environments"]

    assert len(environments) == 3
    assert {item["split_keys"]["signal_type_family"] for item in environments} == {"macd", "chan_buy"}
    assert {item["split_keys"]["freq_bucket"] for item in environments} == {"weekly", "daily", "intraday_30m"}
    assert factory["reward_spec"]["mode"] == "layered_gate"
    assert "evaluation_summary" in factory


def test_signal_first_auto_attributes_technical_cluster_from_concept_constituents():
    from signals.strategy.ai_factor_factory import build_ai_factor_factory

    db = _Db()
    db["terminal_technical_signals"] = _Collection(docs=[
        {
            "market": "A",
            "as_of": "2026-05-08",
            "symbol": "SZ.300001",
            "signal_side": "buy",
            "signal_type": "趋势突破",
            "signal_family": "hard_technical",
            "freq": "日线",
            "scan_scope": "postmarket",
            "total_score": 90,
            "confidence": 0.82,
            "resonance_context": {"grade": "strong_resonance"},
        },
        {
            "market": "A",
            "as_of": "2026-05-08",
            "symbol": "SH.600002",
            "signal_side": "buy",
            "signal_type": "趋势突破",
            "signal_family": "hard_technical",
            "freq": "日线",
            "scan_scope": "postmarket",
            "total_score": 86,
            "confidence": 0.8,
            "resonance_context": {"grade": "strong_resonance"},
        },
    ])
    db["concept_constituents"] = _Collection(docs=[
        {
            "concept_name": "机器人",
            "symbols": ["300001", "600002", "300003"],
            "stock_count": 3,
        },
        {
            "concept_name": "宽基样本",
            "symbols": ["300001"],
            "stock_count": 100,
        },
    ])

    env = build_ai_factor_factory(db=db, include_sample=False)["rl_environments"][0]

    assert env["attribution"]["status"] == "auto_attributed"
    assert env["attribution"]["primary_theme"] == "机器人"
    assert env["attribution"]["support_count"] == 2
    assert env["split_keys"]["theme"] == "机器人"
    assert env["split_keys"]["raw_theme"] == "技术结构共振"
    assert env["factor_exposures"]["groups"] == ["机器人"]


def test_signal_first_overbroad_fallback_cluster_is_not_evaluable():
    from signals.strategy.ai_factor_factory import build_ai_factor_factory

    db = _Db()
    db["terminal_technical_signals"] = _Collection(docs=[
        {
            "market": "A",
            "as_of": "2026-05-08",
            "symbol": f"SZ.{idx:06d}",
            "signal_side": "buy",
            "signal_type": "MACD绿柱扩大_零上",
            "signal_family": "hard_technical",
            "freq": "周线",
            "scan_scope": "postmarket",
            "total_score": 70 + idx % 20,
            "confidence": 0.75,
            "resonance_context": {"grade": "strong_resonance"},
        }
        for idx in range(1, 62)
    ])

    env = build_ai_factor_factory(db=db, include_sample=False)["rl_environments"][0]

    assert env["status"] == "not_evaluable"
    assert "overbroad_cluster" in env["blocking_gates"]
    assert env["environment_metrics"]["unique_symbol_count"] > 50


def test_signal_first_environment_validation_auto_generates_clean_observations():
    from signals.strategy.ai_factor_factory import (
        build_ai_factor_factory,
        run_signal_first_environment_validation,
    )

    db = _Db()
    signal_docs = []
    bar_docs = []
    for idx in range(32):
        code = f"SZ.{300000 + idx:06d}"
        score = 50 + idx * 5
        signal_docs.append({
            "market": "A",
            "as_of": "2026-05-01",
            "symbol": code,
            "signal_side": "buy",
            "signal_type": "三买",
            "signal_family": "hard_technical",
            "freq": "日线",
            "scan_scope": "postmarket",
            "total_score": score,
            "confidence": 0.7 + idx / 200,
            "resonance_context": {"grade": "multi_period"},
        })
        daily_step = 0.004 + idx * 0.0005
        for day in range(24):
            close = 10 * (1 + daily_step * day)
            bar_docs.append({
                "dt": f"2026-05-{day + 1:02d}",
                "meta": {"symbol": code, "freq": "日线"},
                "open": close * 0.99,
                "high": close * 1.01,
                "low": close * 0.98,
                "close": close,
                "vol": 1000,
            })
    db["terminal_technical_signals"] = _Collection(docs=signal_docs)
    db["bars"] = _Collection(docs=bar_docs)

    environment_id = build_ai_factor_factory(db=db, include_sample=False)["rl_environments"][0]["environment_id"]
    result = run_signal_first_environment_validation(
        environment_id=environment_id,
        db=db,
        persist=False,
    )

    assert result["validation"]["sample_count"] == 32
    assert result["validation"]["rejected_sample_count"] == 0
    assert result["evaluation"]["factor_evaluation"]["rank_ic"] > 0
    assert result["evaluation"]["reward"]["status"] == "validated"
    assert result["evaluation"]["reward"]["final_reward"] > 0


def test_signal_first_high_ic_can_still_fail_portfolio_gate_and_publish():
    from signals.strategy.ai_factor_factory import (
        build_ai_factor_factory,
        publish_factor,
        run_signal_first_environment_validation,
    )

    db = _Db()
    db["terminal_technical_signals"] = _Collection(docs=[{
        "market": "A",
        "as_of": "2026-05-01",
        "symbol": "SZ.300001",
        "signal_side": "buy",
        "signal_type": "三买",
        "signal_family": "hard_technical",
        "freq": "日线",
        "scan_scope": "postmarket",
        "total_score": 80,
        "confidence": 0.8,
        "resonance_context": {"grade": "multi_period"},
    }])
    environment_id = build_ai_factor_factory(db=db, include_sample=False)["rl_environments"][0]["environment_id"]
    observations = []
    for idx in range(30):
        factor_value = 0.1 + idx * 0.03
        return_t5 = -1.0 if idx < 5 else 0.25
        observations.append({
            "factor_id": environment_id,
            "source_date": f"2026-04-{idx + 1:02d}",
            "cn_trade_date": f"2026-05-{idx + 1:02d}",
            "symbol": f"SZ.{300000 + idx:06d}",
            "factor_value": factor_value,
            "return_t1": return_t5 / 5,
            "return_t5": return_t5,
            "return_t10": return_t5,
            "return_t20": return_t5,
            "mfe": 0.3,
            "mae": -1.0 if idx < 5 else -0.02,
        })

    result = run_signal_first_environment_validation(
        environment_id=environment_id,
        observations=observations,
        db=db,
    )
    rejected = publish_factor(factor_id=environment_id, db=db)

    assert result["evaluation"]["factor_evaluation"]["rank_ic"] > 0
    assert result["evaluation"]["reward"]["status"] == "observation_only"
    assert "max_drawdown_gate_failed" in result["evaluation"]["reward"]["blocking_gates"]
    assert rejected["status"] == "rejected"
    assert rejected["error"] == "factor_reward_gate_not_validated"


def test_publish_gate_controls_strategy_snapshot_pollution():
    from signals.strategy.ai_factor_factory import publish_factor, run_factor_validation
    from signals.strategy.snapshot import _merge_ai_factor_candidates, build_strategy_snapshot

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
    assert ai_candidates[0]["factor_origin"] == "industry_research"
    assert ai_candidates[0]["factor_research_mode"] == "research_first"
    assert ai_candidates[0]["factor_exposures"]["primary"] == "industry_factor.ai_hardware"
    assert ai_candidates[0]["validation_status"] == "validated"
    assert ai_candidates[0]["industry_beta_score"] >= 0
    assert ai_candidates[0]["expectation_alpha_score"] >= 0
    assert ai_candidates[0]["technical_confirmation_score"] >= 0
    assert "future_leak_guard" in ai_candidates[0]["risk_overlay_flags"]

    crowded_candidates = [
        {
            "symbol": f"SZ.00{i:04d}",
            "name": f"高分候选{i}",
            "score": 300 - i,
            "metadata": {"source": "terminal_stock_pool.focus_stocks"},
        }
        for i in range(12)
    ]
    crowded_candidates[0]["symbol"] = "SZ.300394"
    crowded_candidates[0]["name"] = "天孚通信"

    merged = _merge_ai_factor_candidates(crowded_candidates, db=db)

    assert len(merged) == 12
    assert any(
        item.get("metadata", {}).get("source") == "ai_factor_factory"
        for item in merged
    )
    overlay = next(item for item in merged if item["symbol"] == "SZ.300394")
    assert overlay["metadata"]["source"] == "terminal_stock_pool.focus_stocks"
    assert overlay["metadata"]["ai_factor_factory"]["source"] == "ai_factor_factory"
    assert overlay["ai_factor_score"] > 0
    assert overlay["factor_exposures"]["primary"] == "industry_factor.ai_hardware"
    assert overlay["metadata"]["ai_factor_factory"]["validation_status"] == "validated"
    assert overlay["metadata"]["ai_factor_factory"]["factor_score_breakdown"]["industry_beta_score"] >= 0


def test_demo_validated_factor_does_not_enter_live_strategy_candidates():
    from signals.strategy.ai_factor_factory import (
        build_ai_factor_strategy_candidates,
        publish_factor,
        run_factor_validation,
    )

    db = _Db()
    factor_id = "us_ai_hardware_to_cn_optical_cpo_memory_v1"
    validation = run_factor_validation(
        factor_id=factor_id,
        db=db,
        demo_mode=True,
    )
    publication = publish_factor(
        factor_id=factor_id,
        db=db,
        live_enabled=True,
    )

    assert validation["validation"]["mode"] == "demo"
    assert publication["status"] == "published"
    assert build_ai_factor_strategy_candidates(db=db) == []


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

    rhythm = client.post(
        "/api/strategy/ai-factor-factory/rhythm-demo",
        json={
            "persist": False,
            "idea": "Lumentum/Coherent/Fabrinet 光器件链走强后，A股光模块/CPO 是否 T+1 承接",
        },
    )
    assert rhythm.status_code == 200
    assert rhythm.json()["rhythm"]["mode"] == "demo"
    assert rhythm.json()["metrics"] == {}

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
