# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from signals.core.market_time import infer_market, timestamp_range_to_dates, to_market_naive, to_unix_seconds
from signals.core.trading_dates import a_share_realtime_day_key, normalized_a_share_realtime_minute


UTC = ZoneInfo("UTC")


def test_symbol_and_source_infer_business_market():
    assert infer_market(symbol="SH.000300") == "A"
    assert infer_market(symbol="HK.09988") == "HK"
    assert infer_market(symbol="US.QQQ") == "US"
    assert infer_market(source="yfinance") == "US"
    assert infer_market(source="eastmoney_push2delay") == "A"


def test_naive_a_share_time_is_beijing_not_host_timezone():
    ts = to_unix_seconds("2026-04-27 09:30:00", symbol="SH.000300")
    expected = int(datetime(2026, 4, 27, 1, 30, tzinfo=UTC).timestamp())

    assert ts == expected


def test_naive_us_time_is_eastern_with_dst():
    ts = to_unix_seconds("2026-04-27 09:30:00", symbol="US.QQQ")
    expected = int(datetime(2026, 4, 27, 13, 30, tzinfo=UTC).timestamp())

    assert ts == expected


def test_timestamp_range_to_dates_uses_target_market_day():
    start = int(datetime(2026, 4, 27, 13, 30, tzinfo=UTC).timestamp())
    end = int(datetime(2026, 4, 27, 20, 0, tzinfo=UTC).timestamp())

    assert timestamp_range_to_dates(start, end, symbol="US.QQQ") == ("2026-04-27", "2026-04-27")


def test_timezone_aware_values_are_normalized_to_market_naive_time():
    value = datetime(2026, 4, 30, 2, 30, tzinfo=UTC)

    assert to_market_naive(value, symbol="SH.000001") == datetime(2026, 4, 30, 10, 30)


def test_backtest_dt_to_unix_uses_a_share_market_timezone():
    from signals.web2.api import backtest

    value = datetime(2026, 4, 30, 15, 0)

    assert backtest._dt_to_unix(value) == to_unix_seconds(value, market="A")


def test_a_share_realtime_day_switches_at_call_auction():
    assert a_share_realtime_day_key(now=datetime(2026, 4, 27, 9, 14)) == "2026-04-24"
    assert a_share_realtime_day_key(now=datetime(2026, 4, 27, 9, 15)) == "2026-04-27"
    assert normalized_a_share_realtime_minute(now=datetime(2026, 4, 27, 9, 20)) == datetime(2026, 4, 27, 9, 20)
