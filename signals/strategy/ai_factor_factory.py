# -*- coding: utf-8 -*-
"""AI factor research kernel and read model.

This module keeps AI factor research separate from ``autoresearch``.  AI may
turn a trader's idea into a structured factor and feedback loop, but every
number shown to Agent OS must come from a validation artifact.
"""
from __future__ import annotations

import hashlib
import math
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
    idea_text = idea.strip() or "美股 AI 硬件上涨后，A股光模块/CPO/存储是否有次日联动"
    factor_id = _safe_factor_id(factor_id or idea_text)
    version = "v1"
    experiment_id = _experiment_id(factor_id, "research", idea_text)
    recorder_id = _recorder_id(experiment_id, "draft")
    doc = {
        "_id": factor_id,
        "factor_id": factor_id,
        "version": version,
        "title": DEFAULT_FACTOR_TITLE if factor_id == DEFAULT_FACTOR_ID else idea_text[:48],
        "hypothesis": idea_text,
        "status": "specified",
        "approval_status": "not_requested",
        "live_enabled": False,
        "updated_at": now,
        "last_verified_at": "",
        "research": {
            "idea": idea_text,
            "why_effective": "跨市场资金会沿 AI 硬件产业链寻找 A股映射，因子必须同时满足海外景气变化、A股产业链暴露和盘中承接。",
            "target_universe": "A股光模块、CPO、存储/HBM、PCB、算力链高暴露股票池。",
            "trigger_condition": "美股 AI 硬件链 T 日收盘强势后，A股相关池 T+1 开盘不过热，并出现回踩承接或盘中放量确认。",
            "avoid_condition": "一字涨停、明显缩量、板块内部分化、高开过热、指数宽度恶化或数据源过期。",
            "invalidation_condition": "美股映射反转、A股板块退潮、个股跌破关键承接位或流动性不足。",
            "proof": "等待 Signals 复现历史样本，验证 T+1/T+5/T+10/T+20、IC、分位收益、MFE/MAE 和失败样本。",
        },
        "development": {
            "factor_definition": {
                "inputs": ["NVDA", "AMD", "AVGO", "SMCI", "SOX", "QQQ"],
                "mapped_universe": ["光模块", "CPO", "存储/HBM", "PCB", "算力链"],
                "signal_layer": "US AI hardware overnight strength",
                "factor_layer": "A-share industry-chain mapped exposure plus opening acceptance",
                "event_layer": "cross_market_ai_hardware_mapping_confirmed",
                "trade_observation_layer": "pre-market pool / intraday alert only",
            },
            "czsc_layering": {
                "signal": "atomic cross-market and intraday acceptance signals",
                "factor": "linear combination of signals with explicit weights",
                "event": "same-class factor merge into observable event",
                "trade": "manual review or paper observation; never automatic order",
            },
        },
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
        "reproducibility": doc["reproducibility"],
    })
    if persist and db is not None:
        _upsert(db, "ai_factor_experiments", {"factor_id": factor_id}, doc)
        _upsert(db, "ai_factor_experiment_ledger", {"recorder_id": recorder_id}, ledger)
    return _json_safe({**doc, "ledger": ledger})


def run_factor_validation(
    *,
    factor_id: str = DEFAULT_FACTOR_ID,
    observations: Sequence[Mapping[str, Any]] | None = None,
    db: Any = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Run single-factor validation from structured observations."""
    now = naive_market_now("A")
    db = db if db is not None else _get_db_or_none()
    factor_id = _safe_factor_id(factor_id or DEFAULT_FACTOR_ID)
    draft = _latest_factor_doc(db, factor_id) or create_factor_draft(
        factor_id=factor_id,
        db=db,
        persist=persist,
    )
    rows = list(observations or _load_validation_samples(db, factor_id))
    valid_rows, rejected_rows = _enforce_repro_boundary(rows)
    metrics = _compute_single_factor_metrics(valid_rows)
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
            "sample_count": int(metrics.get("sample_count") or 0),
            "rejected_sample_count": len(rejected_rows),
            "rejected_reason": "future_leak_boundary" if rejected_rows else "",
            "engine_refs": [
                "signals.core.backtest.SignalJournal",
                "signals.core.backtest.ForwardEvaluator",
                "signals.core.backtest.BacktestReport",
            ],
        },
        "failure_samples": _failure_samples(valid_rows),
        "watchlist_symbols": _watchlist_from_rows(valid_rows),
        "feedback": _feedback(metrics, len(rejected_rows)),
        "reproducibility": {
            **_as_dict(draft.get("reproducibility")),
            "data_snapshot": _data_snapshot(valid_rows, now),
            "rejected_future_leak_rows": len(rejected_rows),
        },
        "paper_account": {
            **_as_dict(draft.get("paper_account")),
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
    metrics = _as_dict(doc.get("metrics") or doc.get("validation") or doc.get("verification"))
    draft = _as_dict(doc.get("draft") or doc.get("research") or doc.get("factor_draft") or doc.get("definition"))
    return {
        "factor_id": factor_id or title,
        "version": str(doc.get("version") or doc.get("factor_version") or "v1"),
        "title": title,
        "hypothesis": str(doc.get("hypothesis") or doc.get("idea") or ""),
        "status": str(doc.get("status") or doc.get("publication_status") or "idea"),
        "approval_status": str(doc.get("approval_status") or ""),
        "live_enabled": bool(doc.get("live_enabled") or doc.get("enabled")),
        "updated_at": doc.get("updated_at") or doc.get("created_at") or "",
        "last_verified_at": doc.get("last_verified_at") or doc.get("verified_at") or "",
        "draft": _normalize_draft(draft),
        "research": _as_dict(doc.get("research")) or _normalize_draft(draft),
        "development": _as_dict(doc.get("development")),
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
