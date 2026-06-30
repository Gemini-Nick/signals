# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from signals.replay.evaluate import evaluate_text, load_key_phrases, load_text, normalize_generated_text


def test_replay_evaluator_reports_exact_reference_match():
    target = Path("signals/replay/references/2026-06-05-screenshot.txt").read_text(encoding="utf-8").strip()
    report = evaluate_text(target, target)

    assert report["char_similarity"] == 1.0
    assert report["phrase_coverage"]["covered"] == report["phrase_coverage"]["total"]
    assert report["phrase_coverage"]["missing"] == []


def test_replay_evaluator_loads_named_reference():
    target = load_text("2026-06-05-screenshot")

    assert target.startswith("2026年6月5日复盘")
    assert "盘面看着热闹，尾盘一锅端" in target


def test_replay_evaluator_reports_missing_key_phrases():
    report = evaluate_text("2026年6月5日复盘\n\n今天市场走弱。", "2026年6月5日复盘\n\n中际旭创单日成交583亿。")

    assert report["char_similarity"] < 1.0
    assert "中际旭创单日成交583亿" in report["phrase_coverage"]["missing"]


def test_replay_evaluator_strips_notification_gate():
    target = "2026年6月5日复盘\n\n中际旭创单日成交583亿。"

    assert normalize_generated_text("NOTIFY\n" + target) == target
    assert evaluate_text("NOTIFY\n" + target, target)["char_similarity"] == 1.0


def test_replay_evaluator_loads_word_sample_phrases():
    phrases = load_key_phrases("2026-06-29-word")
    target = load_text("2026-06-29-word")
    report = evaluate_text(target, target, key_phrases=phrases)

    assert "极度分化，科创单骑救主" in phrases
    assert "今日最强板块 TOP10" in phrases
    assert report["phrase_coverage"]["missing"] == []
