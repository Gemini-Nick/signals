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
CHAN_TOKENS = ("一买", "二买", "三买", "一卖", "二卖", "三卖", "背驰", "中枢", "笔", "线段")
BUY_FREQ_BONUS = {"30分钟": 120, "30min": 120, "30m": 120, "15分钟": 110, "15min": 110, "15m": 110, "5分钟": 80, "5min": 80, "5m": 80}
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
    "user_pinned": 10000,
    "technical_signal": 880,
    "generated_risk_signal": 950,
    "custom_signal": 620,
    "chan_signal": 620,
    "knowledge_confirmed": 260,
    "knowledge_conflict": 240,
    "knowledge_watch": 160,
    "chain_core_rep": 700,
    "chain_elastic_rep": 620,
    "source_leader": 600,
    "constituent_hot": 520,
    "active_pool_watch": 260,
    "recent_opened": 180,
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


def _index_codes() -> set[str]:
    import config

    return {
        _pure_a_code(symbol)
        for symbol in getattr(config, "INDEX_AK_CODES", {}).values()
        if _pure_a_code(symbol)
    }


def _signal_text(row: dict[str, Any]) -> str:
    return " ".join(_text(row.get(key)) for key in ("signal_type", "type", "reason", "summary", "details"))


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
    source = _text(row.get("source") or row.get("data_source")).lower()
    side = _signal_side(row)
    if "czsc" in source or "chan" in source:
        return "chan_signal"
    if source.startswith("sqlite.backtest"):
        return "custom_signal"
    if source.startswith("sync.signal_pool.generated") or side == "sell":
        return "generated_risk_signal"
    return "custom_signal"


def _is_generated_daily_signal(row: dict[str, Any]) -> bool:
    source = _text(row.get("source") or row.get("data_source")).lower()
    signal_type = _text(row.get("signal_type") or row.get("type") or row.get("reason"))
    freq = _text(row.get("freq") or row.get("timeframe"))
    return (
        source.startswith("sync.signal_pool.generated")
        or signal_type.startswith("日线候选")
        or signal_type.startswith("日线预警")
        or (freq == "日线" and source.startswith("sync.signal_pool"))
    )


def _is_hard_screen_signal(row: dict[str, Any]) -> bool:
    source = _text(row.get("source") or row.get("data_source")).lower()
    if source.startswith("sqlite.backtest.signal_records"):
        return True
    if "czsc" in source or "chan" in source:
        return True
    signal_type = _signal_text(row)
    return any(token in signal_type for token in CHAN_TOKENS)


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
    row = rows.setdefault(code, _empty_row(code, name))
    if name and not row.get("name"):
        row["name"] = name
    reason_type = _text(reason.get("reason_type"))
    base_weight = REASON_WEIGHTS.get(reason_type, 100)
    freq = _text(reason.get("freq"))
    signal_side = _text(reason.get("signal_side"))
    side_bonus = 180 if signal_side == "sell" else 0
    weight = base_weight + BUY_FREQ_BONUS.get(freq, 0) + side_bonus + _float(reason.get("score")) * 0.05 + _float(reason.get("heat_score")) * 0.05
    normalized = {
        "reason_type": reason_type,
        "weight": round(weight, 3),
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
    if reason_type == "technical_signal":
        resonance_context = normalized["resonance_context"]
        if not resonance_context and isinstance(normalized["evidence"], dict):
            resonance_context = normalized["evidence"].get("resonance_context") or {}
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
        }
        if resonance_context:
            row["resonance_context"] = resonance_context
    if reason_type.startswith("knowledge_"):
        row["knowledge_confirmation"] = {
            "status": normalized.get("knowledge_status") or reason_type.replace("knowledge_", ""),
            "sentiment": _text(reason.get("sentiment")),
            "source_collection": normalized["source_collection"],
            "source_doc_id": normalized["source_doc_id"],
            "as_of": normalized["as_of"],
            "evidence": normalized["evidence"],
        }
    if top.get("signal_side") == "sell":
        row["action_status"] = "risk_review"
        row["trader_action"] = "风险复核"
        row["next_action"] = row["trader_action"]
        row["invalidates_when"] = "卖出/风险信号解除或重新站回关键周期"
    elif top["reason_type"] in {"custom_signal", "chan_signal", "technical_signal"}:
        row["action_status"] = "buy_candidate"
        row["trader_action"] = "等待5m确认" if top.get("freq") not in {"5m", "5min", "5分钟"} else "可试仓"
        row["next_action"] = row["trader_action"]
        row["invalidates_when"] = "5m 无法确认或上级周期转弱"
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
    elif top["reason_type"].startswith("chain_") or top["reason_type"] in {"source_leader", "constituent_hot"}:
        row["action_status"] = "chain_watch"
        row["trader_action"] = "观察产业链共振"
        row["next_action"] = row["trader_action"]
        row["invalidates_when"] = "产业链节点热度转弱或领涨股回落"

    technical = next((item for item in row["inclusion_reasons"] if item.get("reason_type") == "technical_signal"), None)
    if technical:
        if technical.get("signal_side") == "sell":
            row["action_status"] = "risk_review"
            row["trader_action"] = "风险复核"
            row["next_action"] = row["trader_action"]
            row["invalidates_when"] = "卖出/风险信号解除或重新站回关键周期"
        elif row.get("action_status") in {"watch", "knowledge_watch", "chain_watch"}:
            row["action_status"] = "buy_candidate"
            row["trader_action"] = "等待5m确认" if technical.get("freq") not in {"5m", "5min", "5分钟"} else "可试仓"
            row["next_action"] = row["trader_action"]
            row["invalidates_when"] = "5m 无法确认或上级周期转弱"
    if (row.get("knowledge_confirmation") or {}).get("status") == "conflict" and row.get("action_status") == "buy_candidate":
        row["action_status"] = "knowledge_conflict"
        row["trader_action"] = "知识库冲突复核"
        row["next_action"] = row["trader_action"]
        row["invalidates_when"] = "知识观点或技术信号解除冲突"


def _add_user_pinned(rows: dict[str, dict[str, Any]], index_codes: set[str], now) -> None:
    import config

    values = os.getenv("TERMINAL_REALTIME_PRIORITY_CODES", "688802,300575").replace(";", ",").split(",")
    values.extend(getattr(config, "WHITELIST", []))
    for value in values:
        code = _pure_a_code(value)
        _add_reason(rows, code, {
            "reason_type": "user_pinned",
            "source_collection": "config",
            "source_doc_id": "TERMINAL_REALTIME_PRIORITY_CODES/WHITELIST",
            "signal_type": "用户重点观察",
            "signal_side": "buy",
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
            "score": _float(signal.get("score") or signal.get("total_score")),
            "confidence": _float(signal.get("confidence")),
            "as_of": _text(signal.get("signal_date") or signal.get("updated_at"))[:10],
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
            "reason_type": "technical_signal",
            "source_collection": "terminal_technical_signals",
            "source_doc_id": _source_doc_id(signal),
            "signal_type": _text(signal.get("signal_type")),
            "signal_side": _text(signal.get("signal_side")) or _signal_side(signal),
            "signal_family": _text(signal.get("signal_family")) or "hard_technical",
            "freq": _text(signal.get("freq")),
            "score": _float(signal.get("total_score") or signal.get("score")),
            "confidence": _float(signal.get("confidence")),
            "as_of": _text(signal.get("as_of") or signal.get("updated_at"))[:10],
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
            if tech_reason:
                tech_side = _text(tech_reason.get("signal_side"))
        sentiment = _text(view.get("latest_sentiment") or view.get("sentiment"))
        reason_type, status = _knowledge_status_for(sentiment, tech_side)
        sources = view.get("sources") if isinstance(view.get("sources"), list) else []
        _add_reason(rows, code, {
            "reason_type": reason_type,
            "source_collection": "knowledge_market_views",
            "source_doc_id": _text(view.get("view_id")),
            "signal_type": f"知识库{sentiment or '覆盖'}",
            "signal_side": tech_side or "neutral",
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
            _add_reason(rows, item.get("symbol") or item.get("code"), {
                "reason_type": _reason_type_for_signal(probe),
                "source_collection": "strategy_snapshots",
                "source_doc_id": source_doc_id,
                "signal_type": signal_type,
                "signal_side": _signal_side(probe),
                "signal_family": _signal_family(probe),
                "freq": _text(metadata.get("freq") or item.get("freq")),
                "score": _float(item.get("score") or metadata.get("score")),
                "confidence": _float(item.get("confidence") or metadata.get("confidence")),
                "board_or_concept": _text(metadata.get("theme")),
                "as_of": as_of,
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
            _add_reason(rows, rep.get("symbol"), {
                "reason_type": rep_type,
                "source_collection": "chain_heat_snapshots",
                "source_doc_id": chain_key,
                "signal_type": _text(chain.get("trading_signal")),
                "signal_side": "buy" if chain.get("phase") not in {"risk_off", "cooling"} else "sell",
                "score": _float(chain.get("heat_score")),
                "confidence": _float(rep.get("priority")),
                "chain_id": _text(chain.get("chain_id")),
                "node_id": _text(chain.get("node_id")),
                "board_or_concept": board_or_concept,
                "as_of": _text(chain.get("trade_minute")),
                "evidence": {"phase": chain.get("phase"), "range_pattern": chain.get("range_pattern")},
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
                    "signal_side": "buy",
                    "score": _float(domain.get("leader_change_pct")),
                    "confidence": _float(domain.get("mapping_confidence")),
                    "chain_id": _text(chain.get("chain_id")),
                    "node_id": _text(chain.get("node_id")),
                    "board_or_concept": _text(domain.get("name")),
                    "as_of": _text(chain.get("trade_minute")),
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
                    "signal_side": "buy",
                    "heat_score": _float(chain.get("heat_score")),
                    "confidence": _float(domain.get("mapping_confidence")),
                    "chain_id": _text(chain.get("chain_id")),
                    "node_id": _text(chain.get("node_id")),
                    "board_or_concept": _text(domain.get("name")),
                    "as_of": _text(chain.get("trade_minute")),
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


def _selected_rows(rows: dict[str, dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = list(rows.values())
    for row in ordered:
        row["inclusion_reasons"] = sorted(row.get("inclusion_reasons") or [], key=lambda item: _float(item.get("weight")), reverse=True)[:12]
        row["reason"] = " · ".join(
            _text(item.get("signal_type") or item.get("reason_type"))
            for item in row["inclusion_reasons"][:2]
            if _text(item.get("signal_type") or item.get("reason_type"))
        )
        row["exit_condition"] = row.get("invalidates_when")
        row["next_action"] = row.get("next_action") or row.get("trader_action") or "观察"
        if not isinstance(row.get("technical_evidence"), dict) or not row.get("technical_evidence"):
            row["technical_evidence"] = {"status": "missing", "note": "watch_only_not_buy_candidate"}
        if not isinstance(row.get("knowledge_confirmation"), dict) or not row.get("knowledge_confirmation"):
            row["knowledge_confirmation"] = {"status": "none"}
    ordered.sort(key=lambda item: (_float(item.get("sort_score")), _float(item.get("score"))), reverse=True)
    return ordered[:limit], ordered[limit:]


def sync_terminal_realtime_pool(db: Database, proxy_url: str = None) -> dict:
    """Build terminal_stock_pool and mirror the selected codes to legacy terminal_realtime_pool."""
    import config

    now = naive_market_now("A")
    stock_limit = int(os.getenv("TERMINAL_REALTIME_STOCK_LIMIT", "72"))
    strict_sources = str(get_task_env("TERMINAL_POOL_STRICT_SOURCES", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}
    include_legacy_daily = os.getenv("TERMINAL_POOL_INCLUDE_LEGACY_DAILY", "false").strip().lower() in {"1", "true", "yes", "on"}
    index_codes = _index_codes()
    rows: dict[str, dict[str, Any]] = {}

    _add_user_pinned(rows, index_codes, now)
    _add_technical_signal_rows(rows, db, index_codes)
    _add_signal_rows(rows, db, index_codes, include_generated_daily=False)
    _add_chain_rows(rows, db, index_codes)
    _add_knowledge_rows(rows, db, index_codes)
    if not strict_sources:
        if include_legacy_daily:
            _add_signal_rows(rows, db, index_codes, include_generated_daily=True, generated_daily_only=True)
        _add_strategy_rows(rows, db, index_codes)
        _add_active_pool(rows, db, index_codes)
        _add_recent_opened(rows, db, index_codes)

    selected, skipped = _selected_rows(rows, stock_limit)
    reason_counts = Counter(
        reason.get("reason_type")
        for row in selected
        for reason in row.get("inclusion_reasons", [])
        if reason.get("reason_type")
    )
    pool_doc = {
        "pool": "terminal_stock_pool",
        "market": "A",
        "dt": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "updated_at": now,
        "stock_limit": stock_limit,
        "stocks": selected,
        "skipped_stocks": skipped[:100],
        "skipped_count": len(skipped),
        "candidate_count": len(rows),
        "reason_counts": dict(reason_counts),
        "source": "whitebox_pool_builder",
        "source_policy": "postmarket_strict_technical_knowledge_chain" if strict_sources else "runtime_watch_and_signal_blend",
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
        "stocks": [row["raw_code"] for row in selected],
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
            "freshness": "fresh" if selected else "empty",
            "latest_dt": now.date().isoformat(),
            "as_of": now.date().isoformat(),
            "updated_at": now,
            "stale_reason": "" if selected else "terminal_stock_pool_empty",
            "count": len(selected),
            "candidate_count": len(rows),
            "skipped_count": len(skipped),
            "reason_counts": dict(reason_counts),
        }},
        upsert=True,
    )
    logger.info("terminal stock pool: selected=%d candidates=%d skipped=%d", len(selected), len(rows), len(skipped))
    return {
        "inserted": len(selected),
        "stocks": len(selected),
        "candidates": len(rows),
        "skipped": len(skipped),
        "indices": len(legacy_doc["indices"]),
        "industries": len(legacy_doc["industries"]),
        "concepts": len(legacy_doc["concepts"]),
        "reason_counts": dict(reason_counts),
    }
