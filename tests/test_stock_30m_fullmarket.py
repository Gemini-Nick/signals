# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.sync.modules import stock_30m_fullmarket


def test_30m_tail_count_keeps_full_window_for_underfilled_symbol(monkeypatch):
    monkeypatch.delenv("STOCK_30M_FULLMARKET_REFRESH_TAIL_COUNT", raising=False)
    tail_count = stock_30m_fullmarket._tail_count_for_state(
        {"bar_count": 120, "latest_dt": datetime(2026, 4, 28, 15, 0)},
        min_bars=260,
        default_tail_count=320,
        trade_date="2026-04-29",
    )

    assert tail_count == 320


def test_30m_tail_count_uses_small_gap_window_for_filled_symbol(monkeypatch):
    monkeypatch.delenv("STOCK_30M_FULLMARKET_TAIL_OVERLAP", raising=False)
    tail_count = stock_30m_fullmarket._tail_count_for_state(
        {"bar_count": 300, "latest_dt": datetime(2026, 4, 28, 15, 0)},
        min_bars=260,
        default_tail_count=320,
        trade_date="2026-04-29",
    )

    assert tail_count == 40

