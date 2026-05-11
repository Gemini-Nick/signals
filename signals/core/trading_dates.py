# -*- coding: utf-8 -*-
"""Market trading-date helpers.

Business data dates are exchange trading dates. Wall-clock fetch time belongs
in snapshot_at/updated_at and must not be used as A-share trade_date on holidays.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from signals.core import market_time


A_SHARE_AUCTION_OPEN = time(9, 15)


def exchange_for_market(market: Any = "A") -> str:
    raw = str(market or "A").strip().upper()
    return {
        "A": "SSE",
        "CN": "SSE",
        "SH": "SSE",
        "SZ": "SZSE",
        "BJ": "SSE",
        "HK": "HKEX",
        "H": "HKEX",
        "US": "NYSE",
        "NYSE": "NYSE",
        "NASDAQ": "NYSE",
        "AMEX": "NYSE",
    }.get(raw, "SSE")


def to_market_naive_now(market: Any = "A", now: datetime | None = None) -> datetime:
    if now is None:
        return market_time.naive_market_now(market)
    if now.tzinfo is not None:
        return now.astimezone(market_time.market_timezone(market)).replace(tzinfo=None)
    return now


def is_trading_day(market: Any = "A", value: date | datetime | str | None = None) -> bool:
    day = _coerce_date(value)
    if day is None:
        day = to_market_naive_now(market).date()
    try:
        from signals.core.calendar.engine import get_calendar

        return bool(get_calendar().is_trading_day(exchange_for_market(market), day))
    except Exception:
        return day.weekday() < 5


def trading_day(
    market: Any = "A",
    *,
    now: datetime | None = None,
    open_time: time = time(9, 30),
) -> date:
    local = to_market_naive_now(market, now)
    day = local.date()
    if is_trading_day(market, day) and local.time() >= open_time:
        return day
    day -= timedelta(days=1)
    while not is_trading_day(market, day):
        day -= timedelta(days=1)
    return day


def trading_day_key(
    market: Any = "A",
    *,
    now: datetime | None = None,
    compact: bool = False,
    open_time: time = time(9, 30),
) -> str:
    fmt = "%Y%m%d" if compact else "%Y-%m-%d"
    return trading_day(market, now=now, open_time=open_time).strftime(fmt)


def a_share_realtime_day_key(
    *,
    now: datetime | None = None,
    compact: bool = False,
) -> str:
    """A-share realtime feeds switch to the current trading day at call auction."""
    return trading_day_key("A", now=now, compact=compact, open_time=A_SHARE_AUCTION_OPEN)


def normalized_trade_minute(
    market: Any = "A",
    *,
    now: datetime | None = None,
    close_time: time = time(15, 0),
    open_time: time = time(9, 30),
) -> datetime:
    local = to_market_naive_now(market, now)
    day = trading_day(market, now=local, open_time=open_time)
    if day == local.date():
        return local.replace(second=0, microsecond=0)
    return datetime.combine(day, close_time)


def normalized_a_share_realtime_minute(*, now: datetime | None = None) -> datetime:
    """Normalize realtime A-share ticks, including the 09:15 call-auction window."""
    return normalized_trade_minute("A", now=now, open_time=A_SHARE_AUCTION_OPEN)


def _coerce_date(value: date | datetime | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if len(text) >= 8 and text[:8].isdigit() and "-" not in text[:10]:
            return datetime.strptime(text[:8], "%Y%m%d").date()
        return datetime.fromisoformat(text[:10]).date()
    except Exception:
        return None
