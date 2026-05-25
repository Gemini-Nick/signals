# -*- coding: utf-8 -*-
"""
均线关键位系统 (MA Levels)

从日线 RawBar 数据计算日线 + 周线嵌套均线，输出关键支撑/阻力位。
小级别继续纯缠论，均线只承担成本锚与趋势转折提示。

均线用途：给出"稳定的市场共识价位锚点"，与缠论互补。
- 缠论 → 方向（二买=结构向上）
- 均线 → 位置（5周线=关键支撑）
- 方向 + 位置 = 可操作
"""
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd
from czsc import RawBar

KEY_MA_PERIODS = (5, 8, 10, 13, 20, 21)
KEY_MA_COLORS = {
    5: "#f7931a",
    8: "#d97757",
    10: "#6a9bcc",
    13: "#2962ff",
    20: "#e040fb",
    21: "#26a69a",
}


# ─────────────────────────────────────────────────────────
# 数据类
# ─────────────────────────────────────────────────────────

@dataclass
class MALevel:
    name: str           # "5周线", "13周线", "20日线"
    value: float        # 均线值
    distance_pct: float # 当前价距该均线 %（正=上方，负=下方）
    position: str       # "上方" / "下方" / "贴合"
    timeframe: str = "" # "daily" / "weekly"
    period: int = 0
    direction: str = "未知"  # "向上" / "走平" / "向下"
    role: str = ""

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
                price: float, closes: Optional[pd.Series] = None,
                period: int = 0, timeframe: str = "",
                role: str = "") -> Optional[MALevel]:
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
        timeframe=timeframe, period=period,
        direction=_ma_direction(closes, period),
        role=role,
    )


def compute_ma_levels(bars: List[RawBar], symbol: str) -> Optional[MAContext]:
    """
    从日线 RawBar 计算多周期均线关键位。

    计算均线：
    - 日线: MA5, MA8, MA10, MA13, MA20, MA21
    - 周线: MA5, MA8, MA10, MA13, MA20, MA21（日线 resample 周线后计算）

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
    daily_roles = {
        5: "短线趋势/止损线",
        8: "短线动能观察",
        10: "第一次回踩买点/弱势反弹阻力",
        13: "关键节奏线",
        20: "中期趋势转折线",
        21: "趋势防守线",
    }
    for period in KEY_MA_PERIODS:
        lv = _make_level(
            f"{period}日线",
            _compute_ma(df["close"], period),
            price,
            closes=df["close"],
            period=period,
            timeframe="daily",
            role=daily_roles.get(period, ""),
        )
        if lv:
            levels.append(lv)

    # ── 周线均线（日线 resample 周线）──
    weekly = df.resample("W-FRI").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "vol": "sum",
    }).dropna()
    if len(weekly) >= 5:
        weekly_roles = {
            5: "持股信心线",
            8: "短周节奏线",
            10: "中期底部反抽目标",
            13: "周线关键节奏",
            20: "熊市反弹压力位",
            21: "周线趋势防守",
        }
        for period in KEY_MA_PERIODS:
            lv = _make_level(
                f"{period}周线",
                _compute_ma(weekly["close"], period),
                price,
                closes=weekly["close"],
                period=period,
                timeframe="weekly",
                role=weekly_roles.get(period, ""),
            )
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

    # ── 趋势判断（日线 MA5 > MA13 > MA21 → 多头排列）──
    trend = _judge_ma_trend(df["close"], levels)

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
    提炼交易员右栏要看的关键价位。
    周线优先，日线补充；距离近的支撑/阻力必须保留。
    """
    key: List[MALevel] = []
    used_names: set = set()

    # 大级别优先排序
    _PRIORITY = {f"{period}周线": idx for idx, period in enumerate((21, 20, 13, 10, 8, 5))}
    _PRIORITY.update({f"{period}日线": idx + 10 for idx, period in enumerate((21, 20, 13, 10, 8, 5))})

    def add(level: Optional[MALevel]) -> None:
        if level and level.name not in used_names:
            key.append(level)
            used_names.add(level.name)

    add(supports[0] if supports else None)
    add(resistances[0] if resistances else None)

    near_levels = [lv for lv in all_levels if abs(lv.distance_pct) <= 8.0]
    near_levels.sort(key=lambda lv: (_PRIORITY.get(lv.name, 99), abs(lv.distance_pct)))
    for lv in near_levels:
        add(lv)
        if len(key) >= 8:
            break

    return key


def _ma_direction(closes: Optional[pd.Series], period: int) -> str:
    if closes is None or period <= 0 or len(closes) < period + 3:
        return "未知"
    ma = closes.rolling(period).mean().dropna()
    if len(ma) < 4:
        return "未知"
    current = float(ma.iloc[-1])
    previous = float(ma.iloc[-4])
    if previous == 0:
        return "未知"
    slope_pct = (current - previous) / previous * 100
    if abs(slope_pct) < 0.2:
        return "走平"
    return "向上" if slope_pct > 0 else "向下"


def _judge_ma_trend(closes: pd.Series, levels: List[MALevel]) -> str:
    """
    日线均线排列判断：
    - MA5 > MA13 > MA21 → 多头排列
    - MA5 < MA13 < MA21 → 空头排列
    - 其他 → 交织
    """
    ma5 = _compute_ma(closes, 5)
    ma13 = _compute_ma(closes, 13)
    ma21 = _compute_ma(closes, 21)

    if ma5 is None or ma13 is None or ma21 is None:
        return "未知"

    if ma5 > ma13 > ma21:
        base = "多头排列"
    elif ma5 < ma13 < ma21:
        base = "空头排列"
    else:
        base = "交织"

    level_by_name = {lv.name: lv for lv in levels}
    weekly_13 = level_by_name.get("13周线")
    weekly_21 = level_by_name.get("21周线")
    if weekly_13 and weekly_21:
        lower = min(weekly_13.value, weekly_21.value)
        upper = max(weekly_13.value, weekly_21.value)
        latest = closes.iloc[-1]
        if lower <= latest <= upper:
            return f"{base} · 13周/21周夹板区"
    ma21_direction = _ma_direction(closes, 21)
    if ma21_direction in {"向上", "走平", "向下"}:
        return f"{base} · 21日线{ma21_direction}"
    return base


# ─────────────────────────────────────────────────────────
# 格式化输出
# ─────────────────────────────────────────────────────────

def format_key_levels(ctx: MAContext) -> str:
    """
    格式化关键价位行，用于终端和飞书输出。
    示例：关键位: ▼5周线 2970(-2.5%)  ▼13周线 2870(-5.8%)  ▲前高 3177(+4.3%)
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
    "21周线":  ("周线趋势防守有效,中期结构仍可观察",  "周线趋势防守失效,降低仓位"),
    "20周线":  ("中期趋势维持,周线级别震荡偏强",      "中期走弱,周线级别转空,切防守"),
    "13周线":  ("周线节奏维持,反弹仍有延续条件",      "周线节奏转弱,等待重新站回"),
    "10周线":  ("中短期趋势维持,关注科技轮动",        "中期走弱,切防守降仓"),
    "8周线":   ("短周动能维持,观察板块扩散",          "短周动能转弱,降低进攻仓"),
    "5周线":   ("短期反弹延续,关注板块轮动",          "短期转弱,降低仓位观望"),
    "21日线":  ("日线趋势防守有效,可继续观察",        "日线趋势防守失效,减仓观望"),
    "20日线":  ("短线支撑有效,可继续持有",            "短线走弱,注意止损"),
    "13日线":  ("短线节奏保持,可跟踪承接",            "短线节奏走弱,控制仓位"),
    "10日线":  ("短线回踩有效,可继续观察",            "短线回踩失败,等待修复"),
    "8日线":   ("短线动能保持,关注延续",              "短线动能衰减,等待企稳"),
    "5日线":   ("超短线承接有效,保持跟踪",            "超短线失守,观察回补"),
}

_RESISTANCE_TEMPLATES = {
    "21周线":  ("周线趋势重新转强,中期空间打开",       "周线趋势压力仍在,反弹受限"),
    "20周线":  ("周线级别突破,中期转强",              "周线压力仍在,震荡格局延续"),
    "13周线":  ("周线节奏转强,观察放量确认",          "周线节奏压力仍在,继续震荡"),
    "10周线":  ("中短期突破,上方空间打开",            "反弹受阻,继续震荡"),
    "8周线":   ("短周动能突破,关注延续",              "短周动能压力有效,继续观察"),
    "5周线":   ("短期突破向上,追踪板块强度",          "短期反弹受压,观望为主"),
    "21日线":  ("日线趋势转强,观察回踩确认",          "日线趋势压力有效,短期震荡"),
    "20日线":  ("短线突破,可适度参与",                "短线压力仍在,等待突破"),
    "13日线":  ("短线节奏突破,跟踪延续",              "短线节奏压力仍在,等待站回"),
    "10日线":  ("短线突破,观察量能",                  "短线压力有效,继续等待"),
    "8日线":   ("短线动能突破,关注持续性",            "短线动能受压,降低预期"),
    "5日线":   ("超短线站回,观察承接",                "超短线压力有效,等待修复"),
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
