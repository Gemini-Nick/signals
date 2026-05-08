# -*- coding: utf-8 -*-
"""Shared rule layer for chain-mapping candidate adjudication."""
from __future__ import annotations

from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def mapping_specificity(match: dict[str, Any]) -> int:
    evidence_sources = {_text(item) for item in match.get("evidence_sources") or [] if _text(item)}
    if "alias" in evidence_sources:
        return 3
    if "node_keyword" in evidence_sources and "industry" not in evidence_sources:
        return 2
    if "node_keyword" in evidence_sources:
        return 1
    return 0


def filter_mapping_matches(matches: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    if not matches:
        return [], ""

    best_score = _int(matches[0].get("score"))
    retained = [match for match in matches if _int(match.get("score")) >= best_score - 15]
    if not retained:
        return [], ""

    best_specificity = max(mapping_specificity(match) for match in retained)
    if best_specificity > 0:
        specific = [match for match in retained if mapping_specificity(match) == best_specificity]
        specific_best = max(_int(match.get("score")) for match in specific)
        return [match for match in specific if _int(match.get("score")) >= specific_best - 15], "specific_only"

    chain_ids = {_text(match.get("chain_id")) for match in retained if _text(match.get("chain_id"))}
    if len(chain_ids) > 1:
        return [], "ambiguous_industry_only"
    return retained, "industry_only"


def matches_from_ai_decision(matches: list[dict[str, Any]], decision: dict[str, Any]) -> list[dict[str, Any]]:
    if decision.get("status") != "mapped":
        return []
    matches_by_key = {f"{_text(match.get('chain_id'))}:{_text(match.get('node_id'))}": match for match in matches}
    selected: list[dict[str, Any]] = []
    for item in decision.get("decisions") or []:
        key = _text(item.get("candidate_id"))
        match = matches_by_key.get(key)
        if not match:
            continue
        ai_confidence = _int(item.get("confidence"))
        merged = dict(match)
        merged["ai_confidence"] = ai_confidence
        merged["ai_reason"] = _text(item.get("reason"))
        merged["score"] = max(_int(match.get("score")), ai_confidence)
        merged["confidence"] = max(_int(match.get("confidence")), ai_confidence)
        hit_terms = list(dict.fromkeys(list(match.get("hit_terms") or []) + list(item.get("matched_terms") or [])))[:8]
        evidence_sources = list(dict.fromkeys(list(match.get("evidence_sources") or []) + ["ai_semantic_mapper"]))[:8]
        merged["hit_terms"] = hit_terms
        merged["evidence_sources"] = evidence_sources
        selected.append(merged)
    selected.sort(key=lambda row: (_int(row.get("ai_confidence")), _int(row.get("score"))), reverse=True)
    return selected
