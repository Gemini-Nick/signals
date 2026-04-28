# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from signals.sync.modules.technical_signal_scan import _resonance_context


@dataclass
class _Event:
    freq: str
    signal_type: str
    confidence: float = 1.0
    dt: datetime = datetime(2026, 4, 28, 15, 0, 0)


WEIGHTS = {
    "三买": 100,
    "趋势买": 80,
    "背驰买": 70,
    "一卖": -100,
}


def test_resonance_context_marks_single_period_signal():
    context = _resonance_context(
        [_Event("30分钟", "三买")],
        side="buy",
        primary_freq="30分钟",
        direction="buy",
        weights=WEIGHTS,
    )

    assert context["grade"] == "single_period"
    assert context["aligned_freqs"] == ["30分钟"]
    assert context["conflict_freqs"] == []
    assert context["tags"] == ["硬技术"]


def test_resonance_context_marks_multi_period_alignment():
    context = _resonance_context(
        [_Event("日线", "趋势买"), _Event("周线", "背驰买"), _Event("5分钟", "三买")],
        side="buy",
        primary_freq="日线",
        direction="buy",
        weights=WEIGHTS,
    )

    assert context["grade"] == "strong_resonance"
    assert context["aligned_freqs"] == ["周线", "日线", "5分钟"]
    assert "多周期共振" in context["tags"]
    assert "日周同向" in context["tags"]
    assert "5m确认" in context["tags"]


def test_resonance_context_marks_period_conflict():
    context = _resonance_context(
        [_Event("30分钟", "三买"), _Event("日线", "一卖")],
        side="buy",
        primary_freq="30分钟",
        direction="buy",
        weights=WEIGHTS,
    )

    assert context["grade"] == "conflict"
    assert context["aligned_freqs"] == ["30分钟"]
    assert context["conflict_freqs"] == ["日线"]
    assert "周期冲突" in context["tags"]
