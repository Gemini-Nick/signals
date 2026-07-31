# -*- coding: utf-8 -*-
"""Build a postmarket concept/chain relationship graph for the terminal."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.market_time import naive_market_now

logger = logging.getLogger("signals.sync.concept_relationship_graph")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _latest_chain_docs(db: Database) -> tuple[list[dict[str, Any]], Any]:
    latest = db["chain_heat_snapshots"].find_one({"market": "A"}, {"trade_minute": 1}, sort=[("trade_minute", -1)])
    if not latest or latest.get("trade_minute") is None:
        return [], None
    docs = list(db["chain_heat_snapshots"].find(
        {"market": "A", "trade_minute": latest["trade_minute"]},
        {"_id": 0},
    ).sort("rank", 1))
    return [dict(item) for item in docs if isinstance(item, dict)], latest.get("trade_minute")


def _knowledge_views(db: Database) -> list[dict[str, Any]]:
    docs = list(db["knowledge_market_views"].find(
        {"market": "A"},
        {"_id": 0},
    ).sort([("updated_at", -1)]).limit(200))
    return [dict(item) for item in docs if isinstance(item, dict)]


def _source_viewpoint(view: dict[str, Any], author: str, effect: str = "context_only") -> dict[str, Any]:
    sources = view.get("sources") if isinstance(view.get("sources"), list) else []
    first_source = sources[0] if sources and isinstance(sources[0], dict) else {}
    return {
        "author": author,
        "stance": effect,
        "effect": effect,
        "summary": _text(
            view.get("participation_rule")
            or view.get("right_side_requirement")
            or view.get("risk_rule")
            or view.get("mainline_status")
        )[:180],
        "freshness": _text(view.get("freshness"),),
        "as_of": _text(view.get("as_of")),
        "source_title": _text(first_source.get("title")),
        "source_path": _text(first_source.get("path") or first_source.get("meta_path")),
        "source_tier": _text(first_source.get("tier")),
        "candidate_policy": _text(view.get("candidate_policy")),
    }


def _viewpoint_context(views: list[dict[str, Any]], names: list[str]) -> list[dict[str, Any]]:
    names_text = " ".join(names)
    output: list[dict[str, Any]] = []
    for view in views:
        target_type = _text(view.get("target_type"))
        rule_scope = _text(view.get("rule_scope"))
        view_id = _text(view.get("view_id"))
        if rule_scope == "pangge" or "pangge" in view_id:
            output.append(_source_viewpoint(view, "pangge"))
            continue
        if rule_scope == "daozhang" or "daozhang" in view_id:
            output.append(_source_viewpoint(view, "daozhang"))
            continue
        if target_type == "sector":
            sector = _text(view.get("sector"))
            if sector and sector in names_text:
                output.append(_source_viewpoint(view, "knowledge", _text(view.get("knowledge_effect")) or "context_only"))
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in output:
        key = f"{item.get('author')}:{item.get('source_path')}:{item.get('summary')}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:8]


def _relationship_edges(doc: dict[str, Any]) -> list[dict[str, Any]]:
    chain_name = _text(doc.get("chain_name"))
    node_name = _text(doc.get("node_name"))
    node_id = _text(doc.get("node_id") or node_name)
    edges: list[dict[str, Any]] = []
    if chain_name and node_name:
        edges.append({
            "source": chain_name,
            "target": node_name,
            "relation": "chain_contains_node",
            "confidence": _float(doc.get("mapping_confidence"), 60),
        })
    for domain in doc.get("integrated_domains") or []:
        if not isinstance(domain, dict):
            continue
        domain_name = _text(domain.get("name"))
        if not domain_name:
            continue
        kind = _text(domain.get("kind"))
        edges.append({
            "source": node_name or node_id,
            "target": domain_name,
            "relation": "related_concept" if kind == "concept" else "related_industry",
            "confidence": _float(domain.get("mapping_confidence"), _float(doc.get("mapping_confidence"), 60)),
            "evidence_sources": domain.get("evidence_sources") or [],
        })
    return edges[:24]


def _representative_groups(doc: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    groups = {"leaders": [], "elastic": [], "source_leaders": [], "constituents": []}
    for rep in doc.get("representatives") or []:
        if not isinstance(rep, dict):
            continue
        item = {
            "symbol": rep.get("symbol"),
            "name": rep.get("name"),
            "relation": rep.get("relation"),
            "representative_type": rep.get("representative_type"),
            "priority": rep.get("priority"),
            "chain_role": rep.get("relation") or rep.get("representative_type"),
        }
        rep_type = _text(rep.get("representative_type"))
        if rep_type == "core":
            groups["leaders"].append(item)
        elif rep_type == "elastic":
            groups["elastic"].append(item)
        elif rep_type == "source_leader":
            groups["source_leaders"].append(item)
        else:
            groups["constituents"].append(item)
    return groups


def _graph_doc(doc: dict[str, Any], views: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    domains = [item for item in (doc.get("integrated_domains") or []) if isinstance(item, dict)]
    domain_names = [_text(item.get("name")) for item in domains if _text(item.get("name"))]
    names = [_text(doc.get("chain_name")), _text(doc.get("node_name")), *domain_names]
    trade_minute = doc.get("trade_minute")
    trade_date = trade_minute.date().isoformat() if isinstance(trade_minute, datetime) else now.date().isoformat()
    chain_id = _text(doc.get("chain_id"))
    node_id = _text(doc.get("node_id") or "default")
    confidence = _float(doc.get("mapping_confidence"), 60)
    return {
        "graph_id": f"A:{chain_id}:{node_id}:{trade_date}",
        "market": "A",
        "trade_date": trade_date,
        "trade_minute": trade_minute,
        "updated_at": now,
        "chain_id": chain_id,
        "chain_name": _text(doc.get("chain_name")),
        "node_id": node_id,
        "node_name": _text(doc.get("node_name")),
        "layer": _text(doc.get("layer")),
        "stage": _text(doc.get("stage")),
        "phase": _text(doc.get("phase")),
        "heat_score": doc.get("heat_score"),
        "change_pct": doc.get("change_pct"),
        "momentum_5m": doc.get("momentum_5m"),
        "momentum_15m": doc.get("momentum_15m"),
        "momentum_30m": doc.get("momentum_30m"),
        "concepts": [name for name in domain_names if name],
        "domains": domains[:12],
        "relations": _relationship_edges(doc),
        "representative_groups": _representative_groups(doc),
        "viewpoint_context": _viewpoint_context(views, names),
        "evidence_sources": sorted(set(["chain_heat_snapshots", "board_heat_ticks", "industry_chains.yaml", "knowledge_market_views"])),
        "validation_status": "review_base_plus_postmarket_graph",
        "confidence": confidence,
        "needs_review": confidence < 65,
        "construction_mode": "postmarket_ai_graph",
        "ai_note": "盘后图谱构造层；当前以人工审阅产业链、板块热度和知识观点做约束，不覆盖人工 YAML。",
    }


def sync_concept_relationship_graph(db: Database, proxy_url: str = None) -> dict:
    """Materialize the concept graph consumed by the trading terminal."""
    now = naive_market_now("A")
    chain_docs, latest_minute = _latest_chain_docs(db)
    views = _knowledge_views(db)
    graph_docs = [_graph_doc(doc, views, now) for doc in chain_docs]
    if not graph_docs:
        db["data_freshness"].update_one(
            {"domain": "concept_graph", "market": "A", "mode": "postmarket", "collection": "concept_relationship_graph"},
            {"$set": {
                "domain": "concept_graph",
                "market": "A",
                "mode": "postmarket",
                "lane": "postmarket",
                "collection": "concept_relationship_graph",
                "freshness": "empty",
                "latest_dt": now.isoformat(timespec="minutes"),
                "as_of": now.date().isoformat(),
                "updated_at": now,
                "stale_reason": "no_chain_heat_snapshots",
                "count": 0,
            }, "$inc": {"manifest_revision": 1}},
            upsert=True,
        )
        return {"status": "empty", "inserted": 0, "chains": 0}
    ops = [
        UpdateOne(
            {"graph_id": doc["graph_id"]},
            {"$set": doc},
            upsert=True,
        )
        for doc in graph_docs
    ]
    result = db["concept_relationship_graph"].bulk_write(ops, ordered=False)
    written = int(result.upserted_count + result.modified_count)
    db["data_freshness"].update_one(
        {"domain": "concept_graph", "market": "A", "mode": "postmarket", "collection": "concept_relationship_graph"},
        {"$set": {
            "domain": "concept_graph",
            "market": "A",
            "mode": "postmarket",
            "lane": "postmarket",
            "collection": "concept_relationship_graph",
            "freshness": "fresh",
            "latest_dt": latest_minute.isoformat(timespec="minutes") if isinstance(latest_minute, datetime) else now.isoformat(timespec="minutes"),
            "as_of": (latest_minute.date().isoformat() if isinstance(latest_minute, datetime) else now.date().isoformat()),
            "updated_at": now,
            "stale_reason": "",
            "count": len(graph_docs),
            "viewpoint_docs": len(views),
        }, "$inc": {"manifest_revision": 1}},
        upsert=True,
    )
    logger.info("concept relationship graph: docs=%d written=%d", len(graph_docs), written)
    return {"status": "ok", "inserted": written, "chains": len(graph_docs), "viewpoint_docs": len(views)}
