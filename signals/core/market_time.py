# -*- coding: utf-8 -*-
"""Market-aware time helpers for terminal data contracts.

The terminal runs on machines in arbitrary local timezones. Market timestamps
must be interpreted by business market/source, not by the host OS timezone.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from signals.core.market_hours import TZ_BEIJING, TZ_HK, TZ_US_EAST, TZ_UTC

MARKET_TIMEZONES = {
    "A": TZ_BEIJING,
    "CN": TZ_BEIJING,
    "SH": TZ_BEIJING,
    "SZ": TZ_BEIJING,
    "BJ": TZ_BEIJING,
    "HK": TZ_HK,
    "H": TZ_HK,
    "US": TZ_US_EAST,
    "NYSE": TZ_US_EAST,
    "NASDAQ": TZ_US_EAST,
    "AMEX": TZ_US_EAST,
}

US_SOURCE_HINTS = ("alpaca", "yfinance", "polygon", "nasdaq", "nyse")
HK_SOURCE_HINTS = ("futu_hk", "hk", "hsi", "hang_seng")
A_SOURCE_HINTS = ("eastmoney", "ths", "sina", "akshare", "baostock", "tencent")


def infer_market(value: Any = "", *, symbol: Any = "", source: Any = "") -> str:
    """Infer market code from explicit value, symbol prefix, or data source hint."""
    explicit = str(value or "").strip().upper()
    if explicit in MARKET_TIMEZONES:
        return "US" if explicit in {"US", "NYSE", "NASDAQ", "AMEX"} else "A" if explicit in {"CN", "SH", "SZ", "BJ"} else explicit

    raw_symbol = str(symbol or value or "").strip().upper()
    if raw_symbol.startswith(("US.", "NYSE.", "NASDAQ.", "AMEX.")):
        return "US"
    if raw_symbol.startswith("HK.") or (raw_symbol.isdigit() and len(raw_symbol) == 5):
        return "HK"
    if raw_symbol.startswith(("SH.", "SZ.", "BJ.", "SH", "SZ", "BJ")):
        return "A"

    source_text = str(source or "").strip().lower()
    if any(hint in source_text for hint in US_SOURCE_HINTS):
        return "US"
    if any(hint in source_text for hint in HK_SOURCE_HINTS):
        return "HK"
    if any(hint in source_text for hint in A_SOURCE_HINTS):
        return "A"
    return "A"


def market_timezone(value: Any = "", *, symbol: Any = "", source: Any = ""):
    market = infer_market(value, symbol=symbol, source=source)
    return MARKET_TIMEZONES.get(market, TZ_BEIJING)


def market_timezone_name(value: Any = "", *, symbol: Any = "", source: Any = "") -> str:
    tz = market_timezone(value, symbol=symbol, source=source)
    return getattr(tz, "key", str(tz))


def market_now(value: Any = "A", *, symbol: Any = "", source: Any = "") -> datetime:
    return datetime.now(market_timezone(value, symbol=symbol, source=source))


def market_today(value: Any = "A", *, symbol: Any = "", source: Any = ""):
    return market_now(value, symbol=symbol, source=source).date()


def market_date_str(value: Any = "A", *, symbol: Any = "", source: Any = "") -> str:
    return market_today(value, symbol=symbol, source=source).isoformat()


def market_date_key(value: Any = "A", *, symbol: Any = "", source: Any = "") -> str:
    return market_now(value, symbol=symbol, source=source).strftime("%Y%m%d")


def naive_market_now(value: Any = "A", *, symbol: Any = "", source: Any = "") -> datetime:
    return market_now(value, symbol=symbol, source=source).replace(tzinfo=None)


def to_market_naive(value: Any, *, market: Any = "", symbol: Any = "", source: Any = "") -> datetime | None:
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    if pd.isna(ts):
        return None
    tz = market_timezone(market, symbol=symbol, source=source)
    if ts.tzinfo is None:
        return ts.to_pydatetime().replace(tzinfo=None)
    return ts.tz_convert(tz).to_pydatetime().replace(tzinfo=None)


def to_unix_seconds(value: Any, *, market: Any = "", symbol: Any = "", source: Any = "") -> int:
    if value is None:
        return 0
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return 0
    if pd.isna(ts):
        return 0
    if ts.tzinfo is None:
        ts = ts.tz_localize(market_timezone(market, symbol=symbol, source=source))
    else:
        ts = ts.tz_convert(TZ_UTC)
    return int(ts.timestamp())


def timestamp_range_to_dates(
    start: int | None,
    end: int | None,
    *,
    market: Any = "",
    symbol: Any = "",
    source: Any = "",
) -> tuple[str | None, str | None]:
    if not start or not end:
        return None, None
    start_ts = min(start, end)
    end_ts = max(start, end)
    tz = market_timezone(market, symbol=symbol, source=source)
    return (
        datetime.fromtimestamp(start_ts, TZ_UTC).astimezone(tz).strftime("%Y-%m-%d"),
        datetime.fromtimestamp(end_ts, TZ_UTC).astimezone(tz).strftime("%Y-%m-%d"),
    )
