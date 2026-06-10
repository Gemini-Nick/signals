# -*- coding: utf-8 -*-
"""Build the postmarket security -> industry-chain read model.

This module deliberately separates durable chain membership from intraday heat:
THS/Eastmoney source boards provide evidence, while `chain_heat_snapshots`
only adds the current trading phase. Representatives in `industry_chains.yaml`
are taxonomy hints, not a substitute for full-market coverage.
"""
from __future__ import annotations

import hashlib
import math
import os
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from pymongo import UpdateOne
from pymongo.database import Database
import yaml

from signals.core.chain_ai_mapping import decide_chain_mapping
from signals.core.chain_mapping_rules import filter_mapping_matches, mapping_specificity, matches_from_ai_decision
from signals.core.concept_carriers import load_industry_chains, match_industry_chains, non_chain_reason
from signals.core.market_time import naive_market_now
from signals.core.trading_dates import trading_day_key

CATALOG_SOURCES = (
    ("board_ths", "ths", "industry"),
    ("board_em", "em", "industry"),
    ("concept_ths", "ths", "concept"),
    ("concept_em", "em", "concept"),
)

REQUIRED_BOARD_SOURCES = {"ths", "em"}

MAPPING_CONFIDENCE_THRESHOLD = 60
ROLLUP_TOP_SECURITY_LIMIT = 30
HOT_MARKET_DRIVER_SCORE = 8.0
REPRESENTATIVE_TYPE_RANK = {"core": 2, "elastic": 1}
SECURITY_CHAIN_OVERRIDES_PATH = Path(__file__).resolve().parents[2] / "core" / "security_chain_overrides.yaml"
SECURITY_CONCEPT_EVIDENCE_COLLECTION = "security_concept_evidence"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, float) and math.isnan(value):
            return default
        return float(value)
    except Exception:
        return default


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    return _text(value)[:10]


def _stable_id(*parts: Any) -> str:
    raw = "|".join(_text(part) for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


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


def _security_id(symbol: str) -> str:
    value = _text(symbol).upper()
    if value.startswith(("SH.", "SZ.", "BJ.")):
        exchange, code = value.split(".", 1)
        return f"A:{exchange}:{code}"
    if value.startswith("HK."):
        return f"HK:{value.split('.', 1)[1]}"
    if value.startswith("US."):
        return f"US:{value.split('.', 1)[1]}"
    code = _pure_a_code(value)
    return f"A:{_prefixed_a_symbol(code).split('.', 1)[0]}:{code}" if code else value


def _representative_rank(value: Any) -> int:
    return REPRESENTATIVE_TYPE_RANK.get(_text(value), 0)


def _membership_type_rank(value: Any) -> int:
    return {
        "reviewed_primary": 4,
        "core": 3,
        "theme": 2,
        "weak_related": 1,
    }.get(_text(value), 0)


def _membership_sort_key(row: dict[str, Any]) -> tuple[float, ...]:
    return (
        1.0 if row.get("reviewed_override") else 0.0,
        float(_representative_rank(row.get("representative_type"))),
        _float(row.get("representative_priority")),
        _float(row.get("chain_specificity_score")),
        float(_membership_type_rank(row.get("membership_type"))),
        1.0 if row.get("is_primary_chain") else 0.0,
        _float(row.get("exposure_score")),
        _float(row.get("confidence")),
    )


def _primary_membership_sort_key(row: dict[str, Any]) -> tuple[float, ...]:
    hot_market_logic = _float(row.get("market_driver_score")) >= HOT_MARKET_DRIVER_SCORE
    return (
        1.0 if row.get("reviewed_override") else 0.0,
        1.0 if hot_market_logic else 0.0,
        _float(row.get("market_driver_score")),
        float(_membership_type_rank(row.get("membership_type"))),
        _float(row.get("chain_specificity_score")),
        1.0 if row.get("taxonomy_representative") else 0.0,
        float(_representative_rank(row.get("representative_type"))),
        _float(row.get("representative_priority")),
        _float(row.get("exposure_score")),
        _float(row.get("confidence")),
    )


def _taxonomy_representatives_by_node() -> dict[tuple[str, str], dict[str, dict[str, Any]]]:
    reps_by_node: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for chain_id, chain in load_industry_chains().items():
        for node in chain.get("nodes") or []:
            node_id = _text(node.get("node_id"))
            if not node_id:
                continue
            node_key = (chain_id, node_id)
            for rep_type, rep_key in (("core", "core_representatives"), ("elastic", "elastic_representatives")):
                for rep in node.get(rep_key) or []:
                    code = _pure_a_code(rep.get("symbol"))
                    if not code:
                        continue
                    current = reps_by_node[node_key].get(code) or {}
                    candidate = {
                        "symbol": _prefixed_a_symbol(code),
                        "raw_code": code,
                        "name": _text(rep.get("name")),
                        "representative_type": rep_type,
                        "representative_priority": int(rep.get("priority") or 0),
                        "representative_relation": _text(rep.get("relation")),
                        "source_note": _text(rep.get("source_note")),
                    }
                    if _membership_sort_key(candidate) > _membership_sort_key(current):
                        reps_by_node[node_key][code] = candidate
    return reps_by_node


def _apply_taxonomy_representative(row: dict[str, Any], rep: dict[str, Any] | None) -> None:
    if not rep:
        return
    current = {
        "representative_type": row.get("representative_type"),
        "representative_priority": row.get("representative_priority"),
    }
    if _membership_sort_key(rep) >= _membership_sort_key(current):
        row["representative_type"] = rep.get("representative_type")
        row["representative_priority"] = rep.get("representative_priority")
        row["representative_relation"] = rep.get("representative_relation")
        row["source_note"] = rep.get("source_note")
    row["taxonomy_representative"] = True
    if rep.get("name") and not _text(row.get("name")):
        row["name"] = rep.get("name")
    if _text(rep.get("representative_relation")):
        row["role"] = rep.get("representative_relation")
    if rep.get("representative_type") == "core":
        row["membership_type"] = "core"
    row.setdefault("evidence_sources", [])
    row["evidence_sources"].extend(["industry_chains.yaml", "semantic_industry_chain"])


def _load_security_chain_overrides(config_path: str | None = None) -> list[dict[str, Any]]:
    path = Path(config_path) if config_path else SECURITY_CHAIN_OVERRIDES_PATH
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    chains = load_industry_chains()
    rows: list[dict[str, Any]] = []
    for item in raw.get("overrides") or []:
        if not isinstance(item, dict):
            continue
        code = _pure_a_code(item.get("symbol") or item.get("raw_code") or item.get("code"))
        chain_id = _text(item.get("chain_id"))
        node_id = _text(item.get("node_id"))
        chain = chains.get(chain_id) or {}
        node = (chain.get("nodes_by_id") or {}).get(node_id) or {}
        if not code or not chain or not node:
            continue
        rows.append({
            "symbol": _prefixed_a_symbol(code),
            "raw_code": code,
            "name": _text(item.get("name")),
            "chain_id": chain_id,
            "chain_name": chain.get("name"),
            "node_id": node_id,
            "node_name": node.get("name"),
            "layer": node.get("layer"),
            "stage": node.get("stage"),
            "role": _text(item.get("role") or node.get("stage") or node.get("layer")),
            "confidence": int(item.get("confidence") or 98),
            "source_note": _text(item.get("source_note") or item.get("reason") or "人工确认产业链归属"),
            "concept_name": _text(item.get("concept_name")),
            "reviewed_by": _text(item.get("reviewed_by")),
            "effective_from": _text(item.get("effective_from")),
        })
    return rows


def _apply_security_chain_overrides(
    grouped: dict[tuple[str, str, str], dict[str, Any]],
    *,
    trade_date: str,
    now: datetime,
    security_master: dict[str, dict[str, Any]],
    security_names: dict[str, str],
) -> None:
    for override in _load_security_chain_overrides():
        code = _pure_a_code(override.get("raw_code") or override.get("symbol"))
        if not code:
            continue
        master = security_master.get(code)
        symbol = _text((master or {}).get("symbol")) or _text(override.get("symbol")) or _prefixed_a_symbol(code)
        sid = _text((master or {}).get("security_id")) or _security_id(symbol)
        stock_name = _text((master or {}).get("name") or security_names.get(code) or override.get("name"))
        issuer = _text((master or {}).get("issuer_id")) or _issuer_id(market="A", symbol=symbol, code=code, name=stock_name)
        chain_id = _text(override.get("chain_id"))
        node_id = _text(override.get("node_id"))
        key = (sid, chain_id, node_id)
        row = grouped.get(key)
        if row is None:
            row = {
                "_id": f"{trade_date}:{sid}:{chain_id}:{node_id}",
                "trade_date": trade_date,
                "security_id": sid,
                "issuer_id": issuer,
                "market": "A",
                "symbol": symbol,
                "raw_code": code,
                "name": stock_name,
                "chain_id": chain_id,
                "chain_name": override.get("chain_name"),
                "node_id": node_id,
                "node_name": override.get("node_name"),
                "layer": override.get("layer"),
                "stage": override.get("stage"),
                "role": override.get("role"),
                "membership_type": "reviewed_primary",
                "confidence": override.get("confidence"),
                "exposure_score": _exposure_score(kind="industry", confidence=int(override.get("confidence") or 98), source_count=1),
                "is_primary_chain": False,
                "source_boards": [],
                "evidence_sources": [],
                "evidence_docs": [],
                "as_of": trade_date,
                "stale_level": "fresh",
                "updated_at": now,
            }
            grouped[key] = row
        row["reviewed_override"] = True
        row["membership_type"] = "reviewed_primary"
        row["confidence"] = max(int(row.get("confidence") or 0), int(override.get("confidence") or 98))
        row["exposure_score"] = max(
            _float(row.get("exposure_score")),
            _exposure_score(kind="industry", confidence=int(row["confidence"]), source_count=1),
        )
        row["source_note"] = override.get("source_note")
        row["role"] = override.get("role") or row.get("role")
        row["chain_specificity_score"] = max(_float(row.get("chain_specificity_score")), 5.0)
        concept_name = _text(override.get("concept_name"))
        if concept_name:
            row.setdefault("source_boards", []).append({
                "source_board_id": f"reviewed:concept:{concept_name}:{code}",
                "name": concept_name,
                "kind": "reviewed_concept",
                "source": "reviewed_override",
                "confidence": row["confidence"],
            })
        row.setdefault("evidence_sources", []).extend(["reviewed_override", "security_chain_overrides.yaml"])
        row.setdefault("evidence_docs", []).append({
            "collection": "security_chain_overrides.yaml",
            "concept_name": concept_name,
            "reviewed_by": override.get("reviewed_by"),
            "effective_from": override.get("effective_from"),
            "mapping_confidence": row["confidence"],
        })


def _seed_taxonomy_memberships(
    grouped: dict[tuple[str, str, str], dict[str, Any]],
    *,
    trade_date: str,
    now: datetime,
    security_master: dict[str, dict[str, Any]],
    security_names: dict[str, str],
    reps_by_node: dict[tuple[str, str], dict[str, dict[str, Any]]],
) -> None:
    chains = load_industry_chains()
    for (chain_id, node_id), reps_by_code in reps_by_node.items():
        chain = chains.get(chain_id) or {}
        node = (chain.get("nodes_by_id") or {}).get(node_id) or {}
        for code, rep in reps_by_code.items():
            master = security_master.get(code)
            if not master:
                continue
            symbol = _text(master.get("symbol")) or _prefixed_a_symbol(code)
            sid = _text(master.get("security_id")) or _security_id(symbol)
            stock_name = _text(master.get("name") or security_names.get(code) or rep.get("name"))
            issuer = _text(master.get("issuer_id")) or _issuer_id(market="A", symbol=symbol, code=code, name=stock_name)
            key = (sid, chain_id, node_id)
            row = grouped.get(key)
            if row is None:
                confidence = 98 if rep.get("representative_type") == "core" else 92
                row = {
                    "_id": f"{trade_date}:{sid}:{chain_id}:{node_id}",
                    "trade_date": trade_date,
                    "security_id": sid,
                    "issuer_id": issuer,
                    "market": "A",
                    "symbol": symbol,
                    "raw_code": code,
                    "name": stock_name,
                    "chain_id": chain_id,
                    "chain_name": chain.get("name"),
                    "node_id": node_id,
                    "node_name": node.get("name"),
                    "layer": node.get("layer"),
                    "stage": node.get("stage"),
                    "role": node.get("stage") or node.get("layer") or "",
                    "membership_type": "core" if rep.get("representative_type") == "core" else "theme",
                    "confidence": confidence,
                    "exposure_score": _exposure_score(kind="concept", confidence=confidence, source_count=0),
                    "is_primary_chain": False,
                    "source_boards": [],
                    "evidence_sources": ["industry_chains.yaml", "semantic_industry_chain"],
                    "evidence_docs": [{
                        "collection": "industry_chains.yaml",
                        "chain_id": chain_id,
                        "node_id": node_id,
                        "representative_type": rep.get("representative_type"),
                    }],
                    "as_of": trade_date,
                    "stale_level": "fresh",
                    "updated_at": now,
                }
                grouped[key] = row
            _apply_taxonomy_representative(row, rep)


def _issuer_id(*, market: str, symbol: str, code: str, name: str) -> str:
    if name:
        return f"issuer:{market}:{hashlib.sha1(name.encode('utf-8')).hexdigest()[:12]}"
    return f"issuer:{market}:{symbol or code}"


def _latest_value(db: Database, collection: str, field: str) -> Any:
    doc = db[collection].find_one({field: {"$exists": True}}, {field: 1}, sort=[(field, -1)]) or {}
    return doc.get(field)


def _latest_source_docs(db: Database, collection: str) -> list[dict[str, Any]]:
    latest_dt = _latest_value(db, collection, "dt")
    query: dict[str, Any] = {"dt": latest_dt} if latest_dt else {}
    return list(db[collection].find(query, {"_id": 0}).limit(5000))


def _board_name(doc: dict[str, Any]) -> str:
    for key in ("board_name", "concept_name", "concept", "name", "label", "板块名称", "概念名称", "板块"):
        value = _text(doc.get(key))
        if value:
            return value
    return ""


def _board_code(doc: dict[str, Any]) -> str:
    for key in ("board_code", "concept_code", "code", "板块代码", "概念代码"):
        value = _text(doc.get(key))
        if value:
            return value
    return ""


def _catalog_docs(db: Database, *, trade_date: str, now: datetime) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for collection, source, kind in CATALOG_SOURCES:
        for idx, raw in enumerate(_latest_source_docs(db, collection)):
            raw_name = _board_name(raw)
            source_name = _text(raw.get("source")) or source
            key = (collection, source_name, kind, raw_name or f"invalid:{idx}")
            if key in seen:
                continue
            seen.add(key)
            valid = bool(raw_name)
            status = "ok" if valid else "invalid_name"
            source_board_id = (
                f"{source_name}:{kind}:{raw_name}"
                if valid
                else f"{source_name}:{kind}:invalid:{collection}:{idx}:{_stable_id(raw)}"
            )
            docs.append({
                "_id": f"{trade_date}:{source_board_id}",
                "trade_date": trade_date,
                "source_board_id": source_board_id,
                "source": source_name,
                "coverage_scope": "required_em_ths",
                "source_collection": collection,
                "kind": kind,
                "raw_name": raw_name,
                "canonical_name": raw_name,
                "source_code": _board_code(raw),
                "rank_idx": raw.get("rank_idx"),
                "change_pct": _float(raw.get("change_pct") or raw.get("涨跌幅")),
                "provider_status": "ok" if valid else "degraded",
                "normalization_status": status,
                "as_of": _date_text(raw.get("dt")) or trade_date,
                "updated_at": now,
            })
    return docs


def _latest_spot_docs(db: Database) -> tuple[str, str, list[dict[str, Any]]]:
    latest = db["fullmarket_spot_snapshots"].find_one(
        {"date_key": {"$exists": True}},
        {"date_key": 1, "trade_date": 1},
        sort=[("date_key", -1), ("snapshot_at", -1)],
    ) or {}
    date_key = _text(latest.get("date_key"))
    trade_date = _text(latest.get("trade_date"))
    if not date_key:
        return "", trade_date, []
    docs = list(db["fullmarket_spot_snapshots"].find(
        {"date_key": date_key},
        {"_id": 0, "code": 1, "symbol": 1, "name": 1, "source": 1, "snapshot_at": 1},
    ))
    return date_key, trade_date, docs


def _security_master_docs(db: Database, *, now: datetime) -> tuple[dict[str, dict[str, Any]], dict[str, str], str]:
    _, spot_trade_date, spot_docs = _latest_spot_docs(db)
    by_code: dict[str, dict[str, Any]] = {}
    names: dict[str, str] = {}
    for doc in spot_docs:
        code = _pure_a_code(doc.get("code") or doc.get("symbol"))
        if not code:
            continue
        symbol = _text(doc.get("symbol")) or _prefixed_a_symbol(code)
        name = _text(doc.get("name"))
        sid = _security_id(symbol)
        exchange = symbol.split(".", 1)[0] if "." in symbol else ""
        names[code] = name
        by_code[code] = {
            "_id": sid,
            "security_id": sid,
            "market": "A",
            "exchange": exchange,
            "symbol": symbol,
            "raw_code": code,
            "name": name,
            "issuer_id": _issuer_id(market="A", symbol=symbol, code=code, name=name),
            "primary_listing_id": sid,
            "linked_listing_ids": [],
            "currency": "CNY",
            "listing_status": "listed",
            "asset_type": "stock",
            "data_sources": ["fullmarket_spot_snapshots"],
            "as_of": spot_trade_date,
            "updated_at": now,
        }
    return by_code, names, spot_trade_date


def _constituent_doc(db: Database, *, kind: str, name: str) -> dict[str, Any]:
    if kind == "concept":
        return db["concept_constituents"].find_one(
            {"$or": [{"_id": name}, {"concept_name": name}, {"board_name": name}, {"name": name}]},
            {"symbols": 1, "stock_names": 1, "source": 1, "updated_at": 1, "status": 1},
            sort=[("updated_at", -1)],
        ) or {}
    return db["board_constituents"].find_one(
        {"$or": [{"_id": name}, {"board_name": name}, {"name": name}]},
        {"symbols": 1, "stock_names": 1, "source": 1, "updated_at": 1, "status": 1},
        sort=[("updated_at", -1)],
    ) or {}


def _latest_board_heat_context(db: Database) -> dict[tuple[str, str], dict[str, Any]]:
    latest = db["board_heat_ticks"].find_one(
        {"trade_minute": {"$exists": True}},
        {"trade_minute": 1},
        sort=[("trade_minute", -1)],
    ) or {}
    trade_minute = latest.get("trade_minute")
    if trade_minute is None:
        return {}
    rows = db["board_heat_ticks"].find(
        {"trade_minute": trade_minute},
        {
            "_id": 0,
            "kind": 1,
            "name": 1,
            "board_name": 1,
            "concept_name": 1,
            "change_pct": 1,
            "turnover_pct": 1,
            "rank_idx": 1,
            "up_count": 1,
            "down_count": 1,
            "leader_name": 1,
            "leader_symbol": 1,
            "leader_change_pct": 1,
            "source": 1,
            "trade_date": 1,
            "trade_minute": 1,
        },
    )
    output: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        kind = _text(row.get("kind"))
        name = _text(row.get("name") or row.get("board_name") or row.get("concept_name"))
        if not kind or not name:
            continue
        output[(kind, name)] = {
            "source": _text(row.get("source")),
            "change_pct": _float(row.get("change_pct")),
            "turnover_pct": _float(row.get("turnover_pct")),
            "rank": row.get("rank_idx"),
            "up_count": row.get("up_count"),
            "down_count": row.get("down_count"),
            "leader_name": _text(row.get("leader_name")),
            "leader_symbol": _text(row.get("leader_symbol")),
            "leader_change_pct": _float(row.get("leader_change_pct")),
            "trade_date": _date_text(row.get("trade_date") or row.get("trade_minute")),
            "trade_minute": row.get("trade_minute"),
        }
    return output


def _latest_quote_context_by_code(db: Database) -> dict[str, dict[str, Any]]:
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
            "_id": 1,
            "code": 1,
            "symbol": 1,
            "name": 1,
            "change_pct": 1,
            "turnover_pct": 1,
            "vol": 1,
            "amount": 1,
            "price": 1,
            "snapshot_at": 1,
            "freshness": 1,
            "is_stale": 1,
        },
    )
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
            "vol": _float(row.get("vol")),
            "amount": _float(row.get("amount")),
            "price": _float(row.get("price")),
            "trade_date": trade_date,
            "snapshot_at": snapshot_at,
            "freshness": _text(row.get("freshness")),
            "is_stale": bool(row.get("is_stale")),
        }
    return output


def _evidence_type_for_mapping(*, kind: str, mapping_status: str) -> str:
    if mapping_status == "mapped":
        return "vendor_industry_membership" if kind == "industry" else "vendor_concept_membership"
    if mapping_status == "non_chain":
        return "non_chain_theme_membership"
    return "unmapped_source_membership"


def _evidence_layer_for_mapping(*, kind: str, mapping_status: str) -> str:
    if mapping_status == "mapped":
        return "stable_industry" if kind == "industry" else "candidate_theme"
    if mapping_status == "non_chain":
        return "market_theme"
    return "weak_source"


def _primary_policy_for_mapping(*, kind: str, mapping_status: str) -> str:
    if mapping_status != "mapped":
        return "blocked"
    return "direct" if kind == "industry" else "fallback"


def _volume_driver_score(board_heat: dict[str, Any], quote_context: dict[str, Any]) -> float:
    board_change = max(0.0, _float(board_heat.get("change_pct")))
    board_turnover = max(0.0, _float(board_heat.get("turnover_pct")))
    stock_change = max(0.0, _float(quote_context.get("change_pct")))
    stock_turnover = max(0.0, _float(quote_context.get("turnover_pct")))
    return round(min(20.0, board_change * 1.2 + board_turnover * 2.0 + stock_change + stock_turnover * 2.0), 3)


def _market_context(board_heat: dict[str, Any], quote_context: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    if board_heat:
        context["source_board"] = {
            "change_pct": board_heat.get("change_pct"),
            "turnover_pct": board_heat.get("turnover_pct"),
            "rank": board_heat.get("rank"),
            "up_count": board_heat.get("up_count"),
            "down_count": board_heat.get("down_count"),
            "leader_name": board_heat.get("leader_name"),
            "leader_symbol": board_heat.get("leader_symbol"),
            "leader_change_pct": board_heat.get("leader_change_pct"),
            "trade_date": board_heat.get("trade_date"),
            "trade_minute": board_heat.get("trade_minute"),
            "source": board_heat.get("source"),
        }
    if quote_context:
        context["security_quote"] = {
            "change_pct": quote_context.get("change_pct"),
            "turnover_pct": quote_context.get("turnover_pct"),
            "vol": quote_context.get("vol"),
            "amount": quote_context.get("amount"),
            "price": quote_context.get("price"),
            "trade_date": quote_context.get("trade_date"),
            "snapshot_at": quote_context.get("snapshot_at"),
            "freshness": quote_context.get("freshness"),
            "is_stale": quote_context.get("is_stale"),
        }
    return context


def _build_security_concept_evidence(
    db: Database,
    *,
    trade_date: str,
    now: datetime,
    catalog: list[dict[str, Any]],
    mapping_docs: list[dict[str, Any]],
    security_master: dict[str, dict[str, Any]],
    security_names: dict[str, str],
    include_market_context: bool = True,
) -> list[dict[str, Any]]:
    mappings_by_source = {
        _text(item.get("source_board_id")): item
        for item in mapping_docs
        if _text(item.get("source_board_id"))
    }
    heat_by_board = _latest_board_heat_context(db) if include_market_context else {}
    quote_by_code = _latest_quote_context_by_code(db) if include_market_context else {}
    docs: list[dict[str, Any]] = []
    for source in catalog:
        name = _text(source.get("canonical_name") or source.get("raw_name"))
        kind = _text(source.get("kind"))
        if not name or kind not in {"industry", "concept"}:
            continue
        constituent = _constituent_doc(db, kind=kind, name=name)
        symbols = list(constituent.get("symbols") or [])
        if not symbols:
            continue
        stock_names = dict(constituent.get("stock_names") or {})
        source_board_id = _text(source.get("source_board_id"))
        mapping = mappings_by_source.get(source_board_id) or {}
        mapping_status = _text(mapping.get("mapping_status")) or ("mapped" if mapping.get("chain_id") else "unmapped")
        confidence = int(_float(mapping.get("confidence")))
        specificity = _float(mapping.get("mapping_specificity"))
        evidence_type = _evidence_type_for_mapping(kind=kind, mapping_status=mapping_status)
        evidence_layer = _evidence_layer_for_mapping(kind=kind, mapping_status=mapping_status)
        primary_policy = _primary_policy_for_mapping(kind=kind, mapping_status=mapping_status)
        board_heat = heat_by_board.get((kind, name), {})
        source_collection = "concept_constituents" if kind == "concept" else "board_constituents"
        for symbol_value in symbols:
            code = _pure_a_code(symbol_value)
            if not code:
                continue
            master = security_master.get(code)
            symbol = _text((master or {}).get("symbol")) or _prefixed_a_symbol(code)
            sid = _text((master or {}).get("security_id")) or _security_id(symbol)
            stock_name = _text(stock_names.get(code) or stock_names.get(symbol) or security_names.get(code) or (master or {}).get("name"))
            issuer = _text((master or {}).get("issuer_id")) or _issuer_id(market="A", symbol=symbol, code=code, name=stock_name)
            quote_context = quote_by_code.get(code, {})
            volume_score = _volume_driver_score(board_heat, quote_context)
            row: dict[str, Any] = {
                "_id": f"{trade_date}:{sid}:{_stable_id(source_board_id)}",
                "trade_date": trade_date,
                "market": "A",
                "security_id": sid,
                "issuer_id": issuer,
                "symbol": symbol,
                "raw_code": code,
                "name": stock_name,
                "source_board_id": source_board_id,
                "source_board_name": name,
                "source_board_kind": kind,
                "source": source.get("source"),
                "source_collection": source_collection,
                "source_doc_id": str(constituent.get("_id") or name),
                "source_status": constituent.get("status"),
                "evidence_type": evidence_type,
                "evidence_layer": evidence_layer,
                "primary_policy": primary_policy,
                "chain_mapping_status": mapping_status,
                "mapping_filter_reason": mapping.get("mapping_filter_reason") or mapping.get("reason") or "",
                "confidence": confidence,
                "mapping_confidence": confidence,
                "chain_specificity_score": specificity,
                "hit_terms": mapping.get("hit_terms") or [],
                "evidence_sources": mapping.get("evidence_sources") or [],
                "market_context": _market_context(board_heat, quote_context),
                "volume_driver_score": volume_score,
                "evidence_strength": round(float(confidence) + min(12.0, volume_score), 3),
                "promotable": mapping_status == "mapped",
                "as_of": source.get("as_of") or trade_date,
                "updated_at": now,
            }
            if mapping_status == "mapped":
                row.update({
                    "chain_id": mapping.get("chain_id"),
                    "chain_name": mapping.get("chain_name"),
                    "node_id": mapping.get("node_id"),
                    "node_name": mapping.get("node_name"),
                    "layer": mapping.get("layer"),
                    "stage": mapping.get("stage"),
                    "membership_type_candidate": _membership_type(
                        kind=kind,
                        confidence=confidence,
                        specificity=specificity,
                    ),
                })
            docs.append(row)
    if include_market_context:
        docs.extend(_build_company_business_fact_evidence(
            db,
            trade_date=trade_date,
            now=now,
            security_master=security_master,
            security_names=security_names,
            quote_by_code=quote_by_code,
        ))
    return docs


def _business_fact_rows(fact: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in fact.get("business_rows") or []:
        if not isinstance(item, dict):
            continue
        term = _text(item.get("term"))
        if not term:
            continue
        rows.append({
            "term": term,
            "category": _text(item.get("category")),
            "report_date": _text(item.get("report_date")),
            "revenue_ratio": _float(item.get("revenue_ratio")),
            "gross_margin": _float(item.get("gross_margin")),
        })
    industry = _text(fact.get("industry"))
    if industry:
        rows.insert(0, {
            "term": industry,
            "category": "东财行业",
            "report_date": _text(fact.get("as_of")),
            "revenue_ratio": 0.0,
            "gross_margin": 0.0,
        })
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        term = row["term"]
        if term in seen:
            continue
        seen.add(term)
        deduped.append(row)
    return deduped[:8]


def _latest_business_fact_docs(db: Database) -> list[dict[str, Any]]:
    return list(db["security_business_facts"].find(
        {"status": "ok", "business_terms": {"$exists": True}},
        {
            "_id": 1,
            "security_id": 1,
            "issuer_id": 1,
            "symbol": 1,
            "raw_code": 1,
            "name": 1,
            "industry": 1,
            "business_terms": 1,
            "business_rows": 1,
            "source": 1,
            "as_of": 1,
            "updated_at": 1,
        },
    ).limit(20000))


def _build_company_business_fact_evidence(
    db: Database,
    *,
    trade_date: str,
    now: datetime,
    security_master: dict[str, dict[str, Any]],
    security_names: dict[str, str],
    quote_by_code: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for fact in _latest_business_fact_docs(db):
        code = _pure_a_code(fact.get("raw_code") or fact.get("symbol"))
        if not code:
            continue
        master = security_master.get(code)
        symbol = _text((master or {}).get("symbol") or fact.get("symbol")) or _prefixed_a_symbol(code)
        sid = _text((master or {}).get("security_id") or fact.get("security_id")) or _security_id(symbol)
        stock_name = _text((master or {}).get("name") or fact.get("name") or security_names.get(code))
        issuer = _text((master or {}).get("issuer_id") or fact.get("issuer_id")) or _issuer_id(
            market="A",
            symbol=symbol,
            code=code,
            name=stock_name,
        )
        quote_context = quote_by_code.get(code, {})
        for fact_row in _business_fact_rows(fact):
            term = fact_row["term"]
            if non_chain_reason(term):
                continue
            matches = match_industry_chains(term)
            if not matches:
                continue
            best = matches[0]
            confidence = int(best.get("confidence") or 0)
            if confidence < MAPPING_CONFIDENCE_THRESHOLD:
                continue
            specificity = mapping_specificity(best)
            volume_score = _volume_driver_score({}, quote_context)
            docs.append({
                "_id": f"{trade_date}:{sid}:business_fact:{_stable_id(term)}",
                "trade_date": trade_date,
                "market": "A",
                "security_id": sid,
                "issuer_id": issuer,
                "symbol": symbol,
                "raw_code": code,
                "name": stock_name,
                "source_board_id": f"business_fact:{code}:{_stable_id(term)}",
                "source_board_name": term,
                "source_board_kind": "company_fact",
                "source": fact.get("source") or "eastmoney_f10",
                "source_collection": "security_business_facts",
                "source_doc_id": str(fact.get("_id") or code),
                "source_status": "ok",
                "evidence_type": "company_business_fact",
                "evidence_layer": "company_fact",
                "primary_policy": "fallback",
                "chain_mapping_status": "mapped",
                "mapping_filter_reason": "",
                "confidence": confidence,
                "mapping_confidence": confidence,
                "chain_specificity_score": specificity,
                "hit_terms": best.get("hit_terms") or [],
                "evidence_sources": ["company_business_fact", *(best.get("evidence_sources") or [])],
                "market_context": _market_context({}, quote_context),
                "business_context": {
                    "term": term,
                    "category": fact_row.get("category"),
                    "report_date": fact_row.get("report_date"),
                    "revenue_ratio": fact_row.get("revenue_ratio"),
                    "gross_margin": fact_row.get("gross_margin"),
                },
                "volume_driver_score": volume_score,
                "evidence_strength": round(float(confidence) + min(8.0, volume_score), 3),
                "promotable": True,
                "chain_id": best.get("chain_id"),
                "chain_name": best.get("chain_name"),
                "node_id": best.get("node_id"),
                "node_name": best.get("node_name"),
                "layer": best.get("layer"),
                "stage": best.get("stage"),
                "membership_type_candidate": "theme",
                "as_of": _text(fact.get("as_of")) or trade_date,
                "updated_at": now,
            })
    return docs


def _ai_scope() -> str:
    scope = _text(os.getenv("SIGNALS_CHAIN_AI_SCOPE") or "ambiguous").lower()
    return scope if scope in {"off", "ambiguous", "all"} else "ambiguous"


def _industry_only_evidence(match: dict[str, Any]) -> bool:
    sources = {_text(item) for item in match.get("evidence_sources") or [] if _text(item)}
    return sources == {"industry"}


def _should_call_ai(
    matches: list[dict[str, Any]],
    rule_filtered: list[dict[str, Any]],
    rule_reason: str,
) -> bool:
    scope = _ai_scope()
    if scope == "off":
        return False
    if scope == "all":
        return True
    if rule_reason == "ambiguous_industry_only":
        return True
    if not rule_filtered:
        return True
    if len(rule_filtered) > 1:
        return True
    if len(matches) > 1 and any(_industry_only_evidence(match) for match in rule_filtered):
        return True
    if len(matches) > 1 and max(int(_float(match.get("confidence"))) for match in rule_filtered) < 90:
        return True
    return False


def _resolve_mapping_matches(
    db: Database,
    source: dict[str, Any],
    matches: list[dict[str, Any]],
    *,
    now: datetime,
) -> tuple[list[dict[str, Any]], str]:
    rule_filtered, rule_reason = filter_mapping_matches(matches)
    if not _should_call_ai(matches, rule_filtered, rule_reason):
        return rule_filtered, rule_reason

    decision = decide_chain_mapping(db, source, matches, now=now)
    status = decision.get("status")
    if status == "mapped":
        selected = matches_from_ai_decision(matches, decision)
        if selected:
            return selected, "ai_mapped"
    if status in {"unmapped", "ambiguous"}:
        return [], f"ai_{status}"
    if status == "error":
        return rule_filtered, f"ai_error_fallback_{rule_reason}"
    return rule_filtered, rule_reason


def _mapping_doc(db: Database, catalog: dict[str, Any], *, trade_date: str, now: datetime) -> tuple[dict[str, Any], dict[str, Any] | None]:
    name = _text(catalog.get("canonical_name") or catalog.get("raw_name"))
    base = {
        "_id": f"{trade_date}:{catalog.get('source_board_id')}",
        "trade_date": trade_date,
        "source_board_id": catalog.get("source_board_id"),
        "raw_name": catalog.get("raw_name"),
        "canonical_name": name,
        "source": catalog.get("source"),
        "source_collection": catalog.get("source_collection"),
        "kind": catalog.get("kind"),
        "as_of": catalog.get("as_of") or trade_date,
        "updated_at": now,
    }
    if not name or catalog.get("normalization_status") != "ok":
        return {**base, "mapping_status": "invalid_name", "confidence": 0, "evidence_sources": []}, None
    reason = non_chain_reason(name)
    if reason:
        return {**base, "mapping_status": "non_chain", "confidence": 0, "reason": reason, "evidence_sources": ["non_chain_rule"]}, None
    matches = match_industry_chains(name)
    if not matches:
        return {**base, "mapping_status": "unmapped", "confidence": 0, "evidence_sources": []}, None
    initial_best = matches[0]
    initial_confidence = int(initial_best.get("confidence") or 0)
    if initial_confidence < MAPPING_CONFIDENCE_THRESHOLD:
        best = initial_best
        confidence = initial_confidence
        status = "low_confidence"
        filter_reason = "below_threshold"
    else:
        source = {
            "kind": catalog.get("kind"),
            "name": name,
            "code": catalog.get("source_board_id"),
        }
        matches, filter_reason = _resolve_mapping_matches(
            db,
            source,
            [item for item in matches if int(item.get("confidence") or 0) >= MAPPING_CONFIDENCE_THRESHOLD],
            now=now,
        )
        if not matches:
            status = "ambiguous" if "ambiguous" in filter_reason else "unmapped"
            return {**base, "mapping_status": status, "confidence": 0, "mapping_filter_reason": filter_reason, "evidence_sources": []}, None
        best = matches[0]
        confidence = int(best.get("confidence") or 0)
        status = "mapped"
    doc = {
        **base,
        "mapping_status": status,
        "chain_id": best.get("chain_id"),
        "chain_name": best.get("chain_name"),
        "node_id": best.get("node_id"),
        "node_name": best.get("node_name"),
        "layer": best.get("layer"),
        "stage": best.get("stage"),
        "confidence": confidence,
        "ai_confidence": int(best.get("ai_confidence") or 0) if best.get("ai_confidence") is not None else None,
        "ai_reason": _text(best.get("ai_reason")),
        "mapping_type": "ai_rule_hybrid" if best.get("ai_confidence") is not None else "semantic_taxonomy",
        "mapping_filter_reason": filter_reason,
        "mapping_specificity": mapping_specificity(best),
        "hit_terms": best.get("hit_terms") or [],
        "evidence_sources": best.get("evidence_sources") or [],
    }
    return doc, best if status == "mapped" else None


def _membership_type(*, kind: str, confidence: int, specificity: float = 0.0) -> str:
    if kind == "industry" and specificity >= 3:
        return "core"
    if confidence < 65:
        return "weak_related"
    return "core" if kind == "industry" else "theme"


def _stronger_membership_type(current: Any, candidate: str) -> str:
    return candidate if _membership_type_rank(candidate) > _membership_type_rank(current) else _text(current)


def _exposure_score(*, kind: str, confidence: int, source_count: int) -> float:
    kind_bonus = 12.0 if kind == "industry" else 4.0
    return round(float(confidence) + kind_bonus + min(10.0, source_count * 2.0), 3)


def _build_memberships(
    db: Database,
    *,
    trade_date: str,
    now: datetime,
    mappings: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    security_master: dict[str, dict[str, Any]],
    security_names: dict[str, str],
    evidence_docs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    reps_by_node = _taxonomy_representatives_by_node()
    if evidence_docs is None:
        synthesized_mapping_docs = []
        for catalog, mapping, _ in mappings:
            synthesized_mapping_docs.append({
                **mapping,
                "source_board_id": mapping.get("source_board_id") or catalog.get("source_board_id"),
                "mapping_status": mapping.get("mapping_status") or "mapped",
            })
        evidence_docs = _build_security_concept_evidence(
            db,
            trade_date=trade_date,
            now=now,
            catalog=[catalog for catalog, _, _ in mappings],
            mapping_docs=synthesized_mapping_docs,
            security_master=security_master,
            security_names=security_names,
            include_market_context=False,
        )
    for evidence in evidence_docs:
        if _text(evidence.get("chain_mapping_status")) != "mapped":
            continue
        code = _pure_a_code(evidence.get("raw_code") or evidence.get("symbol"))
        chain_id = _text(evidence.get("chain_id"))
        node_id = _text(evidence.get("node_id"))
        if not code or not chain_id or not node_id:
            continue
        kind = _text(evidence.get("source_board_kind"))
        confidence = int(_float(evidence.get("confidence") or evidence.get("mapping_confidence")))
        specificity = _float(evidence.get("chain_specificity_score"))
        symbol = _text(evidence.get("symbol")) or _prefixed_a_symbol(code)
        sid = _text(evidence.get("security_id")) or _security_id(symbol)
        master = security_master.get(code)
        stock_name = _text(evidence.get("name") or security_names.get(code) or (master or {}).get("name"))
        issuer = _text(evidence.get("issuer_id") or (master or {}).get("issuer_id")) or _issuer_id(
            market="A",
            symbol=symbol,
            code=code,
            name=stock_name,
        )
        node_key = (chain_id, node_id)
        key = (sid, chain_id, node_id)
        row = grouped.get(key)
        if row is None:
            row = {
                "_id": f"{trade_date}:{sid}:{chain_id}:{node_id}",
                "trade_date": trade_date,
                "security_id": sid,
                "issuer_id": issuer,
                "market": "A",
                "symbol": symbol,
                "raw_code": code,
                "name": stock_name,
                "chain_id": chain_id,
                "chain_name": evidence.get("chain_name"),
                "node_id": node_id,
                "node_name": evidence.get("node_name"),
                "layer": evidence.get("layer"),
                "stage": evidence.get("stage"),
                "role": evidence.get("stage") or evidence.get("layer") or "",
                "membership_type": evidence.get("membership_type_candidate")
                or _membership_type(kind=kind, confidence=confidence, specificity=specificity),
                "confidence": confidence,
                "chain_specificity_score": specificity,
                "exposure_score": _exposure_score(kind=kind, confidence=confidence, source_count=1),
                "market_driver_score": _float(evidence.get("volume_driver_score")),
                "is_primary_chain": False,
                "source_boards": [],
                "evidence_sources": [],
                "evidence_layers": [],
                "primary_policies": [],
                "evidence_docs": [],
                "as_of": trade_date,
                "stale_level": "fresh",
                "updated_at": now,
            }
            grouped[key] = row
        row["confidence"] = max(int(row.get("confidence") or 0), confidence)
        row["chain_specificity_score"] = max(
            _float(row.get("chain_specificity_score")),
            specificity,
        )
        row["source_boards"].append({
            "source_board_id": evidence.get("source_board_id"),
            "name": evidence.get("source_board_name"),
            "kind": kind,
            "source": evidence.get("source"),
            "confidence": confidence,
            "evidence_layer": evidence.get("evidence_layer"),
            "primary_policy": evidence.get("primary_policy"),
            "volume_driver_score": evidence.get("volume_driver_score"),
            "market_context": evidence.get("market_context") or {},
        })
        row["evidence_sources"].extend([
            _text(evidence.get("source")),
            _text(evidence.get("evidence_type")),
            "source_board_chain_mapping",
            SECURITY_CONCEPT_EVIDENCE_COLLECTION,
        ])
        row.setdefault("evidence_layers", []).append(_text(evidence.get("evidence_layer")))
        row.setdefault("primary_policies", []).append(_text(evidence.get("primary_policy")))
        row["evidence_docs"].append({
            "collection": SECURITY_CONCEPT_EVIDENCE_COLLECTION,
            "evidence_id": evidence.get("_id"),
            "evidence_type": evidence.get("evidence_type"),
            "evidence_layer": evidence.get("evidence_layer"),
            "primary_policy": evidence.get("primary_policy"),
            "board_name": evidence.get("source_board_name"),
            "source": evidence.get("source"),
            "source_collection": evidence.get("source_collection"),
            "mapping_confidence": confidence,
            "volume_driver_score": evidence.get("volume_driver_score"),
        })
        source_count = len({item.get("source_board_id") for item in row["source_boards"]})
        row["membership_type"] = _stronger_membership_type(
            row.get("membership_type"),
            _text(evidence.get("membership_type_candidate"))
            or _membership_type(kind=kind, confidence=confidence, specificity=specificity),
        )
        row["exposure_score"] = max(
            _float(row.get("exposure_score")),
            _exposure_score(kind=kind, confidence=int(row["confidence"]), source_count=source_count),
        )
        row["market_driver_score"] = max(
            _float(row.get("market_driver_score")),
            _float(evidence.get("volume_driver_score")),
        )
        _apply_taxonomy_representative(row, reps_by_node.get(node_key, {}).get(code))

    _seed_taxonomy_memberships(
        grouped,
        trade_date=trade_date,
        now=now,
        security_master=security_master,
        security_names=security_names,
        reps_by_node=reps_by_node,
    )
    _apply_security_chain_overrides(
        grouped,
        trade_date=trade_date,
        now=now,
        security_master=security_master,
        security_names=security_names,
    )

    by_security: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in grouped.values():
        row["source_boards"] = list({item["source_board_id"]: item for item in row["source_boards"]}.values())[:12]
        row["evidence_sources"] = [item for item in dict.fromkeys(row["evidence_sources"]) if item]
        row["evidence_layers"] = [item for item in dict.fromkeys(row.get("evidence_layers") or []) if item]
        row["primary_policies"] = [item for item in dict.fromkeys(row.get("primary_policies") or []) if item]
        row["evidence_docs"] = row["evidence_docs"][:10]
        by_security[row["security_id"]].append(row)
    for rows in by_security.values():
        primary = max(rows, key=_primary_membership_sort_key, default=None)
        if primary:
            primary["is_primary_chain"] = True
    return list(grouped.values())


def _latest_chain_heat(db: Database) -> dict[tuple[str, str], dict[str, Any]]:
    latest = db["chain_heat_snapshots"].find_one({"market": "A"}, {"trade_minute": 1}, sort=[("trade_minute", -1)]) or {}
    trade_minute = latest.get("trade_minute")
    if not trade_minute:
        return {}
    rows = db["chain_heat_snapshots"].find(
        {"market": "A", "trade_minute": trade_minute},
        {"_id": 0},
    )
    return {
        (_text(row.get("chain_id")), _text(row.get("node_id"))): row
        for row in rows
    }


def _rollup_docs(
    memberships: list[dict[str, Any]],
    *,
    trade_date: str,
    now: datetime,
    heat_by_node: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in memberships:
        grouped[(_text(row.get("market")), _text(row.get("chain_id")), _text(row.get("node_id")))].append(row)
    docs: list[dict[str, Any]] = []
    for (market, chain_id, node_id), rows in grouped.items():
        heat = heat_by_node.get((chain_id, node_id), {})
        top = sorted(rows, key=_membership_sort_key, reverse=True)[:ROLLUP_TOP_SECURITY_LIMIT]
        docs.append({
            "_id": f"{trade_date}:{market}:{chain_id}:{node_id}",
            "trade_date": trade_date,
            "market": market,
            "chain_id": chain_id,
            "chain_name": rows[0].get("chain_name"),
            "node_id": node_id,
            "node_name": rows[0].get("node_name"),
            "layer": rows[0].get("layer"),
            "stage": rows[0].get("stage"),
            "covered_security_count": len({row.get("security_id") for row in rows}),
            "primary_security_count": sum(1 for row in rows if row.get("is_primary_chain")),
            "coverage_by_market": dict(Counter(row.get("market") for row in rows)),
            "membership_type_counts": dict(Counter(row.get("membership_type") for row in rows)),
            "avg_confidence": round(sum(_float(row.get("confidence")) for row in rows) / max(1, len(rows)), 3),
            "top_securities": [
                {
                    "security_id": row.get("security_id"),
                    "symbol": row.get("symbol"),
                    "raw_code": row.get("raw_code"),
                    "name": row.get("name"),
                    "membership_type": row.get("membership_type"),
                    "representative_type": row.get("representative_type"),
                    "representative_priority": row.get("representative_priority"),
                    "representative_relation": row.get("representative_relation"),
                    "taxonomy_representative": bool(row.get("taxonomy_representative")),
                    "role": row.get("role"),
                    "source_note": row.get("source_note"),
                    "confidence": row.get("confidence"),
                    "exposure_score": row.get("exposure_score"),
                    "market_driver_score": row.get("market_driver_score"),
                    "is_primary_chain": row.get("is_primary_chain"),
                    "evidence_sources": row.get("evidence_sources") or [],
                    "evidence_layers": row.get("evidence_layers") or [],
                    "primary_policies": row.get("primary_policies") or [],
                }
                for row in top
            ],
            "phase": heat.get("phase") or "mapped",
            "trading_signal": heat.get("trading_signal") or "",
            "heat_score": _float(heat.get("heat_score")),
            "rank": heat.get("rank"),
            "range_pattern": heat.get("range_pattern") or "",
            "change_pct": heat.get("change_pct"),
            "trade_minute": heat.get("trade_minute"),
            "source": "security_chain_memberships",
            "updated_at": now,
        })
    docs.sort(key=lambda item: (_float(item.get("heat_score")), int(item.get("covered_security_count") or 0)), reverse=True)
    for idx, doc in enumerate(docs, start=1):
        doc["coverage_rank"] = idx
    return docs


def _write_many_replace(db: Database, collection: str, docs: list[dict[str, Any]], *, trade_date: str) -> tuple[int, int]:
    db[collection].delete_many({"trade_date": trade_date})
    if not docs:
        return 0, 0
    result = db[collection].bulk_write(
        [UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True) for doc in docs],
        ordered=False,
    )
    return int(result.upserted_count), int(result.modified_count)


def _write_security_master(db: Database, docs_by_code: dict[str, dict[str, Any]]) -> tuple[int, int]:
    docs = list(docs_by_code.values())
    if not docs:
        return 0, 0
    result = db["security_master"].bulk_write(
        [UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True) for doc in docs],
        ordered=False,
    )
    return int(result.upserted_count), int(result.modified_count)


def sync_postmarket_chain_rebuild(db: Database, proxy_url: str = None) -> dict[str, Any]:
    del proxy_url
    started = time.monotonic()
    now = naive_market_now("A")
    trade_date = trading_day_key("A", now=now)

    security_master, security_names, spot_trade_date = _security_master_docs(db, now=now)
    security_inserted, security_modified = _write_security_master(db, security_master)

    catalog = _catalog_docs(db, trade_date=trade_date, now=now)
    catalog_inserted, catalog_modified = _write_many_replace(db, "source_board_catalog", catalog, trade_date=trade_date)

    mapping_docs: list[dict[str, Any]] = []
    mapped_for_membership: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for item in catalog:
        mapping, match = _mapping_doc(db, item, trade_date=trade_date, now=now)
        mapping_docs.append(mapping)
        if match is not None:
            mapped_for_membership.append((item, mapping, match))
    mapping_inserted, mapping_modified = _write_many_replace(db, "source_board_chain_mappings", mapping_docs, trade_date=trade_date)

    concept_evidence = _build_security_concept_evidence(
        db,
        trade_date=trade_date,
        now=now,
        catalog=catalog,
        mapping_docs=mapping_docs,
        security_master=security_master,
        security_names=security_names,
    )
    evidence_inserted, evidence_modified = _write_many_replace(
        db,
        SECURITY_CONCEPT_EVIDENCE_COLLECTION,
        concept_evidence,
        trade_date=trade_date,
    )

    memberships = _build_memberships(
        db,
        trade_date=trade_date,
        now=now,
        mappings=mapped_for_membership,
        security_master=security_master,
        security_names=security_names,
        evidence_docs=concept_evidence,
    )
    membership_inserted, membership_modified = _write_many_replace(db, "security_chain_memberships", memberships, trade_date=trade_date)

    rollups = _rollup_docs(
        memberships,
        trade_date=trade_date,
        now=now,
        heat_by_node=_latest_chain_heat(db),
    )
    rollup_inserted, rollup_modified = _write_many_replace(db, "chain_node_security_rollups", rollups, trade_date=trade_date)

    mapping_counts = Counter(doc.get("mapping_status") for doc in mapping_docs)
    coverage_by_chain = Counter(row.get("chain_id") for row in memberships)
    report = {
        "_id": f"{trade_date}:A",
        "trade_date": trade_date,
        "market": "A",
        "coverage_scope": "eastmoney_ths_required",
        "required_board_sources": sorted(REQUIRED_BOARD_SOURCES),
        "excluded_board_sources": ["sina", "canonical"],
        "security_universe_count": len(security_master),
        "covered_security_count": len({row.get("security_id") for row in memberships}),
        "membership_count": len(memberships),
        "chain_node_count": len(rollups),
        "source_board_count": len(catalog),
        "source_board_status_counts": dict(Counter(doc.get("normalization_status") for doc in catalog)),
        "mapping_status_counts": dict(mapping_counts),
        "security_concept_evidence_count": len(concept_evidence),
        "evidence_layer_counts": dict(Counter(row.get("evidence_layer") for row in concept_evidence)),
        "primary_policy_counts": dict(Counter(row.get("primary_policy") for row in concept_evidence)),
        "coverage_by_chain": dict(coverage_by_chain),
        "coverage_by_market": dict(Counter(row.get("market") for row in memberships)),
        "relationship_edges_status": "deferred_v1",
        "relationship_edges_reason": "第一版不生成公司级供应商/客户关系边，避免弱证据误导交易。",
        "hk_us_status": "schema_ready_seed_pending",
        "spot_trade_date": spot_trade_date,
        "updated_at": now,
    }
    db["chain_coverage_reports"].update_one({"_id": report["_id"]}, {"$set": report}, upsert=True)
    db["chain_rebuild_runs"].update_one(
        {"_id": f"postmarket_chain_rebuild:{trade_date}:v1"},
        {"$set": {
            "run_id": f"postmarket_chain_rebuild:{trade_date}:v1",
            "trade_date": trade_date,
            "status": "ok" if memberships else "degraded",
            "coverage_report_id": report["_id"],
            "started_at": now,
            "finished_at": naive_market_now("A"),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "counts": {
                "security_master": len(security_master),
                "source_board_catalog": len(catalog),
                "source_board_chain_mappings": len(mapping_docs),
                SECURITY_CONCEPT_EVIDENCE_COLLECTION: len(concept_evidence),
                "security_chain_memberships": len(memberships),
                "chain_node_security_rollups": len(rollups),
            },
        }},
        upsert=True,
    )
    db["data_freshness"].update_one(
        {"domain": "chain_rebuild", "market": "A", "mode": "postmarket", "collection": "security_chain_memberships"},
        {"$set": {
            "domain": "chain_rebuild",
            "market": "A",
            "mode": "postmarket",
            "collection": "security_chain_memberships",
            "freshness": "fresh" if memberships else "empty",
            "latest_dt": trade_date,
            "as_of": trade_date,
            "updated_at": now,
            "stale_reason": "" if memberships else "security_chain_memberships_empty",
            "count": len(memberships),
            "security_concept_evidence_count": len(concept_evidence),
            "security_universe_count": len(security_master),
            "covered_security_count": report["covered_security_count"],
            "mapping_status_counts": dict(mapping_counts),
            "coverage_scope": "eastmoney_ths_required",
        }},
        upsert=True,
    )

    inserted = (
        security_inserted
        + catalog_inserted
        + mapping_inserted
        + evidence_inserted
        + membership_inserted
        + rollup_inserted
        + 2
    )
    modified = security_modified + catalog_modified + mapping_modified + evidence_modified + membership_modified + rollup_modified
    return {
        "module": "postmarket_chain_rebuild",
        "status": "ok" if memberships else "degraded",
        "trade_date": trade_date,
        "inserted": inserted,
        "modified": modified,
        "security_universe_count": len(security_master),
        "covered_security_count": report["covered_security_count"],
        "membership_count": len(memberships),
        "security_concept_evidence_count": len(concept_evidence),
        "rollup_count": len(rollups),
        "mapping_status_counts": dict(mapping_counts),
        "elapsed": round(time.monotonic() - started, 3),
        "reason": "" if memberships else "security_chain_memberships_empty",
    }
