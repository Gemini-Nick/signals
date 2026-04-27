#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate offline AI draft prompts for unmapped industry-chain themes.

This script does not call an LLM. It turns the local coverage report into
reviewable draft JSON so AI suggestions can be generated and promoted offline.
Only manually reviewed changes should be copied into signals/core/industry_chains.yaml.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from industry_chain_coverage import collect_local_names  # noqa: E402
from signals.core.concept_carriers import build_mapping_coverage  # noqa: E402

DEFAULT_OUTPUT_DIR = ROOT / "reports" / "industry-chain-drafts"
TARGET_FILE = "signals/core/industry_chains.yaml"


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _draft_prompt(name: str, status: str, evidence: dict[str, Any] | None = None) -> str:
    evidence = evidence or {}
    context = {
        "theme": name,
        "coverage_status": status,
        "current_guess": {
            "chain_id": evidence.get("chain_id"),
            "chain_name": evidence.get("chain_name"),
            "node_id": evidence.get("node_id"),
            "node_name": evidence.get("node_name"),
            "confidence": evidence.get("confidence"),
            "hit_terms": evidence.get("hit_terms") or [],
        },
    }
    return (
        "请基于A股产业链常识，为下面主题生成 industry_chains.yaml 的补全草稿。"
        "只输出候选 chain_id/node_id/keywords/core_representatives/elastic_representatives，"
        "并明确上下游位置、龙头/龙二/龙三、权重股、弹性股、需要验证的成分股来源；"
        "不要改写已审核 YAML。\n"
        f"{json.dumps(context, ensure_ascii=False)}"
    )


def _draft_row(name: str, status: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    evidence = evidence or {}
    return {
        "name": name,
        "status": status,
        "current_mapping": {
            "chain_id": evidence.get("chain_id"),
            "chain_name": evidence.get("chain_name"),
            "node_id": evidence.get("node_id"),
            "node_name": evidence.get("node_name"),
            "confidence": evidence.get("confidence"),
            "hit_terms": evidence.get("hit_terms") or [],
            "evidence_sources": evidence.get("evidence_sources") or [],
        },
        "ai_prompt": _draft_prompt(name, status, evidence),
        "promotion_gate": {
            "requires_manual_review": True,
            "target_file": TARGET_FILE,
            "required_checks": [
                "行情源存在对应概念/行业或成分股证据",
                "至少一个龙头/权重代表具备可用日K",
                "候选弹性股具备近期成交活跃证据",
                "上下游节点不与既有链条重复冲突",
            ],
        },
    }


def build_drafts(names: list[str], *, threshold: int) -> dict[str, Any]:
    coverage = build_mapping_coverage(names, confidence_threshold=threshold)
    drafts: list[dict[str, Any]] = []
    for row in coverage.get("low_confidence") or []:
        if not isinstance(row, dict):
            continue
        name = _text(row.get("name"))
        if name:
            drafts.append(_draft_row(name, "low_confidence", row))
    for name in coverage.get("unmapped") or []:
        text = _text(name)
        if text:
            drafts.append(_draft_row(text, "unmapped"))
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "script": "scripts/generate_industry_chain_drafts.py",
            "coverage_script": "scripts/industry_chain_coverage.py",
            "target_file": TARGET_FILE,
            "confidence_threshold": threshold,
            "local_name_count": len(names),
        },
        "coverage_counts": coverage.get("counts") or {},
        "non_chain": coverage.get("non_chain") or [],
        "draft_count": len(drafts),
        "drafts": drafts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=int, default=65, help="low-confidence cutoff")
    parser.add_argument("--limit", type=int, default=0, help="limit rows per Mongo collection")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--dry-run", action="store_true", help="print JSON without writing a report file")
    parser.add_argument("--compact", action="store_true", help="print compact JSON")
    args = parser.parse_args()

    names = collect_local_names(limit_per_collection=args.limit)
    payload = build_drafts(names, threshold=args.threshold)
    rendered = json.dumps(payload, ensure_ascii=False, indent=None if args.compact else 2)
    if args.dry_run:
        print(rendered)
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(rendered + "\n", encoding="utf-8")
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
