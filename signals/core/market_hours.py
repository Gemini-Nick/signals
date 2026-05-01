# -*- coding: utf-8 -*-
"""
市场交易时段检测 — 根据当前时间自动判断哪些市场开盘

交易时段（本地时间）：
  A+H:  09:00-16:00 北京时间（A股 9:30-15:00 + 港股 9:30-16:00，含缓冲）
  美股:  09:30-16:00 US/Eastern（zoneinfo 自动处理夏令/冬令时）

六时段模型（北京时间，工作日）：
  盘前       07:00-09:30   无市场开盘，日线复盘
  A+H盘中    09:30-15:00   A+H实时，分钟线可用
  H股尾盘    15:00-16:00   A股收盘，H股仍在交易
  盘后       16:00-21:30   全部收盘，日线复盘 → 次日预判
  美股盘中   21:30-04:00   美股实时（夏令时，冬令时22:30-05:00）
  深夜       04:00-07:00   无市场开盘

用法：
  from signals.core.market_hours import get_active_markets, Market
  active = get_active_markets()          # 自动检测

  from signals.core.market_hours import get_session_mode
  session = get_session_mode()           # 精细时段
  if session.a_live: ...                 # A股可拉分钟线
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Optional, Set
from zoneinfo import ZoneInfo

logger = logging.getLogger("signals.market_hours")


# ── 时区 ──────────────────────────────────────────────────
TZ_UTC = ZoneInfo("UTC")
TZ_BEIJING = ZoneInfo("Asia/Shanghai")
TZ_US_EAST = ZoneInfo("America/New_York")

# ── 交易时段（本地时间）─ fallback 用 ────────────────────
_A_MORNING_OPEN = time(9, 30)
_A_MORNING_CLOSE = time(11, 30)
_A_AFTERNOON_OPEN = time(13, 0)
_A_AFTERNOON_CLOSE = time(15, 0)

_HK_MORNING_OPEN = time(9, 30)
_HK_MORNING_CLOSE = time(12, 0)
_HK_AFTERNOON_OPEN = time(13, 0)
_HK_AFTERNOON_CLOSE = time(16, 0)

_US_OPEN = time(9, 30)
_US_CLOSE = time(16, 0)


# ── calendar engine lazy import ───────────────────────────
_calendar: Optional[object] = None
_calendar_load_failed = False


def _get_calendar():
    global _calendar, _calendar_load_failed
    if _calendar is None and not _calendar_load_failed:
        try:
            from signals.core.calendar.engine import get_calendar
            _calendar = get_calendar()
        except Exception as e:
            _calendar_load_failed = True
            logger.warning("Calendar engine unavailable, falling back to weekday-only logic: %s", e)
    return _calendar


# ── Market enum (canonical location) ─────────────────────

class Market(str, Enum):
    A = "A"
    HK = "HK"
    US = "US"


def _live_refresh_interval() -> int:
    minutes = int(os.getenv("SIGNALS_LIVE_REFRESH_MINUTES", "1"))
    return max(60, minutes * 60)


# ── Fallback weekday-only detection ──────────────────────

def _is_a_live(now_bj: datetime) -> bool:
    if now_bj.weekday() >= 5:
        return False
    t = now_bj.time()
    return (_A_MORNING_OPEN <= t < _A_MORNING_CLOSE) or (_A_AFTERNOON_OPEN <= t < _A_AFTERNOON_CLOSE)


def _is_hk_live(now_bj: datetime) -> bool:
    if now_bj.weekday() >= 5:
        return False
    t = now_bj.time()
    return (_HK_MORNING_OPEN <= t < _HK_MORNING_CLOSE) or (_HK_AFTERNOON_OPEN <= t < _HK_AFTERNOON_CLOSE)


def _is_us_live(now_et: datetime) -> bool:
    return now_et.weekday() < 5 and _US_OPEN <= now_et.time() < _US_CLOSE


def _seconds_until(target: datetime, now_utc: datetime) -> int:
    return max(0, int((target.astimezone(TZ_UTC) - now_utc).total_seconds()))


def _next_weekday_start(now_local: datetime, tz: ZoneInfo, start_time: time) -> datetime:
    candidate = datetime.combine(now_local.date(), start_time, tzinfo=tz)
    if candidate <= now_local or now_local.weekday() >= 5:
        candidate = candidate + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate = candidate + timedelta(days=1)
    return candidate


# ── Public API ────────────────────────────────────────────

def get_active_markets(now_utc: datetime = None) -> Set[Market]:
    """返回当前正在交易的市场集合（仅 equity，向后兼容）。"""
    if now_utc is None:
        now_utc = datetime.now(TZ_UTC)

    cal = _get_calendar()
    if cal is not None:
        return cal.active_markets(now_utc)

    # fallback
    active: Set[Market] = set()
    now_bj = now_utc.astimezone(TZ_BEIJING)
    if _is_a_live(now_bj):
        active.add(Market.A)
    if _is_hk_live(now_bj):
        active.add(Market.HK)
    now_et = now_utc.astimezone(TZ_US_EAST)
    if _is_us_live(now_et):
        active.add(Market.US)
    return active


def next_live_check_seconds(now_utc: datetime = None) -> int:
    """Seconds until the next regular-session A/H/US open boundary."""
    if now_utc is None:
        now_utc = datetime.now(TZ_UTC)

    cal = _get_calendar()
    if cal is not None:
        return cal.next_transition(now_utc)

    # fallback
    now_bj = now_utc.astimezone(TZ_BEIJING)
    now_et = now_utc.astimezone(TZ_US_EAST)
    candidates: list[datetime] = []
    if now_bj.weekday() < 5:
        for start in (_A_MORNING_OPEN, _HK_MORNING_OPEN, _A_AFTERNOON_OPEN, _HK_AFTERNOON_OPEN):
            candidate = datetime.combine(now_bj.date(), start, tzinfo=TZ_BEIJING)
            if candidate > now_bj:
                candidates.append(candidate)
    candidates.append(_next_weekday_start(now_bj, TZ_BEIJING, _A_MORNING_OPEN))
    candidates.append(_next_weekday_start(now_bj, TZ_BEIJING, _HK_MORNING_OPEN))
    candidates.append(_next_weekday_start(now_et, TZ_US_EAST, _US_OPEN))
    return min(_seconds_until(candidate, now_utc) for candidate in candidates if candidate.astimezone(TZ_UTC) > now_utc)


def describe_sessions(now_utc: datetime = None) -> str:
    """返回当前市场状态的可读描述。"""
    if now_utc is None:
        now_utc = datetime.now(TZ_UTC)
    active = get_active_markets(now_utc)
    if not active:
        return "无市场开盘"
    parts = []
    if Market.A in active:
        parts.append("A股 开盘中 (09:30-11:30/13:00-15:00 北京)")
    if Market.HK in active:
        parts.append("港股 开盘中 (09:30-12:00/13:00-16:00 北京)")
    if Market.US in active:
        now_et = now_utc.astimezone(TZ_US_EAST)
        dst_label = "夏令时" if now_et.dst() else "冬令时"
        bjt_range = "21:30-04:00" if now_et.dst() else "22:30-05:00"
        parts.append(f"美股 开盘中 ({bjt_range} 北京, {dst_label})")
    return " | ".join(parts)


# ── 精细市场状态 ──────────────────────────────────────────

def _cal_instrument_status(exchange: str, instrument_type_str: str, now_utc: datetime) -> dict:
    cal = _get_calendar()
    if cal is not None:
        from signals.core.calendar.models import InstrumentType
        return cal.instrument_status(exchange, InstrumentType(instrument_type_str), now_utc)
    return {"status": "未知", "icon": "⚪", "detail": ""}


def get_market_detail(now_utc: datetime = None) -> dict:
    """返回每个市场的精细状态，供前端展示。"""
    if now_utc is None:
        now_utc = datetime.now(TZ_UTC)

    cal = _get_calendar()
    if cal is not None:
        result = {}
        result["a_stock"] = cal.instrument_status("SSE", "equity", now_utc)
        result["hk_stock"] = cal.instrument_status("HKEX", "equity", now_utc)
        result["us_stock"] = cal.instrument_status("NYSE", "equity", now_utc)
        result["a_index_futures"] = cal.instrument_status("CFFEX", "index_future", now_utc)
        result["a_commodity_futures"] = cal.instrument_status("SHFE", "commodity_future", now_utc)
        result["hk_futures"] = cal.instrument_status("HKEX", "index_future", now_utc)
        result["us_futures"] = cal.instrument_status("CME", "index_future", now_utc)
        result["a_options"] = cal.instrument_status("SSE", "equity_option", now_utc)
        result["us_options"] = cal.instrument_status("CBOE", "equity_option", now_utc)
        return result

    # fallback: old hardcoded detection
    now_bj = now_utc.astimezone(TZ_BEIJING)
    now_et = now_utc.astimezone(TZ_US_EAST)
    bj_t = now_bj.time()
    et_t = now_et.time()
    bj_wd = now_bj.weekday()
    et_wd = now_et.weekday()

    result = {}
    result["a_stock"] = _detect_a_stock(bj_t, bj_wd)
    result["hk_stock"] = _detect_hk_stock(bj_t, bj_wd)
    result["us_stock"] = _detect_us_stock(et_t, et_wd)
    result["a_index_futures"] = _detect_a_index_futures(bj_t, bj_wd)
    result["a_commodity_futures"] = _detect_a_commodity_futures(bj_t, bj_wd)
    result["hk_futures"] = _detect_hk_futures(bj_t, bj_wd)
    result["us_futures"] = _detect_us_futures(et_t, et_wd)
    result["a_options"] = _detect_a_options(bj_t, bj_wd)
    result["us_options"] = _detect_us_options(et_t, et_wd)
    return result


# ── Fallback detection functions (used when calendar unavailable) ──

def _detect_a_stock(bj_t, bj_wd):
    if bj_wd >= 5:
        return {"status": "休市", "icon": "🔴", "detail": "周末"}
    if bj_t < time(9, 15):
        return {"status": "盘前", "icon": "🔵", "detail": "集合竞价 09:15"}
    elif bj_t < time(9, 30):
        return {"status": "集合竞价", "icon": "🟡", "detail": "09:15-09:30"}
    elif bj_t < time(11, 30):
        return {"status": "盘中", "icon": "🟢", "detail": "09:30-11:30"}
    elif bj_t < time(13, 0):
        return {"status": "午休", "icon": "🟡", "detail": "11:30-13:00"}
    elif bj_t < time(15, 0):
        return {"status": "盘中", "icon": "🟢", "detail": "13:00-15:00"}
    else:
        return {"status": "收盘", "icon": "🔴", "detail": "15:00 后"}


def _detect_hk_stock(bj_t, bj_wd):
    if bj_wd >= 5:
        return {"status": "休市", "icon": "🔴", "detail": "周末"}
    if bj_t < time(9, 0):
        return {"status": "盘前", "icon": "🔵", "detail": "开市前"}
    elif bj_t < time(9, 30):
        return {"status": "竞价时段", "icon": "🟡", "detail": "09:00-09:30"}
    elif bj_t < time(12, 0):
        return {"status": "早盘", "icon": "🟢", "detail": "09:30-12:00"}
    elif bj_t < time(13, 0):
        return {"status": "午休", "icon": "🟡", "detail": "12:00-13:00"}
    elif bj_t < time(16, 0):
        return {"status": "午盘", "icon": "🟢", "detail": "13:00-16:00"}
    elif bj_t < time(16, 10):
        return {"status": "收市竞价", "icon": "🟡", "detail": "16:00-16:10"}
    else:
        return {"status": "收盘", "icon": "🔴", "detail": "16:10 后"}


def _detect_us_stock(et_t, et_wd):
    if et_wd >= 5:
        return {"status": "休市", "icon": "🔴", "detail": "周末"}
    if et_t < time(4, 0):
        return {"status": "休市", "icon": "🔴", "detail": ""}
    elif et_t < time(9, 30):
        return {"status": "盘前", "icon": "🔵", "detail": "04:00-09:30 ET"}
    elif et_t < time(16, 0):
        return {"status": "盘中", "icon": "🟢", "detail": "09:30-16:00 ET"}
    elif et_t < time(20, 0):
        return {"status": "盘后", "icon": "🔵", "detail": "16:00-20:00 ET"}
    else:
        return {"status": "收盘", "icon": "🔴", "detail": "20:00 ET 后"}


def _detect_a_index_futures(bj_t, bj_wd):
    if bj_wd >= 5:
        return {"status": "休市", "icon": "🔴", "detail": "周末"}
    if time(9, 30) <= bj_t < time(11, 30):
        return {"status": "交易中", "icon": "🟢", "detail": "IF/IC/IM 09:30-11:30"}
    elif time(11, 30) <= bj_t < time(13, 0):
        return {"status": "午休", "icon": "🟡", "detail": "11:30-13:00"}
    elif time(13, 0) <= bj_t < time(15, 0):
        return {"status": "交易中", "icon": "🟢", "detail": "IF/IC/IM 13:00-15:00"}
    elif time(15, 0) <= bj_t < time(15, 15):
        return {"status": "交易中", "icon": "🟢", "detail": "国债T/TF 至15:15"}
    else:
        return {"status": "收盘", "icon": "🔴", "detail": "无夜盘"}


def _detect_a_commodity_futures(bj_t, bj_wd):
    if bj_wd >= 5:
        if bj_wd == 5 and bj_t < time(2, 30):
            return {"status": "夜盘", "icon": "🟠", "detail": "周五夜盘至02:30"}
        return {"status": "休市", "icon": "🔴", "detail": "周末"}
    if time(21, 0) <= bj_t <= time(23, 59):
        return {"status": "夜盘", "icon": "🟠", "detail": "21:00-02:30"}
    if bj_t < time(2, 30):
        return {"status": "夜盘", "icon": "🟠", "detail": "21:00-02:30"}
    if time(9, 0) <= bj_t < time(10, 15):
        return {"status": "交易中", "icon": "🟢", "detail": "09:00-10:15"}
    elif time(10, 15) <= bj_t < time(10, 30):
        return {"status": "小节休息", "icon": "🟡", "detail": "10:15-10:30"}
    elif time(10, 30) <= bj_t < time(11, 30):
        return {"status": "交易中", "icon": "🟢", "detail": "10:30-11:30"}
    elif time(11, 30) <= bj_t < time(13, 30):
        return {"status": "午休", "icon": "🟡", "detail": "11:30-13:30"}
    elif time(13, 30) <= bj_t < time(15, 0):
        return {"status": "交易中", "icon": "🟢", "detail": "13:30-15:00"}
    else:
        return {"status": "盘间休息", "icon": "🔴", "detail": "15:00-21:00"}


def _detect_a_options(bj_t, bj_wd):
    if bj_wd >= 5:
        return {"status": "休市", "icon": "🔴", "detail": "周末"}
    if time(9, 15) <= bj_t < time(9, 25):
        return {"status": "集合竞价", "icon": "🟡", "detail": "09:15-09:25"}
    elif time(9, 30) <= bj_t < time(11, 30):
        return {"status": "交易中", "icon": "🟢", "detail": "50ETF/300ETF/IO"}
    elif time(11, 30) <= bj_t < time(13, 0):
        return {"status": "午休", "icon": "🟡", "detail": "11:30-13:00"}
    elif time(13, 0) <= bj_t < time(15, 0):
        return {"status": "交易中", "icon": "🟢", "detail": "50ETF/300ETF/IO"}
    else:
        return {"status": "收盘", "icon": "🔴", "detail": ""}


def _detect_hk_futures(bj_t, bj_wd):
    if bj_wd >= 5:
        if bj_wd == 5 and bj_t < time(3, 0):
            return {"status": "夜盘", "icon": "🟠", "detail": "HSI 周五夜盘至03:00"}
        return {"status": "休市", "icon": "🔴", "detail": "周末"}
    if bj_t < time(3, 0):
        return {"status": "夜盘", "icon": "🟠", "detail": "HSI T+1 17:15-03:00"}
    elif bj_t < time(9, 15):
        return {"status": "休市", "icon": "🔴", "detail": ""}
    elif bj_t < time(12, 0):
        return {"status": "日盘", "icon": "🟢", "detail": "HSI 09:15-12:00"}
    elif bj_t < time(13, 0):
        return {"status": "午休", "icon": "🟡", "detail": "12:00-13:00"}
    elif bj_t < time(16, 30):
        return {"status": "日盘", "icon": "🟢", "detail": "HSI 13:00-16:30"}
    elif bj_t < time(17, 15):
        return {"status": "休盘", "icon": "🟡", "detail": "16:30-17:15 过渡"}
    else:
        return {"status": "夜盘", "icon": "🟠", "detail": "HSI T+1 17:15-03:00"}


def _detect_us_futures(et_t, et_wd):
    if et_wd == 5:
        return {"status": "休市", "icon": "🔴", "detail": "周末"}
    if et_wd == 6:
        if et_t >= time(18, 0):
            return {"status": "交易中", "icon": "🟢", "detail": "ES/NQ 周日18:00开盘"}
        return {"status": "休市", "icon": "🔴", "detail": "18:00 ET 开盘"}
    if time(17, 0) <= et_t < time(18, 0):
        return {"status": "维护", "icon": "🟡", "detail": "暂停 17:00-18:00 ET"}
    if et_wd == 4 and et_t >= time(17, 0):
        return {"status": "周末休市", "icon": "🔴", "detail": "周五17:00 ET收盘"}
    return {"status": "交易中", "icon": "🟢", "detail": "ES/NQ 近24h"}


def _detect_us_options(et_t, et_wd):
    if et_wd >= 5:
        return {"status": "休市", "icon": "🔴", "detail": "周末"}
    if time(4, 0) <= et_t < time(9, 30):
        return {"status": "盘前", "icon": "🔵", "detail": "SPY/QQQ 04:00起"}
    elif time(9, 30) <= et_t < time(16, 0):
        return {"status": "交易中", "icon": "🟢", "detail": "NVDA/SPX 09:30-16:00"}
    elif time(16, 0) <= et_t < time(16, 15):
        return {"status": "延长", "icon": "🟢", "detail": "SPX/VIX 至16:15"}
    elif time(16, 15) <= et_t < time(17, 30):
        return {"status": "盘后", "icon": "🔵", "detail": "SPY/QQQ 至17:30"}
    else:
        return {"status": "收盘", "icon": "🔴", "detail": ""}


# ── 指数/标的过滤 ────────────────────────────────────────

def filter_index_codes(active: Set[Market], ak_codes: dict, futu_codes: dict, us_codes: dict) -> tuple:
    """根据活跃市场过滤三组指数代码字典。"""
    return (
        ak_codes if Market.A in active else {},
        futu_codes if Market.HK in active else {},
        us_codes if Market.US in active else {},
    )


def filter_symbols(active: Set[Market], symbols: list) -> list:
    """过滤 Futu 格式代码列表，只保留活跃市场的标的。"""
    from signals.data.fetcher import detect_market
    _map = {"A": Market.A, "HK": Market.HK, "US": Market.US}
    return [s for s in symbols if _map.get(detect_market(s)) in active]


# ── 六时段模型 ────────────────────────────────────────────

@dataclass
class SessionMode:
    """精细时段模式 — 指导 WebEngine 选择数据加载策略。"""
    name: str
    a_live: bool
    hk_live: bool
    us_live: bool
    label: str
    refresh_interval: int
    use_daily_l3: bool
    active_markets: tuple[str, ...] = ()
    next_check_seconds: int = 0
    next_refresh_at: str = ""


def _session_with_timing(name, a_live, hk_live, us_live, label, refresh_interval, use_daily_l3,
                         now_utc, active_markets=(), next_check_seconds=0):
    next_seconds = next_check_seconds or refresh_interval
    next_refresh_at = ""
    if next_seconds > 0:
        next_refresh_at = (now_utc + timedelta(seconds=next_seconds)).astimezone(TZ_BEIJING).isoformat(timespec="seconds")
    return SessionMode(name, a_live, hk_live, us_live, label, refresh_interval,
                       use_daily_l3, active_markets, next_seconds, next_refresh_at)


def get_session_mode(now_utc: datetime = None) -> SessionMode:
    """检测当前精细时段，返回 SessionMode。"""
    if now_utc is None:
        now_utc = datetime.now(TZ_UTC)

    now_bj = now_utc.astimezone(TZ_BEIJING)
    bj_t = now_bj.time()
    bj_wd = now_bj.weekday()
    active = get_active_markets(now_utc)
    active_values = tuple(market.value for market in sorted(active, key=lambda item: item.value))
    live_interval = _live_refresh_interval()

    if Market.US in active:
        return _session_with_timing("us_intraday", False, False, True, "美股盘中",
                                    live_interval, True, now_utc, active_values)

    if Market.A in active or Market.HK in active:
        a_live = Market.A in active
        hk_live = Market.HK in active
        if a_live and hk_live:
            label, name = "A+H盘中", "ah_intraday"
        elif a_live:
            label, name = "A股盘中", "a_intraday"
        else:
            label, name = "H股盘中", "hk_tail"
        return _session_with_timing(name, a_live, hk_live, False, label,
                                    live_interval, not a_live, now_utc, active_values)

    next_check = next_live_check_seconds(now_utc)

    if bj_wd >= 5:
        return _session_with_timing("ah_post", False, False, False, "盘后复盘",
                                    0, True, now_utc, (), next_check)

    _T_0700 = time(7, 0)
    _T_0930 = time(9, 30)
    _T_1300 = time(13, 0)
    _T_1600 = time(16, 0)

    if bj_t < _T_0700:
        return _session_with_timing("overnight", False, False, False, "深夜",
                                    0, True, now_utc, (), next_check)
    elif bj_t < _T_0930:
        return _session_with_timing("pre_market", False, False, False, "盘前",
                                    0, True, now_utc, (), next_check)
    elif bj_t < _T_1300:
        return _session_with_timing("market_lunch", False, False, False, "午休",
                                    0, True, now_utc, (), next_check)
    elif bj_t < _T_1600:
        return _session_with_timing("market_break", False, False, False, "盘间休息",
                                    0, True, now_utc, (), next_check)
    else:
        return _session_with_timing("ah_post", False, False, False, "盘后复盘",
                                    0, True, now_utc, (), next_check)
