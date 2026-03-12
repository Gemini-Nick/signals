# -*- coding: utf-8 -*-
from dataclasses import dataclass, field
from typing import List, Optional

from .detectors import SignalEvent

# ── 情绪感知系数（P3-1）──
# 恐慌买入=加分（逆向机会），亢奋买入=减分（追高风险）
_SENTIMENT_BUY_MULT = {"恐慌": 1.25, "修复": 1.10, "回落": 1.00, "亢奋": 0.80, "未知": 1.00}
_SENTIMENT_SELL_MULT = {"恐慌": 0.75, "修复": 0.90, "回落": 1.10, "亢奋": 1.25, "未知": 1.00}

# 信号基础分值（买点为正，卖点为负）
SIGNAL_WEIGHTS = {
    "二买":   55,
    "三买":   50,
    "一买":   40,
    "背驰买": 35,
    "趋势买": 30,
    "二卖":  -55,
    "三卖":  -50,
    "一卖":  -40,
    "背驰卖": -35,
    "趋势卖": -30,
    # 经典形态（辅助参考，约缠论的 1/4~1/3 权重）
    "形态:头肩顶": -15,  "形态:头肩底": 15,
    "形态:双头":   -10,  "形态:双底":   10,
    "形态:上升三角": 10,  "形态:下降三角": -10,
}

# 级别系数：大级别信号权重更高，小级别适当折扣
FREQ_MULTIPLIER = {
    "周线":   1.8,
    "日线":   1.5,
    "60分钟": 1.1,
    "30分钟": 1.0,
    "15分钟": 0.7,
    "5分钟":  0.5,
    "1分钟":  0.3,
}


# 共振加分级别
# 大级别确认小级别 → 加分更高（日线+30M > 30M+15M > 日线+15M）
_DAILY_NAMES = {"日线", "D", "daily"}
_30M_NAMES = {"30分钟", "30min", "F30"}
_15M_NAMES = {"15分钟", "15min", "F15"}


def _resonance_bonus(freqs: set) -> int:
    """
    根据共振的级别组合差异化加分。
    三级共振 > 日线+30M > 30M+15M > 其他双级别
    """
    if len(freqs) < 2:
        return 0

    has_daily = bool(freqs & _DAILY_NAMES)
    has_30m = bool(freqs & _30M_NAMES)
    has_15m = bool(freqs & _15M_NAMES)

    if has_daily and has_30m and has_15m:
        return 25  # 三级共振，最强
    if has_daily and has_30m:
        return 20  # 日线+30M，大级别确认
    if has_daily and has_15m:
        return 15  # 日线+15M，跨级别
    if has_30m and has_15m:
        return 12  # 30M+15M，小级别共振
    return 10  # 其他组合


@dataclass
class ScoredSymbol:
    symbol: str
    total_score: float
    signal_count: int
    signals: List[SignalEvent]
    details: str
    direction: str = ""   # "偏多" / "偏空" / "分歧" / ""
    name: str = ""        # 公司名称（由 StockNameResolver 注入）
    ma_confirmation: str = ""  # 均线交叉确认描述
    sentiment_tag: str = ""    # 情绪乘数标签，如 "恐慌×1.25"
    # 社交舆情（P0）
    social_heat: str = ""            # "爆热"/"热门"/"温和"/"冷门"
    social_tag: str = ""             # 简短标签，如 "雪球#38 千评:增持"
    theme_tags: List[str] = field(default_factory=list)  # 关联主题 ["昇腾","算力"]
    # 多维信号融合（P0）
    anomaly_profile: object = None  # AnomalyProfile (异常画像)
    fused_score: object = None      # FusedScore (融合评分)
    fused_total: float = 0.0        # 融合后总分 (方便排序)


# 不同级别的信号半衰期（天）: 短周期信号衰减更快
_FREQ_HALF_LIFE = {
    "周线": 60.0,
    "日线": 30.0,
    "60分钟": 10.0,
    "30分钟": 5.0,
    "15分钟": 3.0,
    "5分钟": 1.0,
    "1分钟": 0.5,
}


def _time_decay(sig_dt, ref_dt=None, half_life_days: float = 30.0,
                freq: str = "") -> float:
    """
    信号时间衰减因子。越新的信号越接近 1.0，越老的越低。
    freq 参数可选: 传入信号级别，自动使用对应半衰期。
    最低不低于 0.1（避免完全归零）。
    """
    from datetime import datetime
    if ref_dt is None:
        ref_dt = datetime.now()
    if sig_dt is None:
        return 1.0
    # 使用级别对应的半衰期（如有）
    if freq and freq in _FREQ_HALF_LIFE:
        half_life_days = _FREQ_HALF_LIFE[freq]
    # sig_dt 可能是 Timestamp
    if hasattr(sig_dt, 'to_pydatetime'):
        sig_dt = sig_dt.to_pydatetime()
    if hasattr(ref_dt, 'to_pydatetime'):
        ref_dt = ref_dt.to_pydatetime()
    # 去掉 tzinfo 避免比较报错
    sig_dt = sig_dt.replace(tzinfo=None) if sig_dt.tzinfo else sig_dt
    ref_dt = ref_dt.replace(tzinfo=None) if ref_dt.tzinfo else ref_dt
    delta_days = (ref_dt - sig_dt).total_seconds() / 86400.0
    if delta_days <= 0:
        return 1.0
    import math
    decay = math.pow(0.5, delta_days / half_life_days)
    return max(decay, 0.05)


def score_signals(symbol: str, signals: List[SignalEvent],
                  enable_decay: bool = True,
                  ma_context=None,
                  sentiment_phase: str = "未知",
                  volume_ratio: float = 0.0,
                  social_score=None) -> ScoredSymbol:
    """
    对单个标的的所有信号计算综合评分。
    enable_decay=True 时启用时间衰减（默认开启，按级别差异化半衰期）。
    ma_context: 可选 MAContext，用于均线+缠论交叉确认加分。
    sentiment_phase: 情绪周期，影响买卖信号权重。
    volume_ratio: 量比（>0 时启用量价确认加减分）。
    """
    if not signals:
        return ScoredSymbol(
            symbol=symbol, total_score=0.0,
            signal_count=0, signals=[], details="无信号",
        )

    # 情绪乘数
    buy_mult = _SENTIMENT_BUY_MULT.get(sentiment_phase, 1.0)
    sell_mult = _SENTIMENT_SELL_MULT.get(sentiment_phase, 1.0)

    total = 0.0
    buy_total = 0.0
    sell_total = 0.0
    for sig in signals:
        base = SIGNAL_WEIGHTS.get(sig.signal_type, 0)
        freq_mult = FREQ_MULTIPLIER.get(sig.freq, 1.0)
        decay = _time_decay(sig.dt, freq=sig.freq) if enable_decay else 1.0
        raw = base * sig.confidence * freq_mult * decay
        # 情绪乘数：买信号乘 buy_mult，卖信号乘 sell_mult
        if "买" in sig.signal_type or base > 0:
            contribution = raw * buy_mult
        elif "卖" in sig.signal_type or base < 0:
            contribution = raw * sell_mult
        else:
            contribution = raw
        total += contribution
        if "买" in sig.signal_type or base > 0:
            buy_total += abs(contribution)
        elif "卖" in sig.signal_type or base < 0:
            sell_total += abs(contribution)

    # 多级别共振加分：根据共振的级别组合差异化加分
    # 日线+30M 共振比 30M+15M 更有价值（大级别确认小级别）
    buy_freqs = {s.freq for s in signals if "买" in s.signal_type}
    sell_freqs = {s.freq for s in signals if "卖" in s.signal_type}
    buy_bonus = _resonance_bonus(buy_freqs)
    sell_bonus = _resonance_bonus(sell_freqs)
    total += buy_bonus
    buy_total += buy_bonus
    total -= sell_bonus
    sell_total += sell_bonus

    # 买卖互斥判断
    if buy_total > 0 and sell_total > 0:
        if abs(buy_total - sell_total) < 20:
            direction = "分歧"
        elif buy_total > sell_total:
            direction = "偏多"
        else:
            direction = "偏空"
    elif buy_total > 0:
        direction = "偏多"
    elif sell_total > 0:
        direction = "偏空"
    else:
        direction = ""

    # 情绪标签
    sentiment_tag = ""
    if sentiment_phase != "未知":
        # 取买/卖中较显著的乘数
        if buy_total >= sell_total:
            sentiment_tag = f"{sentiment_phase}×{buy_mult:.2f}"
        else:
            sentiment_tag = f"{sentiment_phase}×{sell_mult:.2f}"

    details_lines = []
    if sentiment_phase != "未知":
        details_lines.append(f"  [情绪] {sentiment_phase} 买×{buy_mult:.2f} 卖×{sell_mult:.2f}")
    for s in signals:
        decay = _time_decay(s.dt, freq=s.freq) if enable_decay else 1.0
        decay_tag = f" decay={decay:.2f}" if enable_decay and decay < 0.95 else ""
        details_lines.append(
            f"  [{s.freq}] {s.signal_type} conf={s.confidence:.2f} @ {s.price:.2f}"
            f"  ×{FREQ_MULTIPLIER.get(s.freq, 1.0):.1f}{decay_tag}  {s.details}"
        )
    if buy_bonus > 0:
        details_lines.append(f"  [共振+{buy_bonus}] 买信号出现在 {buy_freqs}")
    if sell_bonus > 0:
        details_lines.append(f"  [共振-{sell_bonus}] 卖信号出现在 {sell_freqs}")
    if direction == "分歧":
        details_lines.append(f"  [分歧] 买力={buy_total:.1f} vs 卖力={sell_total:.1f}，方向不明确")

    # ── 均线+缠论交叉确认 ──
    ma_conf = ""
    if ma_context and buy_total > 0:
        # 买信号 + 均线支撑位附近(<2%): +15分
        near_support = any(
            abs(lv.distance_pct) < 2.0 and lv.position in ("下方", "贴合")
            for lv in (ma_context.support_levels or [])
        )
        if near_support:
            total += 8
            nearest = ma_context.support_levels[0] if ma_context.support_levels else None
            ref = f"{nearest.name} {nearest.value:.0f}" if nearest else "支撑位"
            ma_conf = f"回踩{ref}确认"
            details_lines.append(f"  [均线+8] 买信号+均线支撑确认({ref})")

        # 买信号 + 均线多头排列: +5分
        if ma_context.trend_summary == "多头排列":
            total += 5
            ma_conf = (ma_conf + "+多头" if ma_conf else "多头排列")
            details_lines.append("  [均线+5] 均线多头排列")

        # 买信号 + 均线空头排列: -5分（逆势）
        elif ma_context.trend_summary == "空头排列":
            total -= 5
            ma_conf = (ma_conf + " 逆势⚠" if ma_conf else "逆势买入⚠")
            details_lines.append("  [均线-5] 均线空头排列，逆势买入")

    # ── 量价确认 ──
    if volume_ratio > 0:
        if buy_total > sell_total:
            if volume_ratio >= 1.5:
                total += 10
                details_lines.append(f"  [量价+10] 买入信号+放量(量比{volume_ratio:.1f})")
            elif volume_ratio < 0.7:
                total -= 10
                details_lines.append(f"  [量价-10] 买入信号+缩量(量比{volume_ratio:.1f})")
        elif sell_total > buy_total:
            if volume_ratio >= 1.5:
                total -= 10
                details_lines.append(f"  [量价-10] 卖出信号+放量(量比{volume_ratio:.1f})")
            elif volume_ratio < 0.7:
                total += 5
                details_lines.append(f"  [量价+5] 卖出信号+缩量=假信号(量比{volume_ratio:.1f})")

    # ── 社交舆情确认 ──
    social_heat = ""
    social_tag_str = ""
    theme_tags_list: List[str] = []
    if social_score:
        social_heat = getattr(social_score, "heat_grade", "")
        social_tag_str = getattr(social_score, "tag", "")
        theme_tags_list = getattr(social_score, "concepts", []) or []
        heat = getattr(social_score, "heat_score", 0)
        if buy_total > sell_total:
            if heat >= 75:  # 爆热+买入
                total += 12
                details_lines.append(f"  [舆情+12] 买入信号+社交爆热({heat:.0f})")
            elif heat >= 50:  # 热门+买入
                total += 6
                details_lines.append(f"  [舆情+6] 买入信号+社交热门({heat:.0f})")
        elif sell_total > buy_total:
            if heat >= 75:  # 爆热+卖出 → FOMO警告
                details_lines.append(f"  [舆情⚠] 卖出信号+社交爆热({heat:.0f})，散户FOMO风险")

    return ScoredSymbol(
        symbol=symbol,
        total_score=round(total, 1),
        signal_count=len(signals),
        signals=signals,
        details="\n".join(details_lines),
        direction=direction,
        ma_confirmation=ma_conf,
        sentiment_tag=sentiment_tag,
        social_heat=social_heat,
        social_tag=social_tag_str,
        theme_tags=theme_tags_list,
    )
