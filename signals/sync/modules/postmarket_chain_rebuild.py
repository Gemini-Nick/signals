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
import time
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from pymongo import UpdateOne
from pymongo.database import Database

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
REPRESENTATIVE_TYPE_RANK = {"core": 2, "elastic": 1}


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


def _membership_sort_key(row: dict[str, Any]) -> tuple[float, float, float, float, float]:
    return (
        float(_representative_rank(row.get("representative_type"))),
        _float(row.get("representative_priority")),
        1.0 if row.get("is_primary_chain") else 0.0,
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


def _mapping_doc(catalog: dict[str, Any], *, trade_date: str, now: datetime) -> tuple[dict[str, Any], dict[str, Any] | None]:
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
    best = matches[0]
    confidence = int(best.get("confidence") or 0)
    status = "mapped" if confidence >= MAPPING_CONFIDENCE_THRESHOLD else "low_confidence"
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
        "mapping_type": "semantic_taxonomy",
        "hit_terms": best.get("hit_terms") or [],
        "evidence_sources": best.get("evidence_sources") or [],
    }
    return doc, best if status == "mapped" else None


def _membership_type(*, kind: str, confidence: int) -> str:
    if confidence < 65:
        return "weak_related"
    return "core" if kind == "industry" else "theme"


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
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    reps_by_node = _taxonomy_representatives_by_node()
    for catalog, mapping, match in mappings:
        name = _text(catalog.get("canonical_name"))
        kind = _text(catalog.get("kind"))
        constituent = _constituent_doc(db, kind=kind, name=name)
        symbols = list(constituent.get("symbols") or [])
        stock_names = dict(constituent.get("stock_names") or {})
        if not symbols:
            continue
        confidence = int(mapping.get("confidence") or 0)
        for symbol_value in symbols:
            code = _pure_a_code(symbol_value)
            if not code:
                continue
            symbol = _prefixed_a_symbol(code)
            sid = _security_id(symbol)
            master = security_master.get(code)
            stock_name = _text(stock_names.get(code) or stock_names.get(symbol) or security_names.get(code) or (master or {}).get("name"))
            issuer = (master or {}).get("issuer_id") or _issuer_id(market="A", symbol=symbol, code=code, name=stock_name)
            chain_id = _text(mapping.get("chain_id"))
            node_id = _text(mapping.get("node_id"))
            node_key = (chain_id, node_id)
            key = (sid, chain_id, node_id)
            row = grouped.get(key)
            if row is None:
                row = {
                    "_id": f"{trade_date}:{sid}:{mapping.get('chain_id')}:{mapping.get('node_id')}",
                    "trade_date": trade_date,
                    "security_id": sid,
                    "issuer_id": issuer,
                    "market": "A",
                    "symbol": symbol,
                    "raw_code": code,
                    "name": stock_name,
                    "chain_id": mapping.get("chain_id"),
                    "chain_name": mapping.get("chain_name"),
                    "node_id": mapping.get("node_id"),
                    "node_name": mapping.get("node_name"),
                    "layer": mapping.get("layer"),
                    "stage": mapping.get("stage"),
                    "role": mapping.get("stage") or mapping.get("layer") or "",
                    "membership_type": _membership_type(kind=kind, confidence=confidence),
                    "confidence": confidence,
                    "exposure_score": _exposure_score(kind=kind, confidence=confidence, source_count=1),
                    "is_primary_chain": False,
                    "source_boards": [],
                    "evidence_sources": [],
                    "evidence_docs": [],
                    "as_of": trade_date,
                    "stale_level": "fresh",
                    "updated_at": now,
                }
                grouped[key] = row
            row["confidence"] = max(int(row.get("confidence") or 0), confidence)
            row["source_boards"].append({
                "source_board_id": catalog.get("source_board_id"),
                "name": name,
                "kind": kind,
                "source": catalog.get("source"),
                "confidence": confidence,
            })
            row["evidence_sources"].extend([
                _text(catalog.get("source")),
                f"{kind}_constituent",
                "source_board_chain_mapping",
            ])
            row["evidence_docs"].append({
                "collection": "concept_constituents" if kind == "concept" else "board_constituents",
                "board_name": name,
                "source": constituent.get("source"),
                "status": constituent.get("status"),
                "mapping_confidence": confidence,
            })
            source_count = len({item.get("source_board_id") for item in row["source_boards"]})
            row["membership_type"] = "core" if any(item.get("kind") == "industry" for item in row["source_boards"]) and row["confidence"] >= 65 else row["membership_type"]
            row["exposure_score"] = max(
                _float(row.get("exposure_score")),
                _exposure_score(kind=kind, confidence=int(row["confidence"]), source_count=source_count),
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

    by_security: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in grouped.values():
        row["source_boards"] = list({item["source_board_id"]: item for item in row["source_boards"]}.values())[:12]
        row["evidence_sources"] = [item for item in dict.fromkeys(row["evidence_sources"]) if item]
        row["evidence_docs"] = row["evidence_docs"][:10]
        by_security[row["security_id"]].append(row)
    for rows in by_security.values():
        primary = max(rows, key=_membership_sort_key, default=None)
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
                    "is_primary_chain": row.get("is_primary_chain"),
                    "evidence_sources": row.get("evidence_sources") or [],
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
        mapping, match = _mapping_doc(item, trade_date=trade_date, now=now)
        mapping_docs.append(mapping)
        if match is not None:
            mapped_for_membership.append((item, mapping, match))
    mapping_inserted, mapping_modified = _write_many_replace(db, "source_board_chain_mappings", mapping_docs, trade_date=trade_date)

    memberships = _build_memberships(
        db,
        trade_date=trade_date,
        now=now,
        mappings=mapped_for_membership,
        security_master=security_master,
        security_names=security_names,
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
        + membership_inserted
        + rollup_inserted
        + 2
    )
    modified = security_modified + catalog_modified + mapping_modified + membership_modified + rollup_modified
    return {
        "module": "postmarket_chain_rebuild",
        "status": "ok" if memberships else "degraded",
        "trade_date": trade_date,
        "inserted": inserted,
        "modified": modified,
        "security_universe_count": len(security_master),
        "covered_security_count": report["covered_security_count"],
        "membership_count": len(memberships),
        "rollup_count": len(rollups),
        "mapping_status_counts": dict(mapping_counts),
        "elapsed": round(time.monotonic() - started, 3),
        "reason": "" if memberships else "security_chain_memberships_empty",
    }
