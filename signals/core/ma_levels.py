# -*- coding: utf-8 -*-
"""
均线关键位系统 (MA Levels)

从日线 RawBar 数据计算多周期均线，输出关键支撑/阻力位。
只做日线级别以上（日/周/月），小级别继续纯缠论。

均线用途：给出"稳定的市场共识价位锚点"，与缠论互补。
- 缠论 → 方向（二买=结构向上）
- 均线 → 位置（5周线=关键支撑）
- 方向 + 位置 = 可操作
"""
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd
from czsc import RawBar


# ─────────────────────────────────────────────────────────
# 数据类
# ─────────────────────────────────────────────────────────

@dataclass
class MALevel:
    name: str           # "5周线", "10月线", "20日线"
    value: float        # 均线值
    distance_pct: float # 当前价距该均线 %（正=上方，负=下方）
    position: str       # "上方" / "下方" / "贴合"

@dataclass
class MAContext:
    symbol: str
    latest_price: float
    levels: List[MALevel] = field(default_factory=list)
    support_levels: List[MALevel] = field(default_factory=list)     # 价下方，按距离升序
    resistance_levels: List[MALevel] = field(default_factory=list)  # 价上方，按距离升序
    key_levels: List[MALevel] = field(default_factory=list)         # 提炼最关键2-3个
    trend_summary: str = "未知"  # "多头排列" / "空头排列" / "交织"


# ─────────────────────────────────────────────────────────
# 均线计算
# ─────────────────────────────────────────────────────────

def _bars_to_df(bars: List[RawBar]) -> pd.DataFrame:
    """RawBar 列表 → DataFrame (dt 为 index)"""
    df = pd.DataFrame([{
        "dt": b.dt, "open": b.open, "high": b.high,
        "low": b.low, "close": b.close, "vol": b.vol,
    } for b in bars])
    df["dt"] = pd.to_datetime(df["dt"])
    df = df.set_index("dt").sort_index()
    return df


def _compute_ma(closes: pd.Series, period: int) -> Optional[float]:
    """计算简单移动平均线，数据不足返回 None"""
    if len(closes) < period:
        return None
    return round(closes.iloc[-period:].mean(), 2)


def _make_level(name: str, ma_val: Optional[float],
                price: float) -> Optional[MALevel]:
    """构建 MALevel，ma_val 为 None 则跳过"""
    if ma_val is None:
        return None
    dist = (price - ma_val) / ma_val * 100 if ma_val != 0 else 0.0
    if abs(dist) < 1.0:
        position = "贴合"
    elif dist > 0:
        position = "上方"
    else:
        position = "下方"
    return MALevel(
        name=name, value=ma_val,
        distance_pct=round(dist, 2), position=position,
    )


def compute_ma_levels(bars: List[RawBar], symbol: str) -> Optional[MAContext]:
    """
    从日线 RawBar 计算多周期均线关键位。

    计算均线：
    - 日线: MA5, MA10, MA20, MA60
    - 周线: MA5(5周线), MA10, MA20（日线 resample 周线后计算）
    - 月线: MA10(10月线)（日线 resample 月线后计算）

    :param bars: 日线 RawBar 列表（建议 >= 200 根）
    :param symbol: 标的代码
    :return: MAContext，数据不足返回 None
    """
    if not bars or len(bars) < 10:
        return None

    df = _bars_to_df(bars)
    price = df["close"].iloc[-1]

    levels: List[MALevel] = []

    # ── 日线均线 ──
    for period, label in [(5, "5日线"), (10, "10日线"),
                          (20, "20日线"), (60, "60日线")]:
        lv = _make_level(label, _compute_ma(df["close"], period), price)
        if lv:
            levels.append(lv)

    # ── 周线均线（日线 resample 周线）──
    weekly = df.resample("W-FRI").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "vol": "sum",
    }).dropna()
    if len(weekly) >= 5:
        for period, label in [(5, "5周线"), (10, "10周线"), (20, "20周线")]:
            lv = _make_level(label, _compute_ma(weekly["close"], period), price)
            if lv:
                levels.append(lv)

    # ── 月线均线（日线 resample 月线）──
    monthly = df.resample("ME").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "vol": "sum",
    }).dropna()
    if len(monthly) >= 10:
        lv = _make_level("10月线", _compute_ma(monthly["close"], 10), price)
        if lv:
            levels.append(lv)

    if not levels:
        return None

    # ── 分类：支撑 vs 阻力 ──
    supports = sorted(
        [l for l in levels if l.position in ("下方", "贴合")],
        key=lambda l: abs(l.distance_pct)
    )
    resistances = sorted(
        [l for l in levels if l.position in ("上方", "贴合")],
        key=lambda l: abs(l.distance_pct)
    )

    # ── 提炼 key_levels（最关键的2-3个）──
    key = _extract_key_levels(supports, resistances, levels)

    # ── 趋势判断（日线 MA5 > MA20 > MA60 → 多头排列）──
    trend = _judge_ma_trend(df["close"])

    return MAContext(
        symbol=symbol,
        latest_price=round(price, 2),
        levels=levels,
        support_levels=supports,
        resistance_levels=resistances,
        key_levels=key,
        trend_summary=trend,
    )


def _extract_key_levels(supports: List[MALevel],
                        resistances: List[MALevel],
                        all_levels: List[MALevel]) -> List[MALevel]:
    """
    提炼最关键的2-3个价位：
    1. 最近支撑（优先大级别：周线 > 日线）
    2. 最近阻力（优先大级别）
    3. 10月线（如果存在且不与前两个重复）
    """
    key: List[MALevel] = []
    used_names: set = set()

    # 大级别优先排序
    _PRIORITY = {"10月线": 0, "20周线": 1, "10周线": 2, "5周线": 3,
                 "60日线": 4, "20日线": 5, "10日线": 6, "5日线": 7}

    # 最近支撑（大级别优先）
    if supports:
        # 取距离最近的3个，然后按大级别优先选1个
        candidates = supports[:3]
        candidates.sort(key=lambda l: _PRIORITY.get(l.name, 99))
        key.append(candidates[0])
        used_names.add(candidates[0].name)

    # 最近阻力（大级别优先）
    if resistances:
        candidates = [r for r in resistances[:3] if r.name not in used_names]
        if candidates:
            candidates.sort(key=lambda l: _PRIORITY.get(l.name, 99))
            key.append(candidates[0])
            used_names.add(candidates[0].name)

    # 10月线（长期锚点，如果不重复）
    for lv in all_levels:
        if lv.name == "10月线" and lv.name not in used_names:
            key.append(lv)
            break

    return key


def _judge_ma_trend(closes: pd.Series) -> str:
    """
    日线均线排列判断：
    - MA5 > MA20 > MA60 → 多头排列
    - MA5 < MA20 < MA60 → 空头排列
    - 其他 → 交织
    """
    ma5 = _compute_ma(closes, 5)
    ma20 = _compute_ma(closes, 20)
    ma60 = _compute_ma(closes, 60)

    if ma5 is None or ma20 is None or ma60 is None:
        return "未知"

    if ma5 > ma20 > ma60:
        return "多头排列"
    elif ma5 < ma20 < ma60:
        return "空头排列"
    else:
        return "交织"


# ─────────────────────────────────────────────────────────
# 格式化输出
# ─────────────────────────────────────────────────────────

def format_key_levels(ctx: MAContext) -> str:
    """
    格式化关键价位行，用于终端和飞书输出。
    示例：关键位: ▼5周线 2970(-2.5%)  ▼10月线 2870(-5.8%)  ▲前高 3177(+4.3%)
    """
    if not ctx.key_levels:
        return ""

    parts = []
    for lv in ctx.key_levels:
        arrow = "▲" if lv.position == "上方" else "▼" if lv.position == "下方" else "◆"
        parts.append(f"{arrow}{lv.name} {lv.value:.0f}({lv.distance_pct:+.1f}%)")

    return "  关键位: " + "  ".join(parts)
