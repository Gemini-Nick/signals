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


# ─────────────────────────────────────────────────────────
# P3-2: 情景分叉 (Scenario Branches)
# ─────────────────────────────────────────────────────────

@dataclass
class ScenarioBranch:
    """关键价位情景分叉：IF 守住/跌破 → THEN 后续演绎"""
    level_name: str       # "5周线"
    level_price: float    # 2970.0
    distance_pct: float   # -2.5
    is_support: bool      # True=支撑位, False=阻力位
    urgency: str          # "接近"(<2%) / "关注"(2-5%) / "远离"(>5%)
    hold: str             # 守住后的演绎
    break_: str           # 跌破/突破后的演绎


# 情景文案模板
_SUPPORT_TEMPLATES = {
    "10月线":  ("长期趋势维持,月线级别支撑有效",      "长期趋势走坏,月线级别破位,宜大幅降仓"),
    "20周线":  ("中期趋势维持,周线级别震荡偏强",      "中期走弱,周线级别转空,切防守"),
    "10周线":  ("中短期趋势维持,关注科技轮动",        "中期走弱,切防守降仓"),
    "5周线":   ("短期反弹延续,关注板块轮动",          "短期转弱,降低仓位观望"),
    "60日线":  ("日线级别趋势维持,可持股待涨",        "日线趋势转空,减仓观望"),
    "20日线":  ("短线支撑有效,可继续持有",            "短线走弱,注意止损"),
}

_RESISTANCE_TEMPLATES = {
    "10月线":  ("月线级别突破,长期趋势反转向上",       "月线压力有效,反弹空间受限"),
    "20周线":  ("周线级别突破,中期转强",              "周线压力仍在,震荡格局延续"),
    "10周线":  ("中短期突破,上方空间打开",            "反弹受阻,继续震荡"),
    "5周线":   ("短期突破向上,追踪板块强度",          "短期反弹受压,观望为主"),
    "60日线":  ("日线级别突破,趋势转多",              "日线压力有效,短期震荡"),
    "20日线":  ("短线突破,可适度参与",                "短线压力仍在,等待突破"),
}

_DEFAULT_SUPPORT = ("趋势维持,支撑有效", "支撑破位,注意风险")
_DEFAULT_RESISTANCE = ("向上突破,趋势转强", "压力有效,继续震荡")


def _urgency_level(distance_pct: float) -> str:
    """根据距离判断紧迫度"""
    abs_dist = abs(distance_pct)
    if abs_dist < 2.0:
        return "接近"
    elif abs_dist < 5.0:
        return "关注"
    else:
        return "远离"


def build_scenario_branches(
    ctx: MAContext,
    custom_levels: Optional[dict] = None,
    max_distance_pct: float = 5.0,
) -> List[ScenarioBranch]:
    """
    从 MAContext.key_levels 中距离 < max_distance_pct 的生成 IF/THEN 情景分叉。

    :param ctx: 均线上下文
    :param custom_levels: 自定义关键价位 {name: price}，与均线合并
    :param max_distance_pct: 只对距离在此范围内的价位生成分叉
    :return: ScenarioBranch 列表，按距离排序
    """
    if ctx is None:
        return []

    branches: List[ScenarioBranch] = []

    # 从 key_levels 和 levels 中合并（去重，优先 key_levels）
    seen_names = set()
    candidates: List[MALevel] = []
    for lv in (ctx.key_levels or []):
        if lv.name not in seen_names:
            candidates.append(lv)
            seen_names.add(lv.name)
    for lv in (ctx.levels or []):
        if lv.name not in seen_names and abs(lv.distance_pct) < max_distance_pct:
            candidates.append(lv)
            seen_names.add(lv.name)

    # 加入自定义关键价位
    if custom_levels:
        for name, price in custom_levels.items():
            if name not in seen_names and price > 0:
                lv = _make_level(name, price, ctx.latest_price)
                if lv and abs(lv.distance_pct) < max_distance_pct:
                    candidates.append(lv)
                    seen_names.add(name)

    for lv in candidates:
        if abs(lv.distance_pct) > max_distance_pct:
            continue

        is_support = lv.position in ("下方", "贴合")
        urgency = _urgency_level(lv.distance_pct)

        if is_support:
            templates = _SUPPORT_TEMPLATES.get(lv.name, _DEFAULT_SUPPORT)
            hold, break_ = templates
        else:
            templates = _RESISTANCE_TEMPLATES.get(lv.name, _DEFAULT_RESISTANCE)
            hold, break_ = templates[1], templates[0]  # 阻力位：守住=压力有效，突破=向上

        branches.append(ScenarioBranch(
            level_name=lv.name,
            level_price=lv.value,
            distance_pct=lv.distance_pct,
            is_support=is_support,
            urgency=urgency,
            hold=hold,
            break_=break_,
        ))

    # 按距离绝对值排序（最接近的在前）
    branches.sort(key=lambda b: abs(b.distance_pct))
    return branches


def format_scenario_branches(branches: List[ScenarioBranch]) -> str:
    """格式化情景分叉，用于终端输出"""
    if not branches:
        return ""
    lines = ["  情景分叉:"]
    for b in branches:
        arrow = "▼" if b.is_support else "▲"
        action = "守住" if b.is_support else "突破"
        fail = "跌破" if b.is_support else "受阻"
        lines.append(f"    {arrow}{b.level_name} {b.level_price:.0f}({b.distance_pct:+.1f}%) [{b.urgency}]")
        lines.append(f"      IF {action} → {b.hold}")
        lines.append(f"      IF {fail} → {b.break_}")
    return "\n".join(lines)
