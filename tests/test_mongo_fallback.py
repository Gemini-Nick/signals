# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from signals.data import mongo_fallback
from signals.core import market_time
from signals.core.calendar.engine import get_calendar


def test_get_last_trading_day_uses_market_timezone(monkeypatch):
    monkeypatch.setattr(market_time, "naive_market_now", lambda _market: datetime(2026, 4, 29, 17, 30))

    assert mongo_fallback.get_last_trading_day("A") == "2026-04-29"


def test_get_last_trading_day_before_open_uses_previous_workday(monkeypatch):
    monkeypatch.setattr(market_time, "naive_market_now", lambda _market: datetime(2026, 4, 29, 9, 0))

    assert mongo_fallback.get_last_trading_day("A") == "2026-04-28"


def test_get_last_trading_day_skips_cn_labor_day_holiday(monkeypatch):
    monkeypatch.setattr(market_time, "naive_market_now", lambda _market: datetime(2026, 5, 1, 16, 30))

    assert mongo_fallback.get_last_trading_day("A") == "2026-04-30"


def test_calendar_next_transition_does_not_land_on_cn_holiday_gap():
    now = datetime(2026, 5, 1, 16, 49, tzinfo=ZoneInfo("Asia/Shanghai"))
    seconds = get_calendar().next_transition(now)
    next_at = datetime.fromtimestamp(now.timestamp() + seconds, tz=ZoneInfo("Asia/Shanghai"))

    assert next_at.date().isoformat() != "2026-05-04"
