#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit security -> industry-chain assignments for systemic mismatch risks."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from signals.core.concept_carriers import non_chain_reason  # noqa: E402
from signals.sync.db import get_db  # noqa: E402


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _source_board_names(row: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in row.get("source_boards") or []:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        if name and name not in names:
            names.append(name)
    return names


def _source_board_kinds(row: dict[str, Any]) -> set[str]:
    return {
        _text(item.get("kind"))
        for item in row.get("source_boards") or []
        if isinstance(item, dict) and _text(item.get("kind"))
    }


def _specific_industry_primary(row: dict[str, Any]) -> bool:
    return (
        _text(row.get("membership_type")) == "core"
        and _float(row.get("chain_specificity_score")) >= 3
        and "industry" in _source_board_kinds(row)
    )


def _membership_rank(value: Any) -> int:
    return {
        "reviewed_primary": 4,
        "core": 3,
        "theme": 2,
        "weak_related": 1,
    }.get(_text(value), 0)


def _latest_trade_date(db: Any) -> str:
    doc = db["security_chain_memberships"].find_one(
        {"market": "A", "trade_date": {"$exists": True}},
        {"trade_date": 1},
        sort=[("trade_date", -1)],
    ) or {}
    return _text(doc.get("trade_date"))


def _primary_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    for row in rows:
        if row.get("is_primary_chain"):
            return row
    return max(
        rows,
        key=lambda row: (
            1 if row.get("reviewed_override") else 0,
            _membership_rank(row.get("membership_type")),
            _float(row.get("chain_specificity_score")),
            _float(row.get("exposure_score")),
            _float(row.get("confidence")),
        ),
        default={},
    )


def _risk_reasons(primary: dict[str, Any], alternatives: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    board_names = _source_board_names(primary)
    non_chain_boards = [name for name in board_names if non_chain_reason(name)]
    if non_chain_boards:
        reasons.append("primary_from_non_chain_theme:" + ",".join(non_chain_boards[:4]))
    if _float(primary.get("confidence")) < 65 and not _specific_industry_primary(primary):
        reasons.append("primary_low_confidence")
    if _text(primary.get("membership_type")) == "weak_related":
        reasons.append("weak_related_primary")
    if _text(primary.get("membership_type")) == "theme" and "industry" not in _source_board_kinds(primary):
        stronger = [
            row for row in alternatives
            if _membership_rank(row.get("membership_type")) > _membership_rank(primary.get("membership_type"))
            or row.get("reviewed_override")
            or _text(row.get("membership_type")) == "core"
        ]
        if stronger:
            reasons.append("theme_primary_over_stronger_alternative")
    if _text(primary.get("chain_id")) == "consumer" and _text(primary.get("node_id")) == "consumer_goods":
        narrow_food_terms = (
            "白酒", "啤酒", "食品", "饮料", "乳品", "调味品", "预制菜", "化妆品", "宠物", "谷子",
            "零售", "商贸", "电商", "新消费", "免税", "退税", "婴童", "烟", "品牌", "加工",
            "人造肉", "味蕾",
        )
        if board_names and not any(any(term in name for term in narrow_food_terms) for name in board_names):
            reasons.append("consumer_primary_without_narrow_consumer_evidence")
    return reasons


def audit(trade_date: str = "", limit: int = 80) -> dict[str, Any]:
    db = get_db()
    trade_date = trade_date or _latest_trade_date(db)
    if not trade_date:
        return {"status": "empty", "trade_date": "", "risks": []}

    rows = list(db["security_chain_memberships"].find(
        {"market": "A", "trade_date": trade_date},
        {"_id": 0},
    ))
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = _text(row.get("security_id")) or _text(row.get("symbol")) or _text(row.get("raw_code"))
        if key:
            grouped[key].append(row)

    risks: list[dict[str, Any]] = []
    for security_rows in grouped.values():
        primary = _primary_row(security_rows)
        if not primary:
            continue
        alternatives = [row for row in security_rows if row is not primary]
        reasons = _risk_reasons(primary, alternatives)
        if not reasons:
            continue
        risks.append({
            "symbol": primary.get("symbol"),
            "raw_code": primary.get("raw_code"),
            "name": primary.get("name"),
            "chain_id": primary.get("chain_id"),
            "chain_name": primary.get("chain_name"),
            "node_id": primary.get("node_id"),
            "node_name": primary.get("node_name"),
            "membership_type": primary.get("membership_type"),
            "confidence": primary.get("confidence"),
            "exposure_score": primary.get("exposure_score"),
            "source_boards": _source_board_names(primary),
            "risk_reasons": reasons,
            "alternatives": [
                {
                    "chain_id": row.get("chain_id"),
                    "chain_name": row.get("chain_name"),
                    "node_id": row.get("node_id"),
                    "node_name": row.get("node_name"),
                    "membership_type": row.get("membership_type"),
                    "confidence": row.get("confidence"),
                    "source_boards": _source_board_names(row),
                }
                for row in alternatives[:5]
            ],
        })

    risks.sort(key=lambda item: (len(item["risk_reasons"]), _float(item.get("confidence"))), reverse=True)
    return {
        "status": "ok",
        "trade_date": trade_date,
        "membership_count": len(rows),
        "security_count": len(grouped),
        "risk_count": len(risks),
        "risks": risks[:limit],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", default="", help="YYYY-MM-DD, default latest")
    parser.add_argument("--limit", type=int, default=80, help="maximum risk rows to print")
    args = parser.parse_args()
    print(json.dumps(audit(trade_date=args.trade_date, limit=args.limit), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
