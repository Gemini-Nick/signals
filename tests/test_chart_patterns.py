# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.core.chart_patterns import classify_latest_chart_pattern


def test_chart_pattern_marks_daily_ma20_touch_reclaim():
    rows = [
        {"open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0}
        for _ in range(19)
    ]
    rows.append({"open": 105.0, "high": 106.0, "low": 99.0, "close": 100.5})

    pattern = classify_latest_chart_pattern(rows, "daily")
    primary = pattern["primary_chart_signal"]
    ma20 = next(item for item in pattern["level_interactions"] if item["period"] == 20)

    assert primary["label"] == "MA20触线收回"
    assert primary["side"] == "neutral"
    assert ma20["interaction"] == "touch_reclaim"


def test_chart_pattern_marks_weekly_descending_channel_over_ma5_fallback():
    rows = [
        {"open": 84.0, "high": 90.0, "low": 80.0, "close": 85.0},
        {"open": 94.0, "high": 100.0, "low": 90.0, "close": 95.0},
        {"open": 109.0, "high": 120.0, "low": 100.0, "close": 110.0},
        {"open": 106.0, "high": 115.0, "low": 95.0, "close": 105.0},
        {"open": 104.0, "high": 108.0, "low": 90.0, "close": 92.0},
    ]

    pattern = classify_latest_chart_pattern(rows, "weekly")
    primary = pattern["primary_chart_signal"]

    assert pattern["channel_state"]["type"] == "descending_channel"
    assert primary["label"] == "周线下降通道"
    assert primary["priority"] > 90
    assert "5周线反抽未过" in primary["details"]


def test_chart_pattern_keeps_ma5_unconfirmed_as_fallback():
    rows = [
        {"open": 95.0, "high": 96.0, "low": 94.0, "close": 95.0},
        {"open": 105.0, "high": 106.0, "low": 104.0, "close": 105.0},
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
        {"open": 106.0, "high": 107.0, "low": 105.0, "close": 106.0},
        {"open": 100.0, "high": 100.0, "low": 98.0, "close": 99.0},
    ]

    pattern = classify_latest_chart_pattern(rows, "weekly")
    primary = pattern["primary_chart_signal"]

    assert pattern["channel_state"]["type"] == "none"
    assert primary["label"] == "未站稳5周线"
    assert primary["priority"] < 30
