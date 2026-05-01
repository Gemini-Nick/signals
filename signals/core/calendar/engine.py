# -*- coding: utf-8 -*-
"""交易日历引擎 — 多市场多品种统一查询"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import yaml

from signals.core.calendar.models import (
    HolidayDef,
    HolidayType,
    InstrumentSchedule,
    InstrumentType,
    Market,
    SessionSlot,
)

logger = logging.getLogger("signals.calendar")

TZ_UTC = ZoneInfo("UTC")
TZ_BEIJING = ZoneInfo("Asia/Shanghai")
TZ_US_EAST = ZoneInfo("America/New_York")
TZ_US_CENTRAL = ZoneInfo("America/Chicago")

CN_EXCHANGES = {"SSE", "SZSE", "CFFEX", "SHFE", "DCE", "ZCE", "INE", "GFEX"}
HK_EXCHANGES = {"HKEX"}
US_EQUITY_EXCHANGES = {"NYSE", "NASDAQ"}
US_DERIV_EXCHANGES = {"CME", "CBOE"}

EXCHANGE_MARKET: dict[str, Market] = {}
for _ex in CN_EXCHANGES:
    EXCHANGE_MARKET[_ex] = Market.A
for _ex in HK_EXCHANGES:
    EXCHANGE_MARKET[_ex] = Market.HK
for _ex in US_EQUITY_EXCHANGES | US_DERIV_EXCHANGES:
    EXCHANGE_MARKET[_ex] = Market.US


def _data_dir() -> Path:
    custom = os.getenv("SIGNALS_CALENDAR_DIR")
    if custom:
        return Path(custom)
    return Path(__file__).resolve().parent / "data"


def _parse_time(s: str) -> time:
    parts = s.strip().split(":")
    return time(int(parts[0]), int(parts[1]))


def _load_yaml(name: str) -> dict:
    path = _data_dir() / name
    if not path.exists():
        logger.warning("Calendar data file not found: %s", path)
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _generate_us_holidays(year: int) -> list[dict]:
    """Auto-generate US holidays for a given year using fixed rules.

    Returns list of dicts suitable for HolidayDef construction.
    These are NYSE full-close holidays. Early close days still need YAML.
    """
    from calendar import monthrange

    holidays: list[dict] = []

    def _nth_weekday(y: int, m: int, n: int, wd: int) -> date:
        """n-th occurrence of weekday wd in month m of year y (n=1-based)."""
        first_day = date(y, m, 1)
        days_until = (wd - first_day.weekday()) % 7
        return first_day + timedelta(days=days_until + (n - 1) * 7)

    def _last_weekday(y: int, m: int, wd: int) -> date:
        """Last occurrence of weekday wd in month m of year y."""
        last_day = date(y, m, monthrange(y, m)[1])
        return last_day - timedelta(days=(last_day.weekday() - wd) % 7)

    # New Year's Day (Jan 1; if weekend, NYSE observes on nearest weekday)
    jan1 = date(year, 1, 1)
    holidays.append({"date": jan1, "exchange": "NYSE", "holiday_type": "full_close",
                     "applies_to": ["equity", "equity_option"], "description": "New Year's Day"})

    # MLK Day (3rd Monday, January)
    holidays.append({"date": _nth_weekday(year, 1, 3, 0), "exchange": "NYSE",
                     "holiday_type": "full_close", "applies_to": ["equity", "equity_option"],
                     "description": "Martin Luther King Jr. Day"})

    # Presidents' Day (3rd Monday, February)
    holidays.append({"date": _nth_weekday(year, 2, 3, 0), "exchange": "NYSE",
                     "holiday_type": "full_close", "applies_to": ["equity", "equity_option"],
                     "description": "Presidents' Day"})

    # Memorial Day (last Monday, May)
    holidays.append({"date": _last_weekday(year, 5, 0), "exchange": "NYSE",
                     "holiday_type": "full_close", "applies_to": ["equity", "equity_option"],
                     "description": "Memorial Day"})

    # Juneteenth (Jun 19; if weekend, observed nearest weekday)
    holidays.append({"date": date(year, 6, 19), "exchange": "NYSE",
                     "holiday_type": "full_close", "applies_to": ["equity", "equity_option"],
                     "description": "Juneteenth"})

    # Independence Day (Jul 4; if weekend, observed nearest weekday)
    jul4 = date(year, 7, 4)
    holidays.append({"date": jul4, "exchange": "NYSE",
                     "holiday_type": "full_close", "applies_to": ["equity", "equity_option"],
                     "description": "Independence Day"})

    # Labor Day (1st Monday, September)
    holidays.append({"date": _nth_weekday(year, 9, 1, 0), "exchange": "NYSE",
                     "holiday_type": "full_close", "applies_to": ["equity", "equity_option"],
                     "description": "Labor Day"})

    # Thanksgiving (4th Thursday, November)
    holidays.append({"date": _nth_weekday(year, 11, 4, 3), "exchange": "NYSE",
                     "holiday_type": "full_close", "applies_to": ["equity", "equity_option"],
                     "description": "Thanksgiving Day"})

    # Christmas (Dec 25; if weekend, observed nearest weekday)
    holidays.append({"date": date(year, 12, 25), "exchange": "NYSE",
                     "holiday_type": "full_close", "applies_to": ["equity", "equity_option"],
                     "description": "Christmas Day"})

    # Good Friday: approximate as 2 days before Easter (Western)
    # Use the Butcher-Meeus algorithm
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    L = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * L) // 451
    month_val = (h + L - 7 * m + 114) // 31
    day_val = ((h + L - 7 * m + 114) % 31) + 1
    easter = date(year, month_val, day_val)
    good_friday = easter - timedelta(days=2)
    holidays.append({"date": good_friday, "exchange": "NYSE",
                     "holiday_type": "full_close", "applies_to": ["equity", "equity_option"],
                     "description": "Good Friday"})

    return holidays


class TradingCalendar:
    """多市场多品种交易日历引擎。

    用法:
        cal = get_calendar()
        cal.is_trading("SSE", InstrumentType.EQUITY, dt)
        cal.active_markets(dt)  # → {Market.A}
    """

    def __init__(self):
        self._schedules: dict[str, list[InstrumentSchedule]] = {}  # exchange → schedules
        self._holidays: list[HolidayDef] = []
        self._makeup_workdays: set[date] = set()
        self._holiday_index: dict[str, dict[date, HolidayDef]] = {}  # exchange → {date: def}
        self._loaded = False

    def _ensure_loaded(self):
        if self._loaded:
            return
        self._load_sessions()
        self._load_holidays()
        self._build_holiday_index()
        self._loaded = True
        self._check_expiry()

    def _load_sessions(self):
        data = _load_yaml("sessions.yaml")
        for raw in data.get("schedules", []):
            sessions = []
            for s in raw.get("sessions", []):
                sessions.append(SessionSlot(
                    open=_parse_time(s["open"]),
                    close=_parse_time(s["close"]),
                    days=tuple(s.get("days", [0, 1, 2, 3, 4])),
                    label=s.get("label", ""),
                ))
            schedule = InstrumentSchedule(
                exchange=raw["exchange"],
                instrument=InstrumentType(raw["instrument"]),
                market=Market(raw["market"]),
                timezone=raw["timezone"],
                sessions=sessions,
            )
            self._schedules.setdefault(raw["exchange"], []).append(schedule)

    def _load_holidays(self):
        self._holidays = []
        self._makeup_workdays = set()

        cn_data = _load_yaml("cn_holidays.yaml")
        hk_data = _load_yaml("hk_holidays.yaml")
        us_data = _load_yaml("us_holidays.yaml")

        for raw in cn_data.get("holidays", []):
            insts = raw.get("applies_to") or ["equity", "index_future", "commodity_future",
                                               "bond_future", "equity_option", "index_option"]
            self._holidays.append(HolidayDef(
                date=date.fromisoformat(raw["date"]),
                exchange=raw.get("exchange", "ALL_CN"),
                holiday_type=HolidayType(raw["holiday_type"]),
                early_close_time=_parse_time(raw["early_close_time"]) if raw.get("early_close_time") else None,
                applies_to=tuple(InstrumentType(i) for i in insts),
                description=raw.get("description", ""),
            ))

        for raw in hk_data.get("holidays", []):
            insts = raw.get("applies_to") or ["equity", "index_future", "commodity_future",
                                               "equity_option", "index_option"]
            self._holidays.append(HolidayDef(
                date=date.fromisoformat(raw["date"]),
                exchange=raw.get("exchange", "ALL_HK"),
                holiday_type=HolidayType(raw["holiday_type"]),
                early_close_time=_parse_time(raw["early_close_time"]) if raw.get("early_close_time") else None,
                applies_to=tuple(InstrumentType(i) for i in insts),
                description=raw.get("description", ""),
            ))

        for raw in us_data.get("holidays", []):
            insts = raw.get("applies_to") or ["equity"]
            self._holidays.append(HolidayDef(
                date=date.fromisoformat(raw["date"]),
                exchange=raw.get("exchange", ""),
                holiday_type=HolidayType(raw["holiday_type"]),
                early_close_time=_parse_time(raw["early_close_time"]) if raw.get("early_close_time") else None,
                applies_to=tuple(InstrumentType(i) for i in insts),
                description=raw.get("description", ""),
            ))

        # US holidays: also auto-generate for 2025-2028 to cover gaps
        seen_us_dates: set[tuple[date, str]] = set()
        for h in self._holidays:
            if h.exchange == "NYSE" and h.holiday_type == HolidayType.FULL_CLOSE:
                seen_us_dates.add((h.date, h.exchange))

        for year in range(2025, 2029):
            for raw in _generate_us_holidays(year):
                d = raw["date"]
                if (d, "NYSE") not in seen_us_dates:
                    self._holidays.append(HolidayDef(
                        date=d,
                        exchange=raw["exchange"],
                        holiday_type=HolidayType(raw["holiday_type"]),
                        applies_to=tuple(InstrumentType(i) for i in raw.get("applies_to", ["equity"])),
                        description=raw.get("description", ""),
                    ))
                    seen_us_dates.add((d, "NYSE"))

        # makeup workdays from CN
        for raw in cn_data.get("makeup_workdays", []):
            self._makeup_workdays.add(date.fromisoformat(raw))

        # HK makeup
        for raw in hk_data.get("makeup_workdays", []):
            self._makeup_workdays.add(date.fromisoformat(raw))

    def _build_holiday_index(self):
        self._holiday_index = {}
        for h in self._holidays:
            if h.exchange.startswith("ALL_"):
                if h.exchange == "ALL_CN":
                    exchanges = CN_EXCHANGES
                elif h.exchange == "ALL_HK":
                    exchanges = HK_EXCHANGES
                else:
                    exchanges = set()
            elif h.exchange == "NYSE":
                exchanges = US_EQUITY_EXCHANGES  # NYSE holidays also apply to NASDAQ
            else:
                exchanges = {h.exchange}
            for ex in exchanges:
                self._holiday_index.setdefault(ex, {})[h.date] = h

    def _check_expiry(self):
        if not self._holidays:
            return
        info = self.validate()
        if info["warnings"]:
            for w in info["warnings"]:
                logger.warning("Calendar: %s", w)

    def validate(self) -> dict:
        """验证日历数据有效性。返回 coverage 信息和警告列表。

        定时任务可调用此方法检测日历是否需要更新。
        """
        self._ensure_loaded()
        if not self._holidays:
            return {"coverage_end": None, "days_remaining": 0, "warnings": ["No holiday data loaded"]}

        today = date.today()
        max_date = max(h.date for h in self._holidays)
        days_left = (max_date - today).days
        sessions_loaded = sum(len(v) for v in self._schedules.values())
        exchange_count = len(self._schedules)
        holiday_count = len(self._holidays)

        warnings = []
        if days_left < 90:
            warnings.append(
                f"Holiday data ends {max_date} ({days_left}d from now). "
                f"Update {_data_dir()}/*.yaml"
            )
        if sessions_loaded == 0:
            warnings.append("No trading sessions loaded")
        if holiday_count == 0:
            warnings.append("No holiday data loaded")

        return {
            "coverage_end": max_date.isoformat(),
            "days_remaining": days_left,
            "holiday_count": holiday_count,
            "sessions_loaded": sessions_loaded,
            "exchange_count": exchange_count,
            "data_dir": str(_data_dir()),
            "warnings": warnings,
        }

    def check_expiry(self):
        self._ensure_loaded()
        self._check_expiry()

    # ── 公开 API ──

    def is_trading_day(self, exchange: str, d: date) -> bool:
        """判断某天是否是该交易所的交易日。"""
        self._ensure_loaded()
        if d in self._makeup_workdays:
            return True
        holiday = self._holiday_index.get(exchange, {}).get(d)
        if holiday and holiday.holiday_type == HolidayType.FULL_CLOSE:
            return False
        if d.weekday() >= 5 and d not in self._makeup_workdays:
            return False
        return True

    def is_trading(self, exchange: str, instrument: InstrumentType, dt: datetime) -> bool:
        """判断某时刻某交易所某品种是否在交易。"""
        self._ensure_loaded()
        tz = self._tz_for(exchange)
        local = dt.astimezone(tz)
        d = local.date()
        t = local.time()

        schedules = self._schedules.get(exchange, [])
        matched = self._find_matching_slot(schedules, instrument, t, d)

        if not matched:
            return False

        slot_d, _ = matched

        holiday = self._holiday_index.get(exchange, {}).get(slot_d)
        if holiday:
            if holiday.holiday_type == HolidayType.FULL_CLOSE:
                if not holiday.applies_to or instrument in holiday.applies_to:
                    return False
            elif holiday.holiday_type == HolidayType.HALF_DAY:
                if t >= time(12, 0) and slot_d == d:
                    return False
                if slot_d != d:
                    pass
            elif holiday.holiday_type == HolidayType.EARLY_CLOSE:
                if holiday.early_close_time and t >= holiday.early_close_time and slot_d == d:
                    if not holiday.applies_to or instrument in holiday.applies_to:
                        return False

        return True

    def _find_matching_slot(self, schedules, instrument, t, d):
        """Find (slot_date, is_makeup) that covers time t on date d, or None."""
        is_makeup = d in self._makeup_workdays
        for sch in schedules:
            if sch.instrument != instrument:
                continue
            for slot in sch.sessions:
                if d.weekday() in slot.days or is_makeup:
                    if _in_slot_static(t, slot):
                        return (d, is_makeup)

        prev_d = d - timedelta(days=1)
        prev_makeup = prev_d in self._makeup_workdays
        for sch in schedules:
            if sch.instrument != instrument:
                continue
            for slot in sch.sessions:
                if slot.close < slot.open:
                    if prev_d.weekday() in slot.days or prev_makeup:
                        if t < slot.close:
                            return (prev_d, prev_makeup)

        return None

    def active_markets(self, dt: datetime) -> set[Market]:
        """返回当前正在交易的市场集合（仅 equity，向后兼容）。"""
        self._ensure_loaded()
        active: set[Market] = set()
        for ex, market in EXCHANGE_MARKET.items():
            if self.is_trading(ex, InstrumentType.EQUITY, dt):
                active.add(market)
        return active

    def active_instruments(self, dt: datetime, market: Optional[Market] = None) -> list[InstrumentSchedule]:
        """返回当前活跃交易的品种-交易所列表。"""
        self._ensure_loaded()
        result: list[InstrumentSchedule] = []
        for ex, schedules in self._schedules.items():
            if market and EXCHANGE_MARKET.get(ex) != market:
                continue
            for sch in schedules:
                if self.is_trading(ex, sch.instrument, dt):
                    result.append(sch)
        return result

    def instrument_status(self, exchange: str, instrument: InstrumentType, dt: datetime) -> dict:
        """返回某品种某时刻的状态描述（用于 get_market_detail）。"""
        self._ensure_loaded()
        tz = self._tz_for(exchange)
        local = dt.astimezone(tz)
        d = local.date()
        t = local.time()

        trading = self.is_trading(exchange, instrument, dt)

        if trading:
            schedules = self._schedules.get(exchange, [])
            matched = self._find_matching_slot(schedules, instrument, t, d)
            cross_midnight = matched and matched[0] != d
            icon = "🟠" if cross_midnight else "🟢"
            label = self._active_label(exchange, instrument, t, d)
            status = "夜盘" if cross_midnight else "交易中"
            return {"status": status, "icon": icon, "detail": label}

        # Not trading — determine why
        holiday = self._holiday_index.get(exchange, {}).get(d)
        schedules = self._schedules.get(exchange, [])

        # Check if holiday blocks
        if holiday and holiday.holiday_type == HolidayType.FULL_CLOSE:
            if not holiday.applies_to or instrument in holiday.applies_to:
                return {"status": "休市", "icon": "🔴", "detail": holiday.description}

        if holiday and holiday.holiday_type == HolidayType.HALF_DAY:
            if t >= time(12, 0):
                return {"status": "半日市已收盘", "icon": "🔴", "detail": f"{holiday.description} 12:00收盘"}
            # Before 12:00 on half-day but is_trading already said not trading
            if holiday.applies_to and instrument not in holiday.applies_to:
                pass
            else:
                return {"status": "半日市", "icon": "🟡", "detail": f"{holiday.description}"}

        if holiday and holiday.holiday_type == HolidayType.EARLY_CLOSE:
            if holiday.early_close_time and t >= holiday.early_close_time:
                if not holiday.applies_to or instrument in holiday.applies_to:
                    return {"status": "提前收盘", "icon": "🔴", "detail": f"{holiday.description} {holiday.early_close_time}收盘"}

        # Check tonight's/yesterday's cross-midnight session
        matched = self._find_matching_slot(schedules, instrument, t, d)
        if matched:
            slot_d, _ = matched
            if slot_d != d:
                # Cross-midnight from yesterday — holiday on yesterday?
                prev_holiday = self._holiday_index.get(exchange, {}).get(slot_d)
                if prev_holiday and prev_holiday.holiday_type == HolidayType.FULL_CLOSE:
                    if not prev_holiday.applies_to or instrument in prev_holiday.applies_to:
                        return {"status": "休市", "icon": "🔴", "detail": prev_holiday.description}
                return {"status": "夜盘", "icon": "🟠", "detail": "跨日夜盘"}

        # Find next session
        next_slot = self._next_slot_for(exchange, instrument, t, d)
        if next_slot:
            return {"status": "休盘", "icon": "🟡", "detail": f"下一时段 {next_slot.open}"}

        if d.weekday() >= 5 and d not in self._makeup_workdays:
            return {"status": "休市", "icon": "🔴", "detail": "周末"}

        return {"status": "收盘", "icon": "🔴", "detail": ""}

    def _active_label(self, exchange: str, instrument: InstrumentType, t: time, d: date) -> str:
        schedules = self._schedules.get(exchange, [])
        for sch in schedules:
            if sch.instrument != instrument:
                continue
            for slot in sch.sessions:
                if d.weekday() in slot.days and _in_slot_static(t, slot):
                    return slot.label or f"{slot.open}-{slot.close}"
        # Check cross-midnight
        prev_d = d - timedelta(days=1)
        for sch in schedules:
            if sch.instrument != instrument:
                continue
            for slot in sch.sessions:
                if slot.close < slot.open and prev_d.weekday() in slot.days and t < slot.close:
                    return slot.label or f"{slot.open}-{slot.close}"
        return ""

    def next_transition(self, dt: datetime) -> int:
        """Seconds until next open/close event across all instruments."""
        self._ensure_loaded()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_UTC)
        candidates: list[datetime] = []
        for ex in self._schedules:
            tz = self._tz_for(ex)
            local = dt.astimezone(tz)
            for days_ahead in range(14):
                check_d = local.date() + timedelta(days=days_ahead)
                if not self.is_trading_day(ex, check_d):
                    continue
                for sch in self._schedules[ex]:
                    for slot in sch.sessions:
                        is_makeup = check_d in self._makeup_workdays
                        if check_d.weekday() not in slot.days and not is_makeup:
                            continue
                        open_candidate = datetime.combine(check_d, slot.open, tzinfo=tz).astimezone(TZ_UTC)
                        close_date = check_d + timedelta(days=1) if slot.close < slot.open else check_d
                        close_candidate = datetime.combine(close_date, slot.close, tzinfo=tz).astimezone(TZ_UTC)
                        for candidate in (open_candidate, close_candidate):
                            if candidate > dt:
                                candidates.append(candidate)
        if not candidates:
            return 3600
        return max(0, int((min(candidates) - dt).total_seconds()))

    # ── helpers ──

    def _tz_for(self, exchange: str) -> ZoneInfo:
        if exchange in US_EQUITY_EXCHANGES:
            return TZ_US_EAST
        if exchange == "CME":
            return TZ_US_CENTRAL
        if exchange == "CBOE":
            return TZ_US_EAST
        return TZ_BEIJING

    def _next_slot_for(self, exchange: str, instrument: InstrumentType, t: time, d: date) -> Optional[SessionSlot]:
        schedules = self._schedules.get(exchange, [])
        is_makeup = d in self._makeup_workdays
        for sch in schedules:
            if sch.instrument != instrument:
                continue
            for slot in sch.sessions:
                if slot.open > t and (d.weekday() in slot.days or is_makeup):
                    return slot
        next_d = d + timedelta(days=1)
        next_makeup = next_d in self._makeup_workdays
        for sch in schedules:
            if sch.instrument != instrument:
                continue
            for slot in sch.sessions:
                if next_d.weekday() in slot.days or next_makeup:
                    return slot
        return None


def _in_slot_static(t: time, slot: SessionSlot) -> bool:
    if slot.close < slot.open:
        return t >= slot.open or t < slot.close
    return slot.open <= t < slot.close


def schedules_by_instrument(schedules: list[InstrumentSchedule], instrument: InstrumentType) -> list[InstrumentSchedule]:
    return [sch for sch in schedules if sch.instrument == instrument]


_calendar: Optional[TradingCalendar] = None


def get_calendar() -> TradingCalendar:
    global _calendar
    if _calendar is None:
        _calendar = TradingCalendar()
    return _calendar
