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
from dataclasses import dataclass
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


# ── 六时段模型 ────────────────────────────────────────────

@dataclass
class SessionMode:
    """精细时段模式 — 指导 WebEngine 选择数据加载策略。"""
    name: str           # "pre_market"|"ah_intraday"|"hk_tail"|"ah_post"|"us_intraday"|"overnight"
    a_live: bool        # A股可拉实时分钟线
    hk_live: bool       # H股可拉实时数据
    us_live: bool       # 美股可拉实时数据
    label: str          # 中文标签: "盘前"|"A+H盘中"|"H股尾盘"|"盘后复盘"|"美股盘中"|"深夜"
    refresh_interval: int   # 自动刷新间隔(秒)，0=不刷新
    use_daily_l3: bool      # True → L3 用 review_screener (日线)


# 六时段定义 (name, a_live, hk_live, us_live, label, refresh, daily_l3)
_SESSIONS = {
    "pre_market":   SessionMode("pre_market",   False, False, False, "盘前",     0,   True),
    "ah_intraday":  SessionMode("ah_intraday",  True,  True,  False, "A+H盘中",  300, False),
    "hk_tail":      SessionMode("hk_tail",      False, True,  False, "H股尾盘",  300, True),
    "ah_post":      SessionMode("ah_post",      False, False, False, "盘后复盘", 0,   True),
    "us_intraday":  SessionMode("us_intraday",  False, False, True,  "美股盘中", 300, True),
    "overnight":    SessionMode("overnight",    False, False, False, "深夜",     0,   True),
}


def get_session_mode(now_utc: datetime = None) -> SessionMode:
    """
    检测当前精细时段，返回 SessionMode。

    判断逻辑（北京时间，工作日）：
      07:00-09:30  盘前
      09:30-15:00  A+H盘中
      15:00-16:00  H股尾盘
      16:00-21:30* 盘后复盘
      21:30*-04:00 美股盘中 (*夏令时21:30，冬令时22:30，由US/Eastern判断)
      04:00-07:00  深夜

    周末/00:00-04:00需检查美股是否仍在交易（周五晚→周六凌晨算美股盘中）。
    """
    if now_utc is None:
        now_utc = datetime.now(TZ_UTC)

    now_bj = now_utc.astimezone(TZ_BEIJING)
    now_et = now_utc.astimezone(TZ_US_EAST)
    bj_t = now_bj.time()
    et_t = now_et.time()
    bj_wd = now_bj.weekday()   # 0=Mon ... 6=Sun
    et_wd = now_et.weekday()

    # ── 美股检测（优先，因为跨日：北京 21:30→次日04:00）──
    us_open = et_wd < 5 and _US_OPEN <= et_t <= _US_CLOSE
    if us_open:
        return _SESSIONS["us_intraday"]

    # ── 周末（美股也不开）→ 盘后复盘 ──
    if bj_wd >= 5:
        return _SESSIONS["ah_post"]

    # ── 工作日，按北京时间分段 ──
    _T_0700 = time(7, 0)
    _T_0930 = time(9, 30)
    _T_1500 = time(15, 0)
    _T_1600 = time(16, 0)

    if bj_t < _T_0700:
        # 00:00-07:00: 美股已收盘（上面已排除美股开盘），深夜或盘后
        return _SESSIONS["overnight"]
    elif bj_t < _T_0930:
        return _SESSIONS["pre_market"]
    elif bj_t < _T_1500:
        return _SESSIONS["ah_intraday"]
    elif bj_t < _T_1600:
        return _SESSIONS["hk_tail"]
    else:
        # 16:00+ 到美股开盘前（上面已排除美股开盘）
        return _SESSIONS["ah_post"]
