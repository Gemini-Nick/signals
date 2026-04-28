# -*- coding: utf-8 -*-
from __future__ import annotations

import config
from signals.sync.modules import stock_minute


def test_priority_selection_respects_signal_lane_cap():
    selected, skipped = stock_minute._select_symbols_with_priority(
        ["688802", "300575", "600000"],
        {"688802", "300575"},
        max_symbols=1,
    )

    assert selected == ["688802"]
    assert skipped == [
        {"symbol": "300575", "reason": "cap_exceeded", "next_due_hint": "next signal_lane cycle"},
        {"symbol": "600000", "reason": "cap_exceeded", "next_due_hint": "next signal_lane cycle"},
    ]


def test_shanghai_composite_is_treated_as_index_not_stock():
    assert config.INDEX_AK_CODES["上证指数"] == "sh000001"
    assert "000001" in stock_minute._index_codes()


def test_stock_minute_worker_count_is_constrained(monkeypatch):
    monkeypatch.setenv("STOCK_MINUTE_WORKERS", "99")
    assert stock_minute._worker_count() == 6

    monkeypatch.setenv("STOCK_MINUTE_WORKERS", "0")
    assert stock_minute._worker_count() == 1


def test_stock_minute_tail_count_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("STOCK_MINUTE_TAIL_COUNT", raising=False)
    monkeypatch.delenv("STOCK_MINUTE_TAIL_COUNT_5", raising=False)
    monkeypatch.delenv("STOCK_MINUTE_TAIL_COUNT_15", raising=False)
    monkeypatch.delenv("STOCK_MINUTE_TAIL_COUNT_30", raising=False)

    assert stock_minute._tail_count_for_freq("5分钟") == 240
    assert stock_minute._tail_count_for_freq("15分钟") == 160
    assert stock_minute._tail_count_for_freq("30分钟") == 120

    monkeypatch.setenv("STOCK_MINUTE_TAIL_COUNT", "80")
    monkeypatch.setenv("STOCK_MINUTE_TAIL_COUNT_30", "90")
    assert stock_minute._tail_count_for_freq("5分钟") == 80
    assert stock_minute._tail_count_for_freq("30分钟") == 90
