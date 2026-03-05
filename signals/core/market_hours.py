# -*- coding: utf-8 -*-
"""
市场交易时段检测 — 根据当前时间自动判断哪些市场开盘

交易时段（本地时间）：
  A+H:  09:00-16:00 北京时间（A股 9:30-15:00 + 港股 9:30-16:00，含缓冲）
  美股:  09:30-16:00 US/Eastern（zoneinfo 自动处理夏令/冬令时）

用法：
  from signals.core.market_hours import get_active_markets, Market
  active = get_active_markets()          # 自动检测
  if Market.A in active: ...
"""
from datetime import datetime, time
from enum import Enum
from typing import Set
from zoneinfo import ZoneInfo


class Market(str, Enum):
    """市场标识，与 detect_market() 返回值一致。"""
    A = "A"
    HK = "HK"
    US = "US"


# ── 时区 ──────────────────────────────────────────────────
TZ_UTC = ZoneInfo("UTC")
TZ_BEIJING = ZoneInfo("Asia/Shanghai")
TZ_US_EAST = ZoneInfo("America/New_York")

# ── 交易时段（本地时间）──────────────────────────────────
_AH_OPEN = time(9, 0)
_AH_CLOSE = time(16, 0)

_US_OPEN = time(9, 30)
_US_CLOSE = time(16, 0)


def get_active_markets(now_utc: datetime = None) -> Set[Market]:
    """
    返回当前正在交易的市场集合。

    :param now_utc: 可选 UTC 时间（测试用），默认取系统当前时间。
    :return: 如 {Market.A, Market.HK} 或 {Market.US} 或空集合。
    """
    if now_utc is None:
        now_utc = datetime.now(TZ_UTC)

    active: Set[Market] = set()

    # A+H: 北京时间 09:00-16:00 工作日
    now_bj = now_utc.astimezone(TZ_BEIJING)
    if now_bj.weekday() < 5 and _AH_OPEN <= now_bj.time() <= _AH_CLOSE:
        active.add(Market.A)
        active.add(Market.HK)

    # US: Eastern 09:30-16:00 工作日（zoneinfo 自动处理 DST）
    now_et = now_utc.astimezone(TZ_US_EAST)
    if now_et.weekday() < 5 and _US_OPEN <= now_et.time() <= _US_CLOSE:
        active.add(Market.US)

    return active


def describe_sessions(now_utc: datetime = None) -> str:
    """返回当前市场状态的可读描述。"""
    if now_utc is None:
        now_utc = datetime.now(TZ_UTC)

    active = get_active_markets(now_utc)
    if not active:
        return "无市场开盘"

    parts = []
    if Market.A in active or Market.HK in active:
        parts.append("A+H 开盘中 (09:00-16:00 北京)")
    if Market.US in active:
        now_et = now_utc.astimezone(TZ_US_EAST)
        dst_label = "夏令时" if now_et.dst() else "冬令时"
        bjt_range = "21:30-04:00" if now_et.dst() else "22:30-05:00"
        parts.append(f"美股 开盘中 ({bjt_range} 北京, {dst_label})")
    return " | ".join(parts)


def filter_index_codes(active: Set[Market],
                       ak_codes: dict,
                       futu_codes: dict,
                       us_codes: dict) -> tuple:
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
