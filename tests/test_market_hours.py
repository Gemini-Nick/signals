# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from signals.core.market_hours import (
    Market,
    get_active_markets,
    get_market_detail,
    get_session_mode,
    next_live_check_seconds,
)

BJ = ZoneInfo("Asia/Shanghai")
ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")


def bj_dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=BJ).astimezone(UTC)


def et_dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET).astimezone(UTC)


# ═══ existing basic tests (unchanged expectations) ═══

def test_a_and_hk_are_active_at_beijing_open():
    now = bj_dt(2026, 4, 27, 9, 30)
    assert get_active_markets(now) == {Market.A, Market.HK}
    session = get_session_mode(now)
    assert session.name == "ah_intraday"
    assert session.active_markets == ("A", "HK")


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


# ═══ holiday tests ═══

def test_spring_festival_all_cn_closed():
    """2026-02-17 Tuesday = Spring Festival (初一). CN fully closed."""
    now = bj_dt(2026, 2, 17, 10, 0)
    assert get_active_markets(now) == set()

    detail = get_market_detail(now)
    assert detail["a_stock"]["status"] == "休市"
    assert detail["a_index_futures"]["status"] == "休市"
    assert detail["a_commodity_futures"]["status"] == "休市"
    assert detail["a_options"]["status"] == "休市"


def test_cn_makeup_workday_saturday():
    """2026-02-14 Saturday = makeup workday for Spring Festival. CN opens."""
    now = bj_dt(2026, 2, 14, 10, 0)
    assert Market.A in get_active_markets(now)
    # HK is also open (not a HK holiday)
    assert Market.HK in get_active_markets(now)

    detail = get_market_detail(now)
    assert detail["a_stock"]["status"] in ("盘中", "交易中")
    assert detail["a_index_futures"]["status"] in ("盘中", "交易中")


def test_regular_saturday_cn_closed():
    """Ordinary Saturday (not makeup). CN closed."""
    now = bj_dt(2026, 5, 2, 10, 0)  # Saturday
    assert get_active_markets(now) == set()


def test_cn_national_day_holiday():
    """2026-10-01 Thursday = National Day. CN fully closed."""
    now = bj_dt(2026, 10, 1, 10, 0)
    assert get_active_markets(now) == set()

    detail = get_market_detail(now)
    assert detail["a_stock"]["status"] == "休市"
    assert detail["hk_stock"]["status"] == "休市"  # HK also closed for National Day


def test_cn_makeup_workday_sunday_sep():
    """2026-09-20 Sunday = makeup workday for National Day. CN opens."""
    now = bj_dt(2026, 9, 20, 10, 0)
    # HK should not be affected by CN makeup (HK doesn't have makeup days)
    # But HK is open on regular Sunday... no, HK is closed Sundays too
    # CN makeup only affects CN exchanges
    assert Market.A in get_active_markets(now)


# ═══ US holiday tests ═══

def test_mlk_day_equity_closed_futures_open():
    """2026-01-19 MLK Day Monday. NYSE closed, CME futures open."""
    now = et_dt(2026, 1, 19, 10, 0)
    detail = get_market_detail(now)
    assert detail["us_stock"]["status"] == "休市"
    assert detail["us_futures"]["status"] == "交易中"


def test_christmas_day_all_closed():
    """2026-12-25 Christmas Friday. NYSE + CME both closed."""
    now = et_dt(2026, 12, 25, 10, 0)
    detail = get_market_detail(now)
    assert detail["us_stock"]["status"] == "休市"
    assert detail["us_futures"]["status"] == "休市"


def test_christmas_eve_early_close():
    """2026-12-24 Christmas Eve Thursday. NYSE early close 1:00 PM ET."""
    # Before early close time: trading
    now_trading = et_dt(2026, 12, 24, 10, 0)
    detail = get_market_detail(now_trading)
    assert "提前收盘" in detail["us_stock"]["status"] or "交易中" in detail["us_stock"]["status"]

    # After early close time: NOT trading
    now_closed = et_dt(2026, 12, 24, 14, 0)
    detail2 = get_market_detail(now_closed)
    assert detail2["us_stock"]["status"] in ("休市", "收盘", "提前收盘")


def test_independence_day_cme_early_close():
    """2026-07-03 Independence Day (observed). CME early close 12:15 PM CT."""
    # CME is US_Central timezone
    now_trading = datetime(2026, 7, 3, 10, 0, tzinfo=CT).astimezone(UTC)
    detail = get_market_detail(now_trading)
    assert detail["us_futures"]["status"] in ("交易中", "提前收盘")

    # After CME early close
    now_closed = datetime(2026, 7, 3, 13, 0, tzinfo=CT).astimezone(UTC)
    detail2 = get_market_detail(now_closed)
    assert detail2["us_futures"]["status"] not in ("交易中",)


def test_good_friday_nyse_closed():
    """2026-04-03 Good Friday. NYSE closed."""
    now = et_dt(2026, 4, 3, 10, 0)
    assert Market.US not in get_active_markets(now)
    detail = get_market_detail(now)
    assert detail["us_stock"]["status"] == "休市"


# ═══ HK holiday tests ═══

def test_hk_lunar_new_year_eve_half_day():
    """2026-02-16 Lunar NY Eve Monday. HK half-day, closes at 12:00."""
    # Morning: trading
    now_am = bj_dt(2026, 2, 16, 10, 0)
    detail = get_market_detail(now_am)
    assert detail["hk_stock"]["status"] in ("半日市", "早盘", "交易中")

    # Afternoon: closed
    now_pm = bj_dt(2026, 2, 16, 14, 0)
    detail2 = get_market_detail(now_pm)
    assert detail2["hk_stock"]["status"] in ("半日市已收盘", "休市", "收盘")


def test_hk_lunar_new_year_hk_closed():
    """2026-02-17 Lunar NY day 1. HK + CN both closed."""
    now = bj_dt(2026, 2, 17, 10, 0)
    assert get_active_markets(now) == set()


def test_hk_christmas_eve_half_day():
    """2026-12-24 Christmas Eve Thu. HK half-day."""
    now_pm = bj_dt(2026, 12, 24, 14, 0)
    detail = get_market_detail(now_pm)
    assert detail["hk_stock"]["status"] in ("半日市已收盘", "休市", "收盘")


def test_hk_new_year_eve_half_day():
    """2026-12-31 New Year's Eve Thu. HK half-day."""
    now_pm = bj_dt(2026, 12, 31, 14, 0)
    detail = get_market_detail(now_pm)
    assert detail["hk_stock"]["status"] in ("半日市已收盘", "休市", "收盘")


# ═══ futures session tests ═══

def test_cn_commodity_futures_night_session_midnight():
    """Tuesday 01:30 BJ = SHFE night session still active (Monday night -> Tue morning)."""
    now = bj_dt(2026, 4, 28, 1, 30)  # Tuesday 1:30 AM
    detail = get_market_detail(now)
    assert detail["a_commodity_futures"]["status"] in ("夜盘", "交易中")


def test_cn_commodity_futures_night_session_saturday_morning():
    """Saturday 01:30 BJ = Friday night session still active (use a non-holiday Friday)."""
    # 2026-04-25 is Saturday, previous day 04-24 is a regular Friday (not a holiday)
    now = bj_dt(2026, 4, 25, 1, 30)
    detail = get_market_detail(now)
    assert detail["a_commodity_futures"]["icon"] == "🟠"


def test_cn_index_futures_no_night_session():
    """CFFEX index futures have no night session. Tuesday 21:30 should be closed."""
    now = bj_dt(2026, 4, 28, 21, 30)  # Tuesday 21:30
    detail = get_market_detail(now)
    assert detail["a_index_futures"]["status"] != "交易中"


def test_hk_futures_night_session():
    """HK index futures T+1 session: 17:15-03:00. 21:00 should be trading."""
    now = bj_dt(2026, 4, 28, 21, 0)  # Tuesday 21:00
    detail = get_market_detail(now)
    assert detail["hk_futures"]["status"] in ("夜盘", "交易中", "日盘")


def test_hk_futures_night_session_midnight():
    """HK futures T+1 session extends to 03:00. 02:00 should be trading."""
    now = bj_dt(2026, 4, 29, 2, 0)  # Wednesday 2:00 AM
    detail = get_market_detail(now)
    assert detail["hk_futures"]["status"] in ("夜盘", "交易中")


def test_us_futures_cme_sunday_open():
    """CME Globex opens Sunday 17:00 CT (Monday 07:00 BJ in summer)."""
    # Sunday 19:00 ET = Sunday 18:00 CT = after CME Sunday open
    now = datetime(2026, 5, 3, 19, 0, tzinfo=ET).astimezone(UTC)  # Sunday
    detail = get_market_detail(now)
    assert detail["us_futures"]["status"] == "交易中"


def test_us_futures_cme_saturday_closed():
    """CME Globex closed Saturday."""
    now = datetime(2026, 5, 2, 12, 0, tzinfo=CT).astimezone(UTC)  # Saturday noon CT
    detail = get_market_detail(now)
    assert detail["us_futures"]["status"] in ("休市", "周末休市", "休盘")


# ═══ get_market_detail full coverage ═══

def test_market_detail_all_keys_present():
    """get_market_detail returns all expected keys."""
    now = bj_dt(2026, 4, 28, 10, 0)  # Tuesday morning
    detail = get_market_detail(now)
    expected_keys = {
        "a_stock", "hk_stock", "us_stock",
        "a_index_futures", "a_commodity_futures",
        "hk_futures", "us_futures",
        "a_options", "us_options",
    }
    assert set(detail.keys()) == expected_keys


def test_next_live_check_seconds_weekday():
    now = bj_dt(2026, 4, 27, 9, 20)  # Monday pre-market
    secs = next_live_check_seconds(now)
    assert 0 < secs <= 600


def test_next_live_check_seconds_weekend():
    now = bj_dt(2026, 5, 2, 10, 0)  # Saturday
    secs = next_live_check_seconds(now)
    assert secs > 0

# ═══ DST tests ═══

def test_us_dst_spring_forward_2026():
    """After DST spring-forward (Mar 8, 2026), ET is UTC-4. Verify US active at 9:30 EDT."""
    now = datetime(2026, 3, 9, 13, 30, tzinfo=UTC)  # 9:30 AM EDT = 13:30 UTC
    assert Market.US in get_active_markets(now)
