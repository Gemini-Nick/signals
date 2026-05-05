# -*- coding: utf-8 -*-
"""Canonical strategy snapshot for Signals dashboards.

This module turns Mongo-backed cache collections into a business read model.
Raw facts stay in the data gateway; dashboards consume this strategy layer.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Mapping, Optional

from signals.core.market_time import naive_market_now
from signals.core.trading_dates import trading_day_key


WARNING_TOKENS = ("卖", "顶", "风险", "死叉", "减仓", "跌破", "预警")


def _now_bj() -> datetime:
    return naive_market_now("A")


def get_strategy_snapshot(*, db: Any = None, persist: bool = False) -> dict[str, Any]:
    """Build the current strategy snapshot and optionally persist it to Mongo."""
    snapshot = build_strategy_snapshot(db=db)
    if persist:
        persist_strategy_snapshot(snapshot, db=db)
    return snapshot


def build_strategy_snapshot(
    *,
    db: Any = None,
    responses: Optional[Mapping[str, Any]] = None,
    journal_summary: Optional[Mapping[str, Any]] = None,
    previous_snapshot: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build a strategy snapshot from gateway responses.

    `responses` is injectable so unit tests can exercise the business rules
    without a live MongoDB or external data provider.
    """
    now = _now_bj()
    trade_date = trading_day_key("A", now=now)
    responses_provided = responses is not None
    db_provided = db is not None
    responses = dict(responses or _fetch_gateway_responses())
    db = db if db is not None else _get_db_or_none()

    board_resp = responses.get("board")
    concept_resp = responses.get("concept")
    chain_resp = responses.get("chain_heat")
    terminal_pool_resp = responses.get("terminal_pool")
    market_pool_resp = responses.get("market_pool")
    quote_resp = responses.get("quote")
    signal_resp = responses.get("signal")
    if chain_resp is None and db is not None and not responses_provided:
        chain_resp = _latest_chain_heat_response(db)
    if chain_resp is not None:
        responses["chain_heat"] = chain_resp
    if terminal_pool_resp is None and db is not None and not responses_provided:
        terminal_pool_resp = _latest_terminal_pool_response(db)
    if terminal_pool_resp is not None:
        responses["terminal_pool"] = terminal_pool_resp

    pool = _as_dict(_response_data(market_pool_resp))
    pool_items = _pool_items(pool)
    terminal_pool = _as_dict(_response_data(terminal_pool_resp))
    signals = _as_list(_response_data(signal_resp))
    quotes = _quote_map(_as_list(_response_data(quote_resp)))
    themes = _build_themes(board_resp, concept_resp, chain_resp)
    source_confidence = _build_source_confidence(responses, db=db)

    candidates, warnings = _build_candidates(
        signals=signals,
        pool_items=pool_items,
        terminal_pool=terminal_pool,
        quotes=quotes,
        themes=themes,
        source_confidence=source_confidence,
    )
    if db_provided or not responses_provided:
        candidates = _merge_ai_factor_candidates(candidates, db=db)

    if journal_summary is None:
        journal_summary = _journal_summary()
    strategy_kpis = _build_strategy_kpis(
        signals=signals,
        candidates=candidates,
        warnings=warnings,
        pool_size=len(pool_items),
        journal_summary=journal_summary,
    )

    market_regime = _build_market_regime(
        themes=themes,
        candidates=candidates,
        warnings=warnings,
        pool_size=len(pool_items),
        signal_count=len(signals),
        confidence=source_confidence["overall"],
    )
    chart_context = _build_chart_context(candidates, warnings, signals)

    if previous_snapshot is None:
        previous_snapshot = _latest_previous_snapshot(db, as_of=trade_date)

    changed_since_last = _changed_since_last(
        current={"themes": themes, "candidates": candidates, "warnings": warnings},
        previous=previous_snapshot or {},
    )
    daily_brief = _build_daily_brief(
        as_of=trade_date,
        market_regime=market_regime,
        themes=themes,
        candidates=candidates,
        warnings=warnings,
        changed_since_last=changed_since_last,
        source_confidence=source_confidence,
    )
    decision_queue = _build_decision_queue(candidates, warnings)

    return _json_safe({
        "snapshot_id": f"strategy:{trade_date}",
        "as_of": trade_date,
        "generated_at": now.isoformat(timespec="seconds"),
        "market_regime": market_regime,
        "themes": themes,
        "candidates": candidates,
        "warnings": warnings,
        "chart_context": chart_context,
        "daily_brief": daily_brief,
        "decision_queue": decision_queue,
        "strategy_kpis": strategy_kpis,
        "source_confidence": source_confidence,
        "data_lineage": {
            "canonical_store": "mongodb.strategy_snapshots",
            "read_model": "signals.strategy.snapshot",
            "raw_sources": [
                "market_pools",
                "terminal_stock_pool",
                "terminal_technical_signals",
                "signals",
                "quote_snapshots",
                "chain_heat_snapshots",
                "ai_factor_experiments",
                "board_ranking",
                "concept_ranking",
            ],
            "fallback_policy": ".data caches are import/compat sources, not dashboard truth",
        },
    })


def _merge_ai_factor_candidates(candidates: list[dict[str, Any]], *, db: Any = None) -> list[dict[str, Any]]:
    try:
        from signals.strategy.ai_factor_factory import build_ai_factor_strategy_candidates

        ai_candidates = build_ai_factor_strategy_candidates(db=db)
    except Exception:
        ai_candidates = []
    if not ai_candidates:
        return candidates
    merged_by_symbol: dict[str, dict[str, Any]] = {}
    ai_symbols: list[str] = []
    for item in candidates:
        symbol = _candidate_symbol(item)
        if symbol:
            merged_by_symbol[symbol] = dict(item)

    for item in ai_candidates:
        symbol = _candidate_symbol(item)
        if not symbol:
            continue
        ai_symbols.append(symbol)
        if symbol in merged_by_symbol:
            merged_by_symbol[symbol] = _merge_ai_factor_overlay(merged_by_symbol[symbol], item)
        else:
            merged_by_symbol[symbol] = dict(item)

    ranked = sorted(merged_by_symbol.values(), key=lambda item: _float(item.get("score"), 0.0), reverse=True)
    ai_visible: list[dict[str, Any]] = []
    seen_ai: set[str] = set()
    for symbol in ai_symbols:
        if symbol in seen_ai:
            continue
        item = merged_by_symbol.get(symbol)
        if item is None:
            continue
        ai_visible.append(item)
        seen_ai.add(symbol)
        if len(ai_visible) >= 3:
            break

    selected: list[dict[str, Any]] = []
    selected_symbols: set[str] = set()
    for item in ai_visible + ranked:
        symbol = _candidate_symbol(item)
        if not symbol or symbol in selected_symbols:
            continue
        selected.append(item)
        selected_symbols.add(symbol)
        if len(selected) >= 12:
            break
    return selected


def _candidate_symbol(item: Mapping[str, Any]) -> str:
    return str(item.get("symbol") or "").strip()


def _merge_ai_factor_overlay(existing: Mapping[str, Any], ai_item: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    existing_metadata = _as_dict(merged.get("metadata"))
    ai_metadata = _as_dict(ai_item.get("metadata"))
    merged["metadata"] = {
        **existing_metadata,
        "ai_factor_factory": ai_metadata or {"source": "ai_factor_factory"},
    }
    merged["evidence"] = _as_list(merged.get("evidence")) + _as_list(ai_item.get("evidence"))
    merged["promotion_path"] = _unique_text_items(
        list(merged.get("promotion_path") or []) + list(ai_item.get("promotion_path") or [])
    )
    merged["missing_gates"] = _unique_text_items(
        list(merged.get("missing_gates") or []) + list(ai_item.get("missing_gates") or [])
    )
    merged["recommended_action"] = str(
        ai_item.get("recommended_action") or merged.get("recommended_action") or ""
    )
    merged["ai_factor_score"] = _float(ai_item.get("score"), 0.0)
    return merged


def _unique_text_items(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        result.append(text)
        seen.add(text)
    return result


def persist_strategy_snapshot(snapshot: Mapping[str, Any], *, db: Any = None) -> dict[str, Any]:
    """Persist the strategy snapshot as the canonical workbench read model."""
    db = db if db is not None else _get_db_or_none()
    if db is None:
        return {"ok": False, "reason": "mongo_unavailable"}

    now = _now_bj()
    as_of = str(snapshot.get("as_of") or now.date().isoformat())[:10]
    doc = {
        "_id": f"strategy:{as_of}",
        "as_of": as_of,
        "snapshot": _json_safe(dict(snapshot)),
        "candidate_count": len(snapshot.get("candidates") or []),
        "warning_count": len(snapshot.get("warnings") or []),
        "theme_count": len(snapshot.get("themes") or []),
        "updated_at": now,
    }
    result = db["strategy_snapshots"].update_one({"_id": doc["_id"]}, {"$set": doc}, upsert=True)
    db["data_freshness"].update_one(
        {"domain": "strategy", "market": "A", "mode": "derived", "collection": "strategy_snapshots"},
        {"$set": {
            "domain": "strategy",
            "market": "A",
            "mode": "derived",
            "collection": "strategy_snapshots",
            "freshness": "fresh",
            "latest_dt": as_of,
            "as_of": as_of,
            "updated_at": now,
            "stale_reason": "",
            "count": 1,
        }},
        upsert=True,
    )
    return {
        "ok": True,
        "upserted": bool(result.upserted_id),
        "modified": int(result.modified_count),
        "as_of": as_of,
        "target_collection": "strategy_snapshots",
    }


def _fetch_gateway_responses() -> dict[str, Any]:
    try:
        from signals.data.gateway import (
            get_board_rank,
            get_concept_rank,
            get_market_pool,
            get_quote_snapshot,
            get_signal_pool,
        )
        from signals.data.models import DataRequest
    except Exception:
        return {}

    return {
        "board": _safe_call(
            get_board_rank,
            DataRequest(domain="board", mode="realtime", market="A", purpose="cluster", allow_stale=True),
        ),
        "concept": _safe_call(
            get_concept_rank,
            DataRequest(domain="concept", mode="realtime", market="A", purpose="cluster", allow_stale=True),
        ),
        "market_pool": _safe_call(
            get_market_pool,
            DataRequest(domain="market_pool", mode="realtime", market="A", purpose="live", allow_stale=True),
        ),
        "quote": _safe_call(
            get_quote_snapshot,
            DataRequest(domain="quote", mode="realtime", market="A", purpose="quote", allow_stale=True),
        ),
        "signal": _safe_call(
            get_signal_pool,
            DataRequest(domain="signal", mode="historical", market="A", purpose="review", allow_stale=True),
        ),
    }


def _safe_call(fn: Any, request: Any) -> Any:
    try:
        return fn(request)
    except Exception as exc:
        return {
            "data": None,
            "source": getattr(fn, "__name__", "gateway"),
            "freshness": "empty",
            "is_stale": True,
            "errors": [f"{exc.__class__.__name__}: {exc}"],
        }


def _get_db_or_none() -> Any:
    try:
        from signals.data.mongo_fallback import get_db

        return get_db()
    except Exception:
        return None


def _response_data(resp: Any) -> Any:
    if resp is None:
        return None
    if isinstance(resp, Mapping):
        return resp.get("data")
    return getattr(resp, "data", None)


def _response_meta(resp: Any) -> dict[str, Any]:
    if resp is None:
        return {"source": "missing", "freshness": "empty", "is_stale": True, "errors": ["missing_response"]}
    if isinstance(resp, Mapping):
        return {
            "source": str(resp.get("source") or "gateway"),
            "freshness": str(resp.get("freshness") or "unknown"),
            "as_of": resp.get("as_of"),
            "is_stale": bool(resp.get("is_stale")),
            "errors": list(resp.get("errors") or []),
        }
    to_meta = getattr(resp, "to_meta", None)
    if callable(to_meta):
        meta = dict(to_meta())
        meta.setdefault("source", getattr(resp, "source", "gateway"))
        meta.setdefault("freshness", getattr(resp, "freshness", "unknown"))
        meta.setdefault("is_stale", bool(getattr(resp, "is_stale", False)))
        meta.setdefault("errors", list(getattr(resp, "errors", []) or []))
        return meta
    return {
        "source": str(getattr(resp, "source", "gateway")),
        "freshness": str(getattr(resp, "freshness", "unknown")),
        "as_of": getattr(resp, "as_of", None),
        "is_stale": bool(getattr(resp, "is_stale", False)),
        "errors": list(getattr(resp, "errors", []) or []),
    }


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        try:
            records = value.to_dict("records")
            return [dict(item) for item in records]
        except Exception:
            return []
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def _pool_items(pool: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_items = pool.get("items")
    if isinstance(raw_items, list) and raw_items:
        items = [dict(item) for item in raw_items if isinstance(item, Mapping)]
    else:
        items = [{"symbol": item, "sources": ["market_pools"]} for item in pool.get("symbols") or []]
    deduped: dict[str, dict[str, Any]] = {}
    for item in items:
        symbol = _normalize_symbol(item.get("symbol") or item.get("code") or item.get("raw_code"))
        if not symbol:
            continue
        next_item = dict(item)
        next_item["symbol"] = symbol
        deduped.setdefault(symbol, next_item)
    return list(deduped.values())


def _quote_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    quotes: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = _normalize_symbol(row.get("symbol") or row.get("code"))
        if symbol:
            quotes[symbol] = row
            quotes[_symbol_digits(symbol)] = row
    return quotes


def _latest_chain_heat_response(db: Any) -> dict[str, Any]:
    try:
        latest = db["chain_heat_snapshots"].find_one(
            {"market": "A"},
            {"trade_minute": 1},
            sort=[("trade_minute", -1)],
        )
        if not latest or latest.get("trade_minute") is None:
            return {
                "data": [],
                "source": "chain_heat_snapshots",
                "freshness": "empty",
                "is_stale": True,
                "errors": ["no_chain_heat_snapshots"],
            }
        trade_minute = latest["trade_minute"]
        rows = list(db["chain_heat_snapshots"].find(
            {"market": "A", "trade_minute": trade_minute},
            {"_id": 0},
        ).sort("rank", 1).limit(24))
        for row in rows:
            row["theme_symbols"] = _chain_domain_symbols_from_db(db, row)
        as_of = str(trade_minute)[:10]
        today = _now_bj().date().isoformat()
        return {
            "data": rows,
            "source": "chain_heat_snapshots",
            "freshness": "fresh" if as_of == today else "stale",
            "as_of": as_of,
            "is_stale": as_of != today,
            "errors": [],
        }
    except Exception as exc:
        return {
            "data": [],
            "source": "chain_heat_snapshots",
            "freshness": "empty",
            "is_stale": True,
            "errors": [f"{exc.__class__.__name__}: {exc}"],
        }


def _chain_domain_symbols_from_db(db: Any, row: Mapping[str, Any], *, total_limit: int = 160) -> list[str]:
    symbols: list[str] = []

    def add_symbol(value: Any) -> None:
        symbol = _normalize_symbol(value)
        if symbol and symbol not in symbols:
            symbols.append(symbol)

    for rep in row.get("representatives") or []:
        if isinstance(rep, Mapping):
            add_symbol(rep.get("symbol"))
    integrated = row.get("integrated_domains") if isinstance(row.get("integrated_domains"), list) else []
    for domain in integrated:
        if not isinstance(domain, Mapping) or len(symbols) >= total_limit:
            break
        kind = str(domain.get("kind") or "")
        name = str(domain.get("name") or "").strip()
        if not kind or not name:
            continue
        collection = "concept_constituents" if kind == "concept" else "board_constituents"
        try:
            doc = db[collection].find_one(
                {"$or": [{"concept_name": name}, {"board_name": name}, {"name": name}]},
                {"symbols": 1, "stock_names": 1},
                sort=[("updated_at", -1)],
            ) or {}
        except Exception:
            doc = {}
        add_symbol(domain.get("leader_symbol"))
        for symbol in doc.get("symbols") or []:
            add_symbol(symbol)
            if len(symbols) >= total_limit:
                break
    return symbols[:total_limit]


def _latest_terminal_pool_response(db: Any) -> dict[str, Any]:
    try:
        doc = db["terminal_stock_pool"].find_one(
            {"pool": "terminal_stock_pool", "market": "A"},
            {"_id": 0},
            sort=[("updated_at", -1)],
        )
        if not doc:
            return {
                "data": {},
                "source": "terminal_stock_pool",
                "freshness": "empty",
                "is_stale": True,
                "errors": ["terminal_stock_pool_empty"],
            }
        as_of = str(doc.get("dt") or doc.get("updated_at") or "")[:10] or None
        today = _now_bj().date().isoformat()
        return {
            "data": doc,
            "source": "terminal_stock_pool",
            "freshness": "fresh" if as_of == today else "stale",
            "as_of": as_of,
            "is_stale": as_of != today,
            "errors": [],
        }
    except Exception as exc:
        return {
            "data": {},
            "source": "terminal_stock_pool",
            "freshness": "empty",
            "is_stale": True,
            "errors": [f"{exc.__class__.__name__}: {exc}"],
        }


def _chain_leader_name(row: Mapping[str, Any]) -> str:
    representatives = row.get("representatives")
    if isinstance(representatives, list):
        for item in representatives:
            if isinstance(item, Mapping):
                name = str(item.get("name") or "").strip()
                if name:
                    return name
    domains = row.get("integrated_domains")
    if isinstance(domains, list):
        for item in domains:
            if isinstance(item, Mapping):
                leader = str(item.get("leader_name") or item.get("leader_symbol") or "").strip()
                if leader:
                    return leader
    return ""


def _chain_theme_symbols(row: Mapping[str, Any]) -> list[str]:
    symbols: list[str] = []

    def add_symbol(value: Any) -> None:
        symbol = _normalize_symbol(value)
        if symbol and symbol not in symbols:
            symbols.append(symbol)

    representatives = row.get("representatives")
    if isinstance(representatives, list):
        for rep in representatives:
            if isinstance(rep, Mapping):
                add_symbol(rep.get("symbol"))
    for symbol in row.get("theme_symbols") or row.get("constituent_symbols") or []:
        add_symbol(symbol)
    domains = row.get("integrated_domains")
    if isinstance(domains, list):
        for domain in domains:
            if not isinstance(domain, Mapping):
                continue
            add_symbol(domain.get("leader_symbol"))
            domain_reps = domain.get("representatives")
            if isinstance(domain_reps, list):
                for rep in domain_reps:
                    if isinstance(rep, Mapping):
                        add_symbol(rep.get("symbol"))
    return symbols


def _build_chain_themes(chain_resp: Any) -> list[dict[str, Any]]:
    meta = _response_meta(chain_resp)
    rows = _as_list(_response_data(chain_resp))
    rows.sort(key=lambda item: (_float(item.get("heat_score"), 0.0), -_float(item.get("rank"), 999.0)), reverse=True)
    themes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        name = str(row.get("chain_name") or row.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        node = str(row.get("node_name") or "").strip()
        heat_score = _float(row.get("heat_score"))
        change_pct = _float(row.get("change_pct"))
        strength = heat_score if heat_score is not None else (change_pct if change_pct is not None else 0.0)
        phase = str(row.get("phase") or "")
        risk = "一致高潮风险" if phase in {"consensus_climax", "risk_off"} else ("产业链退潮观察" if phase == "cooling" else "")
        symbols = _chain_theme_symbols(row)
        themes.append({
            "theme_id": f"chain_heat:{name}",
            "name": name,
            "domain": "chain_heat",
            "rank": len(themes) + 1,
            "strength": round(strength, 3),
            "change_pct": round(change_pct or 0.0, 3),
            "leader": _chain_leader_name(row),
            "phase": phase or "watch",
            "confidence": _confidence_from_meta(meta),
            "risk": risk,
            "symbols": symbols[:160],
            "representative_symbols": symbols[:24],
            "evidence": [{
                "type": "chain_heat",
                "source": meta["source"],
                "freshness": meta["freshness"],
                "summary": f"{name}{('/' + node) if node else ''} heat {strength:.2f}",
            }],
        })
        if len(themes) >= 5:
            break
    return themes


def _build_themes(board_resp: Any, concept_resp: Any, chain_resp: Any = None) -> list[dict[str, Any]]:
    themes: list[dict[str, Any]] = _build_chain_themes(chain_resp) if chain_resp is not None else []
    seen = {str(item.get("name") or "") for item in themes}
    for domain, resp in (("board", board_resp), ("concept", concept_resp)):
        meta = _response_meta(resp)
        rows = _as_list(_response_data(resp))
        rows.sort(key=lambda item: _float(item.get("change_pct") or item.get("pct_chg"), 0.0), reverse=True)
        for idx, row in enumerate(rows[:5]):
            name = str(row.get("board_name") or row.get("name") or row.get("concept_name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            change_pct = _float(row.get("change_pct") or row.get("pct_chg"))
            strength = change_pct if change_pct is not None else _float(row.get("composite_score"), 0.0)
            leader = str(row.get("leader_name") or row.get("leader") or row.get("leader_symbol") or "")
            phase = "strengthening" if strength >= 2.0 else ("watch" if strength >= 0 else "decaying")
            themes.append({
                "theme_id": f"{domain}:{name}",
                "name": name,
                "domain": domain,
                "rank": idx + 1,
                "strength": round(strength, 3),
                "change_pct": round(change_pct or 0.0, 3),
                "leader": leader,
                "phase": phase,
                "confidence": _confidence_from_meta(meta),
                "risk": "追高风险" if strength >= 5.0 else ("主线衰减观察" if strength < 0 else ""),
                "evidence": [{
                    "type": "rank",
                    "source": meta["source"],
                    "freshness": meta["freshness"],
                    "summary": f"{name} {strength:+.2f}",
                }],
            })
    return themes


def _build_candidates(
    *,
    signals: list[dict[str, Any]],
    pool_items: list[dict[str, Any]],
    terminal_pool: Mapping[str, Any],
    quotes: dict[str, dict[str, Any]],
    themes: list[dict[str, Any]],
    source_confidence: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    terminal_candidates, terminal_warnings = _build_candidates_from_terminal_pool(
        terminal_pool=terminal_pool,
        quotes=quotes,
        themes=themes,
        source_confidence=source_confidence,
    )
    if terminal_candidates or terminal_warnings:
        return terminal_candidates[:12], terminal_warnings[:10]

    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    eligible_signals = _current_live_signals(signals)
    for signal in eligible_signals:
        symbol = _normalize_symbol(signal.get("symbol"))
        if symbol:
            by_symbol[symbol].append(signal)

    candidates: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen: set[str] = set()
    top_theme = themes[0] if themes else {}

    for symbol, symbol_signals in by_symbol.items():
        symbol_signals.sort(key=lambda item: str(item.get("signal_date") or item.get("updated_at") or ""), reverse=True)
        latest = symbol_signals[0]
        status = _signal_status(latest)
        item = _candidate_from_signal(
            symbol=symbol,
            signal=latest,
            quote=quotes.get(symbol) or quotes.get(_symbol_digits(symbol)) or {},
            theme=top_theme,
            overall_confidence=_float(source_confidence.get("overall"), 0.5),
        )
        if status == "warning":
            warnings.append(item)
        else:
            candidates.append(item)
        seen.add(symbol)

    for pool_item in pool_items:
        symbol = _normalize_symbol(pool_item.get("symbol"))
        if not symbol or symbol in seen:
            continue
        item = _candidate_from_pool(
            pool_item=pool_item,
            quote=quotes.get(symbol) or quotes.get(_symbol_digits(symbol)) or {},
            theme=top_theme,
            overall_confidence=_float(source_confidence.get("overall"), 0.5),
        )
        candidates.append(item)
        seen.add(symbol)

    candidates.sort(key=lambda item: _float(item.get("score"), 0.0), reverse=True)
    warnings.sort(key=lambda item: _float(item.get("score"), 0.0))
    return candidates[:12], warnings[:10]


def _terminal_group_rows(terminal_pool: Mapping[str, Any], group: str) -> list[dict[str, Any]]:
    rows = terminal_pool.get(group)
    if rows is None and group == "focus_stocks":
        rows = terminal_pool.get("stocks")
    return [dict(item) for item in rows or [] if isinstance(item, Mapping)]


def _build_candidates_from_terminal_pool(
    *,
    terminal_pool: Mapping[str, Any],
    quotes: dict[str, dict[str, Any]],
    themes: list[dict[str, Any]],
    source_confidence: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not terminal_pool:
        return [], []

    top_theme = themes[0] if themes else {}
    overall_confidence = _float(source_confidence.get("overall"), 0.5)
    candidates: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen: set[str] = set()

    for group, pool_type in (
        ("focus_stocks", "focus"),
        ("watch_stocks", "watch"),
    ):
        for row in _terminal_group_rows(terminal_pool, group):
            symbol = _normalize_symbol(row.get("symbol") or row.get("code") or row.get("raw_code"))
            if not symbol or symbol in seen:
                continue
            candidates.append(_candidate_from_terminal_row(
                row=row,
                pool_type=pool_type,
                quote=quotes.get(symbol) or quotes.get(_symbol_digits(symbol)) or {},
                theme=top_theme,
                overall_confidence=overall_confidence,
            ))
            seen.add(symbol)

    for row in _terminal_group_rows(terminal_pool, "risk_stocks"):
        symbol = _normalize_symbol(row.get("symbol") or row.get("code") or row.get("raw_code"))
        if not symbol:
            continue
        warnings.append(_candidate_from_terminal_row(
            row=row,
            pool_type="risk",
            quote=quotes.get(symbol) or quotes.get(_symbol_digits(symbol)) or {},
            theme=top_theme,
            overall_confidence=overall_confidence,
        ))

    candidates.sort(key=lambda item: _terminal_candidate_priority(item), reverse=True)
    warnings.sort(key=lambda item: _float(item.get("metadata", {}).get("rank_score"), 0.0), reverse=True)
    return candidates, warnings


def _terminal_candidate_priority(item: Mapping[str, Any]) -> tuple[float, float, float, float]:
    metadata = _as_dict(item.get("metadata"))
    stage_bonus = {
        "entry_ready": 300.0,
        "watch_preheat": 200.0,
        "strategy_candidate": 100.0,
    }.get(str(item.get("decision_stage") or ""), 0.0)
    theme_bonus = 50.0 if str(item.get("theme_alignment") or "") == "theme_representative" else 0.0
    rank = _float(metadata.get("rank"), 9999.0)
    rank_bonus = max(0.0, 100.0 - rank)
    return (
        stage_bonus,
        theme_bonus,
        rank_bonus,
        _float(metadata.get("rank_score") or item.get("score"), 0.0),
    )


def _terminal_stage(pool_type: str, row: Mapping[str, Any]) -> str:
    if pool_type == "risk":
        return "risk_first"
    if pool_type == "focus":
        return "entry_ready"
    gate_status = str(row.get("entry_gate_status") or "")
    blocked_by = {str(item) for item in row.get("blocked_by") or []}
    technical = row.get("technical_evidence") if isinstance(row.get("technical_evidence"), Mapping) else {}
    has_technical = bool(technical and technical.get("status") != "missing")
    if gate_status == "watch_only_not_hard_buy" or "missing_buy_technical" in blocked_by or not has_technical:
        return "strategy_candidate"
    return "watch_preheat"


def _terminal_missing_gates(stage: str, row: Mapping[str, Any]) -> list[str]:
    if stage == "risk_first":
        return ["risk_clear"]
    if stage == "entry_ready":
        return []
    gate_status = str(row.get("entry_gate_status") or "")
    blocked_by = {str(item) for item in row.get("blocked_by") or []}
    missing: list[str] = []
    if gate_status == "entry_waiting_30m_confirm" or "30m_missing" in blocked_by:
        missing.append("30m_confirm")
    if gate_status == "entry_waiting_upper_context" or "daily_or_weekly_missing" in blocked_by:
        missing.append("upper_context")
    if gate_status == "entry_waiting_right_side_confirm" or "5m_or_15m_missing" in blocked_by:
        missing.append("right_side_confirm")
    if gate_status == "entry_waiting_resonance_confirm" or "partner_period_missing" in blocked_by:
        missing.append("resonance_confirm")
    if gate_status == "blocked_by_period_conflict":
        missing.append("period_conflict_clear")
    if gate_status == "blocked_by_risk" or "risk_signal_present" in blocked_by:
        missing.append("risk_clear")
    if not missing and stage == "strategy_candidate":
        missing.append("hard_technical")
    return missing


def _terminal_primary_blocker(stage: str, row: Mapping[str, Any], missing_gates: list[str]) -> str:
    if stage == "risk_first":
        return "暂不参与：已有卖点或冲突"
    if stage == "entry_ready":
        return ""
    action = str(row.get("trader_action") or "").strip()
    if action:
        return action
    labels = {
        "hard_technical": "缺硬技术买点",
        "30m_confirm": "缺30m确认",
        "upper_context": "缺日/周上级结构",
        "right_side_confirm": "缺5m/15m下单确认",
        "resonance_confirm": "缺多周期共振",
        "period_conflict_clear": "存在周期冲突",
        "risk_clear": "风险信号未解除",
    }
    return labels.get(missing_gates[0], "等待确认") if missing_gates else "等待确认"


def _terminal_recommended_action(stage: str, row: Mapping[str, Any], primary_blocker: str) -> str:
    if stage == "risk_first":
        return "暂不参与，等冲突解除"
    if stage == "entry_ready":
        return "确认买点复核，先看止损位和仓位上限"
    if stage == "watch_preheat":
        return primary_blocker or "继续观察，等最后一层确认"
    return "先放线索池，等待硬技术确认"


def _terminal_promotion_path(stage: str, row: Mapping[str, Any], missing_gates: list[str]) -> list[dict[str, str]]:
    missing = set(missing_gates)
    has_technical = stage in {"watch_preheat", "entry_ready"}
    path = [
        {"key": "source", "status": "passed", "detail": str(row.get("signal_origin") or row.get("pool_type") or "terminal_stock_pool")},
        {
            "key": "hard_technical",
            "status": "passed" if has_technical else "waiting",
            "detail": "terminal_technical_signals",
        },
        {
            "key": "30m_confirm",
            "status": "waiting" if "30m_confirm" in missing else ("passed" if has_technical else "waiting"),
            "detail": "30m触发确认",
        },
        {
            "key": "upper_context",
            "status": "waiting" if "upper_context" in missing else ("passed" if has_technical else "waiting"),
            "detail": "日/周结构确认",
        },
        {
            "key": "right_side_confirm",
            "status": "waiting" if "right_side_confirm" in missing else ("passed" if stage == "entry_ready" else "waiting"),
            "detail": "5m/15m下单确认",
        },
        {
            "key": "risk_clear",
            "status": "blocked" if "risk_clear" in missing or stage == "risk_first" else "passed",
            "detail": "风险信号处理",
        },
    ]
    if "period_conflict_clear" in missing:
        path.append({"key": "period_conflict_clear", "status": "blocked", "detail": "周期冲突待解除"})
    if "resonance_confirm" in missing:
        path.append({"key": "resonance_confirm", "status": "waiting", "detail": "等待多周期共振"})
    return path


def _candidate_from_terminal_row(
    *,
    row: Mapping[str, Any],
    pool_type: str,
    quote: Mapping[str, Any],
    theme: Mapping[str, Any],
    overall_confidence: float,
) -> dict[str, Any]:
    symbol = _normalize_symbol(row.get("symbol") or row.get("code") or row.get("raw_code"))
    name = str(row.get("name") or quote.get("name") or _resolved_stock_name(symbol) or "")
    reason = str(row.get("latest_signal") or row.get("reason") or row.get("signal_origin") or pool_type)
    stage = _terminal_stage(pool_type, row)
    missing_gates = _terminal_missing_gates(stage, row)
    primary_blocker = _terminal_primary_blocker(stage, row, missing_gates)
    recommended_action = _terminal_recommended_action(stage, row, primary_blocker)
    promotion_path = _terminal_promotion_path(stage, row, missing_gates)
    score = _float(row.get("rank_score") or row.get("sort_score") or row.get("score"), 0.0)
    rank_score = _float(row.get("rank_score"), score)
    alignment = _theme_alignment(symbol, theme)
    source_collection = f"terminal_stock_pool.{pool_type}_stocks"
    thesis = f"{symbol} 位于{row.get('pool_type') or pool_type}层，状态是 {row.get('entry_gate_status') or stage}"
    status = "warning" if stage == "risk_first" else ("open" if stage == "entry_ready" else "watch")
    direction = "sell" if stage == "risk_first" else ("buy" if stage == "entry_ready" else "watch")
    evidence = {
        "raw_fact": {
            "symbol": symbol,
            "rank": row.get("rank"),
            "pool_type": row.get("pool_type") or pool_type,
            "entry_gate_status": row.get("entry_gate_status"),
            "source_collections": row.get("source_collections") or [],
        },
        "signal_evidence": {
            "signal_type": reason,
            "freq": str((_as_dict(row.get("top_buy_reason")).get("freq") or _as_dict(row.get("top_risk_reason")).get("freq") or "")),
            "score": score,
            "confidence": overall_confidence,
        },
        "technical_evidence": row.get("technical_evidence") if isinstance(row.get("technical_evidence"), Mapping) else {},
        "strategy_thesis": thesis,
        "action_recommendation": recommended_action,
    }
    return {
        "symbol": symbol,
        "name": name,
        "display_name": name or symbol,
        "kind": "stock",
        "score": round(score, 2),
        "direction": direction,
        "reason": reason,
        "status": status,
        "decision_stage": stage,
        "missing_gates": missing_gates,
        "primary_blocker": primary_blocker,
        "recommended_action": recommended_action,
        "promotion_path": promotion_path,
        "theme_alignment": alignment,
        "metadata": {
            "thesis": thesis,
            "trigger": reason,
            "risk": str(row.get("invalidates_when") or row.get("exit_condition") or ""),
            "next_action": recommended_action,
            "recommended_action": recommended_action,
            "decision_stage": stage,
            "missing_gates": missing_gates,
            "primary_blocker": primary_blocker,
            "promotion_path": promotion_path,
            "theme_alignment": alignment,
            "theme": theme.get("name", ""),
            "source": source_collection,
            "source_collections": row.get("source_collections") or [],
            "price": _float(quote.get("price") or quote.get("latest_price") or quote.get("close")),
            "freq": str((_as_dict(row.get("top_buy_reason")).get("freq") or _as_dict(row.get("top_risk_reason")).get("freq") or "")),
            "rank": row.get("rank"),
            "rank_score": rank_score,
            "pool_type": row.get("pool_type") or pool_type,
            "entry_gate_status": row.get("entry_gate_status"),
            "blocked_by": list(row.get("blocked_by") or []),
            "technical_evidence": row.get("technical_evidence") if isinstance(row.get("technical_evidence"), Mapping) else {},
            "knowledge_confirmation": row.get("knowledge_confirmation") if isinstance(row.get("knowledge_confirmation"), Mapping) else {},
            "chain_context": row.get("chain_context") if isinstance(row.get("chain_context"), Mapping) else {},
            "inclusion_reasons": list(row.get("inclusion_reasons") or [])[:8],
            "evidence": evidence,
        },
    }


def _candidate_decision_metadata(*, stage: str, theme_alignment: str, status: str) -> dict[str, Any]:
    if stage == "risk_first":
        return {
            "decision_stage": "risk_first",
            "missing_gates": ["risk_clear"],
            "primary_blocker": "暂不参与：已有卖点或冲突",
            "recommended_action": "暂不参与，等冲突解除",
            "promotion_path": [
                {"key": "source", "status": "passed", "detail": "risk signal"},
                {"key": "risk_clear", "status": "blocked", "detail": "风险未处理"},
            ],
        }

    missing = ["hard_technical"]
    primary_blocker = "缺硬技术确认"
    if theme_alignment == "theme_not_confirmed":
        missing.append("theme_alignment")
        primary_blocker = "缺主线产业链共振确认"
    return {
        "decision_stage": "strategy_candidate",
        "missing_gates": missing,
        "primary_blocker": primary_blocker,
        "recommended_action": "先放线索池，等待硬技术确认",
        "promotion_path": [
            {"key": "source", "status": "passed", "detail": "strategy signal"},
            {"key": "hard_technical", "status": "waiting", "detail": "等待30m买点或5m/15m下单确认"},
            {
                "key": "upper_context",
                "status": "waiting" if status != "entry_ready" else "passed",
                "detail": "等待日/周结构确认",
            },
            {"key": "risk_clear", "status": "waiting", "detail": "未进入交易复核"},
        ],
    }


def _theme_alignment(symbol: str, theme: Mapping[str, Any]) -> str:
    theme_symbols = {
        _normalize_symbol(item)
        for item in (theme.get("symbols") or theme.get("representative_symbols") or [])
    }
    theme_symbols.discard("")
    if not theme_symbols:
        return "theme_unknown"
    return "theme_representative" if symbol in theme_symbols else "theme_not_confirmed"


def _signal_date_text(signal: Mapping[str, Any]) -> str:
    return str(signal.get("signal_date") or signal.get("as_of") or signal.get("updated_at") or "")[:10]


def _is_backtest_signal(signal: Mapping[str, Any]) -> bool:
    source = str(signal.get("source") or "").strip().lower()
    return source.startswith("sqlite.backtest.signal_records") or source.startswith("historical_signal_record")


def _is_generated_daily_signal(signal: Mapping[str, Any]) -> bool:
    source = str(signal.get("source") or "").strip().lower()
    return source == "sync.signal_pool.generated"


def _current_live_signals(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    live = [
        signal for signal in signals
        if not _is_backtest_signal(signal) and not _is_generated_daily_signal(signal)
    ]
    dated = [_signal_date_text(signal) for signal in live if _signal_date_text(signal)]
    if not dated:
        return live
    latest_date = max(dated)
    return [
        signal
        for signal in live
        if _signal_date_text(signal) == latest_date
    ]


def _candidate_from_signal(
    *,
    symbol: str,
    signal: Mapping[str, Any],
    quote: Mapping[str, Any],
    theme: Mapping[str, Any],
    overall_confidence: float,
) -> dict[str, Any]:
    signal_type = str(signal.get("signal_type") or signal.get("type") or "策略信号")
    score = _signal_score(signal)
    status = "warning" if _signal_status(signal) == "warning" else "open"
    direction = "sell" if status == "warning" else "buy"
    price = _float(signal.get("price") or quote.get("price") or quote.get("latest_price") or quote.get("close"))
    name = str(signal.get("name") or quote.get("name") or _resolved_stock_name(symbol) or "")
    thesis = _thesis_for(symbol, signal_type, theme)
    risk = _risk_for(status, signal, theme)
    alignment = _theme_alignment(symbol, theme)
    decision = _candidate_decision_metadata(
        stage="risk_first" if status == "warning" else "strategy_candidate",
        theme_alignment=alignment,
        status=status,
    )
    next_action = decision["recommended_action"]
    evidence = {
        "raw_fact": {
            "symbol": symbol,
            "price": price,
            "signal_date": str(signal.get("signal_date") or "")[:10],
        },
        "signal_evidence": {
            "signal_type": signal_type,
            "freq": str(signal.get("freq") or ""),
            "score": score,
            "confidence": _float(signal.get("confidence"), overall_confidence),
        },
        "strategy_thesis": thesis,
        "action_recommendation": next_action,
    }
    public_status = "warning" if status == "warning" else "watch"
    return {
        "symbol": symbol,
        "name": name,
        "display_name": name or symbol,
        "kind": "stock",
        "score": round(score, 2),
        "direction": direction,
        "reason": signal_type,
        "status": public_status,
        "decision_stage": decision["decision_stage"],
        "missing_gates": decision["missing_gates"],
        "primary_blocker": decision["primary_blocker"],
        "recommended_action": decision["recommended_action"],
        "promotion_path": decision["promotion_path"],
        "theme_alignment": alignment,
        "metadata": {
            "thesis": thesis,
            "trigger": signal_type,
            "risk": risk,
            "next_action": next_action,
            "recommended_action": decision["recommended_action"],
            "decision_stage": decision["decision_stage"],
            "missing_gates": decision["missing_gates"],
            "primary_blocker": decision["primary_blocker"],
            "promotion_path": decision["promotion_path"],
            "theme_alignment": alignment,
            "theme": theme.get("name", ""),
            "source": str(signal.get("source") or "signals"),
            "price": price,
            "signal_date": str(signal.get("signal_date") or "")[:10],
            "freq": str(signal.get("freq") or ""),
            "evidence": evidence,
        },
    }


def _candidate_from_pool(
    *,
    pool_item: Mapping[str, Any],
    quote: Mapping[str, Any],
    theme: Mapping[str, Any],
    overall_confidence: float,
) -> dict[str, Any]:
    symbol = _normalize_symbol(pool_item.get("symbol"))
    sources = list(pool_item.get("sources") or ["market_pools"])
    name = str(quote.get("name") or _resolved_stock_name(symbol) or "")
    thesis = f"{symbol} 位于活跃池，优先观察是否与 {theme.get('name') or '当前市场主线'} 共振"
    alignment = _theme_alignment(symbol, theme)
    decision = _candidate_decision_metadata(
        stage="strategy_candidate",
        theme_alignment=alignment,
        status="watch",
    )
    next_action = decision["recommended_action"]
    return {
        "symbol": symbol,
        "name": name,
        "display_name": name or symbol,
        "kind": "stock",
        "score": round(45 + overall_confidence * 20, 2),
        "direction": "watch",
        "reason": "active_pool",
        "status": "watch",
        "decision_stage": decision["decision_stage"],
        "missing_gates": decision["missing_gates"],
        "primary_blocker": decision["primary_blocker"],
        "recommended_action": decision["recommended_action"],
        "promotion_path": decision["promotion_path"],
        "theme_alignment": alignment,
        "metadata": {
            "thesis": thesis,
            "trigger": "active_pool",
            "risk": "只有观察池证据，缺少可执行信号",
            "next_action": next_action,
            "recommended_action": decision["recommended_action"],
            "decision_stage": decision["decision_stage"],
            "missing_gates": decision["missing_gates"],
            "primary_blocker": decision["primary_blocker"],
            "promotion_path": decision["promotion_path"],
            "theme_alignment": alignment,
            "theme": theme.get("name", ""),
            "sources": sources,
            "price": _float(quote.get("price") or quote.get("latest_price") or quote.get("close")),
            "evidence": {
                "raw_fact": {"symbol": symbol, "sources": sources},
                "signal_evidence": {"signal_type": "", "confidence": overall_confidence},
                "strategy_thesis": thesis,
                "action_recommendation": next_action,
            },
        },
    }


def _build_market_regime(
    *,
    themes: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    pool_size: int,
    signal_count: int,
    confidence: float,
) -> dict[str, Any]:
    top_strength = _float(themes[0].get("strength"), 0.0) if themes else 0.0
    if warnings and len(warnings) >= max(len(candidates), 1):
        label = "防守观察"
    elif top_strength >= 2 and candidates:
        label = "偏进攻"
    else:
        label = "均衡观察"
    return {
        "label": label,
        "primary_theme": themes[0].get("name", "") if themes else "",
        "theme_strength": round(top_strength, 3),
        "active_pool_count": pool_size,
        "signal_count": signal_count,
        "candidate_count": len(candidates),
        "warning_count": len(warnings),
        "confidence": round(confidence, 3),
    }


def _build_chart_context(
    candidates: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    signals: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    target = (candidates or warnings or [{}])[0]
    symbol = str(target.get("symbol") or "")
    if not symbol:
        return None
    markers = []
    for signal in signals:
        if _normalize_symbol(signal.get("symbol")) != symbol:
            continue
        markers.append({
            "date_str": str(signal.get("signal_date") or "")[:10],
            "type": str(signal.get("signal_type") or ""),
            "price": _float(signal.get("price")),
            "confidence": _float(signal.get("confidence")),
        })
        if len(markers) >= 8:
            break
    metadata = _as_dict(target.get("metadata"))
    return {
        "symbol": symbol,
        "freq": str(metadata.get("freq") or "daily"),
        "conclusion": str(metadata.get("thesis") or target.get("reason") or ""),
        "latest_signal": str(metadata.get("trigger") or target.get("reason") or ""),
        "key_levels": [],
        "signal_markers": markers,
        "ohlcv_preview": [],
        "metadata": {
            "next_action": metadata.get("next_action", ""),
            "risk": metadata.get("risk", ""),
            "source": "strategy_snapshot",
        },
    }


def _build_daily_brief(
    *,
    as_of: str,
    market_regime: Mapping[str, Any],
    themes: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    changed_since_last: Mapping[str, Any],
    source_confidence: Mapping[str, Any],
) -> dict[str, Any]:
    primary = themes[0]["name"] if themes else "暂无明确主线"
    top_candidate = candidates[0]["symbol"] if candidates else ""
    top_stage = str((candidates[0] if candidates else {}).get("decision_stage") or "")
    summary = f"{market_regime.get('label', '均衡观察')}，主线关注 {primary}"
    if warnings:
        summary += f"，先处理 {len(warnings)} 个风险预警"
    if top_candidate:
        if top_stage == "entry_ready":
            summary += f"，确认买点复核 {top_candidate}"
        else:
            summary += f"，线索池观察 {top_candidate}"
    next_actions = [str(item.get("metadata", {}).get("next_action") or "") for item in candidates[:3]]
    risk_notes = [str(item.get("metadata", {}).get("risk") or "") for item in warnings[:3]]
    return {
        "as_of": as_of,
        "title": f"{as_of} Signals 策略简报",
        "summary": summary,
        "market_line": str(market_regime.get("label") or ""),
        "primary_theme": primary,
        "top_candidate": top_candidate,
        "top_candidate_stage": top_stage or ("strategy_candidate" if top_candidate else ""),
        "changed_since_last": dict(changed_since_last),
        "next_actions": [item for item in next_actions if item],
        "risk_notes": [item for item in risk_notes if item],
        "confidence": source_confidence.get("overall", 0),
    }


def _build_decision_queue(
    candidates: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    queue = []
    for idx, item in enumerate(warnings[:5]):
        metadata = _as_dict(item.get("metadata"))
        title = item.get("display_name") or item.get("name") or item.get("symbol", "")
        queue.append({
            "action_id": f"signals:warning:{item.get('symbol')}:{idx}",
            "symbol": item.get("symbol", ""),
            "name": item.get("name", ""),
            "title": f"卖出复核 · {title}",
            "action": "review_exit",
            "action_label": "复核卖点",
            "priority": "high",
            "summary": "检查是否跌破5日/20日或周线信心线，决定减仓、清仓或保留。",
            "reason": item.get("reason", ""),
            "decision_stage": item.get("decision_stage") or metadata.get("decision_stage") or "risk_first",
            "missing_gates": item.get("missing_gates") or metadata.get("missing_gates") or ["risk_clear"],
            "primary_blocker": item.get("primary_blocker") or metadata.get("primary_blocker") or "已有风险/卖出信号",
            "promotion_path": item.get("promotion_path") or metadata.get("promotion_path") or [],
            "recommended_action": "先处理风险，再看新买点",
            "next_action": metadata.get("next_action", "复核风险"),
            "operator_actions": [
                "打开图表",
                "核对5日线、20日线、5周线与最近低点",
                "写入减仓/退出条件",
            ],
            "status": "open",
            "metadata": metadata,
        })
    for idx, item in enumerate(candidates[:8]):
        metadata = _as_dict(item.get("metadata"))
        title = item.get("display_name") or item.get("name") or item.get("symbol", "")
        status = item.get("status", "open")
        stage = str(item.get("decision_stage") or metadata.get("decision_stage") or "")
        strategy_candidate = stage == "strategy_candidate"
        queue.append({
            "action_id": f"signals:candidate:{item.get('symbol')}:{idx}",
            "symbol": item.get("symbol", ""),
            "name": item.get("name", ""),
            "title": f"{'线索池' if strategy_candidate else '确认买点'} · {title}",
            "action": "watch_candidate" if strategy_candidate else "review_entry",
            "action_label": "等硬技术确认" if strategy_candidate else "复合买点",
            "priority": "medium" if status == "watch" else "high",
            "summary": (
                "线索只说明有来源，还没通过30m/5m/15m等硬技术确认。"
                if strategy_candidate
                else "打开图表确认买点、关键均线方向和止损位；虚线阶段只观察不重仓。"
            ),
            "reason": item.get("reason", ""),
            "decision_stage": stage or "strategy_candidate",
            "missing_gates": item.get("missing_gates") or metadata.get("missing_gates") or [],
            "primary_blocker": item.get("primary_blocker") or metadata.get("primary_blocker") or "",
            "promotion_path": item.get("promotion_path") or metadata.get("promotion_path") or [],
            "recommended_action": (
                metadata.get("recommended_action")
                or item.get("recommended_action")
                or ("等待硬技术确认" if strategy_candidate else "满足买点与风险线后再进入执行")
            ),
            "next_action": metadata.get("next_action", "打开图表复核"),
            "operator_actions": [
                "打开图表",
                "核对10日线/20日线/5周线位置",
                "填写买入价、止损线和仓位上限",
            ],
            "status": status,
            "metadata": metadata,
        })
    return queue[:12]


def _build_strategy_kpis(
    *,
    signals: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    pool_size: int,
    journal_summary: Mapping[str, Any],
) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    for signal in signals:
        key = str(signal.get("signal_type") or "unknown")
        group = groups.setdefault(key, {"signal_type": key, "count": 0, "evaluated": 0, "wins": 0, "return_t5": []})
        group["count"] += 1
        if int(signal.get("evaluated") or 0) == 1:
            group["evaluated"] += 1
            if int(signal.get("direction_correct") or 0) == 1:
                group["wins"] += 1
            ret = _float(signal.get("return_t5"))
            if ret is not None:
                group["return_t5"].append(ret)

    group_rows = []
    evaluated_total = 0
    wins_total = 0
    returns_total: list[float] = []
    for group in groups.values():
        evaluated = int(group["evaluated"])
        wins = int(group["wins"])
        returns = list(group["return_t5"])
        evaluated_total += evaluated
        wins_total += wins
        returns_total.extend(returns)
        group_rows.append({
            "signal_type": group["signal_type"],
            "count": int(group["count"]),
            "evaluated": evaluated,
            "win_rate": round(wins / evaluated * 100, 2) if evaluated else None,
            "avg_return_t5": round(sum(returns) / len(returns), 3) if returns else None,
        })

    group_rows.sort(key=lambda item: item["count"], reverse=True)
    total = int(journal_summary.get("total") or len(signals) or 0)
    evaluated = int(journal_summary.get("evaluated") or evaluated_total or 0)
    pending = int(journal_summary.get("pending") or max(total - evaluated, 0))
    return {
        "signals_total": total,
        "signals_evaluated": evaluated,
        "signals_pending": pending,
        "pool_size": pool_size,
        "candidate_count": len(candidates),
        "warning_count": len(warnings),
        "win_rate": round(wins_total / evaluated_total * 100, 2) if evaluated_total else None,
        "avg_return_t5": round(sum(returns_total) / len(returns_total), 3) if returns_total else None,
        "groups": group_rows[:8],
    }


def _build_source_confidence(responses: Mapping[str, Any], *, db: Any = None) -> dict[str, Any]:
    sources = []
    scores = []
    for key in ("chain_heat", "terminal_pool", "board", "concept", "market_pool", "quote", "signal"):
        meta = _response_meta(responses.get(key))
        score = _confidence_from_meta(meta)
        scores.append(score)
        sources.append({
            "name": key,
            "source": meta.get("source", ""),
            "freshness": meta.get("freshness", "unknown"),
            "as_of": meta.get("as_of"),
            "score": round(score, 3),
            "errors": list(meta.get("errors") or []),
        })

    provider_notes = []
    if db is not None:
        try:
            for doc in db["provider_health"].find({}, {"_id": 0}).sort("updated_at", -1).limit(8):
                provider_notes.append({
                    "provider": str(doc.get("provider") or ""),
                    "endpoint": str(doc.get("endpoint") or ""),
                    "status": str(doc.get("status") or ""),
                    "latency_ms": _float(doc.get("avg_latency_ms") or doc.get("latency_ms")),
                })
        except Exception:
            provider_notes = []

    overall = sum(scores) / len(scores) if scores else 0.0
    if provider_notes and any(item.get("status") == "degraded" for item in provider_notes):
        overall = min(overall, 0.75)
    return {
        "overall": round(overall, 3),
        "sources": sources,
        "provider_notes": provider_notes,
    }


def _confidence_from_meta(meta: Mapping[str, Any]) -> float:
    freshness = str(meta.get("freshness") or "unknown")
    if freshness == "fresh":
        score = 0.9
    elif freshness in {"partial", "pending"}:
        score = 0.6
    elif freshness == "stale":
        score = 0.45
    elif freshness == "empty":
        score = 0.2
    else:
        score = 0.4
    if meta.get("is_stale"):
        score = min(score, 0.5)
    if meta.get("errors"):
        score = max(0.1, score - 0.2)
    return round(score, 3)


def _latest_previous_snapshot(db: Any, *, as_of: str) -> Optional[dict[str, Any]]:
    if db is None:
        return None
    try:
        doc = db["strategy_snapshots"].find_one(
            {"as_of": {"$lt": as_of}},
            {"_id": 0, "snapshot": 1},
            sort=[("as_of", -1), ("updated_at", -1)],
        )
        if doc and isinstance(doc.get("snapshot"), Mapping):
            return dict(doc["snapshot"])
    except Exception:
        return None
    return None


def _changed_since_last(current: Mapping[str, Any], previous: Mapping[str, Any]) -> dict[str, Any]:
    prev_themes = {item.get("name") for item in _as_list(previous.get("themes"))}
    curr_themes = {item.get("name") for item in _as_list(current.get("themes"))}
    prev_candidates = {item.get("symbol") for item in _as_list(previous.get("candidates"))}
    curr_candidates = {item.get("symbol") for item in _as_list(current.get("candidates"))}
    prev_warnings = {item.get("symbol") for item in _as_list(previous.get("warnings"))}
    curr_warnings = {item.get("symbol") for item in _as_list(current.get("warnings"))}
    return {
        "new_themes": sorted(item for item in curr_themes - prev_themes if item),
        "dropped_themes": sorted(item for item in prev_themes - curr_themes if item),
        "new_candidates": sorted(item for item in curr_candidates - prev_candidates if item),
        "dropped_candidates": sorted(item for item in prev_candidates - curr_candidates if item),
        "new_warnings": sorted(item for item in curr_warnings - prev_warnings if item),
    }


def _journal_summary() -> dict[str, int]:
    try:
        from signals.core.backtest import SignalJournal

        journal = SignalJournal()
        try:
            summary = journal.summary()
            return {
                "total": int(summary.get("total") or 0),
                "evaluated": int(summary.get("evaluated") or 0),
                "pending": int(summary.get("pending") or 0),
            }
        finally:
            journal.close()
    except Exception:
        return {"total": 0, "evaluated": 0, "pending": 0}


def _signal_status(signal: Mapping[str, Any]) -> str:
    status = str(signal.get("pool_status") or signal.get("status") or "").lower()
    if status == "warning":
        return "warning"
    text = str(signal.get("signal_type") or signal.get("type") or "")
    return "warning" if any(token in text for token in WARNING_TOKENS) else "candidate"


def _signal_score(signal: Mapping[str, Any]) -> float:
    for key in ("score", "total_score", "fused_total"):
        value = _float(signal.get(key))
        if value is not None:
            return value
    confidence = _float(signal.get("confidence"))
    return (confidence * 100) if confidence is not None else 50.0


def _thesis_for(symbol: str, signal_type: str, theme: Mapping[str, Any]) -> str:
    theme_name = str(theme.get("name") or "当前市场主线")
    return f"{symbol} 出现 {signal_type}，需要验证是否与 {theme_name} 共振"


def _risk_for(status: str, signal: Mapping[str, Any], theme: Mapping[str, Any]) -> str:
    if status == "warning":
        return "已有风险/卖出类信号，优先处理仓位和退出条件"
    if _float(theme.get("strength"), 0.0) >= 5:
        return "主题涨幅较高，避免追高"
    confidence = _float(signal.get("confidence"))
    if confidence is not None and confidence < 0.5:
        return "信号置信度偏低，需要等待二次确认"
    return "按图表确认止损位后再行动"


def _resolved_stock_name(symbol: str) -> str:
    try:
        from signals.core.stock_names import get_resolver

        name = get_resolver().get_name(symbol)
        return "" if name == _symbol_digits(symbol) else name
    except Exception:
        return ""


def _normalize_symbol(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    if raw.startswith(("SH.", "SZ.", "BJ.", "HK.", "US.")):
        return raw
    if len(raw) >= 8 and raw[:2] in {"SH", "SZ", "BJ", "HK", "US"}:
        return f"{raw[:2]}.{raw[2:]}"
    pure = raw.replace("SH", "").replace("SZ", "").replace("BJ", "")
    if pure.isdigit() and len(pure) == 6:
        if pure.startswith(("5", "6", "9")):
            return f"SH.{pure}"
        if pure.startswith(("0", "2", "3")):
            return f"SZ.{pure}"
        if pure.startswith(("4", "8")):
            return f"BJ.{pure}"
    if pure.isdigit() and len(pure) == 5:
        return f"HK.{pure}"
    return raw


def _symbol_digits(symbol: str) -> str:
    value = str(symbol or "").upper()
    if "." in value:
        return value.split(".", 1)[1]
    for prefix in ("SH", "SZ", "BJ", "HK", "US"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def _float(value: Any, default: Any = None) -> Any:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items() if key != "_id"}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if hasattr(value, "date") and callable(value.date):
        try:
            return value.date().isoformat()
        except Exception:
            return str(value)
    return value
