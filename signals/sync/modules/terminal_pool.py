# -*- coding: utf-8 -*-
"""Build the next-session explainable realtime universe for the terminal."""
from __future__ import annotations

import logging
import os
from collections import Counter
from typing import Any

from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.sync.task_context import get_task_env

logger = logging.getLogger("signals.sync.terminal_pool")

SELL_TOKENS = ("卖", "顶", "风险", "死叉", "减仓", "跌破", "预警")
CHAN_TOKENS = ("一买", "二买", "三买", "一卖", "二卖", "三卖", "背驰", "中枢", "笔", "线段", "趋势")
PATTERN_TOKENS = ("头肩", "双底", "双头", "三角形")
MACD_TOKENS = ("MACD", "零上绿柱扩大", "零下绿柱缩小")
GAP_TOKENS = ("缺口", "突破缺口", "持续缺口", "衰竭缺口", "普通缺口")
ENTRY_FACTOR_TOKENS = ("gap", "trend_breakout", "vol_contraction", "candle_run", "candle_accel")
TECHNICAL_TOKENS = CHAN_TOKENS + PATTERN_TOKENS + MACD_TOKENS + GAP_TOKENS + ENTRY_FACTOR_TOKENS
RIGHT_SIDE_FREQS = {"5分钟", "5min", "5m", "15分钟", "15min", "15m"}
BUY_FREQ_BONUS = {"30分钟": 120, "30min": 120, "30m": 120, "15分钟": 110, "15min": 110, "15m": 110, "5分钟": 80, "5min": 80, "5m": 80}
ENTRY_30M_FREQS = {"30分钟", "30min", "30m", "F30", "f30"}
ENTRY_PARTNER_FREQS = {"日线", "daily", "1d", "D", "d", "周线", "weekly", "1w", "W", "w", "15分钟", "15min", "15m", "F15", "f15"}
RISK_ACTION_STATUSES = {"risk_review", "chain_risk_review", "knowledge_blocked", "knowledge_conflict"}
POOL_RANKING_VERSION = "entry_risk_watch_v1"
FREQ_ORDER = {
    "周线": 0,
    "weekly": 0,
    "1w": 0,
    "日线": 1,
    "daily": 1,
    "1d": 1,
    "30分钟": 2,
    "30min": 2,
    "30m": 2,
    "15分钟": 3,
    "15min": 3,
    "15m": 3,
    "5分钟": 4,
    "5min": 4,
    "5m": 4,
}
REASON_WEIGHTS = {
    "user_pinned": 180,
    "technical_trigger": 880,
    "generated_risk_signal": 950,
    "historical_signal_record": 0,
    "custom_signal": 0,
    "chan_signal": 0,
    "knowledge_confirmed": 0,
    "knowledge_conflict": 0,
    "knowledge_watch": 0,
    "chain_context": 0,
    "chain_core_rep": 0,
    "chain_elastic_rep": 0,
    "source_leader": 0,
    "constituent_hot": 0,
    "active_pool_watch": 260,
    "recent_opened": 180,
    "fallback_watch": 160,
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _freq_sort_key(freq: Any) -> tuple[int, str]:
    text = _text(freq)
    return FREQ_ORDER.get(text, FREQ_ORDER.get(text.lower(), 99)), text


def _is_30m_freq(freq: Any) -> bool:
    return _text(freq) in ENTRY_30M_FREQS


def _is_entry_partner_freq(freq: Any) -> bool:
    return _text(freq) in ENTRY_PARTNER_FREQS


def _pure_a_code(symbol: Any) -> str:
    raw = _text(symbol).upper()
    if not raw:
        return ""
    pure = raw.split(".", 1)[-1] if "." in raw else raw
    pure = pure.replace("SH", "").replace("SZ", "").replace("BJ", "")
    return pure if pure.isdigit() and len(pure) == 6 else ""


def _prefixed_symbol(code: str) -> str:
    if not code:
        return ""
    if code.startswith(("6", "9")):
        return f"SH.{code}"
    if code.startswith(("4", "8")):
        return f"BJ.{code}"
    return f"SZ.{code}"


def _add_stock(stocks: list[str], value: Any, *, index_codes: set[str]) -> None:
    code = _pure_a_code(value)
    if code and code not in index_codes and code not in stocks:
        stocks.append(code)


def _index_codes() -> set[str]:
    import config

    return {
        _pure_a_code(symbol)
        for symbol in getattr(config, "INDEX_AK_CODES", {}).values()
        if _pure_a_code(symbol)
    }


def _signal_text(row: dict[str, Any]) -> str:
    return " ".join(_text(row.get(key)) for key in ("signal_type", "type", "reason", "summary", "details"))


def _source(row: dict[str, Any]) -> str:
    return _text(row.get("source") or row.get("data_source")).lower()


def _is_historical_signal_source(row: dict[str, Any]) -> bool:
    source = _source(row)
    return source.startswith("sqlite.backtest.signal_records") or source.startswith("historical_signal_record")


def _signal_side(row: dict[str, Any]) -> str:
    status = _text(row.get("pool_status") or row.get("status") or row.get("direction")).lower()
    text = _signal_text(row)
    if status in {"warning", "sell", "risk"} or any(token in text for token in SELL_TOKENS):
        return "sell"
    return "buy"


def _signal_family(row: dict[str, Any]) -> str:
    text = _signal_text(row)
    if any(token in text for token in CHAN_TOKENS):
        return "chan_style"
    return "custom_or_system"


def _reason_type_for_signal(row: dict[str, Any]) -> str:
    source = _source(row)
    side = _signal_side(row)
    if _is_historical_signal_source(row):
        return "historical_signal_record"
    if "czsc" in source or "chan" in source:
        return "technical_trigger"
    if source.startswith("sync.signal_pool.generated") or side == "sell":
        return "generated_risk_signal"
    return "custom_signal"


def _is_generated_daily_signal(row: dict[str, Any]) -> bool:
    source = _source(row)
    signal_type = _text(row.get("signal_type") or row.get("type") or row.get("reason"))
    freq = _text(row.get("freq") or row.get("timeframe"))
    return (
        source.startswith("sync.signal_pool.generated")
        or signal_type.startswith("日线候选")
        or signal_type.startswith("日线预警")
        or (freq == "日线" and source.startswith("sync.signal_pool"))
    )


def _is_hard_screen_signal(row: dict[str, Any]) -> bool:
    source = _source(row)
    if _is_historical_signal_source(row) or source.startswith("sync.signal_pool.generated"):
        return False
    if "czsc" in source or "chan" in source:
        return True
    signal_type = _signal_text(row)
    return any(token in signal_type for token in TECHNICAL_TOKENS)


def _resonance_grade(aligned_freqs: list[str], conflict_freqs: list[str]) -> str:
    if conflict_freqs:
        return "conflict"
    if len(aligned_freqs) >= 3:
        return "strong_resonance"
    if len(aligned_freqs) >= 2:
        return "multi_period"
    return "single_period"


def _screen_resonance_context(signal: dict[str, Any], sibling_signals: list[dict[str, Any]]) -> dict[str, Any]:
    side = _signal_side(signal)
    primary_freq = _text(signal.get("freq") or signal.get("timeframe"))
    aligned_freqs = sorted(
        {_text(item.get("freq") or item.get("timeframe")) for item in sibling_signals if _is_hard_screen_signal(item) and _signal_side(item) == side},
        key=_freq_sort_key,
    )
    conflict_freqs = sorted(
        {_text(item.get("freq") or item.get("timeframe")) for item in sibling_signals if _is_hard_screen_signal(item) and _signal_side(item) != side},
        key=_freq_sort_key,
    )
    aligned_freqs = [item for item in aligned_freqs if item]
    conflict_freqs = [item for item in conflict_freqs if item]
    grade = _resonance_grade(aligned_freqs, conflict_freqs)
    tags: list[str] = []
    if grade == "conflict":
        tags.append("周期冲突")
    if grade in {"multi_period", "strong_resonance"}:
        tags.append("多周期共振")
    if grade == "strong_resonance":
        tags.append("强共振")
    if "周线" in aligned_freqs and "日线" in aligned_freqs:
        tags.append("日周同向")
    if any(freq in {"5分钟", "5min", "5m"} for freq in aligned_freqs):
        tags.append("5m确认")
    if not tags:
        tags.append("硬技术")
    side_text = "买点" if side == "buy" else "风险"
    return {
        "direction": side,
        "primary_freq": primary_freq,
        "aligned_freqs": aligned_freqs or ([primary_freq] if primary_freq else []),
        "conflict_freqs": conflict_freqs,
        "grade": grade,
        "tags": tags[:5],
        "summary": f"{side_text}筛选信号：{','.join(aligned_freqs or [primary_freq])}",
        "latest_dt": _text(signal.get("signal_date") or signal.get("updated_at"))[:10],
    }


def _right_side_confirmed(aligned_freqs: list[str]) -> bool:
    return any(freq in RIGHT_SIDE_FREQS for freq in aligned_freqs)


def _technical_actionability(side: str, resonance_context: dict[str, Any], freq: str = "") -> tuple[str, str]:
    if side == "sell":
        return "risk_exit_first", "risk_exit_first"
    grade = _text(resonance_context.get("grade"))
    aligned_freqs = [str(item) for item in resonance_context.get("aligned_freqs") or [] if item]
    if not aligned_freqs and freq:
        aligned_freqs = [freq]
    conflict_freqs = [str(item) for item in resonance_context.get("conflict_freqs") or [] if item]
    if grade == "conflict" or conflict_freqs:
        return "review_required", "context_only"
    if len(aligned_freqs) < 2:
        return "observe_only", "context_only"
    if not _right_side_confirmed(aligned_freqs):
        return "entry_waiting_confirm", "entry_waiting_confirm"
    return "entry_ready", "entry_ready"


def _chain_decision_effect(phase: str) -> str:
    if phase == "accelerating":
        return "confirm"
    if phase in {"consensus_climax", "risk_off"}:
        return "exit_priority"
    if phase in {"diverging", "cooling"}:
        return "block"
    return "context_only"


def _is_technical_reason(reason: dict[str, Any]) -> bool:
    return _text(reason.get("reason_type")) in {"technical_trigger", "technical_signal"}


def _source_doc_id(row: dict[str, Any]) -> str:
    return _text(row.get("_id") or row.get("dedupe_key") or row.get("action_id") or row.get("decision_id"))


def _reason_key(reason: dict[str, Any]) -> str:
    return "|".join([
        _text(reason.get("reason_type")),
        _text(reason.get("source_collection")),
        _text(reason.get("source_doc_id")),
        _text(reason.get("signal_type")),
        _text(reason.get("freq")),
        _text(reason.get("board_or_concept")),
    ])


def _empty_row(code: str, name: str = "") -> dict[str, Any]:
    symbol = _prefixed_symbol(code)
    return {
        "symbol": symbol,
        "code": symbol,
        "raw_code": code,
        "name": name,
        "kind": "stock",
        "score": 0.0,
        "sort_score": 0.0,
        "signal_origin": "",
        "signal_family": "",
        "latest_signal": "",
        "action_status": "watch",
        "trader_action": "观察",
        "next_action": "观察",
        "actionability": "context_only",
        "queue_lane": "context_only",
        "decision_effect": "context_only",
        "source_role": "context",
        "invalidates_when": "入池条件失效或产业链热度回落",
        "technical_evidence": {},
        "resonance_context": {},
        "knowledge_confirmation": {"status": "none"},
        "chain_context": {},
        "inclusion_reasons": [],
        "source_tags": [],
        "target_kind": "stock",
        "target_label": symbol,
        "target_symbol": symbol,
        "target_freq": "30min",
    }


def _add_reason(rows: dict[str, dict[str, Any]], value: Any, reason: dict[str, Any], *, index_codes: set[str], name: str = "") -> None:
    code = _pure_a_code(value)
    if not code or code in index_codes:
        return
    if code not in rows and reason.get("can_create_candidate") is False:
        return
    row = rows.setdefault(code, _empty_row(code, name))
    if name and not row.get("name"):
        row["name"] = name
    reason_type = _text(reason.get("reason_type"))
    base_weight = REASON_WEIGHTS.get(reason_type, 100)
    freq = _text(reason.get("freq"))
    signal_side = _text(reason.get("signal_side"))
    side_bonus = 180 if signal_side == "sell" else 0
    decision_effect = _text(reason.get("decision_effect"))
    if not decision_effect:
        decision_effect = "exit_priority" if reason_type == "technical_trigger" and signal_side == "sell" else ("confirm" if reason_type == "technical_trigger" else "context_only")
    source_role = _text(reason.get("source_role")) or ("technical_trigger" if reason_type == "technical_trigger" else "context")
    actionability = _text(reason.get("actionability"))
    queue_lane = _text(reason.get("queue_lane"))
    context_only = decision_effect in {"context_only", "history_pending"} or source_role == "context"
    weight = 0.0 if context_only else base_weight + BUY_FREQ_BONUS.get(freq, 0) + side_bonus + _float(reason.get("score")) * 0.05 + _float(reason.get("heat_score")) * 0.05
    normalized = {
        "reason_type": reason_type,
        "weight": round(weight, 3),
        "source_role": source_role,
        "decision_effect": decision_effect,
        "actionability": actionability,
        "queue_lane": queue_lane,
        "source_collection": _text(reason.get("source_collection")),
        "source_doc_id": _text(reason.get("source_doc_id")),
        "signal_type": _text(reason.get("signal_type")),
        "signal_side": signal_side,
        "signal_family": _text(reason.get("signal_family")),
        "freq": freq,
        "score": _float(reason.get("score")),
        "confidence": _float(reason.get("confidence")),
        "chain_id": _text(reason.get("chain_id")),
        "node_id": _text(reason.get("node_id")),
        "board_or_concept": _text(reason.get("board_or_concept")),
        "as_of": _text(reason.get("as_of")),
        "evidence": reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {},
        "resonance_context": reason.get("resonance_context") if isinstance(reason.get("resonance_context"), dict) else {},
        "knowledge_status": _text(reason.get("knowledge_status")),
        "knowledge_effect": _text(reason.get("knowledge_effect")),
        "backtest_quality": reason.get("backtest_quality") if isinstance(reason.get("backtest_quality"), dict) else {},
    }
    key = _reason_key(normalized)
    existing_keys = {_reason_key(item) for item in row["inclusion_reasons"]}
    if key not in existing_keys:
        row["inclusion_reasons"].append(normalized)
    row["inclusion_reasons"].sort(key=lambda item: _float(item.get("weight")), reverse=True)
    top = row["inclusion_reasons"][0]
    row["sort_score"] = max(_float(row.get("sort_score")), _float(top.get("weight")))
    row["score"] = max(_float(row.get("score")), _float(reason.get("score")), _float(reason.get("heat_score")))
    row["signal_origin"] = top["reason_type"]
    row["signal_family"] = top.get("signal_family") or row.get("signal_family") or ""
    if top.get("signal_type"):
        row["latest_signal"] = top["signal_type"]
    if top.get("chain_id"):
        row["chain_context"] = {
            "chain_id": top.get("chain_id"),
            "node_id": top.get("node_id"),
            "board_or_concept": top.get("board_or_concept"),
        }
    source_tag = reason_type
    if source_tag and source_tag not in row["source_tags"]:
        row["source_tags"].append(source_tag)
    if _is_technical_reason(normalized):
        resonance_context = normalized["resonance_context"]
        if not resonance_context and isinstance(normalized["evidence"], dict):
            resonance_context = normalized["evidence"].get("resonance_context") or {}
        actionability, queue_lane = _technical_actionability(normalized["signal_side"], resonance_context, normalized["freq"])
        normalized["actionability"] = normalized.get("actionability") or actionability
        normalized["queue_lane"] = normalized.get("queue_lane") or queue_lane
        normalized["decision_effect"] = "exit_priority" if queue_lane == "risk_exit_first" else ("confirm" if queue_lane in {"entry_ready", "entry_waiting_confirm"} else "context_only")
        row["technical_evidence"] = {
            "source_collection": normalized["source_collection"],
            "source_doc_id": normalized["source_doc_id"],
            "signal_type": normalized["signal_type"],
            "signal_side": normalized["signal_side"],
            "freq": normalized["freq"],
            "score": normalized["score"],
            "confidence": normalized["confidence"],
            "as_of": normalized["as_of"],
            "evidence": normalized["evidence"],
            "resonance_context": resonance_context,
            "actionability": normalized["actionability"],
            "queue_lane": normalized["queue_lane"],
        }
        if resonance_context:
            row["resonance_context"] = resonance_context
        row["actionability"] = normalized["actionability"]
        row["queue_lane"] = normalized["queue_lane"]
        row["source_role"] = "technical_trigger"
        row["decision_effect"] = normalized["decision_effect"]
    if reason_type.startswith("knowledge_"):
        row["knowledge_confirmation"] = {
            "status": normalized.get("knowledge_status") or reason_type.replace("knowledge_", ""),
            "effect": normalized.get("knowledge_effect") or normalized.get("decision_effect") or "context_only",
            "sentiment": _text(reason.get("sentiment")),
            "source_collection": normalized["source_collection"],
            "source_doc_id": normalized["source_doc_id"],
            "as_of": normalized["as_of"],
            "evidence": normalized["evidence"],
        }
    if reason_type in {"chain_context", "chain_core_rep", "chain_elastic_rep", "source_leader", "constituent_hot"}:
        phase = _text(normalized["evidence"].get("phase") if isinstance(normalized.get("evidence"), dict) else "")
        row["chain_context"] = {
            "chain_id": normalized.get("chain_id"),
            "node_id": normalized.get("node_id"),
            "board_or_concept": normalized.get("board_or_concept"),
            "phase": phase,
            "effect": normalized.get("decision_effect") or _chain_decision_effect(phase),
            "as_of": normalized.get("as_of"),
            "evidence": normalized.get("evidence"),
        }
    if top.get("signal_side") == "sell" and top.get("decision_effect") != "context_only":
        row["action_status"] = "risk_review"
        row["trader_action"] = "减仓/止盈" if top.get("queue_lane") == "risk_exit_first" else "风险复核"
        row["next_action"] = row["trader_action"]
        row["queue_lane"] = top.get("queue_lane") or "risk_exit_first"
        row["actionability"] = top.get("actionability") or "risk_exit_first"
        row["invalidates_when"] = "卖出/风险信号解除或重新站回关键周期"
    elif top["reason_type"] in {"technical_trigger", "technical_signal"}:
        actionability = top.get("actionability") or row.get("actionability") or "observe_only"
        if actionability == "entry_ready":
            row["action_status"] = "entry_ready"
            row["trader_action"] = "可试仓"
        elif actionability == "entry_waiting_confirm":
            row["action_status"] = "entry_waiting_confirm"
            row["trader_action"] = "等待5m/15m确认"
        elif actionability == "review_required":
            row["action_status"] = "period_conflict_review"
            row["trader_action"] = "周期冲突复核"
        else:
            row["action_status"] = "technical_watch"
            row["trader_action"] = "观察"
        row["next_action"] = row["trader_action"]
        row["queue_lane"] = top.get("queue_lane") or row.get("queue_lane")
        row["actionability"] = actionability
        row["invalidates_when"] = "5m/15m 无法确认或上级周期转弱"
    elif top["reason_type"] == "knowledge_conflict":
        row["action_status"] = "knowledge_conflict"
        row["trader_action"] = "知识库冲突复核"
        row["next_action"] = row["trader_action"]
        row["invalidates_when"] = "技术信号或知识观点解除冲突"
    elif top["reason_type"] in {"knowledge_confirmed", "knowledge_watch"}:
        row["action_status"] = "knowledge_watch"
        row["trader_action"] = "知识库观察"
        row["next_action"] = row["trader_action"]
        row["invalidates_when"] = "知识观点过期或缺少硬技术确认"
    elif top["reason_type"] == "fallback_watch":
        row["action_status"] = "fallback_watch"
        row["trader_action"] = "观察/预热"
        row["next_action"] = row["trader_action"]
        row["queue_lane"] = "fallback_watch"
        row["actionability"] = "observe_only"
        row["invalidates_when"] = "硬技术信号未确认或降级候选过期"
    elif top["reason_type"].startswith("chain_") or top["reason_type"] in {"source_leader", "constituent_hot"}:
        row["action_status"] = "chain_watch"
        row["trader_action"] = "观察产业链共振"
        row["next_action"] = row["trader_action"]
        row["invalidates_when"] = "产业链节点热度转弱或领涨股回落"

    technical = next((item for item in row["inclusion_reasons"] if _is_technical_reason(item)), None)
    if technical:
        if technical.get("signal_side") == "sell":
            row["action_status"] = "risk_review"
            row["trader_action"] = "减仓/止盈" if technical.get("queue_lane") == "risk_exit_first" else "风险复核"
            row["next_action"] = row["trader_action"]
            row["queue_lane"] = technical.get("queue_lane") or "risk_exit_first"
            row["actionability"] = technical.get("actionability") or "risk_exit_first"
            row["invalidates_when"] = "卖出/风险信号解除或重新站回关键周期"
        elif row.get("action_status") in {"watch", "knowledge_watch", "chain_watch", "technical_watch"}:
            actionability = technical.get("actionability") or row.get("actionability") or "observe_only"
            if actionability == "entry_ready":
                row["action_status"] = "entry_ready"
                row["trader_action"] = "可试仓"
            elif actionability == "entry_waiting_confirm":
                row["action_status"] = "entry_waiting_confirm"
                row["trader_action"] = "等待5m/15m确认"
            elif actionability == "review_required":
                row["action_status"] = "period_conflict_review"
                row["trader_action"] = "周期冲突复核"
            else:
                row["action_status"] = "technical_watch"
                row["trader_action"] = "观察"
            row["next_action"] = row["trader_action"]
            row["queue_lane"] = technical.get("queue_lane") or row.get("queue_lane")
            row["actionability"] = actionability
            row["invalidates_when"] = "5m/15m 无法确认或上级周期转弱"
    if (row.get("knowledge_confirmation") or {}).get("status") == "conflict" and row.get("action_status") == "buy_candidate":
        row["action_status"] = "knowledge_conflict"
        row["trader_action"] = "知识库冲突复核"
        row["next_action"] = row["trader_action"]
        row["invalidates_when"] = "知识观点或技术信号解除冲突"
    knowledge_effect = _text((row.get("knowledge_confirmation") or {}).get("effect"))
    chain_effect = _text((row.get("chain_context") or {}).get("effect"))
    if row.get("queue_lane") in {"entry_ready", "entry_waiting_confirm"} and knowledge_effect in {"block", "downgrade", "exit_priority"}:
        row["action_status"] = "knowledge_blocked" if knowledge_effect == "block" else "knowledge_downgraded"
        row["trader_action"] = "知识库阻断" if knowledge_effect == "block" else "知识库降级复核"
        row["next_action"] = row["trader_action"]
        row["queue_lane"] = "context_only" if knowledge_effect == "block" else "entry_waiting_confirm"
        row["actionability"] = "review_required" if knowledge_effect == "block" else "entry_waiting_confirm"
        row["invalidates_when"] = "知识库风险解除且右侧确认重新出现"
    if row.get("queue_lane") in {"entry_ready", "entry_waiting_confirm"} and chain_effect in {"block", "exit_priority"}:
        row["action_status"] = "chain_risk_review"
        row["trader_action"] = "产业链风险复核"
        row["next_action"] = row["trader_action"]
        row["queue_lane"] = "risk_exit_first" if chain_effect == "exit_priority" else "context_only"
        row["actionability"] = "risk_exit_first" if chain_effect == "exit_priority" else "review_required"
        row["invalidates_when"] = "产业链退潮/高潮风险解除且5m/15m重新确认"


def _add_user_pinned(rows: dict[str, dict[str, Any]], index_codes: set[str], now) -> None:
    raw_values = os.getenv("TERMINAL_REALTIME_PRIORITY_CODES", "")
    values = raw_values.replace(";", ",").split(",") if raw_values.strip() else []
    for value in values:
        code = _pure_a_code(value)
        _add_reason(rows, code, {
            "reason_type": "user_pinned",
            "source_collection": "config",
            "source_doc_id": "TERMINAL_REALTIME_PRIORITY_CODES",
            "signal_type": "手动关注",
            "signal_side": "neutral",
            "source_role": "context",
            "decision_effect": "context_only",
            "queue_lane": "context_only",
            "as_of": now.date().isoformat(),
            "evidence": {"raw_value": _text(value)},
        }, index_codes=index_codes)


def _add_signal_rows(
    rows: dict[str, dict[str, Any]],
    db: Database,
    index_codes: set[str],
    *,
    include_generated_daily: bool = False,
    generated_daily_only: bool = False,
) -> None:
    cursor = db["signals"].find({}).sort([("signal_date", -1), ("updated_at", -1)]).limit(500)
    signals = list(cursor)
    by_code: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        code = _pure_a_code(signal.get("symbol"))
        if code:
            by_code.setdefault(code, []).append(signal)
    for signal in signals:
        generated_daily = _is_generated_daily_signal(signal)
        if generated_daily_only and not generated_daily:
            continue
        if generated_daily and not include_generated_daily:
            continue
        signal_type = _text(signal.get("signal_type") or signal.get("type") or signal.get("reason"))
        hard_screen_signal = _is_hard_screen_signal(signal) and not generated_daily
        reason_type = "technical_signal" if hard_screen_signal else _reason_type_for_signal(signal)
        if hard_screen_signal:
            reason_type = "technical_trigger"
        code = _pure_a_code(signal.get("symbol"))
        resonance_context = _screen_resonance_context(signal, by_code.get(code, [])) if hard_screen_signal else {}
        _add_reason(rows, signal.get("symbol"), {
            "reason_type": reason_type,
            "source_collection": "signals",
            "source_doc_id": _source_doc_id(signal),
            "signal_type": signal_type,
            "signal_side": _signal_side(signal),
            "signal_family": _signal_family(signal),
            "freq": _text(signal.get("freq") or signal.get("timeframe")),
            "score": 0 if reason_type == "historical_signal_record" else _float(signal.get("score") or signal.get("total_score")),
            "confidence": _float(signal.get("confidence")),
            "as_of": _text(signal.get("signal_date") or signal.get("updated_at"))[:10],
            "source_role": "technical_trigger" if hard_screen_signal else "context",
            "decision_effect": "history_pending" if reason_type == "historical_signal_record" else ("confirm" if hard_screen_signal else "context_only"),
            "can_create_candidate": reason_type not in {"historical_signal_record", "custom_signal"},
            "backtest_quality": {
                "status": "not_evaluated",
                "score": 0,
                "source": "sqlite.backtest.signal_records",
            } if reason_type == "historical_signal_record" else {},
            "resonance_context": resonance_context,
            "evidence": {
                "source": _text(signal.get("source")),
                "dedupe_key": _text(signal.get("dedupe_key")),
                "details": signal.get("details_json") if isinstance(signal.get("details_json"), dict) else {},
                "resonance_context": resonance_context,
            },
        }, index_codes=index_codes, name=_text(signal.get("name")))


def _add_technical_signal_rows(rows: dict[str, dict[str, Any]], db: Database, index_codes: set[str]) -> None:
    limit = max(1, int(os.getenv("TERMINAL_POOL_TECHNICAL_SIGNAL_LIMIT", "1000")))
    cursor = db["terminal_technical_signals"].find(
        {"market": "A"},
        {
            "symbol": 1,
            "raw_code": 1,
            "freq": 1,
            "signal_type": 1,
            "signal_side": 1,
            "signal_family": 1,
            "score": 1,
            "total_score": 1,
            "confidence": 1,
            "as_of": 1,
            "updated_at": 1,
            "dedupe_key": 1,
            "technical_evidence": 1,
            "resonance_context": 1,
            "invalidates_when": 1,
        },
    ).sort([("as_of", -1), ("updated_at", -1), ("total_score", -1), ("confidence", -1)]).limit(limit)
    for signal in cursor:
        evidence = signal.get("technical_evidence") if isinstance(signal.get("technical_evidence"), dict) else {}
        resonance_context = signal.get("resonance_context") if isinstance(signal.get("resonance_context"), dict) else {}
        if not resonance_context and isinstance(evidence, dict):
            resonance_context = evidence.get("resonance_context") or {}
        _add_reason(rows, signal.get("symbol") or signal.get("raw_code"), {
            "reason_type": "technical_trigger",
            "source_collection": "terminal_technical_signals",
            "source_doc_id": _source_doc_id(signal),
            "signal_type": _text(signal.get("signal_type")),
            "signal_side": _text(signal.get("signal_side")) or _signal_side(signal),
            "signal_family": _text(signal.get("signal_family")) or "hard_technical",
            "freq": _text(signal.get("freq")),
            "score": _float(signal.get("total_score") or signal.get("score")),
            "confidence": _float(signal.get("confidence")),
            "as_of": _text(signal.get("as_of") or signal.get("updated_at"))[:10],
            "source_role": "technical_trigger",
            "resonance_context": resonance_context,
            "evidence": evidence,
        }, index_codes=index_codes)


def _knowledge_status_for(sentiment: str, tech_side: str) -> tuple[str, str]:
    if not tech_side:
        return "knowledge_watch", "watch"
    if tech_side == "buy" and sentiment == "看多":
        return "knowledge_confirmed", "confirmed"
    if tech_side == "sell" and sentiment == "看空":
        return "knowledge_confirmed", "confirmed"
    if tech_side == "buy" and sentiment == "看空":
        return "knowledge_conflict", "conflict"
    if tech_side == "sell" and sentiment == "看多":
        return "knowledge_conflict", "conflict"
    return "knowledge_watch", "neutral"


def _add_knowledge_rows(rows: dict[str, dict[str, Any]], db: Database, index_codes: set[str]) -> None:
    cursor = db["knowledge_market_views"].find(
        {"market": "A", "target_type": "stock"},
        {"symbol": 1, "raw_code": 1, "sentiment": 1, "latest_sentiment": 1, "confidence": 1, "as_of": 1,
         "sources": 1, "catalysts": 1, "view_id": 1, "updated_at": 1},
    ).sort([("as_of", -1), ("updated_at", -1)]).limit(500)
    for view in cursor:
        code = _pure_a_code(view.get("symbol") or view.get("raw_code"))
        if not code:
            continue
        existing = rows.get(code)
        tech_side = ""
        if existing:
            tech_reason = next((item for item in existing.get("inclusion_reasons", []) if item.get("reason_type") == "technical_signal"), None)
            if not tech_reason:
                tech_reason = next((item for item in existing.get("inclusion_reasons", []) if _is_technical_reason(item)), None)
            if tech_reason:
                tech_side = _text(tech_reason.get("signal_side"))
        sentiment = _text(view.get("latest_sentiment") or view.get("sentiment"))
        reason_type, status = _knowledge_status_for(sentiment, tech_side)
        knowledge_effect = _text(view.get("knowledge_effect")) or ("block" if status == "conflict" else ("confirm" if status == "confirmed" else "context_only"))
        sources = view.get("sources") if isinstance(view.get("sources"), list) else []
        _add_reason(rows, code, {
            "reason_type": reason_type,
            "source_collection": "knowledge_market_views",
            "source_doc_id": _text(view.get("view_id")),
            "signal_type": f"知识库{sentiment or '覆盖'}",
            "signal_side": tech_side or "neutral",
            "source_role": "context",
            "decision_effect": knowledge_effect,
            "knowledge_effect": knowledge_effect,
            "can_create_candidate": False,
            "score": 0,
            "confidence": _float(view.get("confidence")),
            "as_of": _text(view.get("as_of") or view.get("updated_at"))[:10],
            "sentiment": sentiment,
            "knowledge_status": status,
            "evidence": {
                "sources": sources[:4],
                "catalysts": view.get("catalysts") if isinstance(view.get("catalysts"), list) else [],
                "policy": "confirm_conflict_degrade_only",
            },
        }, index_codes=index_codes)


def _latest_strategy_snapshot(db: Database) -> dict[str, Any]:
    doc = db["strategy_snapshots"].find_one(
        {"snapshot": {"$exists": True}},
        {"snapshot": 1, "as_of": 1, "updated_at": 1, "_id": 1},
        sort=[("updated_at", -1), ("as_of", -1)],
    ) or {}
    snapshot = doc.get("snapshot") or {}
    snapshot["_source_doc_id"] = _text(doc.get("_id"))
    snapshot["_as_of"] = _text(doc.get("as_of") or doc.get("updated_at"))[:10]
    return snapshot


def _add_strategy_rows(rows: dict[str, dict[str, Any]], db: Database, index_codes: set[str]) -> None:
    snapshot = _latest_strategy_snapshot(db)
    source_doc_id = _text(snapshot.get("_source_doc_id")) or "latest"
    as_of = _text(snapshot.get("_as_of"))
    for key in ("warnings", "candidates", "decision_queue"):
        for item in snapshot.get(key) or []:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            signal_type = _text(item.get("reason") or metadata.get("trigger") or item.get("signal_type"))
            if not signal_type:
                continue
            source = _text(metadata.get("source") or item.get("source"))
            probe = {**item, "source": source, "signal_type": signal_type, "pool_status": "warning" if key == "warnings" else item.get("status")}
            signal_side = _signal_side(probe)
            reason_type = "generated_risk_signal" if signal_side == "sell" or key == "warnings" else "fallback_watch"
            _add_reason(rows, item.get("symbol") or item.get("code"), {
                "reason_type": reason_type,
                "source_collection": "strategy_snapshots",
                "source_doc_id": source_doc_id,
                "signal_type": signal_type,
                "signal_side": signal_side,
                "signal_family": "fallback_candidate" if reason_type == "fallback_watch" else _signal_family(probe),
                "freq": _text(metadata.get("freq") or item.get("freq")),
                "score": _float(item.get("score") or metadata.get("score")),
                "confidence": _float(item.get("confidence") or metadata.get("confidence")),
                "board_or_concept": _text(metadata.get("theme")),
                "as_of": as_of,
                "source_role": "fallback" if reason_type == "fallback_watch" else "technical_trigger",
                "decision_effect": "fallback_watch" if reason_type == "fallback_watch" else "exit_priority",
                "actionability": "observe_only" if reason_type == "fallback_watch" else "risk_exit_first",
                "queue_lane": "fallback_watch" if reason_type == "fallback_watch" else "risk_exit_first",
                "evidence": metadata.get("evidence") if isinstance(metadata.get("evidence"), dict) else {"source": source},
            }, index_codes=index_codes, name=_text(item.get("name")))


def _latest_chain_rows(db: Database, limit: int = 24) -> list[dict[str, Any]]:
    latest = db["chain_heat_snapshots"].find_one({"market": "A"}, {"trade_minute": 1}, sort=[("trade_minute", -1)])
    if not latest or latest.get("trade_minute") is None:
        return []
    return list(db["chain_heat_snapshots"].find(
        {"market": "A", "trade_minute": latest["trade_minute"]},
        {"_id": 0},
    ).sort("rank", 1).limit(limit))


def _constituents_for_domain(db: Database, domain: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    kind = _text(domain.get("kind"))
    name = _text(domain.get("name"))
    if not kind or not name:
        return [], {}
    if kind == "concept":
        doc = db["concept_constituents"].find_one(
            {"$or": [{"concept_name": name}, {"board_name": name}, {"name": name}]},
            {"symbols": 1, "stock_names": 1},
            sort=[("updated_at", -1)],
        ) or {}
    else:
        doc = db["board_constituents"].find_one(
            {"$or": [{"board_name": name}, {"name": name}]},
            {"symbols": 1, "stock_names": 1},
            sort=[("updated_at", -1)],
        ) or {}
    return list(doc.get("symbols") or []), dict(doc.get("stock_names") or {})


def _add_chain_rows(rows: dict[str, dict[str, Any]], db: Database, index_codes: set[str]) -> None:
    added_constituents = 0
    for chain in _latest_chain_rows(db):
        chain_key = f"{chain.get('chain_id')}:{chain.get('node_id')}:{chain.get('trade_minute')}"
        board_or_concept = ""
        integrated = chain.get("integrated_domains") if isinstance(chain.get("integrated_domains"), list) else []
        if integrated:
            board_or_concept = _text(integrated[0].get("name"))
        for rep in chain.get("representatives") or []:
            if not isinstance(rep, dict):
                continue
            rep_type = "chain_core_rep" if _text(rep.get("representative_type")) == "core" else "chain_elastic_rep"
            phase = _text(chain.get("phase"))
            chain_effect = _chain_decision_effect(phase)
            _add_reason(rows, rep.get("symbol"), {
                "reason_type": rep_type,
                "source_collection": "chain_heat_snapshots",
                "source_doc_id": chain_key,
                "signal_type": _text(chain.get("trading_signal")),
                "signal_side": "neutral",
                "source_role": "context",
                "decision_effect": chain_effect,
                "can_create_candidate": True,
                "score": _float(chain.get("heat_score")),
                "confidence": _float(rep.get("priority")),
                "chain_id": _text(chain.get("chain_id")),
                "node_id": _text(chain.get("node_id")),
                "board_or_concept": board_or_concept,
                "as_of": _text(chain.get("trade_minute")),
                "evidence": {
                    "phase": phase,
                    "range_pattern": chain.get("range_pattern"),
                    "change_pct": chain.get("change_pct"),
                    "up_count": chain.get("up_count"),
                    "down_count": chain.get("down_count"),
                    "rank": chain.get("rank"),
                    "leader_change_pct": chain.get("leader_change_pct"),
                    "heat_score": chain.get("heat_score"),
                    "momentum_5m": chain.get("momentum_5m"),
                    "momentum_15m": chain.get("momentum_15m"),
                    "momentum_30m": chain.get("momentum_30m"),
                    "mapping_confidence": chain.get("mapping_confidence"),
                    "integrated_count": chain.get("integrated_count"),
                },
            }, index_codes=index_codes, name=_text(rep.get("name")))
        for domain in integrated[:6]:
            if added_constituents >= 36:
                break
            leader_symbol = _text(domain.get("leader_symbol"))
            if leader_symbol:
                _add_reason(rows, leader_symbol, {
                    "reason_type": "source_leader",
                    "source_collection": "chain_heat_snapshots",
                    "source_doc_id": chain_key,
                    "signal_type": _text(chain.get("trading_signal")),
                    "signal_side": "neutral",
                    "source_role": "context",
                    "decision_effect": _chain_decision_effect(_text(chain.get("phase"))),
                    "can_create_candidate": True,
                    "score": _float(domain.get("leader_change_pct")),
                    "confidence": _float(domain.get("mapping_confidence")),
                    "chain_id": _text(chain.get("chain_id")),
                    "node_id": _text(chain.get("node_id")),
                    "board_or_concept": _text(domain.get("name")),
                    "as_of": _text(chain.get("trade_minute")),
                    "evidence": {"phase": chain.get("phase"), "range_pattern": chain.get("range_pattern")},
                }, index_codes=index_codes, name=_text(domain.get("leader_name")))
            symbols, stock_names = _constituents_for_domain(db, domain)
            for symbol in symbols[:2]:
                if added_constituents >= 36:
                    break
                code = _pure_a_code(symbol)
                _add_reason(rows, code, {
                    "reason_type": "constituent_hot",
                    "source_collection": "board_constituents" if domain.get("kind") == "industry" else "concept_constituents",
                    "source_doc_id": _text(domain.get("name")),
                    "signal_type": _text(chain.get("trading_signal")),
                    "signal_side": "neutral",
                    "source_role": "context",
                    "decision_effect": _chain_decision_effect(_text(chain.get("phase"))),
                    "can_create_candidate": False,
                    "heat_score": _float(chain.get("heat_score")),
                    "confidence": _float(domain.get("mapping_confidence")),
                    "chain_id": _text(chain.get("chain_id")),
                    "node_id": _text(chain.get("node_id")),
                    "board_or_concept": _text(domain.get("name")),
                    "as_of": _text(chain.get("trade_minute")),
                    "evidence": {"phase": chain.get("phase"), "range_pattern": chain.get("range_pattern")},
                }, index_codes=index_codes, name=stock_names.get(code, ""))
                added_constituents += 1


def _add_active_pool(rows: dict[str, dict[str, Any]], db: Database, index_codes: set[str]) -> None:
    doc = db["market_pools"].find_one({"pool": "active"}, {"symbols": 1, "items": 1, "dt": 1, "updated_at": 1}, sort=[("dt", -1), ("updated_at", -1)]) or {}
    as_of = _text(doc.get("dt") or doc.get("updated_at"))[:10]
    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        _add_reason(rows, item.get("symbol") or item.get("code"), {
            "reason_type": "active_pool_watch",
            "source_collection": "market_pools",
            "source_doc_id": "active",
            "signal_type": "活跃池观察",
            "signal_side": "buy",
            "as_of": as_of,
            "evidence": {"sources": item.get("sources") or []},
        }, index_codes=index_codes)
    for symbol in doc.get("symbols") or []:
        _add_reason(rows, symbol, {
            "reason_type": "active_pool_watch",
            "source_collection": "market_pools",
            "source_doc_id": "active",
            "signal_type": "活跃池观察",
            "signal_side": "buy",
            "as_of": as_of,
        }, index_codes=index_codes)


def _add_fallback_watch_rows(rows: dict[str, dict[str, Any]], db: Database, index_codes: set[str], *, limit: int, now) -> int:
    if limit <= 0:
        return 0
    before = len(rows)
    snapshot = _latest_strategy_snapshot(db)
    source_doc_id = _text(snapshot.get("_source_doc_id")) or "latest"
    as_of = _text(snapshot.get("_as_of")) or now.date().isoformat()
    for item in snapshot.get("candidates") or []:
        if len(rows) - before >= limit:
            break
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        signal_type = _text(item.get("reason") or metadata.get("trigger") or item.get("latest_signal") or "买点候选")
        _add_reason(rows, item.get("symbol") or item.get("code"), {
            "reason_type": "fallback_watch",
            "source_collection": "strategy_snapshots",
            "source_doc_id": source_doc_id,
            "signal_type": signal_type,
            "signal_side": "buy",
            "signal_family": "fallback_candidate",
            "freq": _text(metadata.get("freq") or item.get("freq")),
            "score": _float(item.get("score") or metadata.get("score")),
            "confidence": _float(item.get("confidence") or metadata.get("confidence")),
            "board_or_concept": _text(metadata.get("theme")),
            "as_of": as_of,
            "source_role": "fallback",
            "decision_effect": "fallback_watch",
            "actionability": "observe_only",
            "queue_lane": "fallback_watch",
            "evidence": metadata.get("evidence") if isinstance(metadata.get("evidence"), dict) else {"policy": "strict_pool_fallback"},
        }, index_codes=index_codes, name=_text(item.get("name")))

    if len(rows) - before >= limit:
        return len(rows) - before

    doc = db["market_pools"].find_one(
        {"pool": "active"},
        {"symbols": 1, "items": 1, "dt": 1, "updated_at": 1},
        sort=[("dt", -1), ("updated_at", -1)],
    ) or {}
    active_as_of = _text(doc.get("dt") or doc.get("updated_at"))[:10] or now.date().isoformat()
    active_items = [item for item in doc.get("items") or [] if isinstance(item, dict)]
    if not active_items:
        active_items = [{"symbol": symbol} for symbol in doc.get("symbols") or []]
    for item in active_items:
        if len(rows) - before >= limit:
            break
        _add_reason(rows, item.get("symbol") or item.get("code"), {
            "reason_type": "fallback_watch",
            "source_collection": "market_pools",
            "source_doc_id": "active",
            "signal_type": "活跃池降级观察",
            "signal_side": "buy",
            "signal_family": "fallback_active_pool",
            "score": _float(item.get("score") or item.get("total_score")),
            "confidence": _float(item.get("confidence")),
            "as_of": active_as_of,
            "source_role": "fallback",
            "decision_effect": "fallback_watch",
            "actionability": "observe_only",
            "queue_lane": "fallback_watch",
            "evidence": {"sources": item.get("sources") or [], "policy": "strict_pool_fallback"},
        }, index_codes=index_codes, name=_text(item.get("name") or item.get("stock_name")))
    return len(rows) - before


def _add_recent_opened(rows: dict[str, dict[str, Any]], db: Database, index_codes: set[str]) -> None:
    cursor = db["sync_log"].find(
        {"module": {"$in": ["stock_minute", "stock_daily"]}, "status": "ok", "symbol": {"$exists": True}},
        {"symbol": 1, "module": 1, "last_run": 1},
    ).sort("last_run", -1).limit(120)
    for doc in cursor:
        _add_reason(rows, doc.get("symbol"), {
            "reason_type": "recent_opened",
            "source_collection": "sync_log",
            "source_doc_id": _text(doc.get("module")),
            "signal_type": "近期终端/同步观察",
            "signal_side": "buy",
            "as_of": _text(doc.get("last_run")),
        }, index_codes=index_codes)


def _top_heat_names(db: Database, kind: str, limit: int) -> list[str]:
    docs = list(db["board_heat_ticks"].find(
        {"kind": kind},
        {"name": 1, "trade_minute": 1, "rank_idx": 1},
    ).sort([("trade_minute", -1), ("rank_idx", 1)]).limit(limit * 4))
    names: list[str] = []
    for doc in docs:
        name = _text(doc.get("name"))
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


def _reason_freqs(reason: dict[str, Any]) -> set[str]:
    freqs = {_text(reason.get("freq"))}
    context = reason.get("resonance_context") if isinstance(reason.get("resonance_context"), dict) else {}
    for key in ("aligned_freqs", "conflict_freqs"):
        for freq in context.get(key) or []:
            if _text(freq):
                freqs.add(_text(freq))
    evidence = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
    nested = evidence.get("resonance_context") if isinstance(evidence.get("resonance_context"), dict) else {}
    for key in ("aligned_freqs", "conflict_freqs"):
        for freq in nested.get(key) or []:
            if _text(freq):
                freqs.add(_text(freq))
    return {freq for freq in freqs if freq}


def _buy_technical_reasons(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        reason
        for reason in row.get("inclusion_reasons") or []
        if isinstance(reason, dict) and _is_technical_reason(reason) and _text(reason.get("signal_side")) == "buy"
    ]


def _risk_reasons(row: dict[str, Any]) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    for reason in row.get("inclusion_reasons") or []:
        if not isinstance(reason, dict):
            continue
        signal_side = _text(reason.get("signal_side"))
        reason_type = _text(reason.get("reason_type"))
        decision_effect = _text(reason.get("decision_effect"))
        knowledge_effect = _text(reason.get("knowledge_effect"))
        if signal_side == "sell" or decision_effect in {"exit_priority", "block"} or knowledge_effect in {"block", "exit_priority"}:
            reasons.append(reason)
            continue
        if reason_type in {"generated_risk_signal", "knowledge_conflict"}:
            reasons.append(reason)
    return reasons


def _source_collections(row: dict[str, Any]) -> list[str]:
    values = []
    for reason in row.get("inclusion_reasons") or []:
        if not isinstance(reason, dict):
            continue
        source = _text(reason.get("source_collection"))
        if source and source not in values:
            values.append(source)
    return values


def _entry_gate(row: dict[str, Any]) -> tuple[bool, str, list[str], dict[str, Any] | None, dict[str, Any] | None]:
    buy_reasons = _buy_technical_reasons(row)
    risk_reasons = _risk_reasons(row)
    top_buy = max(buy_reasons, key=lambda item: (_float(item.get("score")), _float(item.get("confidence"))), default=None)
    top_risk = max(risk_reasons, key=lambda item: (_float(item.get("weight")), abs(_float(item.get("score")))), default=None)
    if top_risk:
        return False, "blocked_by_risk", ["risk_signal_present"], top_buy, top_risk
    if not top_buy:
        return False, "watch_only_not_hard_buy", ["missing_buy_technical"], None, None
    freqs: set[str] = set()
    conflict_freqs: set[str] = set()
    for reason in buy_reasons:
        freqs.update(_reason_freqs(reason))
        context = reason.get("resonance_context") if isinstance(reason.get("resonance_context"), dict) else {}
        conflict_freqs.update(_text(freq) for freq in context.get("conflict_freqs") or [] if _text(freq))
    if conflict_freqs:
        return False, "blocked_by_period_conflict", sorted(conflict_freqs, key=_freq_sort_key), top_buy, top_risk
    has_30m = any(_is_30m_freq(freq) for freq in freqs)
    has_partner = any(_is_entry_partner_freq(freq) for freq in freqs)
    if not has_30m:
        return False, "entry_waiting_30m_confirm", ["30m_missing"], top_buy, top_risk
    if not has_partner:
        return False, "entry_waiting_resonance_confirm", ["partner_period_missing"], top_buy, top_risk
    return True, "entry_confirmed", [], top_buy, top_risk


def _freq_severity(freqs: set[str]) -> float:
    if any(freq in {"周线", "weekly", "1w", "W", "w"} for freq in freqs):
        return 30.0
    if any(freq in {"日线", "daily", "1d", "D", "d"} for freq in freqs):
        return 24.0
    if any(_is_30m_freq(freq) for freq in freqs):
        return 18.0
    if any(freq in {"15分钟", "15min", "15m", "F15", "f15"} for freq in freqs):
        return 14.0
    return 8.0


def _context_adjust(row: dict[str, Any]) -> float:
    adjust = 0.0
    knowledge = row.get("knowledge_confirmation") if isinstance(row.get("knowledge_confirmation"), dict) else {}
    if knowledge.get("status") == "confirmed":
        adjust += 8.0
    if knowledge.get("status") == "conflict" or knowledge.get("effect") == "block":
        adjust -= 40.0
    chain = row.get("chain_context") if isinstance(row.get("chain_context"), dict) else {}
    if chain.get("effect") == "confirm":
        adjust += 6.0
    if chain.get("effect") in {"block", "exit_priority"}:
        adjust -= 25.0
    return adjust


def _entry_components(row: dict[str, Any], top_buy: dict[str, Any]) -> dict[str, float]:
    freqs = _reason_freqs(top_buy)
    context = top_buy.get("resonance_context") if isinstance(top_buy.get("resonance_context"), dict) else {}
    grade = _text(context.get("grade"))
    resonance = 30.0 if grade == "strong_resonance" else 22.0 if grade == "multi_period" else 12.0
    components = {
        "technical_score": max(0.0, _float(top_buy.get("score"))) * 0.35,
        "resonance": resonance,
        "right_side_30m": 24.0 if any(_is_30m_freq(freq) for freq in freqs) else 0.0,
        "confidence": min(1.0, _float(top_buy.get("confidence"))) * 18.0,
        "freshness": 10.0,
        "context_adjust": _context_adjust(row),
    }
    return {key: round(value, 3) for key, value in components.items()}


def _risk_components(row: dict[str, Any], top_risk: dict[str, Any]) -> dict[str, float]:
    freqs = _reason_freqs(top_risk)
    components = {
        "sell_strength": abs(_float(top_risk.get("score"))) * 0.50,
        "timeframe_severity": _freq_severity(freqs),
        "freshness": 16.0,
        "chain_or_knowledge_risk": 18.0 if _text(top_risk.get("reason_type")) in {"knowledge_conflict", "chain_context", "chain_core_rep", "chain_elastic_rep"} else 0.0,
    }
    return {key: round(value, 3) for key, value in components.items()}


def _watch_components(row: dict[str, Any], gate_status: str) -> dict[str, float]:
    source_priority = 0.0
    if _buy_technical_reasons(row):
        source_priority = 40.0
    elif "fallback_watch" in row.get("source_tags", []):
        source_priority = 22.0
    elif "active_pool_watch" in row.get("source_tags", []):
        source_priority = 18.0
    elif any(tag.startswith("chain_") for tag in row.get("source_tags", [])):
        source_priority = 16.0
    proximity = 30.0 if gate_status == "entry_waiting_30m_confirm" else 22.0 if gate_status == "entry_waiting_resonance_confirm" else 10.0
    components = {
        "source_priority": source_priority,
        "entry_proximity": proximity,
        "heat_or_theme": _float(row.get("score")) * 0.05,
        "freshness": 8.0,
    }
    return {key: round(value, 3) for key, value in components.items()}


def _finalize_pool_row(
    row: dict[str, Any],
    *,
    pool_type: str,
    rank_score: float,
    score_components: dict[str, float],
    entry_gate_status: str,
    blocked_by: list[str],
    top_buy: dict[str, Any] | None,
    top_risk: dict[str, Any] | None,
) -> dict[str, Any]:
    row["pool_type"] = pool_type
    row["rank_score"] = round(rank_score, 3)
    row["sort_score"] = row["rank_score"]
    row["score_components"] = score_components
    row["entry_gate_status"] = entry_gate_status
    row["blocked_by"] = blocked_by
    row["top_buy_reason"] = top_buy or {}
    row["top_risk_reason"] = top_risk or {}
    row["source_collections"] = _source_collections(row)
    row["coverage_status"] = "required_freqs_present" if entry_gate_status == "entry_confirmed" else entry_gate_status
    if pool_type == "focus":
        row["action_status"] = "entry_ready"
        row["trader_action"] = "可试仓"
        row["next_action"] = "可试仓"
        row["queue_lane"] = "entry_ready"
        row["actionability"] = "entry_ready"
        row["decision_effect"] = "confirm"
        row["signal_origin"] = _text((top_buy or {}).get("reason_type")) or row.get("signal_origin")
        row["latest_signal"] = _text((top_buy or {}).get("signal_type")) or row.get("latest_signal")
        row["invalidates_when"] = "30m买点失效、上级周期转弱或风险信号出现"
    elif pool_type == "risk":
        row["action_status"] = "risk_review"
        row["trader_action"] = "止盈/止损复核"
        row["next_action"] = row["trader_action"]
        row["queue_lane"] = "risk_exit_first"
        row["actionability"] = "risk_exit_first"
        row["decision_effect"] = "exit_priority"
        row["signal_origin"] = _text((top_risk or {}).get("reason_type")) or row.get("signal_origin")
        row["latest_signal"] = _text((top_risk or {}).get("signal_type")) or row.get("latest_signal")
        row["invalidates_when"] = "风险信号解除或重新站回关键周期"
    else:
        if entry_gate_status == "entry_waiting_30m_confirm":
            row["action_status"] = "entry_waiting_30m_confirm"
            row["trader_action"] = "等待30m确认"
            row["queue_lane"] = "watch_preheat"
        elif entry_gate_status == "entry_waiting_resonance_confirm":
            row["action_status"] = "entry_waiting_resonance_confirm"
            row["trader_action"] = "等待共振确认"
            row["queue_lane"] = "watch_preheat"
        elif entry_gate_status.startswith("blocked_by"):
            row["action_status"] = "watch_blocked"
            row["trader_action"] = "观察/排除风险"
            row["queue_lane"] = "watch_preheat"
        else:
            row["action_status"] = row.get("action_status") if row.get("action_status") != "risk_review" else "watch"
            row["trader_action"] = row.get("trader_action") or "观察/预热"
            row["queue_lane"] = row.get("queue_lane") if row.get("queue_lane") != "risk_exit_first" else "watch_preheat"
        row["next_action"] = row["trader_action"]
        row["actionability"] = "observe_only"
        row["decision_effect"] = "context_only"
        row["invalidates_when"] = row.get("invalidates_when") or "硬技术买点未确认或观察条件过期"
    row["reason"] = " · ".join(
        _text(reason.get("signal_type") or reason.get("reason_type"))
        for reason in (top_buy, top_risk)
        if isinstance(reason, dict) and _text(reason.get("signal_type") or reason.get("reason_type"))
    ) or row.get("reason")
    return row


def _prepare_pool_rows(rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = list(rows.values())
    for row in ordered:
        row["inclusion_reasons"] = sorted(row.get("inclusion_reasons") or [], key=lambda item: _float(item.get("weight")), reverse=True)[:12]
        row["exit_condition"] = row.get("invalidates_when")
        row["next_action"] = row.get("next_action") or row.get("trader_action") or "观察"
        row["queue_lane"] = row.get("queue_lane") or "context_only"
        row["actionability"] = row.get("actionability") or "context_only"
        row["decision_effects"] = [
            _text(item.get("decision_effect"))
            for item in row["inclusion_reasons"]
            if _text(item.get("decision_effect"))
        ][:6]
        if not isinstance(row.get("technical_evidence"), dict) or not row.get("technical_evidence"):
            row["technical_evidence"] = {"status": "missing", "note": "watch_only_not_buy_candidate"}
        if not isinstance(row.get("knowledge_confirmation"), dict) or not row.get("knowledge_confirmation"):
            row["knowledge_confirmation"] = {"status": "none"}
    return ordered


def _split_pool_rows(
    rows: dict[str, dict[str, Any]],
    *,
    focus_limit: int,
    risk_limit: int,
    watch_limit: int,
) -> dict[str, Any]:
    focus: list[dict[str, Any]] = []
    risk: list[dict[str, Any]] = []
    watch: list[dict[str, Any]] = []
    prepared = _prepare_pool_rows(rows)
    for row in prepared:
        entry_ok, gate_status, blocked_by, top_buy, top_risk = _entry_gate(row)
        if top_risk and not entry_ok:
            components = _risk_components(row, top_risk)
            risk.append(_finalize_pool_row(
                row,
                pool_type="risk",
                rank_score=sum(components.values()),
                score_components=components,
                entry_gate_status=gate_status,
                blocked_by=blocked_by,
                top_buy=top_buy,
                top_risk=top_risk,
            ))
        elif entry_ok and top_buy:
            components = _entry_components(row, top_buy)
            focus.append(_finalize_pool_row(
                row,
                pool_type="focus",
                rank_score=sum(components.values()),
                score_components=components,
                entry_gate_status=gate_status,
                blocked_by=blocked_by,
                top_buy=top_buy,
                top_risk=top_risk,
            ))
        else:
            components = _watch_components(row, gate_status)
            watch.append(_finalize_pool_row(
                row,
                pool_type="watch",
                rank_score=sum(components.values()),
                score_components=components,
                entry_gate_status=gate_status,
                blocked_by=blocked_by,
                top_buy=top_buy,
                top_risk=top_risk,
            ))
    for bucket in (focus, risk, watch):
        bucket.sort(key=lambda item: (_float(item.get("rank_score")), _float(item.get("score"))), reverse=True)
    return {
        "focus": focus[:focus_limit],
        "risk": risk[:risk_limit],
        "watch": watch[:watch_limit],
        "skipped": {
            "focus": focus[focus_limit:],
            "risk": risk[risk_limit:],
            "watch": watch[watch_limit:],
        },
        "pool_counts": {
            "focus": len(focus),
            "risk": len(risk),
            "watch": len(watch),
            "total": len(prepared),
        },
    }


def _candidate_meta(rows: dict[str, dict[str, Any]]) -> dict[str, dict[str, int]]:
    by_source: Counter[str] = Counter()
    by_side: Counter[str] = Counter()
    by_freq: Counter[str] = Counter()
    for row in rows.values():
        for reason in row.get("inclusion_reasons") or []:
            if not isinstance(reason, dict):
                continue
            by_source[_text(reason.get("source_collection")) or "unknown"] += 1
            by_side[_text(reason.get("signal_side")) or "unknown"] += 1
            by_freq[_text(reason.get("freq")) or "unknown"] += 1
    return {
        "candidate_counts_by_source": dict(by_source),
        "candidate_counts_by_side": dict(by_side),
        "candidate_counts_by_freq": dict(by_freq),
    }


def _selected_rows(rows: dict[str, dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = _prepare_pool_rows(rows)
    for row in ordered:
        row["reason"] = " · ".join(
            _text(item.get("signal_type") or item.get("reason_type"))
            for item in row["inclusion_reasons"][:2]
            if _text(item.get("signal_type") or item.get("reason_type"))
        )
    ordered.sort(key=lambda item: (_float(item.get("sort_score")), _float(item.get("score"))), reverse=True)
    return ordered[:limit], ordered[limit:]


def sync_terminal_realtime_pool(db: Database, proxy_url: str = None) -> dict:
    """Build terminal_stock_pool and mirror the selected codes to legacy terminal_realtime_pool."""
    import config

    now = naive_market_now("A")
    stock_limit = int(os.getenv("TERMINAL_REALTIME_STOCK_LIMIT", "72"))
    risk_limit = int(os.getenv("TERMINAL_RISK_STOCK_LIMIT", "72"))
    watch_limit = int(os.getenv("TERMINAL_WATCH_STOCK_LIMIT", "120"))
    strict_sources = str(get_task_env("TERMINAL_POOL_STRICT_SOURCES", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}
    include_legacy_daily = os.getenv("TERMINAL_POOL_INCLUDE_LEGACY_DAILY", "false").strip().lower() in {"1", "true", "yes", "on"}
    fallback_min = max(0, int(os.getenv("TERMINAL_POOL_FALLBACK_MIN_STOCKS", "12")))
    index_codes = _index_codes()
    rows: dict[str, dict[str, Any]] = {}

    _add_user_pinned(rows, index_codes, now)
    _add_technical_signal_rows(rows, db, index_codes)
    _add_signal_rows(rows, db, index_codes, include_generated_daily=False)
    _add_chain_rows(rows, db, index_codes)
    _add_knowledge_rows(rows, db, index_codes)
    strict_candidate_count = len(rows)
    fallback_count = 0
    fallback_enabled = False
    if not strict_sources:
        if include_legacy_daily:
            _add_signal_rows(rows, db, index_codes, include_generated_daily=True, generated_daily_only=True)
        _add_strategy_rows(rows, db, index_codes)
        _add_active_pool(rows, db, index_codes)
        _add_recent_opened(rows, db, index_codes)
    elif strict_candidate_count < fallback_min:
        fallback_enabled = True
        fallback_count = _add_fallback_watch_rows(
            rows,
            db,
            index_codes,
            limit=fallback_min - strict_candidate_count,
            now=now,
        )

    split = _split_pool_rows(rows, focus_limit=stock_limit, risk_limit=risk_limit, watch_limit=watch_limit)
    focus_stocks = split["focus"]
    risk_stocks = split["risk"]
    watch_stocks = split["watch"]
    skipped_by_pool = split["skipped"]
    skipped = (skipped_by_pool.get("focus") or []) + (skipped_by_pool.get("risk") or []) + (skipped_by_pool.get("watch") or [])
    candidate_meta = _candidate_meta(rows)
    focus_reason_counts = Counter(
        reason.get("reason_type")
        for row in focus_stocks
        for reason in row.get("inclusion_reasons", [])
        if reason.get("reason_type")
    )
    pool_counts = dict(split["pool_counts"])
    pool_counts.update({
        "focus_selected": len(focus_stocks),
        "risk_selected": len(risk_stocks),
        "watch_selected": len(watch_stocks),
    })
    technical_freshness = db["data_freshness"].find_one(
        {"domain": "technical_signal", "market": "A", "collection": "terminal_technical_signals", "coverage_by_freq": {"$exists": True}},
        {"coverage_by_freq": 1, "required_freqs": 1, "optional_freqs": 1, "is_full_market_complete": 1, "coverage_status": 1},
        sort=[("updated_at", -1)],
    ) or {}
    pool_doc = {
        "pool": "terminal_stock_pool",
        "market": "A",
        "dt": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "updated_at": now,
        "stock_limit": stock_limit,
        "risk_limit": risk_limit,
        "watch_limit": watch_limit,
        "stocks": focus_stocks,
        "focus_stocks": focus_stocks,
        "risk_stocks": risk_stocks,
        "watch_stocks": watch_stocks,
        "skipped_stocks": skipped[:100],
        "skipped_by_pool": {key: value[:50] for key, value in skipped_by_pool.items()},
        "skipped_count": len(skipped),
        "candidate_count": len(rows),
        "strict_candidate_count": strict_candidate_count,
        "fallback_count": fallback_count,
        "fallback_enabled": fallback_enabled,
        "pool_counts": pool_counts,
        "reason_counts": dict(focus_reason_counts),
        **candidate_meta,
        "coverage_by_freq": technical_freshness.get("coverage_by_freq") or {},
        "required_freqs": technical_freshness.get("required_freqs") or ["日线", "周线", "30分钟"],
        "optional_freqs": technical_freshness.get("optional_freqs") or ["15分钟", "5分钟"],
        "is_full_market_complete": bool(technical_freshness.get("is_full_market_complete")),
        "coverage_status": _text(technical_freshness.get("coverage_status")) or "unknown",
        "ranking_version": POOL_RANKING_VERSION,
        "source": "whitebox_pool_builder",
        "source_policy": "postmarket_strict_with_fallback_watch" if strict_sources and fallback_enabled else ("postmarket_strict_technical_knowledge_chain" if strict_sources else "runtime_watch_and_signal_blend"),
        "selection_policy": "buy_entry_focus__risk_exit_separate__watch_preheat",
    }
    db["terminal_stock_pool"].update_one(
        {"pool": "terminal_stock_pool", "market": "A"},
        {"$set": pool_doc},
        upsert=True,
    )

    legacy_doc = {
        "pool": "terminal_realtime",
        "market": "A",
        "dt": pool_doc["dt"],
        "updated_at": now,
        "stocks": [row["raw_code"] for row in (focus_stocks + risk_stocks + watch_stocks)[: max(stock_limit, 72)]],
        "indices": list(getattr(config, "INDEX_AK_CODES", {}).values()),
        "industries": _top_heat_names(db, "industry", 20),
        "concepts": _top_heat_names(db, "concept", 20),
        "stock_limit": stock_limit,
        "source": "terminal_stock_pool_mirror",
    }
    db["terminal_realtime_pool"].update_one(
        {"pool": "terminal_realtime", "market": "A"},
        {"$set": legacy_doc},
        upsert=True,
    )

    db["data_freshness"].update_one(
        {"domain": "terminal_pool", "market": "A", "mode": "realtime", "collection": "terminal_stock_pool"},
        {"$set": {
            "domain": "terminal_pool",
            "market": "A",
            "mode": "realtime",
            "lane": "workbench_lane",
            "collection": "terminal_stock_pool",
            "freshness": "fresh" if focus_stocks else "empty",
            "latest_dt": now.date().isoformat(),
            "as_of": now.date().isoformat(),
            "updated_at": now,
            "stale_reason": "" if focus_stocks else "terminal_focus_stock_pool_empty",
            "count": len(focus_stocks),
            "candidate_count": len(rows),
            "strict_candidate_count": strict_candidate_count,
            "fallback_count": fallback_count,
            "skipped_count": len(skipped),
            "pool_counts": pool_counts,
            "reason_counts": dict(focus_reason_counts),
            **candidate_meta,
            "coverage_by_freq": pool_doc["coverage_by_freq"],
            "coverage_status": pool_doc["coverage_status"],
            "is_full_market_complete": pool_doc["is_full_market_complete"],
            "selection_policy": pool_doc["selection_policy"],
            "ranking_version": POOL_RANKING_VERSION,
        }},
        upsert=True,
    )
    logger.info(
        "terminal stock pool: focus=%d risk=%d watch=%d candidates=%d skipped=%d",
        len(focus_stocks),
        len(risk_stocks),
        len(watch_stocks),
        len(rows),
        len(skipped),
    )
    return {
        "inserted": len(focus_stocks),
        "stocks": len(focus_stocks),
        "focus_stocks": len(focus_stocks),
        "risk_stocks": len(risk_stocks),
        "watch_stocks": len(watch_stocks),
        "candidates": len(rows),
        "strict_candidates": strict_candidate_count,
        "fallback_candidates": fallback_count,
        "skipped": len(skipped),
        "indices": len(legacy_doc["indices"]),
        "industries": len(legacy_doc["industries"]),
        "concepts": len(legacy_doc["concepts"]),
        "pool_counts": pool_counts,
        "reason_counts": dict(focus_reason_counts),
        **candidate_meta,
        "coverage_status": pool_doc["coverage_status"],
        "is_full_market_complete": pool_doc["is_full_market_complete"],
        "ranking_version": POOL_RANKING_VERSION,
    }
