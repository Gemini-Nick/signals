# -*- coding: utf-8 -*-
"""Realtime industry-chain heat snapshots for the trading terminal."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.concept_carriers import match_industry_chains, non_chain_reason
from signals.core.market_time import naive_market_now
from signals.core.trading_dates import normalized_trade_minute, trading_day_key

from ..retry import sync_retry

logger = logging.getLogger("signals.sync.chain_heat")

PHASES = {"warming", "accelerating", "diverging", "consensus_climax", "cooling", "risk_off"}


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
    expected_day = trading_day_key("A")
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
    reps.sort(key=lambda item: (item["representative_type"] == "core", item["priority"]), reverse=True)
    return reps


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

    mapped: list[dict[str, Any]] = []
    unmapped = 0
    non_chain = 0
    low_confidence = 0
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
            best_score = _int(matches[0].get("score"))
            for match in matches:
                if _int(match.get("score")) < best_score - 15:
                    low_confidence += 1
                    continue
                change = _float(doc.get("change_pct"))
                hist = history.get((kind, name), [])
                mapped.append({
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
                    "hit_terms": match.get("hit_terms") or [],
                    "evidence_sources": match.get("evidence_sources") or [],
                    "representatives": _representatives(match),
                })
    meta = {
        "latest_minute": latest_minute,
        "input_counts": {kind: len(docs) for kind, docs in docs_by_kind.items()},
        "mapped_count": len(mapped),
        "unmapped_count": unmapped,
        "non_chain_count": non_chain,
        "low_confidence_count": low_confidence,
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
        trade_date = _doc_trade_date(top, latest_minute) or trading_day_key("A", now=now)
        phase = _phase(_float(top.get("change_pct")), up_count, down_count, m5, m15, m30)
        signal = _trading_signal(phase)
        reps: dict[str, dict[str, Any]] = {}
        for item in items:
            for rep in item.get("representatives") or []:
                symbol = _text(rep.get("symbol")).upper()
                if symbol and (symbol not in reps or _int(rep.get("priority")) > _int(reps[symbol].get("priority"))):
                    reps[symbol] = dict(rep)
        representatives = sorted(
            reps.values(),
            key=lambda item: (item.get("representative_type") == "core", _int(item.get("priority"))),
            reverse=True,
        )[:12]
        snapshots.append({
            "market": "A",
            "dt": _day_start(trade_date),
            "trade_date": trade_date,
            "trade_minute": latest_minute or normalized_trade_minute("A", now=now),
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
        trade_date = trading_day_key("A", now=now)
        trade_minute = normalized_trade_minute("A", now=now)
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
    trade_date = _doc_trade_date(snapshots[0], latest_minute) if snapshots else trading_day_key("A", now=now)
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
