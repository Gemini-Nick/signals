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


# ── 精细市场状态（含盘前/午休/盘后/期货）──────────────

def get_market_detail(now_utc: datetime = None) -> dict:
    """
    返回每个市场的精细状态，供前端展示。

    返回:
        {
            "a_stock":    {"status": "午休", "icon": "🟡", "detail": "11:30-13:00"},
            "hk_stock":   {"status": "盘中", "icon": "🟢", "detail": "09:30-16:00"},
            "us_stock":   {"status": "盘前", "icon": "🔵", "detail": "16:00-21:30 ET"},
            "a_futures":  {"status": "交易中", "icon": "🟢", "detail": "IF/IC/IM 09:30-15:00"},
            "hk_futures": {"status": "休市", "icon": "🔴", "detail": ""},
            "us_futures": {"status": "交易中", "icon": "🟢", "detail": "ES/NQ 近24h"},
        }
    """
    if now_utc is None:
        now_utc = datetime.now(TZ_UTC)

    now_bj = now_utc.astimezone(TZ_BEIJING)
    now_et = now_utc.astimezone(TZ_US_EAST)
    now_hk = now_bj  # 港股同北京时区
    bj_t = now_bj.time()
    et_t = now_et.time()
    bj_wd = now_bj.weekday()
    et_wd = now_et.weekday()

    result = {}

    # ── A 股 ──
    result["a_stock"] = _detect_a_stock(bj_t, bj_wd)

    # ── 港股 ──
    result["hk_stock"] = _detect_hk_stock(bj_t, bj_wd)

    # ── 美股 ──
    result["us_stock"] = _detect_us_stock(et_t, et_wd)

    # ── 期货 ──
    result["a_index_futures"] = _detect_a_index_futures(bj_t, bj_wd)
    result["a_commodity_futures"] = _detect_a_commodity_futures(bj_t, bj_wd)
    result["hk_futures"] = _detect_hk_futures(bj_t, bj_wd)
    result["us_futures"] = _detect_us_futures(et_t, et_wd)

    # ── 期权 ──
    result["a_options"] = _detect_a_options(bj_t, bj_wd)
    result["us_options"] = _detect_us_options(et_t, et_wd)

    return result


def _detect_a_stock(bj_t, bj_wd):
    """A 股精细状态"""
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
    """港股精细状态（与北京时间相同）"""
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
    """美股精细状态（Eastern Time）"""
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
    """
    A股股指期货（中金所）— IF/IC/IM/IH
    交易时段: 09:30-11:30, 13:00-15:00（无夜盘）
    """
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
    """
    A股商品期货 — 上期所/大商所/郑商所/广期所
    日盘: 09:00-10:15, 10:30-11:30, 13:30-15:00
    夜盘: 21:00-次日02:30（品种不同收盘时间不同）
      - 铜/铝/锌等: 21:00-01:00
      - 金/银: 21:00-02:30
      - 螺纹/热卷等: 21:00-23:00
    """
    if bj_wd >= 5:
        # 周六凌晨可能仍有周五夜盘
        if bj_wd == 5 and bj_t < time(2, 30):
            return {"status": "夜盘", "icon": "🟠", "detail": "周五夜盘至02:30"}
        return {"status": "休市", "icon": "🔴", "detail": "周末"}

    # 夜盘（跨日）
    if time(21, 0) <= bj_t <= time(23, 59):
        return {"status": "夜盘", "icon": "🟠", "detail": "21:00-02:30"}
    if bj_t < time(2, 30):
        return {"status": "夜盘", "icon": "🟠", "detail": "21:00-02:30"}

    # 日盘
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
    """
    A股期权 — 50ETF/300ETF期权 + 沪深300指数期权(IO)
    集合竞价: 09:15-09:25
    交易: 09:30-11:30, 13:00-15:00（同A股，无夜盘）
    """
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
    """
    港股期货（HKEX）— 恒指(HSI)/科指(HHIT)
    日盘: 09:15-12:00, 13:00-16:30 (北京时间)
    夜盘(T+1): 17:15-03:00 (次日北京时间)
    """
    if bj_wd >= 5:
        # 周六凌晨可能仍有周五夜盘
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
    """
    美股期货（CME Globex）— ES/NQ/YM/RTY
    交易: 周日18:00 - 周五17:00 ET（几乎24h，每日暂停 17:00-18:00 ET）
    """
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
    """
    美股期权（CBOE/各交易所）— 个股期权(NVDA等) + 指数期权(SPX/VIX)
    常规: 09:30-16:00 ET
    SPX/VIX 指数期权: 可延长至 16:15 ET
    部分ETF期权(SPY/QQQ): 有盘前盘后（04:00-09:30, 16:00-17:30 ET）
    """
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
