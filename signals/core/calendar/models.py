# -*- coding: utf-8 -*-
"""交易日历数据模型"""

from dataclasses import dataclass, field
from datetime import date, time
from enum import Enum
from typing import Optional


class Market(str, Enum):
    A = "A"
    HK = "HK"
    US = "US"


class InstrumentType(str, Enum):
    EQUITY = "equity"
    INDEX_FUTURE = "index_future"
    COMMODITY_FUTURE = "commodity_future"
    BOND_FUTURE = "bond_future"
    EQUITY_OPTION = "equity_option"
    INDEX_OPTION = "index_option"


class HolidayType(str, Enum):
    FULL_CLOSE = "full_close"
    EARLY_CLOSE = "early_close"
    HALF_DAY = "half_day"


@dataclass
class SessionSlot:
    open: time
    close: time
    days: tuple[int, ...]  # 0=Mon .. 6=Sun
    label: str = ""


@dataclass
class InstrumentSchedule:
    exchange: str
    instrument: InstrumentType
    market: Market
    timezone: str
    sessions: list[SessionSlot] = field(default_factory=list)


@dataclass
class HolidayDef:
    date: date
    exchange: str  # e.g. "SSE", "ALL_CN", "ALL_HK", "NYSE", "CME"
    holiday_type: HolidayType
    early_close_time: Optional[time] = None
    applies_to: tuple[InstrumentType, ...] = ()
    description: str = ""
