# -*- coding: utf-8 -*-
"""AI factor research kernel and read model.

This module keeps AI factor research separate from ``autoresearch``.  AI may
turn a trader's idea into a structured factor and feedback loop, but every
number shown to Agent OS must come from a sample replay artifact.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from signals.core.cross_market_chains import build_ai_hardware_portfolio
from signals.core.market_time import naive_market_now
from signals.core.trading_dates import trading_day_key

DEFAULT_FACTOR_ID = "us_ai_hardware_to_cn_optical_cpo_memory_v1"
DEFAULT_FACTOR_TITLE = "美股 AI 硬件 -> A股光模块/CPO/液冷/存储联动因子"
REPRO_BOUNDARY = "US T close is allowed to affect A-share T+1 and later only."
LIFECYCLE_STATES = ["idea", "draft", "specified", "validated", "observable", "published", "disabled"]
FACTOR_FACTORY_PHASES = [
    {
        "phase": "phase_1",
        "mode": "research_first",
        "title": "投研因子 -> 技术验证",
        "goal": "以 AI 硬件行业因子跑通假设、定义、样本复盘、观察账户和策略赋能。",
        "strategy_gate": "样本复盘、人工确认、观察启用后才进入 strategy_snapshot。",
    },
    {
        "phase": "phase_2",
        "mode": "signal_first",
        "title": "技术因子 -> 行业归因",
        "goal": "从全市场技术信号聚类发现候选行业 beta 或预期 alpha，再回到研究验证链路。",
        "strategy_gate": "只进入 factor_idea_queue，不直接进入 live 候选池。",
    },
]
RESEARCH_MODES = {
    "research_first": {
        "label": "投研发现因子",
        "definition": "先有行业/产业链假设，再用技术结构和单因子验证确认市场是否承接。",
    },
    "signal_first": {
        "label": "技术发现因子",
        "definition": "先有全市场技术异动，再按行业、概念、产业链聚类归因。",
    },
}
AI_HARDWARE_FACTOR_FAMILY = {
    "family_id": "industry_factor.ai_hardware",
    "label": "AI硬件行业因子",
    "mode": "research_first",
    "primary_style": "industry_beta_with_expectation_alpha",
    "beta": "ai_hardware_industry_chain",
    "alpha": "ai_expectation_revision",
}
VALIDATION_METRICS_PROFILE = [
    "forward_return",
    "rank_ic",
    "quantile_return",
    "mfe_mae",
    "failure_samples",
    "industry_diffusion_rate",
    "technical_confirmation_rate",
]
SIGNAL_FIRST_COLLECTION = "terminal_technical_signals"
SIGNAL_FIRST_BULLISH_TOKENS = (
    "一买", "二买", "三买", "中枢突破", "突破", "背驰", "macd", "面积收缩",
    "均线多头", "放量", "缩量回踩", "趋势", "买点",
)
SIGNAL_FIRST_FALLBACK_THEME = "技术结构共振"
SIGNAL_FIRST_MIN_EVALUABLE_SAMPLES = 10
SIGNAL_FIRST_MIN_VALIDATED_SAMPLES = 30
SIGNAL_FIRST_MAX_CLEAN_SYMBOLS = 50
SIGNAL_FIRST_MAX_CLEAN_SIGNALS = 200
SIGNAL_FIRST_REWARD_SPEC = {
    "mode": "layered_gate",
    "factor_score_weights": {
        "rank_ic_score": 0.30,
        "quantile_spread_score": 0.25,
        "forward_return_score": 0.20,
        "mfe_mae_score": 0.10,
        "cluster_cleanliness_score": 0.10,
        "sample_robustness_score": 0.05,
    },
    "portfolio_score_weights": {
        "cost_adjusted_return_score": 0.30,
        "sharpe_or_ir_score": 0.25,
        "max_drawdown_score": 0.25,
        "turnover_score": 0.10,
        "hit_rate_stability_score": 0.10,
    },
    "gates": {
        "min_evaluable_samples": SIGNAL_FIRST_MIN_EVALUABLE_SAMPLES,
        "min_validated_samples": SIGNAL_FIRST_MIN_VALIDATED_SAMPLES,
        "rank_ic_must_be_positive": True,
        "quantile_spread_must_be_positive": True,
        "cost_adjusted_return_must_be_positive": True,
        "max_drawdown_floor": -0.15,
        "turnover_ceiling": 1.5,
        "intraday_status": "observation_only",
    },
}


def build_ai_factor_factory(*, db: Any = None, include_sample: bool = True) -> dict[str, Any]:
    """Return the AI factor factory read model consumed by Agent OS."""
    now = naive_market_now("A")
    trade_date = trading_day_key("A", now=now)
    db = db if db is not None else _get_db_or_none()
    factors = _load_factor_docs(db)
    if not factors and include_sample:
        factors = [_sample_factor(now)]
    rl_environments = build_signal_first_rl_environments(db=db)
    candidate_factor_ideas = build_signal_first_candidate_factor_ideas(db=db, environments=rl_environments)

    active_factor_id = str(factors[0].get("factor_id") or "") if factors else ""
    return _json_safe({
        "factory_id": f"ai-factor-factory:{trade_date}",
        "as_of": trade_date,
        "generated_at": now.isoformat(timespec="seconds"),
        "title": "AI因子工厂",
        "summary": _summary(factors, candidate_factor_ideas),
        "active_factor_id": active_factor_id,
        "phases": FACTOR_FACTORY_PHASES,
        "research_modes": RESEARCH_MODES,
        "factor_registry": {
            "industry_factor.ai_hardware": AI_HARDWARE_FACTOR_FAMILY,
        },
        "rl_environments": rl_environments,
        "reward_spec": SIGNAL_FIRST_REWARD_SPEC,
        "evaluation_summary": _signal_first_evaluation_summary(rl_environments),
        "candidate_factor_ideas": candidate_factor_ideas,
        "factor_idea_queue": candidate_factor_ideas,
        "ideas": factors,
        "factors": factors,
        "experiments": _load_experiment_ledger(db),
        "actions": _factory_actions(active_factor_id),
        "data_lineage": {
            "read_model": "signals.strategy.ai_factor_factory",
            "raw_sources": [
                "ai_factor_experiments",
                "ai_factor_publications",
                "ai_factor_validation_samples",
                SIGNAL_FIRST_COLLECTION,
            ],
            "research_loop": "RD-Agent style Research -> Development -> Feedback",
            "phase_1_loop": "research_first idea -> factor spec -> technical confirmation -> sample replay -> paper account -> observation",
            "phase_2_loop": "signal_first atomic signals -> industry/concept clustering -> candidate_factor_ideas -> manual research review",
            "experiment_ledger": "Qlib-style experiment/recorder run ledger",
            "single_factor_validation": "Alphalens-style IC, quantiles, group returns, turnover",
            "validation_profile": VALIDATION_METRICS_PROFILE,
            "repro_boundary": REPRO_BOUNDARY,
            "live_gate": "Only replayed + approved + live_enabled factors can enter strategy_snapshot.",
            "signal_first_gate": "Candidate factor ideas stay in review until research evidence and sample replay are complete.",
            "no_auto_order": True,
        },
    })


def create_factor_draft(
    *,
    idea: str = "",
    factor_id: str = DEFAULT_FACTOR_ID,
    db: Any = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Create a deterministic trader-readable factor definition from an idea."""
    now = naive_market_now("A")
    db = db if db is not None else _get_db_or_none()
    default_idea = "美股 AI 硬件上涨后，A股光模块/CPO/液冷/存储是否有次日联动"
    idea_text = idea.strip() or default_idea
    explicit_factor_id = str(factor_id or "").strip()
    if explicit_factor_id == DEFAULT_FACTOR_ID and idea.strip() and idea_text != default_idea:
        explicit_factor_id = ""
    factor_id = _safe_factor_id(explicit_factor_id or idea_text)
    version = "v1"
    experiment_id = _experiment_id(factor_id, "research", idea_text)
    recorder_id = _recorder_id(experiment_id, "draft")
    research = _draft_research_from_idea(idea_text)
    portfolio_construction = _portfolio_construction_for_idea(idea_text)
    research_workflow = _research_workflow_for_idea(idea_text)
    identity = _factor_identity_for_idea(idea_text)
    technical_confirmation = _technical_confirmation_profile()
    strategy_integration = _strategy_integration_profile()
    risk_overlay_flags = _risk_overlay_flags_for_factor(identity)
    doc = {
        "_id": factor_id,
        "factor_id": factor_id,
        "version": version,
        "title": _factor_title_from_idea(idea_text, factor_id),
        "hypothesis": idea_text,
        "research_mode": identity["research_mode"],
        "factor_origin": identity["factor_origin"],
        "factor_family": identity["factor_family"],
        "industry_beta": identity["industry_beta"],
        "expectation_alpha": identity["expectation_alpha"],
        "technical_confirmation": technical_confirmation,
        "strategy_integration": strategy_integration,
        "factor_exposures": _factor_exposures_for_factor({
            **identity,
            "portfolio_construction": portfolio_construction,
        }),
        "validation_profile": _validation_profile(),
        "risk_overlay_flags": risk_overlay_flags,
        "status": "specified",
        "approval_status": "not_requested",
        "live_enabled": False,
        "updated_at": now,
        "last_verified_at": "",
        "draft": _normalize_draft(research),
        "research": research,
        "development": {
            "factor_definition": {
                "mode": identity["research_mode"],
                "origin": identity["factor_origin"],
                "components": [
                    "industry_beta",
                    "expectation_alpha",
                    "cross_market_lead_lag",
                    "a_share_acceptance_confirmation",
                ],
                "industry_beta": identity["industry_beta"],
                "expectation_alpha": identity["expectation_alpha"],
                "cross_market_lead_lag": {
                    "rule": REPRO_BOUNDARY,
                    "source": "US trigger basket",
                    "target": "A-share reaction basket",
                },
                "a_share_acceptance_confirmation": technical_confirmation,
                "inputs": _basket_symbols(portfolio_construction.get("us_trigger_basket")),
                "mapped_universe": _basket_groups(portfolio_construction.get("cn_reaction_basket")),
                "signal_layer": "US AI hardware overnight strength",
                "factor_layer": "A-share mapped exposure plus opening acceptance",
                "event_layer": "idea_mapping_confirmed",
                "trade_observation_layer": "pre-market pool / intraday alert only",
                "idea_terms": _idea_terms(idea_text),
            },
            "czsc_layering": {
                "signal": "atomic cross-market and intraday acceptance signals",
                "factor": "linear combination of signals with explicit weights",
                "event": "same-class factor merge into observable event",
                "trade": "manual review or paper observation; never automatic order",
            },
        },
        "research_workflow": research_workflow,
        "portfolio_construction": portfolio_construction,
        "rhythm": {
            "mode": "not_run",
            "status": "pending_kline_fusion",
            "demo": False,
            "windows": portfolio_construction.get("rhythm_windows") or [],
            "multi_timeframe_map": portfolio_construction.get("multi_timeframe_map") or {},
            "no_auto_order": True,
        },
        "reproducibility": _default_reproducibility(now),
        "lifecycle": {
            "state": "specified",
            "states": LIFECYCLE_STATES,
            "next_allowed": ["validated", "disabled"],
            "live_gate": "sample_replayed + approved + live_enabled",
        },
        "paper_account": {
            "enabled": False,
            "mode": "observe_only",
            "no_auto_order": True,
            "tracks": ["position_exposure", "paper_pnl", "drawdown", "hit_rate"],
        },
        "metrics": {},
        "validation": {"status": "not_run", "verified": False},
        "ai_explanation": [
            _ai_factor_positioning_explanation(identity),
            "AI 只负责把想法拆成可复现定义、验证边界和反馈建议。",
            "该因子未验证、未批准前不会进入策略页盘前池。",
        ],
        "failure_samples": [],
        "watchlist_symbols": [],
        "risk_tags": ["高开追价", "跨市场映射失效", "板块分化", "未来函数"],
        "experiment_id": experiment_id,
        "recorder_id": recorder_id,
    }
    ledger = _ledger_doc(doc, stage="research_development", status="FINISHED", metrics={}, artifacts={
        "factor_definition": doc["development"]["factor_definition"],
        "research_workflow": doc["research_workflow"],
        "portfolio_construction": doc["portfolio_construction"],
        "reproducibility": doc["reproducibility"],
    })
    if persist and db is not None:
        _upsert(db, "ai_factor_experiments", {"factor_id": factor_id}, doc)
        _upsert(db, "ai_factor_experiment_ledger", {"recorder_id": recorder_id}, ledger)
    return _json_safe({**doc, "ledger": ledger})


def run_factor_validation(
    *,
    factor_id: str = DEFAULT_FACTOR_ID,
    idea: str = "",
    observations: Sequence[Mapping[str, Any]] | None = None,
    db: Any = None,
    persist: bool = True,
    demo_mode: bool = True,
) -> dict[str, Any]:
    """Run single-factor validation from structured observations."""
    now = naive_market_now("A")
    db = db if db is not None else _get_db_or_none()
    factor_id = _safe_factor_id(factor_id or idea or DEFAULT_FACTOR_ID)
    idea_text = idea.strip()
    existing_draft = _latest_factor_doc(db, factor_id)
    if idea_text:
        draft = create_factor_draft(
            idea=idea_text,
            factor_id=factor_id,
            db=db,
            persist=persist,
        )
        if existing_draft:
            draft = {
                **_as_dict(existing_draft),
                **_as_dict(draft),
                "approval_status": existing_draft.get("approval_status") or draft.get("approval_status"),
                "live_enabled": bool(existing_draft.get("live_enabled")),
            }
    else:
        draft = existing_draft or create_factor_draft(
            factor_id=factor_id,
            db=db,
            persist=persist,
        )
    sample_source = "observations" if observations is not None else "ai_factor_validation_samples"
    rows = list(observations) if observations is not None else _load_validation_samples(db, factor_id)
    if not rows and demo_mode:
        rows = _demo_validation_samples(
            factor_id,
            {**_as_dict(draft), "hypothesis": idea_text or draft.get("hypothesis") or draft.get("title")},
        )
        sample_source = "demo"
    valid_rows, rejected_rows = _enforce_repro_boundary(rows)
    metrics = _compute_single_factor_metrics(valid_rows)
    validation_artifact = _validation_artifact(metrics, valid_rows, rejected_rows)
    evaluation = _layered_reward_evaluation(metrics, valid_rows, rejected_rows)
    validation_status = "validated" if metrics.get("verified") else "not_enough_samples"
    experiment_id = _experiment_id(factor_id, "validation", str(len(rows)))
    recorder_id = _recorder_id(experiment_id, "alphalens")
    report = {
        "_id": factor_id,
        **_as_dict(draft),
        "factor_id": factor_id,
        "version": str(draft.get("version") or "v1"),
        "status": validation_status,
        "approval_status": str(draft.get("approval_status") or "not_requested"),
        "live_enabled": bool(draft.get("live_enabled")),
        "updated_at": now,
        "last_verified_at": now.isoformat(timespec="seconds") if metrics.get("verified") else "",
        "metrics": metrics,
        "validation": {
            "status": validation_status,
            "verified": bool(metrics.get("verified")),
            "mode": "demo" if sample_source == "demo" else "observed",
            "sample_source": sample_source,
            "sample_count": int(metrics.get("sample_count") or 0),
            "rejected_sample_count": len(rejected_rows),
            "rejected_reason": "future_leak_boundary" if rejected_rows else "",
            "rejected_future_leak": validation_artifact["rejected_future_leak"],
            "artifact": validation_artifact,
            "engine_refs": [
                "signals.core.backtest.SignalJournal",
                "signals.core.backtest.ForwardEvaluator",
                "signals.core.backtest.BacktestReport",
            ],
        },
        "evaluation": evaluation,
        "failure_samples": validation_artifact["failure_samples"],
        "watchlist_symbols": _watchlist_from_rows(valid_rows),
        "feedback": _feedback(metrics, len(rejected_rows)),
        "reproducibility": {
            **_as_dict(draft.get("reproducibility")),
            "data_snapshot": _data_snapshot(valid_rows, now),
            "rejected_future_leak_rows": len(rejected_rows),
        },
        "paper_account": {
            **_as_dict(draft.get("paper_account")),
            **validation_artifact["paper_account"],
            "enabled": bool(metrics.get("verified")),
            "mode": "paper_observation",
            "no_auto_order": True,
            "estimated_exposure": _round(metrics.get("avg_abs_factor_exposure"), 4),
        },
        "experiment_id": experiment_id,
        "recorder_id": recorder_id,
    }
    ledger = _ledger_doc(report, stage="feedback_validation", status="FINISHED", metrics=metrics, artifacts={
        "validation_report": {
            "metrics": metrics,
            "artifact": validation_artifact,
            "failure_samples": report["failure_samples"],
            "feedback": report["feedback"],
        },
        "repro_boundary": REPRO_BOUNDARY,
    })
    if persist and db is not None:
        _upsert(db, "ai_factor_experiments", {"factor_id": factor_id}, report)
        _upsert(db, "ai_factor_experiment_ledger", {"recorder_id": recorder_id}, ledger)
    return _json_safe({**report, "ledger": ledger})


def run_signal_first_environment_validation(
    *,
    environment_id: str,
    observations: Sequence[Mapping[str, Any]] | None = None,
    db: Any = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Validate a signal-first RL environment from historical signal rows."""
    now = naive_market_now("A")
    db = db if db is not None else _get_db_or_none()
    environments = build_signal_first_rl_environments(db=db)
    environment = next(
        (item for item in environments if str(item.get("environment_id") or "") == str(environment_id or "")),
        None,
    )
    if environment is None:
        environment = _empty_signal_first_environment(environment_id)

    rows = list(observations) if observations is not None else _signal_first_observations_for_environment(
        db,
        environment,
    )
    valid_rows, rejected_rows = _enforce_repro_boundary(rows)
    metrics = _compute_single_factor_metrics(valid_rows)
    validation_artifact = _validation_artifact(metrics, valid_rows, rejected_rows)
    evaluation = _layered_reward_evaluation(metrics, valid_rows, rejected_rows, environment=environment)
    reward = _as_dict(evaluation.get("reward"))
    status = str(reward.get("status") or "not_evaluable")
    factor_id = _safe_factor_id(str(environment.get("factor_id") or environment.get("environment_id") or environment_id or ""))
    experiment_id = _experiment_id(factor_id, "signal_first_validation", str(len(rows)))
    recorder_id = _recorder_id(experiment_id, "signal_first")
    report = {
        "_id": factor_id,
        "factor_id": factor_id,
        "version": "signal_first",
        "title": str(environment.get("title") or "Signal-first RL environment"),
        "hypothesis": str(environment.get("hypothesis") or ""),
        "research_mode": "signal_first",
        "factor_origin": "technical_discovery",
        "factor_family": environment.get("factor_family", {}),
        "factor_exposures": environment.get("factor_exposures", {}),
        "environment": environment,
        "environment_id": str(environment.get("environment_id") or environment_id),
        "status": status,
        "approval_status": "not_requested",
        "live_enabled": False,
        "updated_at": now,
        "last_verified_at": now.isoformat(timespec="seconds") if status == "validated" else "",
        "metrics": metrics,
        "validation": {
            "status": status,
            "verified": status == "validated",
            "mode": "signal_first",
            "sample_source": "observations" if observations is not None else SIGNAL_FIRST_COLLECTION,
            "sample_count": int(metrics.get("sample_count") or 0),
            "rejected_sample_count": len(rejected_rows),
            "rejected_reason": "future_leak_boundary" if rejected_rows else "",
            "rejected_future_leak": validation_artifact["rejected_future_leak"],
            "artifact": validation_artifact,
        },
        "evaluation": evaluation,
        "failure_samples": validation_artifact["failure_samples"],
        "watchlist_symbols": _watchlist_from_rows(valid_rows),
        "feedback": _feedback(metrics, len(rejected_rows)),
        "paper_account": {
            **validation_artifact["paper_account"],
            "enabled": status in {"validated", "observation_only"},
            "mode": "paper_observation",
            "no_auto_order": True,
        },
        "reproducibility": {
            **_default_reproducibility(now),
            "as_of_boundary": "Signal rows generated on T can only be scored against T+1 and later bars.",
            "data_snapshot": _data_snapshot(valid_rows, now),
            "rejected_future_leak_rows": len(rejected_rows),
        },
        "experiment_id": experiment_id,
        "recorder_id": recorder_id,
    }
    ledger = _ledger_doc(report, stage="signal_first_feedback_validation", status="FINISHED", metrics=metrics, artifacts={
        "environment": environment,
        "evaluation": evaluation,
        "validation_report": {
            "metrics": metrics,
            "artifact": validation_artifact,
            "failure_samples": report["failure_samples"],
        },
    })
    if persist and db is not None:
        _upsert(db, "ai_factor_experiments", {"factor_id": factor_id}, report)
        _upsert(db, "ai_factor_experiment_ledger", {"recorder_id": recorder_id}, ledger)
    return _json_safe({**report, "ledger": ledger})


def run_factor_rhythm_demo(
    *,
    factor_id: str = DEFAULT_FACTOR_ID,
    idea: str = "",
    db: Any = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Run a deterministic front-end rhythm simulation without creating validation metrics."""
    now = naive_market_now("A")
    db = db if db is not None else _get_db_or_none()
    factor_id = _safe_factor_id(factor_id or idea or DEFAULT_FACTOR_ID)
    existing = _latest_factor_doc(db, factor_id)
    if idea.strip():
        draft = create_factor_draft(idea=idea, factor_id=factor_id, db=db, persist=persist)
        if existing:
            draft = {**_as_dict(existing), **_as_dict(draft)}
    else:
        draft = existing or create_factor_draft(factor_id=factor_id, db=db, persist=persist)

    portfolio = _as_dict(draft.get("portfolio_construction")) or _portfolio_construction_for_idea(
        str(draft.get("hypothesis") or idea)
    )
    rhythm = _rhythm_demo_from_portfolio(portfolio)
    experiment_id = _experiment_id(factor_id, "rhythm_demo", str(now.date()))
    recorder_id = _recorder_id(experiment_id, "rhythm")
    report = {
        **_as_dict(draft),
        "_id": factor_id,
        "factor_id": factor_id,
        "status": str(draft.get("status") or "specified"),
        "updated_at": now,
        "rhythm": rhythm,
        "metrics": _as_dict(draft.get("metrics")),
        "validation": _as_dict(draft.get("validation")) or {"status": "not_run", "verified": False},
        "experiment_id": experiment_id,
        "recorder_id": recorder_id,
    }
    ledger = _ledger_doc(report, stage="rhythm_demo", status="FINISHED", metrics={}, artifacts={
        "rhythm": rhythm,
        "portfolio_construction": portfolio,
    })
    if persist and db is not None:
        _upsert(db, "ai_factor_experiments", {"factor_id": factor_id}, report)
        _upsert(db, "ai_factor_experiment_ledger", {"recorder_id": recorder_id}, ledger)
    return _json_safe({**report, "ledger": ledger})


def publish_factor(
    *,
    factor_id: str,
    db: Any = None,
    live_enabled: bool = True,
    approved_by: str = "trader",
) -> dict[str, Any]:
    """Publish a validated factor into the strategy snapshot gate."""
    db = db if db is not None else _get_db_or_none()
    factor_id = _safe_factor_id(factor_id or DEFAULT_FACTOR_ID)
    factor = _latest_factor_doc(db, factor_id)
    metrics = _as_dict(factor.get("metrics") if factor else {})
    if not factor or not metrics.get("verified") or int(metrics.get("sample_count") or 0) <= 0:
        return _json_safe({
            "factor_id": factor_id,
            "status": "rejected",
            "live_enabled": False,
            "error": "factor_requires_verified_validation_before_publish",
        })
    reward = _as_dict(_as_dict(factor.get("evaluation")).get("reward"))
    if (
        str(factor.get("research_mode") or "") == "signal_first"
        and reward
        and str(reward.get("status") or "") != "validated"
    ):
        return _json_safe({
            "factor_id": factor_id,
            "status": "rejected",
            "live_enabled": False,
            "error": "factor_reward_gate_not_validated",
            "blocking_gates": reward.get("blocking_gates", []),
        })
    now = naive_market_now("A")
    publication = {
        **factor,
        "_id": factor_id,
        "factor_id": factor_id,
        "status": "published",
        "approval_status": "approved",
        "approved_by": approved_by,
        "live_enabled": bool(live_enabled),
        "updated_at": now,
        "published_at": now,
        "lifecycle": {
            **_as_dict(factor.get("lifecycle")),
            "state": "published",
            "next_allowed": ["disabled"],
        },
        "paper_account": {
            **_as_dict(factor.get("paper_account")),
            "enabled": True,
            "mode": "paper_observation",
            "no_auto_order": True,
        },
    }
    if db is not None:
        _upsert(db, "ai_factor_publications", {"factor_id": factor_id}, publication)
    return _json_safe(publication)


def disable_factor(*, factor_id: str, db: Any = None, reason: str = "") -> dict[str, Any]:
    db = db if db is not None else _get_db_or_none()
    now = naive_market_now("A")
    doc = {
        "factor_id": factor_id,
        "status": "disabled",
        "live_enabled": False,
        "disabled_at": now,
        "disable_reason": reason,
        "updated_at": now,
    }
    if db is not None:
        _upsert(db, "ai_factor_experiments", {"factor_id": factor_id}, doc)
        _upsert(db, "ai_factor_publications", {"factor_id": factor_id}, doc)
    return _json_safe(doc)


def build_ai_factor_strategy_candidates(*, db: Any = None) -> list[dict[str, Any]]:
    """Build strategy candidates from published and approved AI factors only."""
    factory = build_ai_factor_factory(db=db, include_sample=False)
    candidates: list[dict[str, Any]] = []
    for factor in factory.get("factors", []):
        if not _is_live_factor(factor):
            continue
        for symbol_item in _as_list(factor.get("watchlist_symbols") or factor.get("symbols")):
            symbol = str(symbol_item.get("symbol") or symbol_item.get("code") or "").strip()
            if not symbol:
                continue
            name = str(symbol_item.get("name") or symbol).strip()
            metrics = _as_dict(factor.get("metrics"))
            draft = _as_dict(factor.get("draft") or factor.get("research"))
            score_breakdown = _strategy_score_breakdown(factor, symbol_item)
            factor_exposures = _factor_exposures_for_factor(factor)
            risk_overlay_flags = _risk_overlay_flags_for_factor(factor)
            validation_status = str(_as_dict(factor.get("validation")).get("status") or factor.get("status") or "")
            candidates.append({
                "symbol": symbol,
                "name": name,
                "display_name": f"{name} · {str(_as_dict(factor.get('factor_family')).get('label') or 'AI因子')}",
                "score": _float(symbol_item.get("score"), _float(metrics.get("fitness"), 0.0)),
                "status": "watch",
                "decision_stage": "strategy_candidate",
                "reason": draft.get("why_effective") or factor.get("hypothesis") or factor.get("title") or "",
                "recommended_action": "进入盘前池观察，等待触发条件与风险线确认",
                "missing_gates": ["intraday_trigger_confirmation", "manual_risk_review"],
                "primary_blocker": "",
                "promotion_path": ["AI因子验证", "盘前池观察", "盘中触发复核"],
                "factor_exposures": factor_exposures,
                "factor_origin": str(factor.get("factor_origin") or ""),
                "factor_research_mode": str(factor.get("research_mode") or ""),
                "validation_status": validation_status,
                "factor_score_breakdown": score_breakdown,
                "industry_beta_score": score_breakdown["industry_beta_score"],
                "expectation_alpha_score": score_breakdown["expectation_alpha_score"],
                "technical_confirmation_score": score_breakdown["technical_confirmation_score"],
                "risk_overlay_flags": risk_overlay_flags,
                "metadata": {
                    "source": "ai_factor_factory",
                    "factor_id": factor.get("factor_id", ""),
                    "factor_version": factor.get("version", ""),
                    "factor_origin": factor.get("factor_origin", ""),
                    "research_mode": factor.get("research_mode", ""),
                    "factor_family": factor.get("factor_family", {}),
                    "industry_beta": factor.get("industry_beta", {}),
                    "expectation_alpha": factor.get("expectation_alpha", {}),
                    "technical_confirmation": factor.get("technical_confirmation", {}),
                    "strategy_integration": factor.get("strategy_integration", {}),
                    "factor_exposures": factor_exposures,
                    "factor_score_breakdown": score_breakdown,
                    "validation_status": validation_status,
                    "industry_beta_score": score_breakdown["industry_beta_score"],
                    "expectation_alpha_score": score_breakdown["expectation_alpha_score"],
                    "technical_confirmation_score": score_breakdown["technical_confirmation_score"],
                    "risk_overlay_flags": risk_overlay_flags,
                    "trigger_condition": draft.get("trigger_condition", ""),
                    "invalidation_condition": draft.get("invalidation_condition", ""),
                    "risk_tags": factor.get("risk_tags", []),
                    "validation_metrics": metrics,
                    "paper_account": factor.get("paper_account", {}),
                    "next_action": "等待盘中触发，不自动下单",
                    "recommended_action": "进入盘前池观察",
                },
                "evidence": [{
                    "type": "ai_factor",
                    "source": "ai_factor_factory",
                    "freshness": factor.get("status", "published"),
                    "summary": factor.get("title", ""),
                }],
            })
    return candidates


def build_signal_first_rl_environments(*, db: Any = None, limit: int = 24) -> list[dict[str, Any]]:
    """Build clean signal-first RL environments from technical signal rows."""
    rows = _load_recent_technical_signal_rows(db, limit=1500)
    if not rows:
        return []
    attribution_index = _load_signal_first_attribution_index(db)
    clusters: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not _is_constructive_technical_signal(row):
            continue
        key = _signal_first_environment_key(row)
        cluster = clusters.setdefault(key, {
            "environment_id": key,
            "theme": _technical_signal_theme(row),
            "signal_family": str(row.get("signal_family") or "unknown"),
            "signal_type_family": _signal_type_family(row),
            "freq_bucket": _freq_bucket(row),
            "resonance_grade": _resonance_grade_from_row(row),
            "scan_scope": str(row.get("scan_scope") or "unknown"),
            "symbols": [],
            "signal_types": [],
            "source_signals": [],
            "score_total": 0.0,
            "confidence_total": 0.0,
            "count": 0,
        })
        symbol = str(row.get("symbol") or row.get("raw_code") or row.get("code") or "").strip()
        signal_type = str(row.get("signal_type") or row.get("type") or row.get("reason") or "").strip()
        if symbol and symbol not in cluster["symbols"]:
            cluster["symbols"].append(symbol)
        if signal_type and signal_type not in cluster["signal_types"]:
            cluster["signal_types"].append(signal_type)
        cluster["source_signals"].append(_technical_signal_preview(row))
        cluster["score_total"] += _float(row.get("total_score") or row.get("score"), 0.0) or 0.0
        cluster["confidence_total"] += _float(row.get("confidence"), 0.0) or 0.0
        cluster["count"] += 1

    environments: list[dict[str, Any]] = []
    for cluster in clusters.values():
        count = int(cluster["count"])
        if count <= 0:
            continue
        symbol_count = len(cluster["symbols"])
        avg_score = cluster["score_total"] / max(1, count)
        avg_confidence = cluster["confidence_total"] / max(1, count)
        raw_theme = str(cluster["theme"])
        attribution = _infer_signal_first_attribution(
            attribution_index,
            cluster["symbols"],
            fallback_theme=raw_theme,
        )
        theme = str(attribution.get("primary_theme") or raw_theme)
        family_id = f"candidate_industry_factor.{_safe_factor_id(theme)}"
        cleanliness = _cluster_cleanliness(
            theme=theme,
            symbol_count=symbol_count,
            signal_count=count,
            signal_type_count=len(cluster["signal_types"]),
        )
        status = "pending_validation"
        blocking_gates: list[str] = []
        if str(cluster["scan_scope"]).lower().startswith("intraday"):
            status = "observation_only"
            blocking_gates.append("intraday_signal_first_observation_only")
        if _is_overbroad_signal_first_cluster(theme, symbol_count, count):
            status = "not_evaluable"
            blocking_gates.append("overbroad_cluster")
        technical_score = _round(min(100.0, max(0.0, avg_score + avg_confidence * 20 + symbol_count * 4)), 2)
        environments.append({
            "environment_id": str(cluster["environment_id"]),
            "factor_id": _safe_factor_id(str(cluster["environment_id"])),
            "version": "signal_first_env_v1",
            "title": _signal_first_environment_title({**cluster, "theme": theme}),
            "hypothesis": f"全市场技术信号在「{theme}」的 {cluster['signal_type_family']} / {cluster['freq_bucket']} / {cluster['resonance_grade']} 环境中聚集，可能形成可验证因子。",
            "research_mode": "signal_first",
            "factor_origin": "technical_discovery",
            "status": status,
            "live_enabled": False,
            "blocking_gates": blocking_gates,
            "split_keys": {
                "signal_family": cluster["signal_family"],
                "signal_type_family": cluster["signal_type_family"],
                "freq_bucket": cluster["freq_bucket"],
                "resonance_grade": cluster["resonance_grade"],
                "scan_scope": cluster["scan_scope"],
                "theme": theme,
                "raw_theme": raw_theme,
            },
            "attribution": attribution,
            "factor_family": {
                "family_id": family_id,
                "label": f"{theme}候选技术环境",
                "mode": "signal_first",
                "primary_style": "industry_beta" if symbol_count >= 3 else "potential_expectation_alpha",
            },
            "factor_exposures": {
                "primary": family_id,
                "mode": "signal_first",
                "origin": "technical_discovery",
                "theme": theme,
                "symbols": cluster["symbols"][:20],
                "groups": [theme],
            },
            "environment_metrics": {
                "source_signal_count": count,
                "unique_symbol_count": symbol_count,
                "signal_type_count": len(cluster["signal_types"]),
                "avg_source_score": _round(avg_score, 4),
                "avg_confidence": _round(avg_confidence, 4),
                "cluster_cleanliness": cleanliness,
                "technical_confirmation_score": technical_score,
            },
            "validation_profile": _validation_profile(),
            "risk_overlay_flags": _risk_overlay_flags_for_factor({"research_mode": "signal_first"}),
            "source_signal_types": cluster["signal_types"][:8],
            "source_signals": cluster["source_signals"][:12],
            "next_action": "自动生成 forward-return observations；通过分层 reward 后再进入人工复核。",
        })
    return sorted(
        environments,
        key=lambda item: (
            _float(_as_dict(item.get("environment_metrics")).get("cluster_cleanliness"), 0.0),
            _float(_as_dict(item.get("environment_metrics")).get("technical_confirmation_score"), 0.0),
            _float(_as_dict(item.get("environment_metrics")).get("source_signal_count"), 0.0),
        ),
        reverse=True,
    )[:limit]


def build_signal_first_candidate_factor_ideas(
    *,
    db: Any = None,
    environments: list[dict[str, Any]] | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Discover candidate industry factors from technical signals.

    These ideas are intentionally not live factors.  They must go through
    research review and sample replay before affecting strategy candidates.
    """
    environments = environments if environments is not None else build_signal_first_rl_environments(db=db)
    ideas: list[dict[str, Any]] = []
    for environment in _candidate_idea_sources(environments):
        metrics = _as_dict(environment.get("environment_metrics"))
        split_keys = _as_dict(environment.get("split_keys"))
        symbol_count = int(metrics.get("unique_symbol_count") or 0)
        classification = "industry_beta" if symbol_count >= 3 else "potential_expectation_alpha"
        theme = str(split_keys.get("theme") or environment.get("title") or SIGNAL_FIRST_FALLBACK_THEME)
        factor_id = str(environment.get("factor_id") or _safe_factor_id(f"signal_first:{theme}:{classification}"))
        family_id = f"candidate_industry_factor.{_safe_factor_id(theme)}"
        title_suffix = "技术扩散因子" if classification == "industry_beta" else "预期差技术因子"
        idea = {
            "factor_id": factor_id,
            "version": "idea",
            "title": f"{theme}{title_suffix}",
            "hypothesis": str(environment.get("hypothesis") or f"全市场技术信号在「{theme}」聚集，可能指向新的行业 beta 或预期 alpha。"),
            "research_mode": "signal_first",
            "factor_origin": "technical_discovery",
            "environment_id": environment.get("environment_id", ""),
            "rl_environment": environment,
            "factor_family": {
                "family_id": family_id,
                "label": f"{theme}候选行业因子",
                "mode": "signal_first",
                "primary_style": classification,
            },
            "industry_beta": {
                "name": f"{_safe_factor_id(theme)}_technical_diffusion",
                "classification": classification,
                "evidence": "同产业链/概念内技术信号同步聚集" if classification == "industry_beta" else "少数高暴露标的技术结构先走强",
                "symbol_count": symbol_count,
            },
            "expectation_alpha": {
                "name": f"{_safe_factor_id(theme)}_expectation_gap",
                "classification": classification,
                "evidence": "需要投研补充事件、订单、政策、产业链或资金扩散证据。",
            },
            "technical_confirmation": {
                **_technical_confirmation_profile(),
                "source_signal_types": environment.get("source_signal_types", []),
                "source_signal_count": int(metrics.get("source_signal_count") or 0),
                "unique_symbol_count": symbol_count,
                "technical_confirmation_score": _round(metrics.get("technical_confirmation_score"), 2),
            },
            "strategy_integration": {
                **_strategy_integration_profile(),
                "live_gate": "signal_first 候选只进入 factor_idea_queue；完成投研复核和样本复盘前不能进入 live 候选池。",
            },
            "factor_exposures": {
                "primary": family_id,
                "mode": "signal_first",
                "origin": "technical_discovery",
                "theme": theme,
                "symbols": _string_list(_as_dict(environment.get("factor_exposures")).get("symbols"))[:20],
                "groups": [theme],
            },
            "validation_profile": _validation_profile(),
            "risk_overlay_flags": _risk_overlay_flags_for_factor({"research_mode": "signal_first"}),
            "status": "idea",
            "approval_status": "not_requested",
            "live_enabled": False,
            "metrics": {"verified": False, "sample_count": 0},
            "validation": {
                "status": "not_run",
                "verified": False,
                "required_before_live": True,
            },
            "source_signals": environment.get("source_signals", [])[:10],
            "beta_alpha_assessment": {
                "classification": classification,
                "industry_beta_score": _round(min(100.0, symbol_count * 18 + _float(metrics.get("avg_confidence"), 0.0) * 20), 2),
                "expectation_alpha_score": _round(min(100.0, max(20.0, _float(metrics.get("avg_source_score"), 0.0) + (4 - min(symbol_count, 4)) * 8)), 2),
                "technical_confirmation_score": _round(metrics.get("technical_confirmation_score"), 2),
            },
            "next_action": "进入 Agent OS 复核：补产业链/事件证据，再创建 research_first 因子草稿和复盘样本。",
            "ai_explanation": [
                "这是技术发现因子候选，不是可直接使用的策略因子。",
                "必须先完成投研归因、样本复盘和人工观察确认，才能影响 live 候选池。",
            ],
        }
        ideas.append(idea)

    return sorted(
        ideas,
        key=lambda item: _float(_as_dict(item.get("technical_confirmation")).get("technical_confirmation_score"), 0.0),
        reverse=True,
    )[:limit]


def _signal_first_environment_key(row: Mapping[str, Any]) -> str:
    parts = [
        "signal_first",
        _safe_factor_id(_technical_signal_theme(row)),
        _safe_factor_id(str(row.get("signal_family") or "unknown")),
        _signal_type_family(row),
        _freq_bucket(row),
        _resonance_grade_from_row(row),
        _safe_factor_id(str(row.get("scan_scope") or "unknown")),
    ]
    return ":".join(parts)


def _candidate_idea_sources(environments: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    for environment in environments:
        split_keys = _as_dict(environment.get("split_keys"))
        theme = str(split_keys.get("theme") or SIGNAL_FIRST_FALLBACK_THEME)
        if theme == SIGNAL_FIRST_FALLBACK_THEME:
            passthrough.append(dict(environment))
            continue
        group = grouped.setdefault(theme, {
            "environment_id": f"signal_first:{_safe_factor_id(theme)}:aggregate",
            "factor_id": _safe_factor_id(f"signal_first:{_safe_factor_id(theme)}:aggregate"),
            "title": f"{theme}技术扩散环境",
            "hypothesis": f"全市场技术信号在「{theme}」聚集，可能指向新的行业 beta 或预期 alpha。",
            "research_mode": "signal_first",
            "factor_origin": "technical_discovery",
            "status": "pending_validation",
            "split_keys": {"theme": theme, "signal_family": "aggregate", "signal_type_family": "aggregate", "freq_bucket": "aggregate", "resonance_grade": "aggregate", "scan_scope": "aggregate"},
            "factor_exposures": {"primary": f"candidate_industry_factor.{_safe_factor_id(theme)}", "symbols": [], "groups": [theme]},
            "source_signal_types": [],
            "source_signals": [],
            "environment_metrics": {
                "source_signal_count": 0,
                "unique_symbol_count": 0,
                "signal_type_count": 0,
                "avg_source_score": 0.0,
                "avg_confidence": 0.0,
                "cluster_cleanliness": 1.0,
                "technical_confirmation_score": 0.0,
            },
        })
        group_metrics = group.get("environment_metrics")
        if not isinstance(group_metrics, dict):
            group_metrics = {
                "source_signal_count": 0,
                "unique_symbol_count": 0,
                "signal_type_count": 0,
                "avg_source_score": 0.0,
                "avg_confidence": 0.0,
                "cluster_cleanliness": 1.0,
                "technical_confirmation_score": 0.0,
            }
            group["environment_metrics"] = group_metrics
        env_metrics = _as_dict(environment.get("environment_metrics"))
        factor_exposures = group.get("factor_exposures")
        if not isinstance(factor_exposures, dict):
            factor_exposures = {"primary": f"candidate_industry_factor.{_safe_factor_id(theme)}", "symbols": [], "groups": [theme]}
            group["factor_exposures"] = factor_exposures
        group_symbols = factor_exposures.setdefault("symbols", [])
        for symbol in _string_list(_as_dict(environment.get("factor_exposures")).get("symbols")):
            if symbol not in group_symbols:
                group_symbols.append(symbol)
        for signal_type in _string_list(environment.get("source_signal_types")):
            if signal_type not in group["source_signal_types"]:
                group["source_signal_types"].append(signal_type)
        group["source_signals"].extend(_as_list(environment.get("source_signals"))[:8])
        group_metrics["source_signal_count"] += int(env_metrics.get("source_signal_count") or 0)
        group_metrics["unique_symbol_count"] = len(group_symbols)
        group_metrics["signal_type_count"] = len(group["source_signal_types"])
        group_metrics["avg_source_score"] = max(
            _float(group_metrics.get("avg_source_score"), 0.0) or 0.0,
            _float(env_metrics.get("avg_source_score"), 0.0) or 0.0,
        )
        group_metrics["avg_confidence"] = max(
            _float(group_metrics.get("avg_confidence"), 0.0) or 0.0,
            _float(env_metrics.get("avg_confidence"), 0.0) or 0.0,
        )
        group_metrics["cluster_cleanliness"] = min(
            _float(group_metrics.get("cluster_cleanliness"), 1.0) or 1.0,
            _float(env_metrics.get("cluster_cleanliness"), 1.0) or 1.0,
        )
        group_metrics["technical_confirmation_score"] = max(
            _float(group_metrics.get("technical_confirmation_score"), 0.0) or 0.0,
            _float(env_metrics.get("technical_confirmation_score"), 0.0) or 0.0,
        )
    return list(grouped.values()) + passthrough


def _signal_type_family(row: Mapping[str, Any]) -> str:
    text = str(row.get("signal_type") or row.get("type") or row.get("reason") or "").lower()
    if "macd" in text:
        return "macd"
    if "背驰" in text:
        return "divergence"
    if "一买" in text or "二买" in text or "三买" in text:
        return "chan_buy"
    if "一卖" in text or "二卖" in text or "三卖" in text or "卖" in text:
        return "chan_sell"
    if "缺口" in text or "gap" in text:
        return "gap"
    if "形态" in text:
        return "pattern"
    if "趋势" in text or "突破" in text:
        return "trend_breakout"
    return _safe_factor_id(text or "other")[:32]


def _freq_bucket(row: Mapping[str, Any]) -> str:
    text = str(row.get("freq") or "").strip().lower()
    if text in {"周线", "weekly", "w", "1w"}:
        return "weekly"
    if text in {"日线", "daily", "d", "1d"}:
        return "daily"
    if text in {"30分钟", "30min", "30m", "f30"}:
        return "intraday_30m"
    if text in {"15分钟", "15min", "15m", "f15"}:
        return "intraday_15m"
    if text in {"5分钟", "5min", "5m", "f5"}:
        return "intraday_5m"
    return _safe_factor_id(text or "unknown")


def _resonance_grade_from_row(row: Mapping[str, Any]) -> str:
    context = _as_dict(row.get("resonance_context"))
    grade = str(context.get("grade") or "").strip()
    return _safe_factor_id(grade or "unknown")


def _signal_first_environment_title(cluster: Mapping[str, Any]) -> str:
    return (
        f"{cluster.get('theme') or SIGNAL_FIRST_FALLBACK_THEME} / "
        f"{cluster.get('signal_type_family') or 'technical'} / "
        f"{cluster.get('freq_bucket') or 'freq'} / "
        f"{cluster.get('resonance_grade') or 'resonance'}"
    )


def _cluster_cleanliness(*, theme: str, symbol_count: int, signal_count: int, signal_type_count: int) -> float:
    base = 0.95 if theme != SIGNAL_FIRST_FALLBACK_THEME else 0.62
    symbol_penalty = max(0.0, symbol_count - 25) / 120
    signal_penalty = max(0.0, signal_count - 80) / 500
    type_penalty = max(0.0, signal_type_count - 4) * 0.035
    return _round(max(0.05, min(1.0, base - symbol_penalty - signal_penalty - type_penalty)), 4)


def _is_overbroad_signal_first_cluster(theme: str, symbol_count: int, signal_count: int) -> bool:
    if theme != SIGNAL_FIRST_FALLBACK_THEME:
        return False
    return symbol_count > SIGNAL_FIRST_MAX_CLEAN_SYMBOLS or signal_count > SIGNAL_FIRST_MAX_CLEAN_SIGNALS


def _empty_signal_first_environment(environment_id: str) -> dict[str, Any]:
    return {
        "environment_id": str(environment_id or ""),
        "factor_id": _safe_factor_id(str(environment_id or "")),
        "title": "Unknown signal-first environment",
        "research_mode": "signal_first",
        "factor_origin": "technical_discovery",
        "status": "not_evaluable",
        "blocking_gates": ["environment_not_found"],
        "split_keys": {},
        "environment_metrics": {"cluster_cleanliness": 0.0, "source_signal_count": 0, "unique_symbol_count": 0},
        "source_signals": [],
    }


def _signal_first_evaluation_summary(environments: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for item in environments:
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    clean_scores = [
        _float(_as_dict(item.get("environment_metrics")).get("cluster_cleanliness"), 0.0) or 0.0
        for item in environments
    ]
    return {
        "environment_count": len(environments),
        "status_counts": status_counts,
        "avg_cluster_cleanliness": _round(sum(clean_scores) / len(clean_scores), 4) if clean_scores else 0.0,
        "overbroad_count": status_counts.get("not_evaluable", 0),
        "reward_mode": SIGNAL_FIRST_REWARD_SPEC["mode"],
    }


def _signal_first_observations_for_environment(db: Any, environment: Mapping[str, Any]) -> list[dict[str, Any]]:
    if db is None:
        return []
    environment_id = str(environment.get("environment_id") or "")
    rows = [
        row for row in _load_recent_technical_signal_rows(db, limit=2500)
        if _signal_first_environment_key(row) == environment_id
    ]
    observations: list[dict[str, Any]] = []
    group = str(_as_dict(environment.get("split_keys")).get("theme") or SIGNAL_FIRST_FALLBACK_THEME)
    for row in rows:
        if str(row.get("scan_scope") or "").lower().startswith("intraday"):
            continue
        symbol = str(row.get("symbol") or row.get("raw_code") or row.get("code") or "").strip()
        source_date = _parse_date(row.get("as_of") or row.get("dt") or row.get("updated_at"))
        if not symbol or source_date is None:
            continue
        returns = _forward_returns_from_bars(db, symbol, source_date)
        if not returns:
            continue
        observations.append({
            "factor_id": environment_id,
            "source_date": source_date.isoformat(),
            "cn_trade_date": returns["trade_date"],
            "symbol": symbol,
            "name": str(row.get("name") or row.get("symbol_name") or symbol),
            "group": group,
            "factor_value": _row_signal_strength(row),
            "return_t1": returns.get("return_t1"),
            "return_t5": returns.get("return_t5"),
            "return_t10": returns.get("return_t10"),
            "return_t20": returns.get("return_t20"),
            "mfe": returns.get("mfe"),
            "mae": returns.get("mae"),
            "sample_mode": "signal_first_auto",
        })
    return observations


def _row_signal_strength(row: Mapping[str, Any]) -> float:
    score = _float(row.get("total_score") or row.get("score"), 0.0) or 0.0
    confidence = _float(row.get("confidence"), 0.0) or 0.0
    if score:
        return _round(max(0.0, min(1.0, score / 300.0)), 4)
    return _round(max(0.0, min(1.0, confidence)), 4)


def _forward_returns_from_bars(db: Any, symbol: str, source_date: date) -> dict[str, Any]:
    docs = _daily_bar_docs(db, symbol)
    if not docs:
        return {}
    dated = []
    for doc in docs:
        dt = _parse_date(doc.get("dt") or doc.get("date"))
        close = _float(doc.get("close"))
        high = _float(doc.get("high"), close)
        low = _float(doc.get("low"), close)
        if dt is None or close is None:
            continue
        dated.append({"dt": dt, "close": close, "high": high, "low": low})
    dated.sort(key=lambda item: item["dt"])
    trade_index = next((idx for idx, item in enumerate(dated) if item["dt"] > source_date), None)
    if trade_index is None:
        return {}
    base = _float(dated[trade_index]["close"])
    if not base:
        return {}
    out: dict[str, Any] = {"trade_date": dated[trade_index]["dt"].isoformat()}
    for window in (1, 5, 10, 20):
        target = min(trade_index + window, len(dated) - 1)
        if target <= trade_index:
            continue
        out[f"return_t{window}"] = _round(dated[target]["close"] / base - 1.0, 4)
    path = dated[trade_index:min(len(dated), trade_index + 6)]
    if path:
        highs = [_float(item.get("high"), item.get("close")) for item in path]
        lows = [_float(item.get("low"), item.get("close")) for item in path]
        out["mfe"] = _round(max(value for value in highs if value is not None) / base - 1.0, 4)
        out["mae"] = _round(min(value for value in lows if value is not None) / base - 1.0, 4)
    return out


def _daily_bar_docs(db: Any, symbol: str) -> list[dict[str, Any]]:
    query_values = _symbol_query_values_for_factor(symbol)
    try:
        cursor = db["bars"].find({
            "meta.symbol": {"$in": query_values},
            "meta.freq": {"$in": ["日线", "daily", "D", "1d"]},
        }).sort([("dt", 1)])
        docs = [dict(item) for item in cursor]
    except Exception:
        docs = []
    if docs:
        return docs
    try:
        return [dict(item) for item in db["bars"].find({"symbol": {"$in": query_values}}).sort([("dt", 1)])]
    except Exception:
        return []


def _symbol_query_values_for_factor(symbol: str) -> list[str]:
    raw = str(symbol or "").strip()
    upper = raw.upper()
    values = [raw]
    if "." in raw:
        values.append(raw.split(".", 1)[-1])
    digits = "".join(ch for ch in upper if ch.isdigit())
    if len(digits) == 6:
        values.extend([digits, f"SH.{digits}" if digits.startswith(("6", "9")) else f"SZ.{digits}"])
    return _unique_strings(values)


def _layered_reward_evaluation(
    metrics: Mapping[str, Any],
    valid_rows: Sequence[Mapping[str, Any]],
    rejected_rows: Sequence[Mapping[str, Any]],
    *,
    environment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    environment = environment or {}
    factor_eval = _factor_evaluation(metrics, environment)
    portfolio_eval = _portfolio_evaluation(metrics, valid_rows)
    reward = _layered_reward(metrics, factor_eval, portfolio_eval, valid_rows, rejected_rows, environment)
    return {
        "methodology": "Alphalens-style factor predictive evaluation first; Qlib-style portfolio risk gate second.",
        "factor_evaluation": factor_eval,
        "portfolio_evaluation": portfolio_eval,
        "reward": reward,
    }


def _factor_evaluation(metrics: Mapping[str, Any], environment: Mapping[str, Any]) -> dict[str, Any]:
    env_metrics = _as_dict(environment.get("environment_metrics"))
    rank_ic = _float(metrics.get("rank_ic"), 0.0) or 0.0
    spread = _float(metrics.get("long_short_quantile_spread") or metrics.get("long_short_return"), 0.0) or 0.0
    t5 = _float(metrics.get("avg_return_t5"), 0.0) or 0.0
    t20 = _float(metrics.get("avg_return_t20"), t5) or t5
    mfe = _float(metrics.get("mfe"), 0.0) or 0.0
    mae = _float(metrics.get("mae"), 0.0) or 0.0
    sample_count = int(_float(metrics.get("sample_count"), 0) or 0)
    cleanliness = _float(env_metrics.get("cluster_cleanliness"), 0.7)
    rank_ic_score = _positive_score(rank_ic, 0.12)
    spread_score = _positive_score(spread, 0.05)
    forward_score = _round((_positive_score(t5, 0.03) + _positive_score(t20, 0.06)) / 2, 2)
    mfe_score = _round(min(100.0, max(0.0, _positive_score(mfe, 0.08) * 0.7 + max(0.0, 1 - abs(mae) / 0.15) * 30)), 2)
    cleanliness_score = _round(max(0.0, min(100.0, (cleanliness or 0.0) * 100)), 2)
    robustness_score = _round(min(100.0, sample_count / SIGNAL_FIRST_MIN_VALIDATED_SAMPLES * 100), 2)
    factor_score = _round(
        rank_ic_score * 0.30
        + spread_score * 0.25
        + forward_score * 0.20
        + mfe_score * 0.10
        + cleanliness_score * 0.10
        + robustness_score * 0.05,
        2,
    )
    return {
        "rank_ic": _round(rank_ic, 4),
        "quantile_spread": _round(spread, 4),
        "avg_return_t5": _round(t5, 4),
        "avg_return_t20": _round(t20, 4),
        "mfe": _round(mfe, 4),
        "mae": _round(mae, 4),
        "cluster_cleanliness": _round(cleanliness, 4),
        "sample_count": sample_count,
        "component_scores": {
            "rank_ic_score": rank_ic_score,
            "quantile_spread_score": spread_score,
            "forward_return_score": forward_score,
            "mfe_mae_score": mfe_score,
            "cluster_cleanliness_score": cleanliness_score,
            "sample_robustness_score": robustness_score,
        },
        "factor_score": factor_score,
    }


def _portfolio_evaluation(metrics: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    paper = _paper_account_from_rows(rows, {**dict(metrics), "verified": bool(rows)})
    turnover = _float(metrics.get("turnover"), 0.0) or 0.0
    total_return = _float(paper.get("total_return"), 0.0) or 0.0
    cost_adjusted_return = _round(total_return - turnover * 0.002, 4)
    returns = [_row_float(row, "return_t5", "forward_return_t5") for row in rows]
    returns = [value for value in returns if value is not None]
    sharpe = _sharpe_like(returns)
    information_ratio = sharpe
    max_drawdown = _float(paper.get("max_drawdown"), 0.0) or 0.0
    hit_rate = _float(metrics.get("win_rate"), 0.0) or 0.0
    portfolio_score = _round(
        _positive_score(cost_adjusted_return, 0.06) * 0.30
        + _positive_score(max(sharpe, information_ratio), 1.5) * 0.25
        + _drawdown_score(max_drawdown) * 0.25
        + _turnover_score(turnover) * 0.10
        + max(0.0, min(100.0, hit_rate * 100)) * 0.10,
        2,
    )
    return {
        "cost_adjusted_return": cost_adjusted_return,
        "sharpe": _round(sharpe, 4),
        "information_ratio": _round(information_ratio, 4),
        "max_drawdown": _round(max_drawdown, 4),
        "turnover": _round(turnover, 4),
        "hit_rate": _round(hit_rate, 4),
        "component_scores": {
            "cost_adjusted_return_score": _positive_score(cost_adjusted_return, 0.06),
            "sharpe_or_ir_score": _positive_score(max(sharpe, information_ratio), 1.5),
            "max_drawdown_score": _drawdown_score(max_drawdown),
            "turnover_score": _turnover_score(turnover),
            "hit_rate_stability_score": max(0.0, min(100.0, hit_rate * 100)),
        },
        "portfolio_score": portfolio_score,
    }


def _layered_reward(
    metrics: Mapping[str, Any],
    factor_eval: Mapping[str, Any],
    portfolio_eval: Mapping[str, Any],
    valid_rows: Sequence[Mapping[str, Any]],
    rejected_rows: Sequence[Mapping[str, Any]],
    environment: Mapping[str, Any],
) -> dict[str, Any]:
    sample_count = int(_float(metrics.get("sample_count"), 0) or 0)
    factor_score = _float(factor_eval.get("factor_score"), 0.0) or 0.0
    portfolio_score = _float(portfolio_eval.get("portfolio_score"), 0.0) or 0.0
    blocking_gates = list(environment.get("blocking_gates") or [])
    if rejected_rows and not valid_rows:
        blocking_gates.append("future_leak")
    if sample_count < SIGNAL_FIRST_MIN_EVALUABLE_SAMPLES:
        blocking_gates.append("insufficient_samples")
    if _float(factor_eval.get("rank_ic"), 0.0) <= 0:
        blocking_gates.append("rank_ic_not_positive")
    if _float(factor_eval.get("quantile_spread"), 0.0) <= 0:
        blocking_gates.append("quantile_spread_not_positive")
    if _float(portfolio_eval.get("cost_adjusted_return"), 0.0) <= 0:
        blocking_gates.append("cost_adjusted_return_not_positive")
    if _float(portfolio_eval.get("max_drawdown"), 0.0) < -0.15:
        blocking_gates.append("max_drawdown_gate_failed")
    if _float(portfolio_eval.get("turnover"), 0.0) > 1.5:
        blocking_gates.append("turnover_gate_failed")
    blocking_gates = _unique_strings(blocking_gates)

    if "future_leak" in blocking_gates or "overbroad_cluster" in blocking_gates or "insufficient_samples" in blocking_gates:
        status = "not_evaluable"
        final_reward = min(40.0, factor_score * 0.4)
    elif str(environment.get("status") or "") == "observation_only" or sample_count < SIGNAL_FIRST_MIN_VALIDATED_SAMPLES:
        status = "observation_only"
        final_reward = min(60.0, factor_score * 0.75 + portfolio_score * 0.25)
    elif any(gate in blocking_gates for gate in ("rank_ic_not_positive", "quantile_spread_not_positive")):
        status = "rejected"
        final_reward = min(50.0, factor_score * 0.4)
    elif any(gate in blocking_gates for gate in ("cost_adjusted_return_not_positive", "max_drawdown_gate_failed", "turnover_gate_failed")):
        status = "observation_only"
        final_reward = min(60.0, factor_score * 0.75 + portfolio_score * 0.25)
    else:
        status = "validated"
        final_reward = factor_score * 0.70 + portfolio_score * 0.30

    return {
        "factor_score": _round(factor_score, 2),
        "portfolio_score": _round(portfolio_score, 2),
        "final_reward": _round(max(-100.0, min(100.0, final_reward)), 2),
        "status": status,
        "blocking_gates": blocking_gates,
        "primary_reason": _reward_primary_reason(status, blocking_gates),
    }


def _reward_primary_reason(status: str, gates: Sequence[str]) -> str:
    if gates:
        return str(gates[0])
    if status == "validated":
        return "factor_predictive_power_and_portfolio_gate_passed"
    if status == "observation_only":
        return "factor_requires_more_samples_or_portfolio_confirmation"
    return status


def _positive_score(value: Any, target: float) -> float:
    numeric = _float(value, 0.0) or 0.0
    if numeric <= 0 or target <= 0:
        return 0.0
    return _round(min(100.0, numeric / target * 100), 2)


def _drawdown_score(max_drawdown: Any) -> float:
    drawdown = _float(max_drawdown, 0.0) or 0.0
    if drawdown >= -0.05:
        return 100.0
    if drawdown <= -0.25:
        return 0.0
    return _round((0.25 + drawdown) / 0.20 * 100, 2)


def _turnover_score(turnover: Any) -> float:
    value = _float(turnover, 0.0) or 0.0
    if value <= 0.3:
        return 100.0
    if value >= 1.5:
        return 0.0
    return _round((1.5 - value) / 1.2 * 100, 2)


def _sharpe_like(values: Sequence[float]) -> float:
    filtered = [value for value in values if value is not None and math.isfinite(value)]
    if len(filtered) < 2:
        return 0.0
    mean = sum(filtered) / len(filtered)
    variance = sum((value - mean) ** 2 for value in filtered) / (len(filtered) - 1)
    std = math.sqrt(variance)
    if std <= 0:
        return 0.0
    return _round(mean / std * math.sqrt(len(filtered)), 4)


def _factor_identity_for_idea(idea_text: str) -> dict[str, Any]:
    if _is_ai_hardware_idea(idea_text):
        return {
            "research_mode": "research_first",
            "factor_origin": "industry_research",
            "factor_family": dict(AI_HARDWARE_FACTOR_FAMILY),
            "industry_beta": {
                "name": "ai_hardware_industry_chain",
                "label": "AI硬件产业链行业 beta",
                "definition": "海外订单、资本开支、GB200、光模块、液冷、铜连接、PCB、服务器和存储链的同步景气扩散。",
                "evidence_required": ["chain_breadth", "cross_market_lead_lag", "a_share_acceptance"],
            },
            "expectation_alpha": {
                "name": "ai_expectation_revision",
                "label": "AI 产业预期修正 alpha",
                "definition": "AI 产业预期上修、分支订单/价格/供需变化或预期差先于全链条扩散被市场定价。",
                "evidence_required": ["expectation_change", "leader_confirmation", "failure_sample_boundary"],
            },
        }
    terms = _idea_terms(idea_text)
    theme = terms[0] if terms else "dynamic_industry"
    slug = _safe_factor_id(theme)
    return {
        "research_mode": "research_first",
        "factor_origin": "industry_research",
        "factor_family": {
            "family_id": f"industry_factor.{slug}",
            "label": f"{theme}行业因子",
            "mode": "research_first",
            "primary_style": "industry_research_hypothesis",
        },
        "industry_beta": {
            "name": f"{slug}_industry_chain",
            "label": f"{theme}行业 beta",
            "definition": "投研假设中的同产业链股票同步承接。",
            "evidence_required": ["theme_breadth", "chain_breadth", "a_share_acceptance"],
        },
        "expectation_alpha": {
            "name": f"{slug}_expectation_revision",
            "label": f"{theme}预期修正 alpha",
            "definition": "少数高暴露标的先于行业扩散定价预期变化。",
            "evidence_required": ["expectation_change", "leader_confirmation", "failure_sample_boundary"],
        },
    }


def _technical_confirmation_profile() -> dict[str, Any]:
    return {
        "chan": "一买/二买/三买、中枢突破、分型和背驰用于判断趋势还是震荡内反弹。",
        "ma": "均线多头、关键均线不破或破后收回，用于确认趋势承接。",
        "macd": "MACD 红绿柱面积、零轴状态、背驰和面积收缩用于判断趋势会不会破。",
        "volume": "放量突破、缩量回踩和组内扩散确认市场是否承接行业故事。",
        "multi_timeframe": "日线/周线/30分钟/15分钟/5分钟共振优先，防止单周期噪音。",
        "control_warning": "高控盘标的可能故意击穿关键位，需结合收回、量能和组内扩散复核。",
    }


def _strategy_integration_profile() -> dict[str, Any]:
    return {
        "outputs": [
            "factor_exposure",
            "factor_origin",
            "validation_status",
            "industry_beta_score",
            "expectation_alpha_score",
            "technical_confirmation_score",
            "risk_overlay_flags",
        ],
        "candidate_sorting": "提高同因子族中已验证且技术承接更强标的排序。",
        "watch_pool": "只在样本复盘和人工复核通过后进入观察池。",
        "risk_gate": "高开过热、关键位破坏、组内分化和未来函数边界失败时降权或阻断。",
        "agent_os_review": "复核理由必须同时说明行业 beta、预期 alpha、技术确认和风险覆盖。",
    }


def _validation_profile() -> dict[str, Any]:
    return {
        "metrics": list(VALIDATION_METRICS_PROFILE),
        "forward_windows": ["T+1", "T+5", "T+10", "T+20"],
        "engines": ["Alphalens-style single factor validation", "paper factor account"],
        "must_not": ["future_leak", "unverified_live_publish", "auto_order"],
    }


def _ai_factor_positioning_explanation(identity: Mapping[str, Any]) -> str:
    family = _as_dict(identity.get("factor_family"))
    if family.get("family_id") == "industry_factor.ai_hardware":
        return "AI 硬件是第一条被产品化的行业因子，定位为行业 beta + AI 产业预期 alpha，不是一套通用方法论。"
    return "该因子走 research_first 路径：先明确投研假设，再用技术结构和样本复盘证明市场承接。"


def _risk_overlay_flags_for_factor(factor: Mapping[str, Any]) -> list[str]:
    flags = _string_list(factor.get("risk_overlay_flags"))
    if flags:
        return flags
    base = ["gap_overheat", "chain_divergence", "support_break_without_reclaim", "future_leak_guard"]
    if str(factor.get("research_mode") or "") == "signal_first":
        return base + ["unvalidated_factor_idea", "event_evidence_missing"]
    return base


def _factor_exposures_for_factor(factor: Mapping[str, Any]) -> dict[str, Any]:
    family = _as_dict(factor.get("factor_family"))
    portfolio = _as_dict(factor.get("portfolio_construction"))
    return {
        "primary": str(family.get("family_id") or ""),
        "mode": str(factor.get("research_mode") or ""),
        "origin": str(factor.get("factor_origin") or ""),
        "industry_beta": str(_as_dict(factor.get("industry_beta")).get("name") or ""),
        "expectation_alpha": str(_as_dict(factor.get("expectation_alpha")).get("name") or ""),
        "groups": _basket_groups(portfolio.get("cn_reaction_basket") or portfolio.get("reaction_basket")),
    }


def _strategy_score_breakdown(factor: Mapping[str, Any], symbol_item: Mapping[str, Any]) -> dict[str, float]:
    metrics = _as_dict(factor.get("metrics"))
    symbol_score = _float(symbol_item.get("score"), _float(metrics.get("fitness"), 0.0)) or 0.0
    rank_ic = max(0.0, _float(metrics.get("rank_ic"), 0.0) or 0.0)
    long_short = max(0.0, _float(metrics.get("long_short_return"), 0.0) or 0.0)
    win_rate = max(0.0, _float(metrics.get("win_rate"), 0.0) or 0.0)
    beta_score = min(100.0, symbol_score * 0.45 + win_rate * 35 + rank_ic * 20)
    alpha_score = min(100.0, symbol_score * 0.35 + long_short * 600 + rank_ic * 25)
    technical_score = min(100.0, symbol_score * 0.55 + max(0.0, _float(symbol_item.get("return_t5"), 0.0) or 0.0) * 450)
    validation_score = min(100.0, (_float(metrics.get("fitness"), 0.0) or 0.0) + rank_ic * 15)
    return {
        "industry_beta_score": _round(beta_score, 2),
        "expectation_alpha_score": _round(alpha_score, 2),
        "technical_confirmation_score": _round(technical_score, 2),
        "validation_score": _round(validation_score, 2),
        "risk_adjustment": 0.0,
    }


def _load_recent_technical_signal_rows(db: Any, limit: int = 500) -> list[dict[str, Any]]:
    if db is None:
        return []
    try:
        latest = db[SIGNAL_FIRST_COLLECTION].find_one(
            {"market": "A", "as_of": {"$exists": True}},
            {"as_of": 1},
            sort=[("as_of", -1), ("updated_at", -1)],
        ) or {}
    except Exception:
        latest = {}
    query: dict[str, Any] = {"market": "A"}
    if latest.get("as_of"):
        query["as_of"] = latest.get("as_of")
    try:
        cursor = db[SIGNAL_FIRST_COLLECTION].find(query).sort(
            [("total_score", -1), ("score", -1), ("confidence", -1), ("updated_at", -1)]
        ).limit(limit)
        return [dict(item) for item in cursor]
    except Exception:
        return []


def _is_constructive_technical_signal(row: Mapping[str, Any]) -> bool:
    side = str(row.get("signal_side") or row.get("side") or "").lower()
    text = " ".join(str(row.get(key) or "") for key in ("signal_type", "signal_family", "reason", "summary"))
    if side == "sell" or any(token in text for token in ("卖", "顶", "死叉", "跌破", "减仓")):
        return False
    if side == "buy":
        return True
    lowered = text.lower()
    return any(token.lower() in lowered for token in SIGNAL_FIRST_BULLISH_TOKENS)


def _technical_signal_theme(row: Mapping[str, Any]) -> str:
    evidence = _as_dict(row.get("technical_evidence"))
    candidates = [
        row.get("industry_chain"),
        row.get("chain"),
        row.get("concept"),
        row.get("board"),
        row.get("industry"),
        row.get("theme"),
        evidence.get("industry_chain"),
        evidence.get("concept"),
        evidence.get("theme"),
        row.get("signal_family"),
    ]
    for value in candidates:
        text = str(value or "").strip()
        if text and text not in {"hard_technical", "technical", "buy"}:
            return text[:24]
    return "技术结构共振"


def _symbol_match_keys(symbol: Any) -> set[str]:
    text = str(symbol or "").strip().upper()
    if not text:
        return set()
    compact = text.replace(".", "").replace("-", "")
    digits = "".join(ch for ch in compact if ch.isdigit())
    keys = {text, compact}
    if len(digits) >= 6:
        code = digits[-6:]
        keys.update({code, f"SZ.{code}", f"SH.{code}"})
    return {item for item in keys if item}


def _normalized_symbol_code(symbol: Any) -> str:
    keys = _symbol_match_keys(symbol)
    code = next((item for item in keys if item.isdigit() and len(item) == 6), "")
    return code or str(symbol or "").strip().upper()


def _load_signal_first_attribution_index(db: Any) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    if db is None:
        return index
    for collection_name, domain in (
        ("concept_constituents", "concept"),
        ("board_constituents", "board"),
    ):
        try:
            rows = list(db[collection_name].find({}))
        except Exception:
            rows = []
        for row in rows:
            theme = str(
                row.get("concept_name")
                or row.get("board_name")
                or row.get("name")
                or ""
            ).strip()
            if not theme:
                continue
            row_symbols = set()
            for item in _string_list(row.get("symbols")):
                row_symbols.update(_symbol_match_keys(item))
            if not row_symbols:
                continue
            stock_count = int(_float(row.get("stock_count"), len(row_symbols)) or len(row_symbols) or 1)
            record = {
                "theme": theme[:24],
                "domain": domain,
                "stock_count": stock_count,
                "row_key": f"{domain}:{theme}:{stock_count}",
            }
            for key in row_symbols:
                index.setdefault(key, []).append(record)
    return index


def _infer_signal_first_attribution(
    attribution_index: Mapping[str, Sequence[Mapping[str, Any]]],
    symbols: Sequence[Any],
    *,
    fallback_theme: str,
) -> dict[str, Any]:
    normalized_symbols = [_normalized_symbol_code(symbol) for symbol in symbols]
    normalized_symbols = _unique_strings([symbol for symbol in normalized_symbols if symbol])
    if not normalized_symbols:
        return {
            "status": "insufficient_symbols",
            "primary_theme": fallback_theme,
            "domain": "technical",
            "confidence": 0.0,
            "support_count": 0,
            "matched_symbols": [],
            "candidates": [],
        }

    candidate_map: dict[str, dict[str, Any]] = {}
    for symbol in normalized_symbols:
        for key in _symbol_match_keys(symbol):
            for record in attribution_index.get(key, []):
                row_key = str(record.get("row_key") or f"{record.get('domain')}:{record.get('theme')}")
                item = candidate_map.setdefault(row_key, {
                    "theme": str(record.get("theme") or "")[:24],
                    "domain": str(record.get("domain") or ""),
                    "stock_count": int(record.get("stock_count") or 1),
                    "matched_symbols": [],
                })
                if symbol not in item["matched_symbols"]:
                    item["matched_symbols"].append(symbol)

    candidates: list[dict[str, Any]] = []
    for item in candidate_map.values():
        matched = _unique_strings(item.get("matched_symbols") or [])
        stock_count = int(item.get("stock_count") or 1)
        coverage = len(matched) / max(1, len(normalized_symbols))
        breadth_penalty = min(0.18, max(0, stock_count - 80) / 600)
        score = coverage * 0.82 + min(0.16, len(matched) * 0.04) - breadth_penalty
        candidates.append({
            "theme": str(item.get("theme") or "")[:24],
            "domain": str(item.get("domain") or ""),
            "score": _round(max(0.0, min(1.0, score)), 4),
            "support_count": len(matched),
            "stock_count": stock_count,
            "matched_symbols": matched[:12],
        })

    candidates = sorted(
        candidates,
        key=lambda item: (
            _float(item.get("score"), 0.0),
            int(item.get("support_count") or 0),
            -int(item.get("stock_count") or 0),
        ),
        reverse=True,
    )
    top = candidates[0] if candidates else {}
    if top:
        return {
            "status": "auto_attributed",
            "primary_theme": top["theme"],
            "domain": top["domain"],
            "confidence": top["score"],
            "support_count": top["support_count"],
            "matched_symbols": top["matched_symbols"],
            "candidates": candidates[:5],
            "fallback_theme": fallback_theme,
        }
    return {
        "status": "technical_only",
        "primary_theme": fallback_theme,
        "domain": "technical",
        "confidence": 0.0,
        "support_count": 0,
        "matched_symbols": [],
        "candidates": [],
    }


def _technical_signal_preview(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(row.get("symbol") or row.get("raw_code") or row.get("code") or ""),
        "name": str(row.get("name") or row.get("symbol_name") or ""),
        "freq": str(row.get("freq") or ""),
        "signal_type": str(row.get("signal_type") or row.get("type") or ""),
        "signal_side": str(row.get("signal_side") or row.get("side") or ""),
        "score": _round(_float(row.get("total_score") or row.get("score"), 0.0), 4),
        "confidence": _round(_float(row.get("confidence"), 0.0), 4),
        "as_of": str(row.get("as_of") or row.get("updated_at") or "")[:10],
        "source": SIGNAL_FIRST_COLLECTION,
    }


def _load_factor_docs(db: Any) -> list[dict[str, Any]]:
    if db is None:
        return []
    docs: list[dict[str, Any]] = []
    for collection_name in ("ai_factor_publications", "ai_factor_experiments"):
        try:
            cursor = db[collection_name].find({}).sort(
                [("updated_at", -1), ("last_verified_at", -1)]
            ).limit(50)
            for doc in cursor:
                normalized = _normalize_factor_doc(doc)
                if normalized:
                    docs.append(normalized)
        except Exception:
            continue
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in docs:
        key = str(item.get("factor_id") or item.get("title") or "")
        if key and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _load_experiment_ledger(db: Any) -> list[dict[str, Any]]:
    if db is None:
        return []
    try:
        cursor = db["ai_factor_experiment_ledger"].find({}).sort([("updated_at", -1)]).limit(20)
        return [_json_safe(dict(item)) for item in cursor]
    except Exception:
        return []


def _normalize_factor_doc(doc: Mapping[str, Any]) -> dict[str, Any]:
    factor_id = str(doc.get("factor_id") or doc.get("id") or doc.get("_id") or "").strip()
    title = str(doc.get("title") or doc.get("name") or factor_id).strip()
    if not factor_id and not title:
        return {}
    idea_text = str(doc.get("hypothesis") or doc.get("idea") or title or factor_id)
    metrics = _as_dict(doc.get("metrics") or doc.get("validation") or doc.get("verification"))
    draft = _as_dict(doc.get("draft") or doc.get("research") or doc.get("factor_draft") or doc.get("definition"))
    portfolio = _as_dict(doc.get("portfolio_construction"))
    if not portfolio or (
        _is_ai_hardware_idea(idea_text)
        and (
            not portfolio.get("us_driver_nodes")
            or not portfolio.get("cn_mapping_nodes")
            or _portfolio_uses_concept_placeholders(portfolio)
        )
    ):
        portfolio = _portfolio_construction_for_idea(idea_text)
    identity = _factor_identity_for_idea(idea_text)
    technical_confirmation = _as_dict(doc.get("technical_confirmation")) or _technical_confirmation_profile()
    return {
        "factor_id": factor_id or title,
        "version": str(doc.get("version") or doc.get("factor_version") or "v1"),
        "title": title,
        "hypothesis": idea_text,
        "research_mode": str(doc.get("research_mode") or doc.get("mode") or identity["research_mode"]),
        "factor_origin": str(doc.get("factor_origin") or doc.get("origin") or identity["factor_origin"]),
        "factor_family": _as_dict(doc.get("factor_family")) or identity["factor_family"],
        "industry_beta": _as_dict(doc.get("industry_beta")) or identity["industry_beta"],
        "expectation_alpha": _as_dict(doc.get("expectation_alpha")) or identity["expectation_alpha"],
        "technical_confirmation": technical_confirmation,
        "strategy_integration": _as_dict(doc.get("strategy_integration")) or _strategy_integration_profile(),
        "factor_exposures": _as_dict(doc.get("factor_exposures")) or _factor_exposures_for_factor({
            **identity,
            "portfolio_construction": portfolio,
        }),
        "validation_profile": _as_dict(doc.get("validation_profile")) or _validation_profile(),
        "risk_overlay_flags": _string_list(doc.get("risk_overlay_flags")) or _risk_overlay_flags_for_factor(doc or identity),
        "status": str(doc.get("status") or doc.get("publication_status") or "idea"),
        "approval_status": str(doc.get("approval_status") or ""),
        "live_enabled": bool(doc.get("live_enabled") or doc.get("enabled")),
        "updated_at": doc.get("updated_at") or doc.get("created_at") or "",
        "last_verified_at": doc.get("last_verified_at") or doc.get("verified_at") or "",
        "draft": _normalize_draft(draft),
        "research": _as_dict(doc.get("research")) or _normalize_draft(draft),
        "development": _as_dict(doc.get("development")),
        "research_workflow": _as_dict(doc.get("research_workflow")) or _research_workflow_for_idea(idea_text),
        "portfolio_construction": portfolio,
        "rhythm": _as_dict(doc.get("rhythm")),
        "reproducibility": _as_dict(doc.get("reproducibility")),
        "lifecycle": _as_dict(doc.get("lifecycle")),
        "paper_account": _as_dict(doc.get("paper_account")),
        "metrics": _normalize_metrics(metrics),
        "validation": _as_dict(doc.get("validation")),
        "feedback": _as_dict(doc.get("feedback")),
        "ai_explanation": _string_list(doc.get("ai_explanation") or doc.get("explanation")),
        "failure_samples": _as_list(doc.get("failure_samples")),
        "watchlist_symbols": _as_list(doc.get("watchlist_symbols") or doc.get("symbols")),
        "risk_tags": _string_list(doc.get("risk_tags")),
        "experiment_id": str(doc.get("experiment_id") or ""),
        "recorder_id": str(doc.get("recorder_id") or ""),
    }


def _sample_factor(now: datetime) -> dict[str, Any]:
    draft = create_factor_draft(db=None, persist=False)
    return {
        "factor_id": DEFAULT_FACTOR_ID,
        "version": "v1",
        "title": DEFAULT_FACTOR_TITLE,
        "hypothesis": "美股 AI 硬件链强势时，A股光模块、CPO、存储等高暴露标的存在次日或盘中联动机会。",
        "research_mode": draft["research_mode"],
        "factor_origin": draft["factor_origin"],
        "factor_family": draft["factor_family"],
        "industry_beta": draft["industry_beta"],
        "expectation_alpha": draft["expectation_alpha"],
        "technical_confirmation": draft["technical_confirmation"],
        "strategy_integration": draft["strategy_integration"],
        "factor_exposures": draft["factor_exposures"],
        "validation_profile": draft["validation_profile"],
        "risk_overlay_flags": draft["risk_overlay_flags"],
        "status": "idea",
        "approval_status": "not_requested",
        "live_enabled": False,
        "updated_at": now.isoformat(timespec="seconds"),
        "last_verified_at": "",
        "draft": _normalize_draft(draft["research"]),
        "development": draft["development"],
        "research_workflow": _default_research_workflow(),
        "portfolio_construction": _default_portfolio_construction(),
        "rhythm": {
            "mode": "not_run",
            "status": "pending_kline_fusion",
            "demo": False,
            "windows": _default_portfolio_construction().get("rhythm_windows", []),
            "multi_timeframe_map": _default_portfolio_construction().get("multi_timeframe_map", {}),
            "no_auto_order": True,
        },
        "reproducibility": _default_reproducibility(now),
        "lifecycle": {"state": "idea", "states": LIFECYCLE_STATES, "next_allowed": ["specified", "disabled"]},
        "paper_account": {"enabled": False, "mode": "observe_only", "no_auto_order": True},
        "metrics": {"verified": False},
        "validation": {"status": "not_run", "verified": False},
        "ai_explanation": [
            "AI 硬件是第一条行业因子研发链路，定位为行业 beta + AI 产业预期 alpha。",
            "当前尚未产生样本复盘结果。",
            "只有运行验证并人工批准后，才会进入策略页盘前池。",
        ],
        "failure_samples": [],
        "watchlist_symbols": [],
        "risk_tags": ["高开追价", "跨市场映射失效", "板块分化", "未来函数"],
    }


def _normalize_draft(draft: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "why_effective": str(draft.get("why_effective") or draft.get("why") or ""),
        "target_universe": str(draft.get("target_universe") or draft.get("universe") or ""),
        "trigger_condition": str(draft.get("trigger_condition") or draft.get("trigger") or ""),
        "avoid_condition": str(draft.get("avoid_condition") or draft.get("avoid") or ""),
        "invalidation_condition": str(draft.get("invalidation_condition") or draft.get("invalidation") or ""),
        "proof": str(draft.get("proof") or draft.get("historical_proof") or ""),
    }


def _portfolio_uses_concept_placeholders(portfolio: Mapping[str, Any]) -> bool:
    for item in _as_list(portfolio.get("cn_reaction_basket") or portfolio.get("reaction_basket")):
        symbols = item.get("symbols")
        if isinstance(symbols, list) and any(str(symbol).startswith("concept:") for symbol in symbols):
            return True
    return False


def _draft_research_from_idea(idea_text: str) -> dict[str, Any]:
    if _is_ai_hardware_idea(idea_text):
        portfolio = _portfolio_construction_for_idea(idea_text)
        cn_groups = _basket_groups(portfolio.get("cn_reaction_basket"))
        us_groups = _basket_groups(portfolio.get("us_trigger_basket"))
        target_universe = "A股" + "、".join(cn_groups) + "高暴露股票池。" if cn_groups else "A股 AI 硬件映射股票池。"
        trigger_groups = "、".join(us_groups) if us_groups else "美股 AI 硬件链"
        if _is_gb200_infra_idea(idea_text):
            return {
                "idea": idea_text,
                "why_effective": "GB200/AI server 订单变化会先反映在美股硬件链，再沿光互联、液冷热管理、铜互联、PCB/CCL、服务器和存储节点映射到 A股；有效性必须由隔夜触发、A股开盘承接和组内扩散共同确认。",
                "target_universe": target_universe,
                "trigger_condition": f"{trigger_groups} T 日强势后，A股 {('、'.join(cn_groups) or 'AI硬件映射')} 反应池 T+1 开盘不过热，并出现回踩承接或放量扩散。",
                "avoid_condition": "高开过热、一字涨停、单票孤立拉升、板块内部分化、指数宽度恶化、海外链反转或数据源过期。",
                "invalidation_condition": "美股 AI server 映射反转、A股映射支链组内退潮、个股跌破开盘承接位、失败样本集中出现高开低走。",
                "proof": "demo 模式生成可复盘样本结果；接入真实 observations 后复算 T+1/T+5/T+10/T+20、Rank IC、分位差、MFE/MAE 和失败样本。",
            }
        return {
            "idea": idea_text,
            "why_effective": "跨市场资金会沿 AI 硬件产业链寻找 A股映射，因子必须同时满足海外节点景气变化、A股产业链暴露和盘中承接。",
            "target_universe": target_universe,
            "trigger_condition": f"{trigger_groups} T 日收盘强势后，A股 {('、'.join(cn_groups) or '相关')} 池 T+1 开盘不过热，并出现回踩承接或盘中放量确认。",
            "avoid_condition": "一字涨停、明显缩量、板块内部分化、高开过热、指数宽度恶化或数据源过期。",
            "invalidation_condition": "美股映射反转、A股板块退潮、个股跌破关键承接位或流动性不足。",
            "proof": "等待 Signals 复现历史样本，验证 T+1/T+5/T+10/T+20、IC、分位收益、MFE/MAE 和失败样本。",
        }
    terms = _idea_terms(idea_text)
    theme = "、".join(terms[:3]) if terms else "该主题"
    return {
        "idea": idea_text,
        "why_effective": f"将「{idea_text}」拆成可复现触发源、A股反应篮子和承接确认，观察资金是否沿 {theme} 扩散。",
        "target_universe": f"A股 {theme} 相关股票池，并保留同产业链补涨、上游供给和下游应用候选。",
        "trigger_condition": f"触发源在 T 日出现政策、订单、价格、海外映射或资金强度后，A股 {theme} 篮子 T+1 不高开过热，并出现量价承接。",
        "avoid_condition": "一字涨停、缩量冲高、同组扩散失败、指数宽度恶化、流动性不足或数据源过期。",
        "invalidation_condition": f"{theme} 主线回落、触发源反转、个股跌破承接位、分位收益转负或失败样本集中出现。",
        "proof": "demo 模式先生成可见样本复盘结果；接入真实 observations 后复算 T+1/T+5/T+10/T+20、Rank IC、分位差、MAE 和失败样本。",
    }


def _research_workflow_for_idea(idea_text: str) -> dict[str, Any]:
    if _is_ai_hardware_idea(idea_text):
        portfolio = _portfolio_construction_for_idea(idea_text)
        us_groups = _basket_groups(portfolio.get("us_trigger_basket"))
        cn_groups = _basket_groups(portfolio.get("cn_reaction_basket"))
        if _is_gb200_infra_idea(idea_text):
            return {
                "czsc_signal_event_trade": {
                    "signals": [
                        {
                            "name": "us_gb200_server_chain_strength",
                            "layer": "signal",
                            "source": "US AI server trigger basket",
                            "definition": f"{('、'.join(us_groups) or '美股 AI server 触发篮子')} 相对 SOX/QQQ 的隔夜超额、上涨家数宽度和订单/新闻强度，T 日收盘后定格。",
                        },
                        {
                            "name": "cn_liquid_copper_pcb_acceptance",
                            "layer": "signal",
                            "source": "A-share liquid/copper/PCB reaction basket",
                            "definition": f"{('、'.join(cn_groups) or 'A股 AI硬件映射池')} T+1 开盘不过热，回踩承接或盘中放量扩散。",
                        },
                        {
                            "name": "risk_filter",
                            "layer": "signal_not",
                            "source": "market risk",
                            "definition": "海外映射反转、高开低走、组内分化、指数宽度恶化或未来函数边界不通过时禁止进入事件。",
                        },
                    ],
                    "event": {
                        "operate": "LO",
                        "signals_all": ["us_gb200_server_chain_strength", "cn_liquid_copper_pcb_acceptance"],
                        "signals_any": ["volume_confirmation", "opening_pullback_support", "theme_breadth_expansion"],
                        "signals_not": ["risk_filter"],
                        "next": "event true -> paper observation only",
                    },
                    "trade_observation": {
                        "position": "watch_pool_candidate",
                        "entry": "加入盘前池/盘中提醒，不自动下单",
                        "exit": "失效条件触发后停用或回到失败样本修正",
                    },
                },
                "vnpy_lifecycle": [
                    {"state": "created", "action": "保存 GB200 映射因子实例、触发篮子、反应池和参数", "gate": "factor_id 唯一，参数可复现"},
                    {"state": "inited", "action": "固定 US T close -> A股 T+1 as-of 边界并加载样本", "gate": "数据长度、as-of、成本、滑点通过"},
                    {"state": "validated", "action": "运行单因子验证", "gate": "样本数、Rank IC、分层收益、失败样本通过"},
                    {"state": "trading_false", "action": "进入 paper factor account 模拟观察", "gate": "只记录模拟持仓、收益、回撤和暴露"},
                    {"state": "disabled", "action": "失败样本或失效条件触发后停用", "gate": "不会污染策略页盘前池"},
                ],
                "quantaxis_local_simulation": {
                    "data": "Signals 本地数据快照；demo 模式使用确定性 GB200 映射样本，不替代真实回测。",
                    "account": "paper factor account；记录模拟持仓、收益、回撤、暴露和换手。",
                    "portfolio": "美股 AI 硬件篮子只做触发源，A股光互联/液冷/铜连接/PCB/服务器/存储反应池才做观察组合。",
                    "storage": "研究账本写入 ai_factor_experiment_ledger，进入观察池前同步 strategy_snapshot。",
                },
            }
        return _default_research_workflow()
    terms = _idea_terms(idea_text)
    theme = "、".join(terms[:2]) if terms else "idea basket"
    return {
        "czsc_signal_event_trade": {
            "signals": [
                {
                    "name": "idea_trigger_strength",
                    "layer": "signal",
                    "source": "dynamic trigger basket",
                    "definition": f"围绕「{idea_text}」提取政策、订单、价格、海外映射或新闻强度。",
                },
                {
                    "name": "cn_basket_acceptance",
                    "layer": "signal",
                    "source": "dynamic A-share reaction basket",
                    "definition": f"{theme} 反应篮子 T+1 开盘不过热，盘中有放量、回踩承接或组内扩散。",
                },
                {
                    "name": "risk_filter",
                    "layer": "signal_not",
                    "source": "market risk",
                    "definition": "高开低走、单票一字、指数宽度恶化、同组分化或未来函数边界不通过时禁止进入事件。",
                },
            ],
            "event": {
                "operate": "LO",
                "signals_all": ["idea_trigger_strength", "cn_basket_acceptance"],
                "signals_any": ["volume_confirmation", "opening_pullback_support", "theme_breadth_expansion"],
                "signals_not": ["risk_filter"],
                "next": "event true -> paper observation only",
            },
            "trade_observation": {
                "position": "watch_pool_candidate",
                "entry": "加入盘前池/盘中提醒，不自动下单",
                "exit": "失效条件触发后停用或回到研究修正",
            },
        },
        "vnpy_lifecycle": [
            {"state": "created", "action": "保存动态因子实例和参数", "gate": "factor_id 唯一，idea/basket 可复现"},
            {"state": "inited", "action": "加载或生成 demo 样本并计算指标", "gate": "样本、as-of、成本、滑点通过"},
            {"state": "validated", "action": "运行单因子验证", "gate": "样本数、Rank IC、分层收益、失败样本通过"},
            {"state": "trading_false", "action": "进入模拟观察", "gate": "只记录 paper account，不发真实委托"},
            {"state": "disabled", "action": "停用或回到修正", "gate": "失效条件、数据漂移或人工否决"},
        ],
        "quantaxis_local_simulation": {
            "data": "Signals 本地 Mongo/缓存数据快照；demo 模式使用确定性样本，不替代真实回测。",
            "account": "paper factor account；记录模拟持仓、收益、回撤、暴露和换手。",
            "portfolio": "触发篮子只做信号源，A股反应篮子才做观察组合。",
            "storage": "研究账本写入 ai_factor_experiment_ledger，进入观察池前同步 strategy_snapshot。",
        },
    }


def _portfolio_construction_for_idea(idea_text: str) -> dict[str, Any]:
    if _is_ai_hardware_idea(idea_text):
        return build_ai_hardware_portfolio(idea_text)
    terms = _idea_terms(idea_text)
    while len(terms) < 4:
        terms.append(["核心资产", "产业链上游", "产业链中游", "产业链下游"][len(terms)])
    trigger_basket = [
        {
            "group": "外部催化",
            "symbols": [f"event:{terms[0]}", "event:政策/订单/价格"],
            "weight": 0.35,
            "role": "捕捉 idea 的外部触发源和景气变化。",
        },
        {
            "group": "市场确认",
            "symbols": ["index:沪深300", "index:创业板指", "breadth:theme"],
            "weight": 0.25,
            "role": "过滤纯个股噪音，确认市场宽度和风险偏好。",
        },
        {
            "group": "同类映射",
            "symbols": [f"concept:{term}" for term in terms[1:3]],
            "weight": 0.25,
            "role": "观察相邻概念和产业链映射是否同步扩散。",
        },
        {
            "group": "新闻/资金强度",
            "symbols": ["signal:news_strength", "signal:money_flow"],
            "weight": 0.15,
            "role": "用新闻热度和资金流强度做二次确认。",
        },
    ]
    cn_reaction_basket = [
        {
            "group": term,
            "symbols": [f"concept:{term}", f"basket:{term}"],
            "weight": [0.35, 0.25, 0.20, 0.20][index],
            "role": f"观察 {term} 在 A股中的承接、扩散和失败样本。",
        }
        for index, term in enumerate(terms[:4])
    ]
    return {
        "us_trigger_basket": trigger_basket,
        "cn_reaction_basket": cn_reaction_basket,
        "trigger_basket": trigger_basket,
        "reaction_basket": cn_reaction_basket,
        "mapping_rule": f"将「{idea_text}」先拆成触发篮子，再映射到 A股反应篮子；A股必须用 T+1 开盘承接或盘中量价确认二次过滤。",
        "signal_formula": "idea_strength = 0.35*外部催化 + 0.25*市场确认 + 0.25*同类映射 + 0.15*新闻/资金强度；cn_score = 反应篮子权重 * T+1承接确认 * 放量确认。",
        "rebalance": "日频；T 日触发源定格，只允许影响 A股 T+1 及之后。",
        "portfolio_role": "触发篮子只负责方向和强度，反应篮子才进入观察池；两者都不是自动下单组合。",
    }


def _is_ai_hardware_idea(idea_text: str) -> bool:
    text = idea_text.lower()
    return any(token in text for token in (
        "ai 硬件", "ai硬件", "ai server", "gb200", "nvda", "英伟达", "amd", "avgo",
        "cohr", "coherent", "lite", "lumentum", "fn", "fabrinet", "aaoi", "cien",
        "vrt", "vertiv", "etn", "eaton", "nvt", "nvent", "smci", "dell", "hpe",
        "aph", "amphenol", "ttmi", "pcb", "铜连接", "液冷", "光器件",
        "cpo", "光模块", "hbm", "算力",
    ))


def _is_gb200_infra_idea(idea_text: str) -> bool:
    text = idea_text.lower()
    return any(token in text for token in (
        "gb200", "液冷", "热管理", "散热", "铜连接", "高速连接", "pcb", "ai server", "服务器",
        "vrt", "vertiv", "etn", "eaton", "nvt", "nvent", "smci", "dell", "hpe",
    ))


def _factor_title_from_idea(idea_text: str, factor_id: str) -> str:
    text = idea_text.lower()
    has_optical = any(token in text for token in (
        "lumentum", "lite", "coherent", "cohr", "fabrinet", "fn", "光模块", "光器件", "cpo",
    ))
    has_liquid = any(token in text for token in (
        "vertiv", "vrt", "eaton", "etn", "nvent", "nvt", "液冷", "热管理", "散热",
    ))
    if has_optical and has_liquid:
        return "美股光器件/液冷链 -> A股光模块/液冷联动因子"
    if has_liquid:
        return "美股数据中心液冷链 -> A股液冷/热管理联动因子"
    if _is_gb200_infra_idea(idea_text):
        return "GB200 AI服务器链 -> A股液冷/铜连接/PCB联动因子"
    if factor_id == DEFAULT_FACTOR_ID or _is_ai_hardware_idea(idea_text):
        return DEFAULT_FACTOR_TITLE
    return idea_text[:48]


def _idea_terms(idea_text: str, limit: int = 4) -> list[str]:
    known_terms = [
        "低空经济", "eVTOL", "无人机", "通航", "机器人", "人形机器人", "减速器", "伺服",
        "光模块", "CPO", "存储", "HBM", "PCB", "算力", "液冷", "数据中心",
        "券商", "银行", "保险", "地产", "煤炭", "有色", "黄金", "铜", "锂电", "固态电池",
        "光伏", "风电", "半导体", "芯片", "消费电子", "医药", "创新药", "军工", "卫星互联网",
    ]
    lowered = idea_text.lower()
    terms: list[str] = []
    for term in known_terms:
        if term.lower() in lowered and term not in terms:
            terms.append(term)
    for part in re.split(r"[\s,，、/；;:：。！？\-\+()（）]+", idea_text):
        token = part.strip()
        if not token or token in terms:
            continue
        if token in {"A股", "美股", "因子", "联动", "是否", "验证", "策略", "观察"}:
            continue
        if 2 <= len(token) <= 12:
            terms.append(token)
        if len(terms) >= limit:
            break
    return terms[:limit]


def _basket_symbols(basket: Any) -> list[str]:
    symbols: list[str] = []
    for item in _as_list(basket):
        raw_symbols = item.get("symbols")
        if isinstance(raw_symbols, list):
            symbols.extend(str(symbol) for symbol in raw_symbols if symbol)
    return _unique_strings(symbols)


def _basket_groups(basket: Any) -> list[str]:
    return _unique_strings(str(item.get("group") or "") for item in _as_list(basket))


def _default_research_workflow() -> dict[str, Any]:
    return {
        "czsc_signal_event_trade": {
            "signals": [
                {
                    "name": "us_ai_hardware_strength",
                    "layer": "signal",
                    "source": "US AI hardware ontology trigger basket",
                    "definition": "COHR/LITE/FN 光器件链、AVGO/ANET/MRVL 网络 ASIC、NVDA/AMD GPU、SMCI/DELL/HPE 服务器和 MU 存储节点相对 SOX/QQQ 的隔夜强度、上涨家数宽度和新闻订单强度。",
                },
                {
                    "name": "cn_opening_acceptance",
                    "layer": "signal",
                    "source": "A-share reaction basket",
                    "definition": "T+1 开盘不过热，回踩承接或盘中放量确认，且概念池内部扩散不塌缩。",
                },
                {
                    "name": "risk_filter",
                    "layer": "signal_not",
                    "source": "market risk",
                    "definition": "一字涨停、高开低走、板块退潮、指数宽度恶化、数据源过期时禁止进入事件。",
                },
            ],
            "event": {
                "operate": "LO",
                "signals_all": ["us_ai_hardware_strength", "cn_opening_acceptance"],
                "signals_any": ["volume_confirmation", "opening_pullback_support"],
                "signals_not": ["risk_filter"],
                "next": "event true -> paper observation only",
            },
            "trade_observation": {
                "position": "watch_pool_candidate",
                "entry": "加入盘前池/盘中提醒，不自动下单",
                "exit": "失效条件触发后停用或回到研究修正",
            },
        },
        "vnpy_lifecycle": [
            {"state": "created", "action": "保存因子实例和参数", "gate": "factor_id 唯一，参数可复现"},
            {"state": "inited", "action": "加载历史样本并计算指标", "gate": "数据长度、as-of、成本、滑点通过"},
            {"state": "validated", "action": "运行单因子验证", "gate": "样本数、IC、分层收益、失败样本通过"},
            {"state": "trading_false", "action": "进入模拟观察", "gate": "只记录 paper account，不发真实委托"},
            {"state": "disabled", "action": "停用或回到修正", "gate": "失效条件、数据漂移或人工否决"},
        ],
        "quantaxis_local_simulation": {
            "data": "Signals 本地 Mongo/缓存数据快照；美股/A股日历按 as-of 固定。",
            "account": "paper factor account；记录模拟持仓、收益、回撤、暴露和换手。",
            "portfolio": "美股篮子只做触发源，A股反应池才做观察组合。",
            "storage": "研究账本写入 ai_factor_experiment_ledger，进入观察池前同步 strategy_snapshot。",
        },
    }


def _default_portfolio_construction() -> dict[str, Any]:
    return build_ai_hardware_portfolio("美股 AI 硬件上涨后，A股光模块/CPO/液冷/存储是否有次日联动")


def _normalize_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    normalized = {
        "verified": bool(metrics.get("verified")),
        "sample_count": int(_float(metrics.get("sample_count") or metrics.get("samples"), 0) or 0),
        "win_rate": _float(metrics.get("win_rate")),
        "avg_return_t1": _float(metrics.get("avg_return_t1") or metrics.get("return_t1")),
        "avg_return_t5": _float(metrics.get("avg_return_t5") or metrics.get("return_t5")),
        "avg_return_t10": _float(metrics.get("avg_return_t10") or metrics.get("return_t10")),
        "avg_return_t20": _float(metrics.get("avg_return_t20") or metrics.get("return_t20")),
        "mfe": _float(metrics.get("mfe")),
        "mae": _float(metrics.get("mae")),
        "ic": _float(metrics.get("ic")),
        "rank_ic": _float(metrics.get("rank_ic")),
        "long_short_return": _float(metrics.get("long_short_return")),
        "t_plus_5": _float(metrics.get("t_plus_5") or metrics.get("avg_return_t5") or metrics.get("return_t5")),
        "long_short_quantile_spread": _float(metrics.get("long_short_quantile_spread") or metrics.get("long_short_return")),
        "turnover": _float(metrics.get("turnover")),
        "fitness": _float(metrics.get("fitness"), 0),
        "return_unit": str(metrics.get("return_unit") or "decimal"),
    }
    for key in ("quantile_returns", "group_returns"):
        if isinstance(metrics.get(key), Mapping):
            normalized[key] = dict(metrics[key])
    return normalized


def _summary(
    factors: list[dict[str, Any]],
    candidate_factor_ideas: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    candidate_factor_ideas = candidate_factor_ideas or []
    live = [item for item in factors if _is_live_factor(item)]
    verified = [item for item in factors if _as_dict(item.get("metrics")).get("verified")]
    mode_counts: dict[str, int] = {}
    for item in factors + candidate_factor_ideas:
        mode = str(item.get("research_mode") or item.get("mode") or "unknown")
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
    return {
        "total": len(factors),
        "verified": len(verified),
        "live_enabled": len(live),
        "draft": sum(1 for item in factors if item.get("status") in {"idea", "draft", "specified"}),
        "published": sum(1 for item in factors if item.get("status") == "published"),
        "requires_validation": sum(1 for item in factors if not _as_dict(item.get("metrics")).get("verified")),
        "candidate_factor_ideas": len(candidate_factor_ideas),
        "research_modes": mode_counts,
    }


def _is_live_factor(factor: Mapping[str, Any]) -> bool:
    status = str(factor.get("status") or "").lower()
    metrics = _as_dict(factor.get("metrics"))
    return (
        status == "published"
        and str(factor.get("approval_status") or "").lower() == "approved"
        and bool(factor.get("live_enabled"))
        and bool(metrics.get("verified"))
        and int(metrics.get("sample_count") or 0) > 0
    )


def _compute_single_factor_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"verified": False, "sample_count": 0, "return_unit": "decimal"}
    returns = {window: [_row_float(row, f"return_t{window}", f"forward_return_t{window}") for row in rows] for window in (1, 5, 10, 20)}
    primary_returns = [value for value in returns[5] if value is not None]
    factor_values = [_row_float(row, "factor_value", "score", "signal_strength") for row in rows]
    paired = [(factor_values[i], returns[5][i]) for i in range(len(rows)) if factor_values[i] is not None and returns[5][i] is not None]
    wins = [value for value in primary_returns if value > 0]
    mfes = [_row_float(row, "mfe", "max_favorable_excursion") for row in rows]
    maes = [_row_float(row, "mae", "max_adverse_excursion") for row in rows]
    quantile_returns = _quantile_returns(rows)
    long_short = None
    if "q1" in quantile_returns and "q5" in quantile_returns:
        long_short = quantile_returns["q5"] - quantile_returns["q1"]
    metrics = {
        "verified": True,
        "sample_count": len(rows),
        "win_rate": _round(len(wins) / len(primary_returns), 4) if primary_returns else None,
        "avg_return_t1": _avg(returns[1]),
        "avg_return_t5": _avg(returns[5]),
        "avg_return_t10": _avg(returns[10]),
        "avg_return_t20": _avg(returns[20]),
        "mfe": _avg(mfes),
        "mae": _avg(maes),
        "ic": _corr([x for x, _ in paired], [y for _, y in paired]),
        "rank_ic": _rank_corr([x for x, _ in paired], [y for _, y in paired]),
        "quantile_returns": quantile_returns,
        "long_short_return": _round(long_short, 4),
        "group_returns": _group_returns(rows),
        "turnover": _turnover(rows),
        "failure_sample_count": len(_failure_samples(rows)),
        "avg_abs_factor_exposure": _avg([abs(v) for v in factor_values if v is not None]),
        "fitness": _fitness(primary_returns, long_short),
        "return_unit": "decimal",
    }
    metrics["t_plus_5"] = metrics["avg_return_t5"]
    metrics["long_short_quantile_spread"] = metrics["long_short_return"]
    return {key: value for key, value in metrics.items() if value is not None}


def _enforce_repro_boundary(rows: Sequence[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    valid: list[Mapping[str, Any]] = []
    rejected: list[Mapping[str, Any]] = []
    for row in rows:
        us_date = _parse_date(row.get("us_signal_date") or row.get("us_as_of") or row.get("source_date"))
        cn_date = _parse_date(row.get("cn_trade_date") or row.get("trade_date") or row.get("date"))
        if us_date and cn_date and cn_date <= us_date:
            rejected.append(row)
            continue
        valid.append(row)
    return valid, rejected


def _failure_samples(rows: Sequence[Mapping[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    ranked = sorted(
        [row for row in rows if _row_float(row, "return_t5", "forward_return_t5") is not None],
        key=lambda row: _row_float(row, "return_t5", "forward_return_t5") or 0.0,
    )
    failures = []
    for row in ranked[:limit]:
        ret = _row_float(row, "return_t5", "forward_return_t5")
        if ret is None or ret >= 0:
            continue
        failures.append({
            "case_id": str(row.get("case_id") or f"{row.get('symbol', 'basket')}:{row.get('trade_date', '')}"),
            "symbol": str(row.get("symbol") or row.get("basket") or ""),
            "title": str(row.get("title") or "T+5 负收益样本"),
            "occurred_at": str(row.get("cn_trade_date") or row.get("trade_date") or row.get("date") or ""),
            "return_t5": _round(ret, 4),
            "reason": str(row.get("failure_reason") or row.get("reason") or "触发后未获得 A股承接。"),
        })
    return failures


def _watchlist_from_rows(rows: Sequence[Mapping[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        ret = _row_float(row, "return_t5", "forward_return_t5") or 0.0
        score = _row_float(row, "factor_value", "score", "signal_strength") or 0.0
        current = best.get(symbol)
        if current is None or ret > _float(current.get("return_t5"), -999):
            best[symbol] = {
                "symbol": symbol,
                "name": str(row.get("name") or symbol),
                "score": _round(max(score * 100, ret * 100), 2),
                "return_t5": _round(ret, 4),
                "group": str(row.get("group") or row.get("industry") or row.get("concept") or ""),
            }
    return sorted(best.values(), key=lambda item: _float(item.get("return_t5"), 0), reverse=True)[:limit]


def _feedback(metrics: Mapping[str, Any], rejected_count: int) -> dict[str, Any]:
    if not metrics.get("verified"):
        return {
            "summary": "样本不足，不能发布到盘前池。",
            "next_mutation": "补齐历史观察样本后再运行验证。",
            "blocked_by": ["not_enough_samples"],
        }
    blockers = []
    if rejected_count:
        blockers.append("future_leak_rows_rejected")
    if _float(metrics.get("rank_ic"), 0) < 0.03:
        blockers.append("rank_ic_weak")
    if _float(metrics.get("long_short_return"), 0) <= 0:
        blockers.append("quantile_spread_not_positive")
    return {
        "summary": "验证完成，可进入人工复核；是否发布取决于 Rank IC、分位收益和失败样本边界。",
        "next_mutation": "优先检查失败样本中的高开低走和指数风险覆盖，收窄触发条件。",
        "blocked_by": blockers,
    }


def _ledger_doc(
    factor: Mapping[str, Any],
    *,
    stage: str,
    status: str,
    metrics: Mapping[str, Any],
    artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    now = naive_market_now("A")
    factor_id = str(factor.get("factor_id") or DEFAULT_FACTOR_ID)
    return {
        "_id": str(factor.get("recorder_id") or _recorder_id(str(factor.get("experiment_id") or factor_id), stage)),
        "experiment_id": str(factor.get("experiment_id") or _experiment_id(factor_id, stage, "")),
        "recorder_id": str(factor.get("recorder_id") or _recorder_id(str(factor.get("experiment_id") or factor_id), stage)),
        "factor_id": factor_id,
        "factor_version": str(factor.get("version") or "v1"),
        "stage": stage,
        "status": status,
        "params": {
            "lifecycle": factor.get("lifecycle", {}),
            "reproducibility": factor.get("reproducibility", {}),
        },
        "metrics": dict(metrics),
        "artifacts": dict(artifacts),
        "tags": {
            "qlib_recorder_style": True,
            "rd_agent_loop": "research_development_feedback",
            "alphalens_style": True,
            "no_auto_order": True,
        },
        "updated_at": now,
    }


def _factory_actions(active_factor_id: str) -> list[dict[str, Any]]:
    factor_id = active_factor_id or DEFAULT_FACTOR_ID
    return [
        _action("pack:signals:ai_factor:draft", "生成因子草稿", {"factor_id": factor_id}),
        _action("pack:signals:ai_factor:rhythm", "融合K线节奏", {"factor_id": factor_id}),
        _action("pack:signals:ai_factor:validate", "运行验证", {"factor_id": factor_id}),
        _action("pack:signals:ai_factor:publish", "加入观察池", {"factor_id": factor_id, "live_enabled": True}),
        _action("pack:signals:ai_factor:disable", "停用因子", {"factor_id": factor_id}),
    ]


def _action(action_id: str, label: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "action_id": action_id,
        "run_id": "ai_factor_factory",
        "kind": "signals_api",
        "label": label,
        "payload": dict(payload),
        "metadata": {"workspace": "ai_factor_factory"},
    }


def _rhythm_demo_from_portfolio(portfolio: Mapping[str, Any]) -> dict[str, Any]:
    windows = _as_list(portfolio.get("rhythm_windows"))
    if not windows:
        windows = [
            {"window_id": "us_overnight_close", "label": "昨夜美股", "market": "US", "timeframe": "daily/60m/15m"},
            {"window_id": "cn_call_auction", "label": "今日竞价", "market": "A", "timeframe": "集合竞价"},
            {"window_id": "cn_open_30m", "label": "开盘30分钟", "market": "A", "timeframe": "5m/30m"},
            {"window_id": "cn_intraday_confirm", "label": "盘中确认", "market": "A", "timeframe": "5m/30m"},
            {"window_id": "cn_close_review", "label": "收盘复盘", "market": "A", "timeframe": "daily/30m"},
        ]
    demo_windows = []
    labels = ["美股尾盘加速", "竞价不过热", "开盘回踩承接", "同链扩散确认", "收盘进入成功样本"]
    for index, window in enumerate(windows):
        row = _as_dict(window)
        demo_windows.append({
            **row,
            "status": "demo_pass" if index != 2 else "demo_watch",
            "demo_observation": labels[index] if index < len(labels) else "demo observation",
            "kline_marker": {
                "type": "event" if index != 2 else "watch",
                "label": labels[index] if index < len(labels) else "demo",
            },
        })

    drivers = _as_list(portfolio.get("us_driver_nodes"))
    mappings = _as_list(portfolio.get("cn_mapping_nodes"))
    first_driver = _as_dict(drivers[0] if drivers else {})
    first_mapping = _as_dict(mappings[0] if mappings else {})
    cn_candidates = _as_list(first_mapping.get("top_candidates") or first_mapping.get("core_candidates"))
    cn_symbol = _as_dict(cn_candidates[0] if cn_candidates else {})
    us_symbols = _string_list(first_driver.get("symbols"))
    us_symbol = us_symbols[0] if us_symbols else ""

    return {
        "mode": "demo",
        "demo": True,
        "status": "rhythm_simulated",
        "no_auto_order": True,
        "selected_us_driver": {
            "node_id": first_driver.get("node_id") or "",
            "name": first_driver.get("name") or "",
            "symbol": us_symbol,
        },
        "selected_cn_mapping": {
            "source_node_id": first_mapping.get("source_node_id") or "",
            "group": first_mapping.get("group") or "",
            "symbol": cn_symbol.get("symbol") or "",
            "name": cn_symbol.get("name") or "",
        },
        "windows": demo_windows,
        "path_samples": [
            {
                "case_id": "demo:success:optical_or_liquid:001",
                "path": "美股主驱动强 -> A股竞价不过热 -> 开盘30分钟承接 -> 盘中同链扩散",
                "outcome": "success_observation",
                "us_symbol": us_symbol,
                "cn_symbol": cn_symbol.get("symbol") or "",
                "cn_name": cn_symbol.get("name") or "",
                "demo": True,
            },
            {
                "case_id": "demo:failure:gap_fade:001",
                "path": "美股强 -> A股高开过热 -> 开盘30分钟高开低走",
                "outcome": "failure_boundary",
                "failure_reason": "高开低走和组内分化时，不能只凭美股强度进入观察账户。",
                "demo": True,
            },
        ],
    }


def _demo_validation_samples(factor_id: str, draft: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Deterministic demo samples for the frontend workflow.

    These rows are intentionally marked as demo observations.  They exercise
    the same validation path as real observations, including future-leak
    rejection, but they are not a substitute for production data.
    """
    idea_text = str(draft.get("hypothesis") or draft.get("title") or "")
    if _is_gb200_infra_idea(idea_text):
        rows = [
            ("2026-03-18", "2026-03-19", "SZ.002837", "英维克", "液冷/散热", 0.14, -0.006, -0.026, -0.018, -0.010, 0.018, -0.052, "高开后组内未扩散，午后资金回流光模块。"),
            ("2026-03-21", "2026-03-24", "SH.688629", "华丰科技", "铜连接/高速连接器", 0.27, 0.002, -0.008, 0.004, 0.012, 0.032, -0.035, "海外触发强但 A股高开过热，T+5 未兑现。"),
            ("2026-03-25", "2026-03-26", "SZ.002463", "沪电股份", "PCB/服务器材料", 0.41, 0.006, 0.014, 0.026, 0.031, 0.054, -0.021, ""),
            ("2026-03-28", "2026-03-31", "SZ.300476", "胜宏科技", "PCB/服务器材料", 0.55, 0.011, 0.027, 0.039, 0.048, 0.071, -0.018, ""),
            ("2026-04-02", "2026-04-03", "SZ.300502", "新易盛", "光模块/CPO", 0.68, 0.016, 0.041, 0.057, 0.066, 0.093, -0.015, ""),
            ("2026-04-07", "2026-04-08", "SZ.300308", "中际旭创", "光模块/CPO", 0.82, 0.023, 0.058, 0.071, 0.086, 0.118, -0.014, ""),
            ("2026-04-10", "2026-04-13", "SZ.300394", "天孚通信", "光模块/CPO", 0.94, 0.031, 0.076, 0.091, 0.112, 0.146, -0.010, ""),
            ("2026-04-15", "2026-04-15", "SZ.999999", "未来函数样本", "bad", 0.99, 0.050, 0.220, 0.260, 0.300, 0.320, -0.004, "US T close 不能影响 A股 T 日。"),
        ]
    else:
        terms = _idea_terms(idea_text)
        theme = terms[0] if terms else "主题"
        rows = [
            ("2026-03-18", "2026-03-19", "SZ.000001", f"{theme}失败样本A", theme, 0.12, -0.004, -0.018, -0.014, -0.006, 0.020, -0.050, "开盘承接不足，组内扩散失败。"),
            ("2026-03-22", "2026-03-23", "SZ.000002", f"{theme}失败样本B", theme, 0.28, 0.000, -0.004, 0.006, 0.010, 0.028, -0.036, "触发源有效但 A股高开过热。"),
            ("2026-03-25", "2026-03-26", "SZ.000003", f"{theme}样本C", theme, 0.43, 0.006, 0.012, 0.023, 0.028, 0.048, -0.022, ""),
            ("2026-03-29", "2026-03-30", "SZ.000004", f"{theme}样本D", theme, 0.58, 0.011, 0.026, 0.034, 0.043, 0.063, -0.018, ""),
            ("2026-04-02", "2026-04-03", "SZ.000005", f"{theme}样本E", theme, 0.76, 0.019, 0.044, 0.055, 0.069, 0.087, -0.014, ""),
            ("2026-04-08", "2026-04-09", "SZ.000006", f"{theme}样本F", theme, 0.91, 0.027, 0.066, 0.082, 0.104, 0.133, -0.012, ""),
            ("2026-04-12", "2026-04-12", "SZ.999999", "未来函数样本", "bad", 0.99, 0.050, 0.200, 0.250, 0.300, 0.310, -0.004, "T 日触发源不能影响 T 日 A股。"),
        ]
    return [
        {
            "factor_id": factor_id,
            "us_signal_date": us_date,
            "cn_trade_date": cn_date,
            "symbol": symbol,
            "name": name,
            "group": group,
            "factor_value": factor_value,
            "return_t1": return_t1,
            "return_t5": return_t5,
            "return_t10": return_t10,
            "return_t20": return_t20,
            "mfe": mfe,
            "mae": mae,
            "failure_reason": reason,
            "sample_mode": "demo",
        }
        for us_date, cn_date, symbol, name, group, factor_value, return_t1, return_t5,
        return_t10, return_t20, mfe, mae, reason in rows
    ]


def _validation_artifact(
    metrics: Mapping[str, Any],
    valid_rows: Sequence[Mapping[str, Any]],
    rejected_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    failure_samples = _failure_samples(valid_rows)
    rejected_future_leak = [_sample_preview(row) for row in rejected_rows[:8]]
    for sample in rejected_future_leak:
        sample["reason"] = "rejected_future_leak_boundary"
    return {
        "metrics": dict(metrics),
        "sample_count": int(metrics.get("sample_count") or 0),
        "win_rate": metrics.get("win_rate"),
        "T+5": metrics.get("avg_return_t5"),
        "t_plus_5": metrics.get("t_plus_5") or metrics.get("avg_return_t5"),
        "rank_ic": metrics.get("rank_ic"),
        "long_short_quantile_spread": metrics.get("long_short_quantile_spread") or metrics.get("long_short_return"),
        "mae": metrics.get("mae"),
        "sample_preview": [_sample_preview(row) for row in valid_rows[:8]],
        "failure_samples": failure_samples,
        "rejected_future_leak": {
            "count": len(rejected_rows),
            "samples": rejected_future_leak,
            "boundary": REPRO_BOUNDARY,
        },
        "paper_account": _paper_account_from_rows(valid_rows, metrics),
    }


def _sample_preview(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "us_signal_date": str(row.get("us_signal_date") or row.get("us_as_of") or ""),
        "cn_trade_date": str(row.get("cn_trade_date") or row.get("trade_date") or ""),
        "symbol": str(row.get("symbol") or ""),
        "name": str(row.get("name") or row.get("symbol") or ""),
        "group": str(row.get("group") or row.get("industry") or row.get("concept") or ""),
        "factor_value": _round(_row_float(row, "factor_value", "score", "signal_strength"), 4),
        "return_t5": _round(_row_float(row, "return_t5", "forward_return_t5"), 4),
        "sample_mode": str(row.get("sample_mode") or ""),
        "reason": str(row.get("failure_reason") or row.get("reason") or ""),
    }


def _paper_account_from_rows(rows: Sequence[Mapping[str, Any]], metrics: Mapping[str, Any]) -> dict[str, Any]:
    if not metrics.get("verified"):
        return {
            "enabled": False,
            "mode": "observe_only",
            "no_auto_order": True,
            "equity_curve": [],
            "positions": [],
            "trades": [],
        }
    starting_cash = 1_000_000.0
    equity = starting_cash
    peak = starting_cash
    max_drawdown = 0.0
    equity_curve: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    weighted_exposures: list[float] = []
    exposure_by_group: dict[str, float] = {}
    sorted_rows = sorted(
        rows,
        key=lambda row: str(row.get("cn_trade_date") or row.get("trade_date") or row.get("date") or ""),
    )
    for row in sorted_rows:
        factor_value = _row_float(row, "factor_value", "score", "signal_strength") or 0.0
        return_t5 = _row_float(row, "return_t5", "forward_return_t5") or 0.0
        target_weight = min(0.16, max(0.04, factor_value * 0.14))
        pnl = equity * target_weight * return_t5
        equity += pnl
        peak = max(peak, equity)
        drawdown = (equity / peak - 1.0) if peak else 0.0
        max_drawdown = min(max_drawdown, drawdown)
        weighted_exposures.append(target_weight)
        trade_date = str(row.get("cn_trade_date") or row.get("trade_date") or row.get("date") or "")
        group = str(row.get("group") or row.get("industry") or row.get("concept") or "ungrouped")
        exposure_by_group[group] = exposure_by_group.get(group, 0.0) + target_weight
        equity_curve.append({
            "date": trade_date,
            "equity": _round(equity, 2),
            "drawdown": _round(drawdown, 4),
            "paper_pnl": _round(pnl, 2),
        })
        trades.append({
            "date": trade_date,
            "symbol": str(row.get("symbol") or ""),
            "name": str(row.get("name") or row.get("symbol") or ""),
            "group": group,
            "target_weight": _round(target_weight, 4),
            "return_t5": _round(return_t5, 4),
            "paper_pnl": _round(pnl, 2),
            "action": "observe",
        })
    positions = [
        {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "group": item.get("group"),
            "target_weight": _round(min(0.16, max(0.04, _float(item.get("score"), 0) / 100 * 0.14)), 4),
            "return_t5": item.get("return_t5"),
        }
        for item in _watchlist_from_rows(rows, limit=6)
    ]
    return {
        "enabled": True,
        "mode": "paper_observation",
        "no_auto_order": True,
        "starting_cash": _round(starting_cash, 2),
        "ending_equity": _round(equity, 2),
        "total_return": _round(equity / starting_cash - 1.0, 4),
        "max_drawdown": _round(max_drawdown, 4),
        "gross_exposure": _avg(weighted_exposures),
        "exposure": {
            "gross": _avg(weighted_exposures),
            "net": _avg(weighted_exposures),
            "by_group": {key: _round(value, 4) for key, value in exposure_by_group.items()},
        },
        "turnover": _round(metrics.get("turnover"), 4),
        "equity_curve": equity_curve,
        "curve": equity_curve,
        "positions": positions,
        "holdings": positions,
        "trades": trades[:12],
    }


def _default_reproducibility(now: datetime) -> dict[str, Any]:
    return {
        "calendar": "A-share trading calendar",
        "as_of_boundary": REPRO_BOUNDARY,
        "data_snapshot": f"signals-local:{trading_day_key('A', now=now)}",
        "cost_model": {"open_cost": 0.0005, "close_cost": 0.0015, "min_cost": 5},
        "slippage": {"model": "fixed_bps", "bps": 5},
        "benchmark": "沪深300",
        "code_boundary": "factor_definition + validation_samples + Signals backtest engine refs",
    }


def _latest_factor_doc(db: Any, factor_id: str) -> dict[str, Any] | None:
    if db is None:
        return None
    for collection_name in ("ai_factor_publications", "ai_factor_experiments"):
        try:
            doc = db[collection_name].find_one({"factor_id": factor_id})
            if doc:
                return dict(doc)
        except Exception:
            continue
    return None


def _load_validation_samples(db: Any, factor_id: str) -> list[dict[str, Any]]:
    if db is None:
        return []
    try:
        return [dict(item) for item in db["ai_factor_validation_samples"].find({"factor_id": factor_id})]
    except Exception:
        return []


def _data_snapshot(rows: Sequence[Mapping[str, Any]], now: datetime) -> str:
    if not rows:
        return f"signals-local:{trading_day_key('A', now=now)}:empty"
    dates = sorted(str(row.get("cn_trade_date") or row.get("trade_date") or row.get("date") or "") for row in rows)
    digest = hashlib.sha1("|".join(dates).encode("utf-8")).hexdigest()[:12]
    return f"signals-local:{dates[0]}:{dates[-1]}:{digest}"


def _experiment_id(factor_id: str, stage: str, seed: str) -> str:
    digest = hashlib.sha1(f"{factor_id}:{stage}:{seed}".encode("utf-8")).hexdigest()[:12]
    return f"exp:{factor_id}:{stage}:{digest}"


def _recorder_id(experiment_id: str, stage: str) -> str:
    digest = hashlib.sha1(f"{experiment_id}:{stage}".encode("utf-8")).hexdigest()[:10]
    return f"rec:{stage}:{digest}"


def _safe_factor_id(value: str) -> str:
    text = value.strip()
    if text == DEFAULT_FACTOR_ID:
        return DEFAULT_FACTOR_ID
    if not text:
        return DEFAULT_FACTOR_ID
    allowed = []
    for ch in text.lower().replace(" ", "_"):
        allowed.append(ch if ch.isalnum() or ch == "_" else "_")
    slug = "_".join(part for part in "".join(allowed).split("_") if part)
    if not slug:
        slug = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    if len(slug) > 72:
        slug = slug[:59] + "_" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return slug


def _get_db_or_none() -> Any:
    try:
        from signals.data.mongo_fallback import get_db

        return get_db()
    except Exception:
        return None


def _upsert(db: Any, collection_name: str, query: Mapping[str, Any], doc: Mapping[str, Any]) -> None:
    try:
        db[collection_name].update_one(dict(query), {"$set": dict(doc)}, upsert=True)
    except Exception:
        return


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return []


def _unique_strings(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _float(value: Any, default: Any = None) -> Any:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _row_float(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float(row.get(key))
        if value is not None:
            return value
    return None


def _avg(values: Sequence[float | None]) -> float | None:
    filtered = [value for value in values if value is not None and math.isfinite(value)]
    if not filtered:
        return None
    return _round(sum(filtered) / len(filtered), 4)


def _corr(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return _round(cov / math.sqrt(var_x * var_y), 4)


def _rank_corr(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 2:
        return None
    return _corr(_ranks(xs), _ranks(ys))


def _ranks(values: Sequence[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j + 2) / 2
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = avg_rank
        i = j + 1
    return ranks


def _quantile_returns(rows: Sequence[Mapping[str, Any]], buckets: int = 5) -> dict[str, float]:
    paired = [
        (value, ret)
        for row in rows
        for value, ret in [(_row_float(row, "factor_value", "score", "signal_strength"), _row_float(row, "return_t5", "forward_return_t5"))]
        if value is not None and ret is not None
    ]
    if not paired:
        return {}
    paired.sort(key=lambda item: item[0])
    result: dict[str, float] = {}
    for index, (_, ret) in enumerate(paired):
        bucket = min(buckets, int(index * buckets / len(paired)) + 1)
        result.setdefault(f"q{bucket}", 0.0)
    for bucket in list(result):
        bucket_items = [
            ret for index, (_, ret) in enumerate(paired)
            if f"q{min(buckets, int(index * buckets / len(paired)) + 1)}" == bucket
        ]
        result[bucket] = _round(sum(bucket_items) / len(bucket_items), 4)
    return result


def _group_returns(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        group = str(row.get("group") or row.get("industry") or row.get("concept") or "ungrouped")
        ret = _row_float(row, "return_t5", "forward_return_t5")
        if ret is not None:
            grouped.setdefault(group, []).append(ret)
    return {key: _round(sum(vals) / len(vals), 4) for key, vals in grouped.items() if vals}


def _turnover(rows: Sequence[Mapping[str, Any]]) -> float | None:
    by_date: dict[str, set[str]] = {}
    for row in rows:
        trade_date = str(row.get("cn_trade_date") or row.get("trade_date") or row.get("date") or "")
        symbol = str(row.get("symbol") or row.get("basket") or "")
        if trade_date and symbol:
            by_date.setdefault(trade_date, set()).add(symbol)
    dates = sorted(by_date)
    if len(dates) < 2:
        return None
    turns = []
    for prev, curr in zip(dates, dates[1:]):
        prev_set = by_date[prev]
        curr_set = by_date[curr]
        base = len(prev_set | curr_set)
        if base:
            turns.append(len(prev_set ^ curr_set) / base)
    return _avg(turns)


def _fitness(primary_returns: Sequence[float], long_short: float | None) -> float:
    if not primary_returns:
        return 0.0
    avg_return = sum(primary_returns) / len(primary_returns)
    win_rate = len([item for item in primary_returns if item > 0]) / len(primary_returns)
    spread = long_short or 0.0
    return _round(max(0.0, min(100.0, 40 * win_rate + 300 * avg_return + 200 * spread)), 2)


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except Exception:
        return None


def _round(value: Any, ndigits: int = 4) -> Any:
    if value is None:
        return None
    try:
        return round(float(value), ndigits)
    except Exception:
        return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items() if key != "_id"}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return value
