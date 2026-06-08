# -*- coding: utf-8 -*-
"""Evaluate generated replay text against a screenshot/reference target."""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any


REFERENCE_DIR = Path(__file__).resolve().parent / "references"


KEY_PHRASES = [
    "盘面看着热闹，尾盘一锅端",
    "中际旭创单日成交583亿",
    "开盘后光模块方向直接低开",
    "步步高从4.46直接拉到4.86封涨停",
    "这时有一条暗线开始露头—商业航天",
    "9点33分是第一个关键转折点",
    "10点半是第二个关键转折点",
    "下午机器人方向被资金平铺买入",
    "下午1点30分是整个盘面的崩塌点",
    "主力全天买入426亿卖出490亿",
    "尾盘情绪彻底崩溃",
    "看一下今天盘面的卡位结构",
    "关于情绪温度",
    "时间周期维度",
    "明天的核心问题是",
]


def load_text(path_or_name: str) -> str:
    if path_or_name == "-":
        return sys.stdin.read().strip()
    path = Path(path_or_name)
    if not path.exists():
        path = REFERENCE_DIR / f"{path_or_name}.txt"
    return path.read_text(encoding="utf-8").strip()


def normalize_generated_text(text: str) -> str:
    lines = text.strip().splitlines()
    if lines and lines[0].strip() in {"NOTIFY", "DONT_NOTIFY"}:
        return "\n".join(lines[1:]).strip()
    return text.strip()


def _paragraphs(text: str) -> list[str]:
    return [part.strip() for part in text.strip().split("\n\n") if part.strip()]


def _similarity(a: str, b: str) -> float:
    return round(difflib.SequenceMatcher(a=a, b=b).ratio(), 6)


def evaluate_text(generated: str, target: str) -> dict[str, Any]:
    generated = normalize_generated_text(generated)
    target = target.strip()
    target_paragraphs = _paragraphs(target)
    generated_paragraphs = _paragraphs(generated)
    phrase_hits = {phrase: phrase in generated for phrase in KEY_PHRASES}
    paragraph_scores = []
    for index, paragraph in enumerate(target_paragraphs):
        best = max((_similarity(paragraph, candidate) for candidate in generated_paragraphs), default=0.0)
        paragraph_scores.append({"index": index + 1, "similarity": best, "target_start": paragraph[:40]})
    return {
        "char_similarity": _similarity(generated, target),
        "target_chars": len(target),
        "generated_chars": len(generated),
        "target_paragraphs": len(target_paragraphs),
        "generated_paragraphs": len(generated_paragraphs),
        "phrase_coverage": {
            "covered": sum(1 for hit in phrase_hits.values() if hit),
            "total": len(phrase_hits),
            "missing": [phrase for phrase, hit in phrase_hits.items() if not hit],
        },
        "paragraph_scores": paragraph_scores,
    }


def unified_diff(generated: str, target: str, *, max_lines: int = 120) -> str:
    lines = list(
        difflib.unified_diff(
            target.splitlines(),
            generated.splitlines(),
            fromfile="target",
            tofile="generated",
            lineterm="",
        )
    )
    return "\n".join(lines[:max_lines])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare generated replay text against a reference target.")
    parser.add_argument("--target", default="2026-06-05-screenshot")
    parser.add_argument("--generated", required=True)
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("--min-similarity", type=float, default=0.0)
    parser.add_argument("--require-all-phrases", action="store_true")
    args = parser.parse_args(argv)

    target = load_text(args.target)
    generated = load_text(args.generated)
    result = evaluate_text(generated, target)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.diff:
        print("\n--- diff ---")
        print(unified_diff(generated, target))
    failed = result["char_similarity"] < args.min_similarity
    if args.require_all_phrases and result["phrase_coverage"]["missing"]:
        failed = True
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
