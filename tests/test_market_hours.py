# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from signals.core.market_hours import Market, get_active_markets, get_session_mode


BJ = ZoneInfo("Asia/Shanghai")
ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def bj_dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=BJ).astimezone(UTC)


def et_dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET).astimezone(UTC)


def test_a_and_hk_are_active_at_beijing_open():
    now = bj_dt(2026, 4, 27, 9, 30)

    assert get_active_markets(now) == {Market.A, Market.HK}
    session = get_session_mode(now)
    assert session.name == "ah_intraday"
    assert session.active_markets == ("A", "HK")
    assert session.refresh_interval == 60


def test_a_lunch_hk_still_active_at_1145():
    now = bj_dt(2026, 4, 27, 11, 45)

    assert get_active_markets(now) == {Market.HK}
    session = get_session_mode(now)
    assert session.name == "hk_tail"
    assert session.active_markets == ("HK",)


def test_a_and_hk_lunch_at_1230():
    now = bj_dt(2026, 4, 27, 12, 30)

    assert get_active_markets(now) == set()
    session = get_session_mode(now)
    assert session.name == "market_lunch"
    assert session.next_check_seconds == 30 * 60


def test_only_hk_active_after_a_close():
    now = bj_dt(2026, 4, 27, 15, 30)

    assert get_active_markets(now) == {Market.HK}
    session = get_session_mode(now)
    assert session.active_markets == ("HK",)


def test_us_market_uses_eastern_time_and_dst():
    now = et_dt(2026, 4, 27, 9, 30)

    assert Market.US in get_active_markets(now)
    session = get_session_mode(now)
    assert session.name == "us_intraday"
    assert session.us_live is True


def test_premarket_schedules_open_boundary():
    now = bj_dt(2026, 4, 27, 9, 20)

    assert get_active_markets(now) == set()
    session = get_session_mode(now)
    assert session.name == "pre_market"
    assert session.next_check_seconds == 10 * 60
