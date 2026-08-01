# -*- coding: utf-8 -*-
"""Realtime industry-chain heat snapshots for the trading terminal."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.chain_mapping_rules import filter_mapping_matches as _filter_mapping_matches
from signals.core.concept_carriers import load_industry_chains, match_industry_chains, non_chain_reason
from signals.core.market_time import naive_market_now
from signals.core.trading_dates import (
    a_share_realtime_day_key,
    normalized_a_share_realtime_minute,
)
from signals.sync.trade_date import a_share_task_trade_date

from ..retry import sync_retry

logger = logging.getLogger("signals.sync.chain_heat")

PHASES = {"warming", "accelerating", "diverging", "consensus_climax", "cooling", "risk_off"}
MIN_MARKET_LOGIC_OVERLAY_CHANGE = 0.8


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _date_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value or "")[:10]


def _day_start(day_text: str) -> datetime:
    return datetime.strptime(day_text[:10], "%Y-%m-%d")


def _doc_trade_date(doc: dict[str, Any], fallback: Any = None) -> str:
    return _date_text(doc.get("trade_date") or doc.get("dt") or doc.get("trade_minute") or fallback)


def _latest_heat_docs(db: Database, kind: str) -> list[dict[str, Any]]:
    expected_day = a_share_realtime_day_key()
    day_start = _day_start(expected_day)
    day_end = day_start + timedelta(days=1)
    query = {
        "kind": kind,
        "$or": [
            {"trade_date": expected_day},
            {"dt": {"$gte": day_start, "$lt": day_end}},
            {"trade_minute": {"$gte": day_start, "$lt": day_end}},
        ],
    }
    latest = db["board_heat_ticks"].find_one(query, {"trade_minute": 1}, sort=[("trade_minute", -1)])
    if not latest:
        latest = db["board_heat_ticks"].find_one({"kind": kind}, {"trade_minute": 1}, sort=[("trade_minute", -1)])
    if not latest or latest.get("trade_minute") is None:
        return []
    return list(db["board_heat_ticks"].find({"kind": kind, "trade_minute": latest["trade_minute"]}, {"_id": 0}))


def _history_by_name(db: Database, latest_minute: Any, names_by_kind: dict[str, set[str]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    if latest_minute is None:
        return {}
    start = latest_minute - timedelta(minutes=35)
    output: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for kind, names in names_by_kind.items():
        if not names:
            continue
        cursor = db["board_heat_ticks"].find(
            {"kind": kind, "name": {"$in": sorted(names)}, "trade_minute": {"$gte": start, "$lte": latest_minute}},
            {"_id": 0, "kind": 1, "name": 1, "trade_minute": 1, "change_pct": 1},
        ).sort("trade_minute", 1)
        for doc in cursor:
            key = (_text(doc.get("kind")), _text(doc.get("name")))
            output.setdefault(key, []).append(doc)
    return output


def _momentum(rows: list[dict[str, Any]], latest_change: float, minutes: int) -> float:
    if not rows:
        return 0.0
    latest_minute = rows[-1].get("trade_minute")
    if latest_minute is None:
        return 0.0
    threshold = latest_minute - timedelta(minutes=minutes)
    baseline = rows[0]
    for row in rows:
        if row.get("trade_minute") and row["trade_minute"] >= threshold:
            baseline = row
            break
    return round(latest_change - _float(baseline.get("change_pct")), 3)


def _row_heat_score(doc: dict[str, Any]) -> float:
    change = _float(doc.get("change_pct"))
    up = _int(doc.get("up_count"))
    down = _int(doc.get("down_count"))
    total = max(up + down, 1)
    breadth = (up - down) / total
    rank = _int(doc.get("rank_idx"), 999)
    rank_score = max(0.0, 100.0 - rank) / 20.0
    leader = max(_float(doc.get("leader_change_pct")), 0.0) * 0.7
    return round(change * 8.0 + breadth * 12.0 + rank_score + leader, 3)


def _phase(change_pct: float, up_count: int, down_count: int, m5: float, m15: float, m30: float) -> str:
    breadth = up_count - down_count
    total = max(up_count + down_count, 1)
    breadth_ratio = up_count / total
    if change_pct <= -1.0 or (change_pct < 0 and m15 < -0.4):
        return "risk_off"
    if m15 < -0.5 or m30 < -0.8:
        return "cooling"
    extended_momentum = m15 >= 0.6 or m30 >= 1.0
    if change_pct >= 3.0 and breadth_ratio >= 0.82 and m5 < 0.15 and extended_momentum:
        return "consensus_climax"
    if change_pct >= 1.0 and breadth <= 0:
        return "diverging"
    if m5 >= 0.25 and m15 >= 0.45:
        return "accelerating"
    return "warming"


def _range_pattern(phase: str, m5: float, m15: float, m30: float) -> str:
    if phase == "accelerating":
        return "short_mid_acceleration"
    if phase == "diverging":
        return "price_breadth_divergence"
    if phase == "consensus_climax":
        return "consensus_climax"
    if phase == "cooling":
        return "momentum_cooling"
    if phase == "risk_off":
        return "negative_or_breakdown"
    if m5 >= 0 and m15 >= 0 and m30 >= 0:
        return "steady_strength"
    return "early_warming"


def _trading_signal(phase: str) -> dict[str, str]:
    mapping = {
        "accelerating": ("chain_acceleration", "产业链加速，优先复核链主和弹性代表。", "5m/15m 热度转弱或领涨股回落。"),
        "warming": ("chain_warming", "产业链升温，观察扩散和节点共振。", "节点热度回落或上涨家数收缩。"),
        "diverging": ("chain_divergence", "涨幅和广度背离，谨慎追高。", "广度修复或涨幅回落。"),
        "consensus_climax": ("chain_consensus_climax", "产业链一致高潮，先别追，等热度回落后再看。", "热度回落后重新走出买点确认。"),
        "cooling": ("chain_cooling", "产业链降温，等待重新放量。", "15m/30m 动量重新转正。"),
        "risk_off": ("chain_risk_off", "产业链走弱，暂不参与。", "重新站回正涨幅且广度修复。"),
    }
    signal, action, invalidates = mapping.get(phase, mapping["warming"])
    return {"signal": signal, "trader_action": action, "invalidates_when": invalidates}


_SOURCE_KIND_LABELS = {
    "industry": "行业",
    "concept": "概念",
}


def _source_event_payload(item: dict[str, Any]) -> dict[str, Any]:
    kind = _text(item.get("kind"))
    kind_label = _SOURCE_KIND_LABELS.get(kind, kind or "来源")
    name = _text(item.get("name"))
    return {
        "kind": kind,
        "kind_label": kind_label,
        "name": name,
        "label": f"{kind_label}:{name}" if name else kind_label,
        "code": _text(item.get("code")),
        "change_pct": _float(item.get("change_pct")),
        "up_count": _int(item.get("up_count")),
        "down_count": _int(item.get("down_count")),
        "leader_name": _text(item.get("leader_name")),
        "leader_symbol": _text(item.get("leader_symbol")),
        "leader_change_pct": _float(item.get("leader_change_pct")),
        "rank": _int(item.get("rank")),
        "heat_score": _float(item.get("heat_score")),
        "mapping_confidence": _int(item.get("mapping_confidence")),
        "hit_terms": item.get("hit_terms") or [],
        "evidence_sources": item.get("evidence_sources") or [],
    }


def _route_explain(item: dict[str, Any]) -> str:
    source = _source_event_payload(item)
    source_name = source.get("name") or "未知来源"
    chain_name = _text(item.get("chain_name")) or "未映射产业链"
    node_name = _text(item.get("node_name"))
    target = "/".join([part for part in (chain_name, node_name) if part])
    return f"源[{source.get('kind_label')}] {source_name} -> {target}"


def _representatives(match: dict[str, Any]) -> list[dict[str, Any]]:
    reps: list[dict[str, Any]] = []
    for rep in match.get("representatives") or []:
        symbol = _text(rep.get("symbol"))
        if not symbol:
            continue
        reps.append({
            "symbol": symbol,
            "name": _text(rep.get("name")),
            "relation": _text(rep.get("relation")),
            "representative_type": _text(rep.get("representative_type")) or "core",
            "priority": _int(rep.get("priority")),
            "source_note": _text(rep.get("source_note")),
        })
    tier = {"core": 4, "upstream": 3, "downstream": 2, "elastic": 1}
    reps.sort(key=lambda item: (tier.get(item["representative_type"], 0), item["priority"]), reverse=True)
    return reps


def _pure_a_code(value: Any) -> str:
    raw = _text(value).upper()
    if not raw:
        return ""
    pure = raw.split(".", 1)[-1] if "." in raw else raw
    pure = pure.replace("SH", "").replace("SZ", "").replace("BJ", "")
    return pure if pure.isdigit() and len(pure) == 6 else ""


def _prefixed_a_symbol(code: str) -> str:
    if code.startswith(("6", "9")):
        return f"SH.{code}"
    if code.startswith(("4", "8")):
        return f"BJ.{code}"
    return f"SZ.{code}"


def _constituent_symbols(doc: dict[str, Any]) -> list[str]:
    output: list[str] = []
    for raw in doc.get("symbols") or doc.get("stocks") or doc.get("constituents") or []:
        code = _pure_a_code(raw)
        if code and code not in output:
            output.append(code)
    return output


def _stock_name(doc: dict[str, Any], code: str, quote: dict[str, Any] | None = None) -> str:
    names = doc.get("stock_names") or {}
    symbol = _prefixed_a_symbol(code)
    return _text(
        names.get(code)
        or names.get(symbol)
        or names.get(symbol.upper())
        or (quote or {}).get("name")
        or code
    )


def _source_constituent_doc(db: Database, *, kind: str, name: str) -> dict[str, Any]:
    collection_name = "concept_constituents" if kind == "concept" else "board_constituents"
    try:
        return db[collection_name].find_one(
            {"$or": [{"_id": name}, {"concept_name": name}, {"board_name": name}, {"name": name}]},
            {"_id": 0, "symbols": 1, "stocks": 1, "constituents": 1, "stock_names": 1, "source": 1, "updated_at": 1, "status": 1},
            sort=[("updated_at", -1)],
        ) or {}
    except Exception:
        return {}


def _latest_quote_context_by_code(db: Database) -> dict[str, dict[str, Any]]:
    try:
        latest = db["quote_snapshots"].find_one(
            {"trade_date": {"$exists": True}},
            {"trade_date": 1},
            sort=[("trade_date", -1), ("snapshot_at", -1)],
        ) or {}
        trade_date = _text(latest.get("trade_date"))
        if not trade_date:
            return {}
        rows = db["quote_snapshots"].find(
            {"trade_date": trade_date},
            {
                "_id": 0,
                "code": 1,
                "symbol": 1,
                "name": 1,
                "change_pct": 1,
                "turnover_pct": 1,
                "amount": 1,
                "price": 1,
                "trade_date": 1,
                "snapshot_at": 1,
                "freshness": 1,
                "is_stale": 1,
            },
        )
    except Exception:
        return {}
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = _pure_a_code(row.get("code") or row.get("symbol"))
        if not code:
            continue
        existing = output.get(code)
        snapshot_at = row.get("snapshot_at")
        if existing and existing.get("snapshot_at") and snapshot_at and snapshot_at < existing["snapshot_at"]:
            continue
        output[code] = {
            "symbol": _text(row.get("symbol")) or _prefixed_a_symbol(code),
            "name": _text(row.get("name")),
            "change_pct": _float(row.get("change_pct")),
            "turnover_pct": _float(row.get("turnover_pct")),
            "amount": _float(row.get("amount")),
            "price": _float(row.get("price")),
            "trade_date": trade_date,
            "snapshot_at": snapshot_at,
            "freshness": _text(row.get("freshness")),
            "is_stale": bool(row.get("is_stale")),
        }
    return output


def _source_constituent_representatives(
    db: Database,
    source: dict[str, Any],
    quote_by_code: dict[str, dict[str, Any]],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    kind = _text(source.get("kind"))
    name = _text(source.get("name"))
    if not kind or not name:
        return []
    doc = _source_constituent_doc(db, kind=kind, name=name)
    codes = _constituent_symbols(doc)
    if not codes:
        return []
    leader_name = _text(source.get("leader_name"))
    source_change = _float(source.get("change_pct"))
    require_positive = source_change > 0
    rows: list[dict[str, Any]] = []
    for code in codes:
        quote = quote_by_code.get(code, {})
        stock_name = _stock_name(doc, code, quote)
        quote_change = _float(quote.get("change_pct"), default=None)
        is_leader = bool(leader_name and stock_name == leader_name)
        if quote_change is None and is_leader:
            quote_change = _float(source.get("leader_change_pct"))
        if require_positive and (quote_change is None or quote_change <= 0):
            continue
        if quote_change is None:
            continue
        rep_type = "source_leader" if is_leader else f"{kind}_constituent"
        rows.append({
            "symbol": quote.get("symbol") or _prefixed_a_symbol(code),
            "name": stock_name,
            "relation": f"{name}真实涨幅成分",
            "source_note": f"{_SOURCE_KIND_LABELS.get(kind, kind)}成分 + 当日涨幅",
            "representative_type": rep_type,
            "priority": int((220 if is_leader else 160) + max(0.0, quote_change) * 10),
            "source": "source_board_constituents",
            "source_board_name": name,
            "source_board_kind": kind,
            "day_change_pct": quote_change,
            "turnover_pct": _float(quote.get("turnover_pct")),
            "amount": _float(quote.get("amount")),
            "price": quote.get("price"),
            "quote_trade_date": quote.get("trade_date"),
        })
    rows.sort(
        key=lambda item: (
            1 if item.get("representative_type") == "source_leader" else 0,
            _float(item.get("day_change_pct")),
            _float(item.get("amount")),
            _int(item.get("priority")),
        ),
        reverse=True,
    )
    return rows[:limit]


def _latest_concept_heat(db: Database, name: str) -> dict[str, Any]:
    try:
        return db["board_heat_ticks"].find_one(
            {"kind": "concept", "name": name},
            {
                "_id": 0,
                "kind": 1,
                "name": 1,
                "code": 1,
                "change_pct": 1,
                "up_count": 1,
                "down_count": 1,
                "leader_name": 1,
                "leader_symbol": 1,
                "leader_change_pct": 1,
                "rank_idx": 1,
                "trade_date": 1,
                "trade_minute": 1,
            },
            sort=[("trade_minute", -1)],
        ) or {}
    except Exception:
        return {}


def _overlay_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        1 if item.get("primary_source") else 0,
        1 if item.get("matched_source_leader") else 0,
        1 if item.get("matched_overlay_leader") else 0,
        _float(item.get("change_pct")),
        _int(item.get("matched_count")),
        _float(item.get("leader_change_pct")),
        -_int(item.get("source_order"), 999),
    )


def _source_market_overlays(
    db: Database,
    source_events: list[dict[str, Any]],
    *,
    limit: int = 8,
    only_non_chain: bool = False,
) -> list[dict[str, Any]]:
    overlays_by_name: dict[str, dict[str, Any]] = {}
    for source_order, source in enumerate(source_events[:5]):
        source_name = _text(source.get("name"))
        source_kind = _text(source.get("kind"))
        if not source_name or not source_kind:
            continue
        source_doc = _source_constituent_doc(db, kind=source_kind, name=source_name)
        source_codes = _constituent_symbols(source_doc)
        if not source_codes:
            continue
        try:
            cursor = db["concept_constituents"].find(
                {"symbols": {"$in": [_prefixed_a_symbol(code) for code in source_codes] + source_codes}},
                {"_id": 0, "concept_name": 1, "board_name": 1, "name": 1, "symbols": 1, "stock_names": 1},
            )
        except Exception:
            continue
        source_code_set = set(source_codes)
        source_stock_names = source_doc.get("stock_names") or {}
        for doc in cursor:
            concept_name = _text(doc.get("concept_name") or doc.get("board_name") or doc.get("name"))
            if not concept_name or concept_name == source_name:
                continue
            reason = non_chain_reason(concept_name)
            if only_non_chain:
                if not reason:
                    continue
            elif reason:
                continue
            concept_codes = set(_constituent_symbols(doc))
            matched_codes = sorted(source_code_set.intersection(concept_codes))
            if not matched_codes:
                continue
            heat = _latest_concept_heat(db, concept_name)
            change_pct = _float(heat.get("change_pct"), default=None)
            if change_pct is None or change_pct <= 0:
                continue
            concept_stock_names = doc.get("stock_names") or {}
            source_leader_name = _text(source.get("leader_name"))
            overlay_leader_name = _text(heat.get("leader_name"))
            matched_names = [
                _text(concept_stock_names.get(code) or concept_stock_names.get(_prefixed_a_symbol(code)) or source_stock_names.get(code) or source_stock_names.get(_prefixed_a_symbol(code)) or code)
                for code in matched_codes
            ]
            matched_source_leader = bool(source_leader_name and source_leader_name in matched_names)
            matched_overlay_leader = bool(overlay_leader_name and overlay_leader_name in matched_names)
            current = overlays_by_name.get(concept_name) or {
                "kind": "theme" if reason else "concept",
                "kind_label": "主题" if reason else "概念",
                "name": concept_name,
                "change_pct": change_pct,
                "leader_name": _text(heat.get("leader_name")),
                "leader_symbol": _text(heat.get("leader_symbol")),
                "leader_change_pct": _float(heat.get("leader_change_pct")),
                "up_count": _int(heat.get("up_count")),
                "down_count": _int(heat.get("down_count")),
                "rank": _int(heat.get("rank_idx")),
                "non_chain_reason": reason,
                "matched_symbols": [],
                "matched_names": [],
                "source_boards": [],
                "source_order": source_order,
                "primary_source": source_order == 0,
                "matched_source_leader": False,
                "matched_overlay_leader": False,
                "matched_source_leaders": [],
                "matched_overlay_leaders": [],
            }
            current_source_order = current.get("source_order")
            current["source_order"] = min(
                _int(current_source_order, source_order),
                source_order,
            )
            current["primary_source"] = bool(current.get("primary_source")) or source_order == 0
            current["matched_source_leader"] = bool(current.get("matched_source_leader")) or matched_source_leader
            current["matched_overlay_leader"] = bool(current.get("matched_overlay_leader")) or matched_overlay_leader
            if matched_source_leader:
                current["matched_source_leaders"] = list(dict.fromkeys([*current.get("matched_source_leaders", []), source_leader_name]))
            if matched_overlay_leader:
                current["matched_overlay_leaders"] = list(dict.fromkeys([*current.get("matched_overlay_leaders", []), overlay_leader_name]))
            current["change_pct"] = max(_float(current.get("change_pct")), change_pct)
            current["source_boards"] = list(dict.fromkeys([*current.get("source_boards", []), source_name]))
            current["matched_symbols"] = list(dict.fromkeys([*current.get("matched_symbols", []), *matched_codes]))[:8]
            current["matched_names"] = list(dict.fromkeys([*current.get("matched_names", []), *matched_names]))[:8]
            current["matched_count"] = len(current["matched_symbols"])
            overlays_by_name[concept_name] = current
    overlays = list(overlays_by_name.values())
    overlays.sort(key=_overlay_sort_key, reverse=True)
    return overlays[:limit]


def _source_concept_overlays(db: Database, source_events: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    return _source_market_overlays(db, source_events, limit=limit, only_non_chain=False)


def _source_theme_overlays(db: Database, source_events: list[dict[str, Any]], *, limit: int = 6) -> list[dict[str, Any]]:
    return _source_market_overlays(db, source_events, limit=limit, only_non_chain=True)


def _unique_overlays(items: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    for item in items:
        name = _text(item.get("name"))
        if not name:
            continue
        existing = by_name.get(name)
        if not existing or _overlay_sort_key(item) > _overlay_sort_key(existing):
            by_name[name] = item
    rows = list(by_name.values())
    rows.sort(key=_overlay_sort_key, reverse=True)
    return rows[:limit]


def _primary_overlay_for_top(top: dict[str, Any], overlays: list[dict[str, Any]]) -> dict[str, Any]:
    top_name = _text(top.get("name"))
    if not top_name:
        return {}
    anchored = [
        overlay
        for overlay in overlays
        if top_name in {_text(name) for name in overlay.get("source_boards") or []}
        and _float(overlay.get("change_pct")) >= MIN_MARKET_LOGIC_OVERLAY_CHANGE
    ]
    if not anchored:
        return {}
    anchored.sort(key=_overlay_sort_key, reverse=True)
    return anchored[0]


def _market_logic_payload(top: dict[str, Any], overlays: list[dict[str, Any]], themes: list[dict[str, Any]]) -> dict[str, Any]:
    primary_overlay = _primary_overlay_for_top(top, overlays)
    if primary_overlay:
        primary = primary_overlay
        return {
            "status": "hot_concept_overlay",
            "logic_name": primary.get("name"),
            "logic_kind": primary.get("kind"),
            "change_pct": primary.get("change_pct"),
            "leader_name": primary.get("leader_name"),
            "matched_names": primary.get("matched_names") or [],
            "source_boards": primary.get("source_boards") or [],
            "evidence": "source_board_constituents+concept_heat",
        }
    primary_theme = _primary_overlay_for_top(top, themes)
    if primary_theme:
        primary = primary_theme
        return {
            "status": "hot_theme_overlay",
            "logic_name": primary.get("name"),
            "logic_kind": primary.get("kind"),
            "change_pct": primary.get("change_pct"),
            "leader_name": primary.get("leader_name"),
            "matched_names": primary.get("matched_names") or [],
            "source_boards": primary.get("source_boards") or [],
            "evidence": "source_board_constituents+theme_heat",
        }
    return {
        "status": "source_board",
        "logic_name": top.get("name"),
        "logic_kind": top.get("kind"),
        "change_pct": top.get("change_pct"),
        "leader_name": top.get("leader_name"),
        "evidence": "board_heat_ticks",
    }


def _node_representative_codes(node: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    for key in (
        "core_representatives",
        "elastic_representatives",
        "upstream_representatives",
        "downstream_representatives",
    ):
        for rep in node.get(key) or []:
            code = _pure_a_code(rep.get("symbol"))
            if code:
                codes.add(code)
    return codes


def _market_logic_node_override(top: dict[str, Any], overlays: list[dict[str, Any]]) -> dict[str, Any]:
    """Promote display routing when hot evidence implies a more specific node.

    The source board taxonomy is still retained in node_id. This only creates a
    display/logic node so broad boards like 半导体材料 can surface the current
    traded sub-logic, e.g. 半导体材料成分 + 热门交叉概念 -> 半导体特气/前驱体.
    """

    chain_id = _text(top.get("chain_id"))
    current_node_id = _text(top.get("node_id"))
    top_name = _text(top.get("name"))
    if not chain_id or not top_name:
        return {}
    anchored = [
        overlay
        for overlay in overlays
        if top_name in {_text(name) for name in overlay.get("source_boards") or []}
        and _float(overlay.get("change_pct")) >= MIN_MARKET_LOGIC_OVERLAY_CHANGE
    ]
    if not anchored:
        return {}
    source_codes = {
        code
        for rep in top.get("source_representatives") or []
        for code in [_pure_a_code(rep.get("symbol"))]
        if code
    }
    overlay_names: list[str] = []
    for overlay in anchored:
        overlay_names.append(_text(overlay.get("name")))
    if not source_codes and not any(overlay.get("matched_symbols") for overlay in anchored):
        return {}
    chain = load_industry_chains().get(chain_id) or {}
    candidates: list[tuple[int, int, int, float, dict[str, Any], list[str], list[str]]] = []
    for node in chain.get("nodes") or []:
        node_id = _text(node.get("node_id"))
        if not node_id or node_id == current_node_id:
            continue
        node_codes = _node_representative_codes(node)
        source_overlap = sorted(source_codes.intersection(node_codes))
        contributing_overlays = []
        overlay_overlap_codes: set[str] = set()
        direct_semantic_overlay = False
        for overlay in anchored:
            matched_codes = {
                code
                for raw in overlay.get("matched_symbols") or []
                for code in [_pure_a_code(raw)]
                if code
            }
            overlap = matched_codes.intersection(node_codes)
            if not overlap:
                continue
            contributing_overlays.append(overlay)
            overlay_overlap_codes.update(overlap)
            overlay_name = _text(overlay.get("name"))
            for match in match_industry_chains(overlay_name):
                if (
                    _text(match.get("chain_id")) == chain_id
                    and _text(match.get("node_id")) == node_id
                    and _int(match.get("confidence")) >= 80
                ):
                    direct_semantic_overlay = True
                    break
        overlap = sorted(set(source_overlap).union(overlay_overlap_codes))
        if not overlap:
            continue
        if len(overlap) < 2 and not direct_semantic_overlay:
            continue
        overlay_strength = max((_float(overlay.get("change_pct")) for overlay in contributing_overlays), default=0.0)
        candidates.append((
            len(source_overlap),
            len(overlay_overlap_codes),
            len(overlap),
            overlay_strength,
            node,
            overlap,
            [_text(overlay.get("name")) for overlay in contributing_overlays],
        ))
    if not candidates:
        return {}
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
    source_overlap_count, overlay_overlap_count, overlap_count, overlay_strength, node, overlap, contributing_names = candidates[0]
    evidence_parts = ["industry_chains.yaml"]
    if source_overlap_count:
        evidence_parts.insert(0, "source_board_constituents")
    if overlay_overlap_count:
        evidence_parts.insert(-1, "source_concept_overlays")
    return {
        "chain_id": chain_id,
        "chain_name": chain.get("name") or top.get("chain_name"),
        "node_id": node.get("node_id"),
        "node_name": node.get("name"),
        "layer": node.get("layer"),
        "stage": node.get("stage"),
        "status": "hot_overlay_constituent_route",
        "evidence": "+".join(evidence_parts),
        "source_board": top_name,
        "overlay_names": [name for name in dict.fromkeys(contributing_names or overlay_names) if name],
        "matched_symbols": overlap[:8],
        "matched_count": overlap_count,
        "overlay_change_pct": overlay_strength,
        "taxonomy_node_id": current_node_id,
        "taxonomy_node_name": top.get("node_name"),
    }


def _mapped_items(db: Database) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    docs_by_kind = {
        "industry": _latest_heat_docs(db, "industry"),
        "concept": _latest_heat_docs(db, "concept"),
    }
    latest_minutes = [doc.get("trade_minute") for docs in docs_by_kind.values() for doc in docs if doc.get("trade_minute")]
    latest_minute = max(latest_minutes) if latest_minutes else None
    names_by_kind = {
        kind: {_text(doc.get("name")) for doc in docs if _text(doc.get("name"))}
        for kind, docs in docs_by_kind.items()
    }
    history = _history_by_name(db, latest_minute, names_by_kind)
    quote_by_code = _latest_quote_context_by_code(db)

    mapped: list[dict[str, Any]] = []
    unmapped = 0
    non_chain = 0
    low_confidence = 0
    ambiguous_industry_only = 0
    for kind, docs in docs_by_kind.items():
        for doc in docs:
            name = _text(doc.get("name"))
            if not name:
                continue
            reason = non_chain_reason(name)
            if reason:
                non_chain += 1
                continue
            matches = [item for item in match_industry_chains(name) if _int(item.get("confidence")) >= 60]
            if not matches:
                unmapped += 1
                continue
            matches, filter_reason = _filter_mapping_matches(matches)
            if not matches:
                if filter_reason == "ambiguous_industry_only":
                    ambiguous_industry_only += 1
                else:
                    unmapped += 1
                continue
            best_score = _int(matches[0].get("score"))
            for match in matches:
                if _int(match.get("score")) < best_score - 15:
                    low_confidence += 1
                    continue
                change = _float(doc.get("change_pct"))
                hist = history.get((kind, name), [])
                item = {
                    "kind": kind,
                    "name": name,
                    "code": _text(doc.get("code")),
                    "source": _text(doc.get("source")) or "eastmoney_push2delay",
                    "rank": _int(doc.get("rank_idx")) + 1,
                    "change_pct": change,
                    "up_count": _int(doc.get("up_count")),
                    "down_count": _int(doc.get("down_count")),
                    "leader_name": _text(doc.get("leader_name")),
                    "leader_symbol": _text(doc.get("leader_symbol")),
                    "leader_change_pct": _float(doc.get("leader_change_pct")),
                    "trade_minute": doc.get("trade_minute"),
                    "trade_date": _doc_trade_date(doc),
                    "heat_score": _row_heat_score(doc),
                    "momentum_5m": _momentum(hist, change, 5),
                    "momentum_15m": _momentum(hist, change, 15),
                    "momentum_30m": _momentum(hist, change, 30),
                    "chain_id": _text(match.get("chain_id")),
                    "chain_name": _text(match.get("chain_name")),
                    "node_id": _text(match.get("node_id")),
                    "node_name": _text(match.get("node_name")),
                    "layer": _text(match.get("layer")),
                    "stage": _text(match.get("stage")),
                    "mapping_confidence": _int(match.get("confidence")),
                    "mapping_type": "semantic_taxonomy",
                    "hit_terms": match.get("hit_terms") or [],
                    "evidence_sources": match.get("evidence_sources") or [],
                    "representatives": _representatives(match),
                }
                if change > 0 and _int(doc.get("rank_idx"), 999) <= 300:
                    item["source_representatives"] = _source_constituent_representatives(db, item, quote_by_code)
                if change > 0 and (_int(doc.get("rank_idx"), 999) <= 120 or _float(item.get("heat_score")) >= 10):
                    item["source_concept_overlays"] = _source_concept_overlays(db, [item], limit=6)
                    item["source_theme_overlays"] = _source_theme_overlays(db, [item], limit=4)
                mapped.append(item)
    meta = {
        "latest_minute": latest_minute,
        "input_counts": {kind: len(docs) for kind, docs in docs_by_kind.items()},
        "mapped_count": len(mapped),
        "unmapped_count": unmapped,
        "non_chain_count": non_chain,
        "low_confidence_count": low_confidence,
        "ambiguous_industry_only_count": ambiguous_industry_only,
    }
    return mapped, meta


def _aggregate(mapped: list[dict[str, Any]], latest_minute: Any) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in mapped:
        key = (item["chain_id"], item["node_id"] or "default")
        if key[0]:
            buckets.setdefault(key, []).append(item)

    snapshots: list[dict[str, Any]] = []
    now = naive_market_now("A")
    for (chain_id, node_id), items in buckets.items():
        items.sort(key=lambda item: item["heat_score"], reverse=True)
        top = items[0]
        up_count = sum(_int(item.get("up_count")) for item in items[:5])
        down_count = sum(_int(item.get("down_count")) for item in items[:5])
        heat_score = round(sum(_float(item.get("heat_score")) for item in items[:5]) / min(len(items), 5), 3)
        m5 = round(sum(_float(item.get("momentum_5m")) for item in items[:5]) / min(len(items), 5), 3)
        m15 = round(sum(_float(item.get("momentum_15m")) for item in items[:5]) / min(len(items), 5), 3)
        m30 = round(sum(_float(item.get("momentum_30m")) for item in items[:5]) / min(len(items), 5), 3)
        mapping_confidence = round(sum(_float(item.get("mapping_confidence")) for item in items[:5]) / min(len(items), 5), 1)
        trade_date = _doc_trade_date(top, latest_minute) or a_share_realtime_day_key(now=now)
        phase = _phase(_float(top.get("change_pct")), up_count, down_count, m5, m15, m30)
        signal = _trading_signal(phase)
        reps: dict[str, dict[str, Any]] = {}
        for item in items:
            for rep in [*(item.get("source_representatives") or []), *(item.get("representatives") or [])]:
                symbol = _text(rep.get("symbol")).upper()
                if symbol and (symbol not in reps or _int(rep.get("priority")) > _int(reps[symbol].get("priority"))):
                    reps[symbol] = dict(rep)
        representatives = sorted(
            reps.values(),
            key=lambda item: (
                {
                    "source_leader": 7,
                    "concept_constituent": 6,
                    "industry_constituent": 5,
                    "core": 4,
                    "upstream": 3,
                    "downstream": 2,
                    "elastic": 1,
                }.get(_text(item.get("representative_type")), 0),
                _int(item.get("priority")),
                _float(item.get("day_change_pct")),
            ),
            reverse=True,
        )[:12]
        source_events = [_source_event_payload(item) for item in items[:10]]
        source_concept_overlays = _unique_overlays(
            [overlay for item in items[:10] for overlay in item.get("source_concept_overlays") or []],
            limit=8,
        )
        source_theme_overlays = _unique_overlays(
            [overlay for item in items[:10] for overlay in item.get("source_theme_overlays") or []],
            limit=6,
        )
        source_event_concept_overlays = [
            {"source": _source_event_payload(item), "concepts": (item.get("source_concept_overlays") or [])[:3]}
            for item in items[:10]
            if item.get("source_concept_overlays")
        ][:6]
        source_event_theme_overlays = [
            {"source": _source_event_payload(item), "themes": (item.get("source_theme_overlays") or [])[:2]}
            for item in items[:10]
            if item.get("source_theme_overlays")
        ][:6]
        market_logic_node = _market_logic_node_override(top, source_concept_overlays)
        market_logic = _market_logic_payload(top, source_concept_overlays, source_theme_overlays)
        if market_logic_node:
            market_logic["route_node_id"] = market_logic_node.get("node_id")
            market_logic["route_node_name"] = market_logic_node.get("node_name")
            market_logic["route_status"] = market_logic_node.get("status")
            market_logic["route_evidence"] = market_logic_node.get("evidence")
            market_logic["route_overlay_names"] = market_logic_node.get("overlay_names") or []
        source_kind_mix = {
            kind: sum(1 for item in items if _text(item.get("kind")) == kind)
            for kind in ("industry", "concept")
        }
        snapshots.append({
            "market": "A",
            "dt": _day_start(trade_date),
            "trade_date": trade_date,
            "trade_minute": latest_minute or normalized_a_share_realtime_minute(now=now),
            "updated_at": now,
            "chain_id": chain_id,
            "chain_name": top.get("chain_name"),
            "node_id": node_id,
            "node_name": top.get("node_name"),
            "layer": top.get("layer"),
            "stage": top.get("stage"),
            "rank": 0,
            "change_pct": _float(top.get("change_pct")),
            "heat_score": heat_score,
            "momentum_5m": m5,
            "momentum_15m": m15,
            "momentum_30m": m30,
            "range_pattern": _range_pattern(phase, m5, m15, m30),
            "phase": phase,
            "trading_signal": signal["signal"],
            "latest_signal": signal["signal"],
            "trader_action": signal["trader_action"],
            "invalidates_when": signal["invalidates_when"],
            "up_count": up_count,
            "down_count": down_count,
            "leader_name": top.get("leader_name"),
            "leader_symbol": top.get("leader_symbol"),
            "leader_change_pct": top.get("leader_change_pct"),
            "source": "chain_heat_snapshots",
            "heat_source": "eastmoney_push2delay",
            "ranking_source": "+".join(sorted({item.get("kind", "") for item in items if item.get("kind")})),
            "taxonomy_source": "industry_chains.yaml",
            "mapping_status": "mapped",
            "mapping_confidence": mapping_confidence,
            "integrated_count": len(items),
            "integrated_domains": items[:10],
            "source_driver": source_events[0] if source_events else {},
            "source_events": source_events,
            "source_concept_overlays": source_concept_overlays,
            "source_event_concept_overlays": source_event_concept_overlays,
            "source_theme_overlays": source_theme_overlays,
            "source_event_theme_overlays": source_event_theme_overlays,
            "market_logic": market_logic,
            "market_logic_node": market_logic_node,
            "source_kind_mix": source_kind_mix,
            "route_explain": _route_explain(top),
            "route_confidence": _int(top.get("mapping_confidence")),
            "route_basis": {
                "hit_terms": top.get("hit_terms") or [],
                "evidence_sources": top.get("evidence_sources") or [],
            },
            "representatives": representatives,
            "evidence_sources": ["board_heat_ticks", "industry_chains.yaml"],
        })
    snapshots.sort(key=lambda item: (_float(item.get("heat_score")), _float(item.get("change_pct"))), reverse=True)
    for idx, item in enumerate(snapshots, start=1):
        item["rank"] = idx
    return snapshots


@sync_retry(max_attempts=2, min_wait=1)
def sync_chain_heat_snapshots(db: Database, proxy_url: str = None) -> dict:
    mapped, meta = _mapped_items(db)
    latest_minute = meta.get("latest_minute")
    snapshots = _aggregate(mapped, latest_minute)
    if not snapshots:
        now = naive_market_now("A")
        trade_date = a_share_task_trade_date(now=now)
        trade_minute = (
            datetime.fromisoformat(trade_date).replace(hour=15, minute=0, second=0, microsecond=0)
            if trade_date != now.date().isoformat()
            else normalized_a_share_realtime_minute(now=now)
        )
        db["data_freshness"].update_one(
            {"domain": "chain_heat", "market": "A", "mode": "realtime", "collection": "chain_heat_snapshots"},
            {"$set": {
                "domain": "chain_heat",
                "market": "A",
                "mode": "realtime",
                "lane": "board_lane",
                "collection": "chain_heat_snapshots",
                "freshness": "empty",
                "latest_dt": trade_minute.isoformat(timespec="minutes"),
                "as_of": trade_date,
                "updated_at": now,
                "stale_reason": "no_mapped_chain_heat",
                "count": 0,
                **meta,
            }},
            upsert=True,
        )
        return {"status": "empty", "inserted": 0, **meta}

    ops = [
        UpdateOne(
            {
                "market": "A",
                "chain_id": doc["chain_id"],
                "node_id": doc["node_id"],
                "trade_minute": doc["trade_minute"],
            },
            {"$set": doc},
            upsert=True,
        )
        for doc in snapshots
    ]
    trade_minute = snapshots[0].get("trade_minute")
    valid_nodes = [
        {"chain_id": doc["chain_id"], "node_id": doc["node_id"]}
        for doc in snapshots
    ]
    stale_deleted = 0
    if trade_minute is not None and valid_nodes:
        stale_deleted = int(db["chain_heat_snapshots"].delete_many({
            "market": "A",
            "trade_minute": trade_minute,
            "$nor": valid_nodes,
        }).deleted_count)
    result = db["chain_heat_snapshots"].bulk_write(ops, ordered=False)
    now = naive_market_now("A")
    written = int(result.upserted_count + result.modified_count)
    trade_date = _doc_trade_date(snapshots[0], latest_minute) if snapshots else a_share_realtime_day_key(now=now)
    db["data_freshness"].update_one(
        {"domain": "chain_heat", "market": "A", "mode": "realtime", "collection": "chain_heat_snapshots"},
        {"$set": {
            "domain": "chain_heat",
            "market": "A",
            "mode": "realtime",
            "lane": "board_lane",
            "collection": "chain_heat_snapshots",
            "freshness": "fresh",
            "latest_dt": latest_minute.isoformat(timespec="minutes") if latest_minute else now.isoformat(timespec="minutes"),
            "as_of": trade_date,
            "updated_at": now,
            "stale_reason": "",
            "count": len(snapshots),
            **meta,
        }},
        upsert=True,
    )
    logger.info("chain heat snapshots: %d nodes, written=%d, stale_deleted=%d", len(snapshots), written, stale_deleted)
    return {"status": "ok", "inserted": written, "nodes": len(snapshots), "stale_deleted": stale_deleted, **meta}
