# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from signals.replay.evaluate import evaluate_text
from signals.replay.training_renderer import load_training_facts, render_training_sample


def test_training_renderer_matches_screenshot_reference_exactly():
    target = Path("signals/replay/references/2026-06-05-screenshot.txt").read_text(encoding="utf-8").strip()
    generated = render_training_sample(load_training_facts("2026-06-05-screenshot"))
    report = evaluate_text(generated, target)

    assert generated == target
    assert report["char_similarity"] == 1.0
    assert report["phrase_coverage"]["missing"] == []
