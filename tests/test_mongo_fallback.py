# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.data import mongo_fallback
from signals.core import market_time


def test_get_last_trading_day_uses_market_timezone(monkeypatch):
    monkeypatch.setattr(market_time, "naive_market_now", lambda _market: datetime(2026, 4, 29, 17, 30))

    assert mongo_fallback.get_last_trading_day("A") == "2026-04-29"


def test_get_last_trading_day_before_open_uses_previous_workday(monkeypatch):
    monkeypatch.setattr(market_time, "naive_market_now", lambda _market: datetime(2026, 4, 29, 9, 0))

    assert mongo_fallback.get_last_trading_day("A") == "2026-04-28"
