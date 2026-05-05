# -*- coding: utf-8 -*-
"""AI factor research kernel and read model.

This module keeps AI factor research separate from ``autoresearch``.  AI may
turn a trader's idea into a structured factor and feedback loop, but every
number shown to Agent OS must come from a validation artifact.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from signals.core.market_time import naive_market_now
from signals.core.trading_dates import trading_day_key

DEFAULT_FACTOR_ID = "us_ai_hardware_to_cn_optical_cpo_memory_v1"
DEFAULT_FACTOR_TITLE = "美股 AI 硬件 -> A股光模块/CPO/存储联动因子"
REPRO_BOUNDARY = "US T close is allowed to affect A-share T+1 and later only."
LIFECYCLE_STATES = ["idea", "draft", "specified", "validated", "observable", "published", "disabled"]


def build_ai_factor_factory(*, db: Any = None, include_sample: bool = True) -> dict[str, Any]:
    """Return the AI factor factory read model consumed by Agent OS."""
    now = naive_market_now("A")
    trade_date = trading_day_key("A", now=now)
    db = db if db is not None else _get_db_or_none()
    factors = _load_factor_docs(db)
    if not factors and include_sample:
        factors = [_sample_factor(now)]

    active_factor_id = str(factors[0].get("factor_id") or "") if factors else ""
    return _json_safe({
        "factory_id": f"ai-factor-factory:{trade_date}",
        "as_of": trade_date,
        "generated_at": now.isoformat(timespec="seconds"),
        "title": "AI因子工厂",
        "summary": _summary(factors),
        "active_factor_id": active_factor_id,
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
            ],
            "research_loop": "RD-Agent style Research -> Development -> Feedback",
            "experiment_ledger": "Qlib-style experiment/recorder run ledger",
            "single_factor_validation": "Alphalens-style IC, quantiles, group returns, turnover",
            "repro_boundary": REPRO_BOUNDARY,
            "live_gate": "Only published + approved + live_enabled + verified factors can enter strategy_snapshot.",
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
    default_idea = "美股 AI 硬件上涨后，A股光模块/CPO/存储是否有次日联动"
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
    doc = {
        "_id": factor_id,
        "factor_id": factor_id,
        "version": version,
        "title": _factor_title_from_idea(idea_text, factor_id),
        "hypothesis": idea_text,
        "status": "specified",
        "approval_status": "not_requested",
        "live_enabled": False,
        "updated_at": now,
        "last_verified_at": "",
        "draft": _normalize_draft(research),
        "research": research,
        "development": {
            "factor_definition": {
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
        "reproducibility": _default_reproducibility(now),
        "lifecycle": {
            "state": "specified",
            "states": LIFECYCLE_STATES,
            "next_allowed": ["validated", "disabled"],
            "live_gate": "published + approved + live_enabled + metrics.verified",
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
            candidates.append({
                "symbol": symbol,
                "name": name,
                "display_name": f"{name} · AI因子",
                "score": _float(symbol_item.get("score"), _float(metrics.get("fitness"), 0.0)),
                "status": "watch",
                "decision_stage": "strategy_candidate",
                "reason": draft.get("why_effective") or factor.get("hypothesis") or factor.get("title") or "",
                "recommended_action": "进入盘前池观察，等待触发条件与风险线确认",
                "missing_gates": ["intraday_trigger_confirmation", "manual_risk_review"],
                "primary_blocker": "",
                "promotion_path": ["AI因子验证", "盘前池观察", "盘中触发复核"],
                "metadata": {
                    "source": "ai_factor_factory",
                    "factor_id": factor.get("factor_id", ""),
                    "factor_version": factor.get("version", ""),
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
    return {
        "factor_id": factor_id or title,
        "version": str(doc.get("version") or doc.get("factor_version") or "v1"),
        "title": title,
        "hypothesis": idea_text,
        "status": str(doc.get("status") or doc.get("publication_status") or "idea"),
        "approval_status": str(doc.get("approval_status") or ""),
        "live_enabled": bool(doc.get("live_enabled") or doc.get("enabled")),
        "updated_at": doc.get("updated_at") or doc.get("created_at") or "",
        "last_verified_at": doc.get("last_verified_at") or doc.get("verified_at") or "",
        "draft": _normalize_draft(draft),
        "research": _as_dict(doc.get("research")) or _normalize_draft(draft),
        "development": _as_dict(doc.get("development")),
        "research_workflow": _as_dict(doc.get("research_workflow")) or _research_workflow_for_idea(idea_text),
        "portfolio_construction": _as_dict(doc.get("portfolio_construction")) or _portfolio_construction_for_idea(idea_text),
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
    return {
        "factor_id": DEFAULT_FACTOR_ID,
        "version": "v1",
        "title": DEFAULT_FACTOR_TITLE,
        "hypothesis": "美股 AI 硬件链强势时，A股光模块、CPO、存储等高暴露标的存在次日或盘中联动机会。",
        "status": "idea",
        "approval_status": "not_requested",
        "live_enabled": False,
        "updated_at": now.isoformat(timespec="seconds"),
        "last_verified_at": "",
        "draft": _normalize_draft(create_factor_draft(db=None, persist=False)["research"]),
        "development": create_factor_draft(db=None, persist=False)["development"],
        "research_workflow": _default_research_workflow(),
        "portfolio_construction": _default_portfolio_construction(),
        "reproducibility": _default_reproducibility(now),
        "lifecycle": {"state": "idea", "states": LIFECYCLE_STATES, "next_allowed": ["specified", "disabled"]},
        "paper_account": {"enabled": False, "mode": "observe_only", "no_auto_order": True},
        "metrics": {"verified": False},
        "validation": {"status": "not_run", "verified": False},
        "ai_explanation": [
            "这是样板因子想法，尚未产生验证 artifact。",
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


def _draft_research_from_idea(idea_text: str) -> dict[str, Any]:
    if _is_ai_hardware_idea(idea_text):
        if _is_gb200_infra_idea(idea_text):
            return {
                "idea": idea_text,
                "why_effective": "GB200/AI server 订单变化会先反映在美股硬件链，再沿散热、铜互联、PCB 和光互联映射到 A股；有效性必须由隔夜触发、A股开盘承接和组内扩散共同确认。",
                "target_universe": "A股液冷、铜连接/高速连接器、PCB、光模块/CPO、服务器电源和算力基础设施高暴露股票池。",
                "trigger_condition": "NVDA/AVGO/SMCI/ANET/DELL 等美股 AI server 链 T 日强势后，A股液冷/铜连接/PCB/CPO 反应池 T+1 开盘不过热，并出现回踩承接或放量扩散。",
                "avoid_condition": "高开过热、一字涨停、单票孤立拉升、板块内部分化、指数宽度恶化、海外链反转或数据源过期。",
                "invalidation_condition": "美股 AI server 映射反转、A股液冷/铜连接/PCB 组内退潮、个股跌破开盘承接位、失败样本集中出现高开低走。",
                "proof": "demo 模式生成可复现验证 artifact；接入真实 observations 后复算 T+1/T+5/T+10/T+20、Rank IC、分位差、MFE/MAE 和失败样本。",
            }
        return {
            "idea": idea_text,
            "why_effective": "跨市场资金会沿 AI 硬件产业链寻找 A股映射，因子必须同时满足海外景气变化、A股产业链暴露和盘中承接。",
            "target_universe": "A股光模块、CPO、存储/HBM、PCB、算力链高暴露股票池。",
            "trigger_condition": "美股 AI 硬件链 T 日收盘强势后，A股相关池 T+1 开盘不过热，并出现回踩承接或盘中放量确认。",
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
        "proof": "demo 模式先生成可见验证 artifact；接入真实 observations 后复算 T+1/T+5/T+10/T+20、Rank IC、分位差、MAE 和失败样本。",
    }


def _research_workflow_for_idea(idea_text: str) -> dict[str, Any]:
    if _is_ai_hardware_idea(idea_text):
        if _is_gb200_infra_idea(idea_text):
            return {
                "czsc_signal_event_trade": {
                    "signals": [
                        {
                            "name": "us_gb200_server_chain_strength",
                            "layer": "signal",
                            "source": "US AI server trigger basket",
                            "definition": "NVDA/AVGO/SMCI/ANET/DELL 相对 SOX/QQQ 的隔夜超额、上涨家数宽度和订单/新闻强度，T 日收盘后定格。",
                        },
                        {
                            "name": "cn_liquid_copper_pcb_acceptance",
                            "layer": "signal",
                            "source": "A-share liquid/copper/PCB reaction basket",
                            "definition": "液冷、铜连接/高速连接器、PCB、光模块/CPO 反应池 T+1 开盘不过热，回踩承接或盘中放量扩散。",
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
                    "portfolio": "美股 GB200/AI server 篮子只做触发源，A股液冷/铜连接/PCB/CPO 反应池才做观察组合。",
                    "storage": "实验账本写入 ai_factor_experiment_ledger，发布门禁写入 strategy_snapshot 前。",
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
            "storage": "实验账本写入 ai_factor_experiment_ledger，发布门禁写入 strategy_snapshot 前。",
        },
    }


def _portfolio_construction_for_idea(idea_text: str) -> dict[str, Any]:
    if _is_ai_hardware_idea(idea_text):
        if _is_gb200_infra_idea(idea_text):
            return {
                "us_trigger_basket": [
                    {
                        "group": "GPU/GB200",
                        "symbols": ["NVDA", "AMD"],
                        "weight": 0.30,
                        "role": "AI server 需求和估值锚。",
                    },
                    {
                        "group": "网络/ASIC",
                        "symbols": ["AVGO", "ANET"],
                        "weight": 0.25,
                        "role": "GB200 集群网络、交换和专用芯片景气。",
                    },
                    {
                        "group": "服务器/OEM",
                        "symbols": ["SMCI", "DELL", "HPE"],
                        "weight": 0.25,
                        "role": "AI server 订单、交付和散热配置变化。",
                    },
                    {
                        "group": "指数校准",
                        "symbols": ["SOX", "QQQ"],
                        "weight": 0.20,
                        "role": "剔除半导体/纳指 beta 后的相对强度。",
                    },
                ],
                "cn_reaction_basket": [
                    {
                        "group": "液冷/散热",
                        "symbols": ["concept:液冷", "concept:数据中心散热"],
                        "weight": 0.30,
                        "role": "GB200 功耗提升对应散热方案升级映射。",
                    },
                    {
                        "group": "铜连接/高速连接器",
                        "symbols": ["concept:铜连接", "concept:高速连接器"],
                        "weight": 0.25,
                        "role": "机柜内高速互联和铜缆替代/补充光互联映射。",
                    },
                    {
                        "group": "PCB/服务器材料",
                        "symbols": ["concept:PCB", "concept:AI服务器材料"],
                        "weight": 0.25,
                        "role": "服务器、交换机和加速卡材料侧弹性。",
                    },
                    {
                        "group": "光模块/CPO",
                        "symbols": ["concept:光模块", "concept:CPO"],
                        "weight": 0.20,
                        "role": "集群网络高速光互联映射。",
                    },
                ],
                "mapping_rule": "美股 GB200/AI server 触发篮子先算相对强度，再映射到 A股液冷、铜连接、PCB、光模块反应池；A股必须用 T+1 开盘承接或盘中量价确认二次过滤。",
                "signal_formula": "us_strength = 0.40*AI server等权超额 + 0.25*SOX超额 + 0.20*上涨家数宽度 + 0.15*订单/新闻强度；cn_score = 产业链暴露权重 * T+1承接确认 * 放量扩散。",
                "rebalance": "日频；US T 日收盘定格，美股信号只允许影响 A股 T+1 及之后。",
                "portfolio_role": "触发篮子只负责方向和强度，反应篮子才进入观察池；两者都不是自动下单组合。",
            }
        return _default_portfolio_construction()
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
    return any(token in text for token in ("ai 硬件", "ai硬件", "nvda", "英伟达", "cpo", "光模块", "hbm", "算力"))


def _is_gb200_infra_idea(idea_text: str) -> bool:
    text = idea_text.lower()
    return any(token in text for token in ("gb200", "液冷", "铜连接", "高速连接", "pcb", "ai server", "服务器"))


def _factor_title_from_idea(idea_text: str, factor_id: str) -> str:
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
                    "source": "US trigger basket",
                    "definition": "NVDA/AMD/AVGO/SMCI/ANET 相对 SOX/QQQ 的隔夜强度、上涨家数宽度和新闻订单强度。",
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
            "storage": "实验账本写入 ai_factor_experiment_ledger，发布门禁写入 strategy_snapshot 前。",
        },
    }


def _default_portfolio_construction() -> dict[str, Any]:
    return {
        "us_trigger_basket": [
            {
                "group": "GPU/加速卡",
                "symbols": ["NVDA", "AMD"],
                "weight": 0.35,
                "role": "AI 算力需求和估值锚",
            },
            {
                "group": "ASIC/网络芯片",
                "symbols": ["AVGO", "ANET"],
                "weight": 0.25,
                "role": "AI 集群网络与专用芯片景气",
            },
            {
                "group": "服务器/OEM",
                "symbols": ["SMCI", "DELL"],
                "weight": 0.20,
                "role": "AI server 订单和交付弹性",
            },
            {
                "group": "指数校准",
                "symbols": ["SOX", "QQQ"],
                "weight": 0.20,
                "role": "剔除半导体/纳指 beta 后的相对强度",
            },
        ],
        "cn_reaction_basket": [
            {
                "group": "光模块/CPO",
                "symbols": ["concept:光模块", "concept:CPO"],
                "weight": 0.35,
                "role": "海外 AI capex 对高速光互联的映射",
            },
            {
                "group": "存储/HBM",
                "symbols": ["concept:存储芯片", "concept:HBM"],
                "weight": 0.25,
                "role": "AI 服务器存储链映射",
            },
            {
                "group": "PCB/高速连接",
                "symbols": ["concept:PCB", "concept:高速连接器"],
                "weight": 0.20,
                "role": "服务器和交换机材料侧映射",
            },
            {
                "group": "算力基础设施",
                "symbols": ["concept:液冷", "concept:数据中心", "concept:算力租赁"],
                "weight": 0.20,
                "role": "A股本地算力链弹性和情绪扩散",
            },
        ],
        "mapping_rule": "美股触发篮子先算相对强度，再映射到 A股产业链暴露；A股必须用 T+1 开盘承接或盘中量价确认二次过滤。",
        "signal_formula": "us_strength = 0.45*AI硬件等权超额 + 0.25*SOX超额 + 0.20*上涨家数宽度 + 0.10*订单/新闻强度；cn_score = 产业链暴露权重 * T+1承接确认 * 放量确认。",
        "rebalance": "日频；US T 日收盘定格，美股信号只允许影响 A股 T+1 及之后。",
        "portfolio_role": "触发篮子只负责方向和强度，反应篮子才进入观察池；两者都不是自动下单组合。",
    }


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


def _summary(factors: list[dict[str, Any]]) -> dict[str, Any]:
    live = [item for item in factors if _is_live_factor(item)]
    verified = [item for item in factors if _as_dict(item.get("metrics")).get("verified")]
    return {
        "total": len(factors),
        "verified": len(verified),
        "live_enabled": len(live),
        "draft": sum(1 for item in factors if item.get("status") in {"idea", "draft", "specified"}),
        "published": sum(1 for item in factors if item.get("status") == "published"),
        "requires_validation": sum(1 for item in factors if not _as_dict(item.get("metrics")).get("verified")),
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
