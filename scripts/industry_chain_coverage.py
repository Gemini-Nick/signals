#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate concept/board coverage against industry_chains.yaml.

The report accounts for every local concept or industry name as mapped,
low_confidence, or unmapped. It does not silently fall back to a leader stock.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from signals.core.concept_carriers import build_mapping_coverage  # noqa: E402

COLLECTIONS = (
    "concept_ranking",
    "concept_em",
    "concept_sina",
    "concept_ths",
    "board_ranking",
    "board_constituents",
    "concept_constituents",
)
NAME_FIELDS = ("board_name", "concept_name", "concept", "name", "label")


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def collect_local_names(limit_per_collection: int = 0) -> list[str]:
    from signals.sync.db import get_db

    db = get_db()
    names: list[str] = []
    existing = set(db.list_collection_names())
    for collection in COLLECTIONS:
        if collection not in existing:
            continue
        cursor = db[collection].find({}, {field: 1 for field in NAME_FIELDS})
        if limit_per_collection > 0:
            cursor = cursor.limit(limit_per_collection)
        for row in cursor:
            for field in NAME_FIELDS:
                name = _text(row.get(field))
                if name and name not in names:
                    names.append(name)
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=int, default=65, help="low-confidence cutoff")
    parser.add_argument("--limit", type=int, default=0, help="limit rows per Mongo collection")
    parser.add_argument("--compact", action="store_true", help="print compact JSON")
    args = parser.parse_args()

    names = collect_local_names(limit_per_collection=args.limit)
    report = build_mapping_coverage(names, confidence_threshold=args.threshold)
    report["source"] = {
        "collections": list(COLLECTIONS),
        "local_name_count": len(names),
        "confidence_threshold": args.threshold,
    }
    print(json.dumps(report, ensure_ascii=False, indent=None if args.compact else 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
