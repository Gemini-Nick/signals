# -*- coding: utf-8 -*-
"""Typed request/response objects for the Signals data gateway."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Optional


DataDomain = Literal[
    "kline",
    "board",
    "concept",
    "constituents",
    "social",
    "index",
    "quote",
    "market_pool",
    "signal",
]
DataMode = Literal["historical", "realtime", "auto"]
ResolvedMode = Literal["historical", "realtime"]
Market = Literal["A", "HK", "US", "all"]
Freshness = Literal["fresh", "stale", "partial", "empty", "pending", "unknown"]


@dataclass(frozen=True)
class DataRequest:
    """Gateway request with explicit intent.

    `mode` describes the caller's intent. `auto` is resolved by purpose, as_of,
    and market session. Historical callers never drift into realtime providers
    just because the current clock is inside trading hours.
    """

    domain: DataDomain
    mode: DataMode = "auto"
    market: Market = "A"
    as_of: Optional[str] = None
    freq: Optional[str] = None
    symbol: Optional[str] = None
    board_name: Optional[str] = None
    concept_name: Optional[str] = None
    allow_stale: bool = True
    purpose: str = ""


@dataclass
class DataResponse:
    """Gateway response with provenance and freshness metadata."""

    data: Any
    mode_used: ResolvedMode
    source: str
    as_of: Optional[str] = None
    freshness: Freshness = "unknown"
    is_stale: bool = False
    is_partial: bool = False
    latency_ms: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_meta(self) -> dict[str, Any]:
        return {
            "mode_used": self.mode_used,
            "source": self.source,
            "as_of": self.as_of,
            "freshness": self.freshness,
            "is_stale": self.is_stale,
            "is_partial": self.is_partial,
            "latency_ms": round(self.latency_ms, 1),
            "errors": self.errors,
        }


HISTORICAL_PURPOSES = {"backtest", "review", "analog"}
REALTIME_PURPOSES = {"intraday", "cluster", "live", "quote"}


def normalize_as_of(as_of: Optional[str], market: str = "A") -> str:
    """Return YYYY-MM-DD, treating empty/today as the latest trading day."""
    if not as_of or str(as_of).lower() == "today":
        try:
            from signals.data.mongo_fallback import get_last_trading_day

            return get_last_trading_day(market)
        except Exception:
            pass
        try:
            from signals.core.market_time import market_date_str

            return market_date_str(market)
        except Exception:
            return date.today().isoformat()
    value = str(as_of)
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    try:
        return pd_date(value).isoformat()
    except Exception:
        return value[:10]


def pd_date(value: str) -> date:
    from pandas import Timestamp

    return Timestamp(value).date()


def resolve_mode(request: DataRequest) -> ResolvedMode:
    """Resolve auto mode from caller intent and market state."""
    if request.mode in ("historical", "realtime"):
        return request.mode

    purpose = (request.purpose or "").lower()
    if purpose in HISTORICAL_PURPOSES:
        return "historical"
    if purpose in REALTIME_PURPOSES:
        return "realtime"

    as_of = normalize_as_of(request.as_of, request.market)
    try:
        from signals.core.market_time import market_date_str

        today = market_date_str(request.market)
    except Exception:
        today = date.today().isoformat()
    if as_of < today:
        return "historical"

    try:
        from signals.data.mongo_fallback import is_any_market_live

        return "realtime" if is_any_market_live() else "historical"
    except Exception:
        try:
            from signals.core.market_time import naive_market_now

            now = naive_market_now(request.market)
        except Exception:
            now = datetime.now()
        return "realtime" if now.weekday() < 5 and 9 <= now.hour < 16 else "historical"
