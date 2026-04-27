# -*- coding: utf-8 -*-
from __future__ import annotations

import config
from signals.sync.modules import stock_minute


def test_priority_selection_can_exceed_signal_lane_cap():
    selected, skipped = stock_minute._select_symbols_with_priority(
        ["688802", "300575", "600000"],
        {"688802", "300575"},
        max_symbols=1,
    )

    assert selected == ["688802", "300575"]
    assert skipped == [{"symbol": "600000", "reason": "cap_exceeded", "next_due_hint": "next signal_lane cycle"}]


def test_shanghai_composite_is_treated_as_index_not_stock():
    assert config.INDEX_AK_CODES["上证指数"] == "sh000001"
    assert "000001" in stock_minute._index_codes()
