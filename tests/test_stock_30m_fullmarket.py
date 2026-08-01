# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.sync.modules import stock_30m_fullmarket as fullmarket_30m


class _Collection:
    def __init__(self, rows=None, distinct_values=None):
        self.rows = rows or []
        self.distinct_values = distinct_values or []

    def find(self, *args, **kwargs):
        return list(self.rows)

    def distinct(self, *args, **kwargs):
        return list(self.distinct_values)


class _Db(dict):
    def __missing__(self, key):
        self[key] = _Collection()
        return self[key]


def test_fullmarket_30m_uses_valid_current_spot_universe(monkeypatch):
    monkeypatch.setattr(
        fullmarket_30m,
        "a_share_task_trade_date",
        lambda now=None: "2026-07-31",
    )
    db = _Db({
        "fullmarket_spot_snapshots": _Collection([
            {"code": "600000", "symbol": "SH.600000", "price": 10.0, "open": 9.8, "high": 10.2, "low": 9.7, "prev_close": 9.9},
            {"code": "000001", "symbol": "SZ.000001", "price": 0, "open": 1, "high": 1, "low": 1, "prev_close": 1},
            {"code": "000016", "symbol": "SZ.000016", "price": 2.8, "open": 2.7, "high": 2.9, "low": 2.6, "prev_close": 2.7},
            {"code": "159999", "symbol": "SZ.159999", "price": 1.0, "open": 1, "high": 1.1, "low": 0.9, "prev_close": 1.0, "asset_class": "etf"},
        ]),
        "bars": _Collection(distinct_values=["300001"]),
    })

    assert fullmarket_30m._symbols_with_daily(db) == ["000016", "600000"]


def test_short_history_symbol_is_done_when_today_is_present():
    assert fullmarket_30m._needs_refresh(
        {"bar_count": 16, "latest_dt": datetime(2026, 7, 31, 15, 0)},
        min_bars=260,
        trade_date="2026-07-31",
        require_today=True,
    ) is False


def test_short_history_symbol_is_due_when_today_is_missing():
    assert fullmarket_30m._needs_refresh(
        {"bar_count": 16, "latest_dt": datetime(2026, 7, 30, 15, 0)},
        min_bars=260,
        trade_date="2026-07-31",
        require_today=True,
    ) is True
