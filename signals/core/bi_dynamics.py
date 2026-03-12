# -*- coding: utf-8 -*-
"""
笔动力学引擎 (Bi Dynamics Engine)

分析已完成笔力度轨迹 + 未完成笔实时动量，预测启动点/转折点/衰竭点。
核心预测模块，权重占比 60%。

用法:
    from signals.core.bi_dynamics import analyze_bi_dynamics, analyze_multi_freq_dynamics
    profile = analyze_bi_dynamics(analyzer)
    profiles = analyze_multi_freq_dynamics({"日线": az_d, "30分钟": az_30})
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from czsc import Direction

from config import FUSION_WEIGHTS


@dataclass
class BiDynamicsProfile:
    """笔动力学分析结果"""
    symbol: str = ""
    freq: str = ""
    # 已完成笔力度对比
    last_bi_direction: str = ""         # "Up" / "Down"
    last_bi_power: float = 0.0
    prev_same_dir_power: float = 0.0
    power_ratio: float = 0.0           # >1加速, <1衰竭
    power_trend: str = ""              # "加速"/"衰竭"/"持平"/"数据不足"
    # 未完成笔分析 (核心预测部分)
    ubi_bar_count: int = 0
    ubi_power: float = 0.0
    ubi_vs_last_ratio: float = 0.0     # ubi_power / last_bi_power
    ubi_momentum: str = ""             # "强势延续"/"力度不足"/"刚起步"
    # 连续阳/阴线 (ubi 内)
    consecutive_bullish: int = 0
    consecutive_bearish: int = 0
    avg_body_ratio: float = 0.0
    volume_expanding: bool = False     # 量能递增
    # 防假阳
    gain_diminishing: bool = False     # 涨幅递减
    fake_positive: bool = False        # 假阳（涨幅递减+量萎缩）
    # 卖点预警
    sell_warning: str = ""             # ""/"力度见顶"/"笔即将完成"/"加速转衰竭"
    sell_warning_score: float = 0.0    # [0, 100]  越高越危险
    # 综合
    dynamics_score: float = 0.0        # [-100, +100]
    signal: str = ""                   # "笔加速做多"/"笔衰竭见顶"/...
    detail: str = ""


def analyze_bi_dynamics(analyzer) -> BiDynamicsProfile:
    """
    分析单级别笔动力学。

    :param analyzer: SymbolAnalyzer 实例
    :return: BiDynamicsProfile
    """
    profile = BiDynamicsProfile(
        symbol=getattr(analyzer, 'symbol', ''),
        freq=getattr(analyzer, 'freq', ''),
    )

    try:
        bis = analyzer.czsc.finished_bis
    except Exception:
        profile.power_trend = "数据不足"
        return profile

    if len(bis) < 3:
        profile.power_trend = "数据不足"
        return profile

    last = bis[-1]
    profile.last_bi_direction = "Up" if last.direction == Direction.Up else "Down"
    profile.last_bi_power = last.power_price

    # ── 找上一根同向笔 ──
    prev_same = None
    for bi in reversed(bis[:-1]):
        if bi.direction == last.direction:
            prev_same = bi
            break

    if prev_same:
        profile.prev_same_dir_power = prev_same.power_price
        ratio = last.power_price / max(prev_same.power_price, 1e-9)
        profile.power_ratio = round(ratio, 3)
        if ratio > 1.2:
            profile.power_trend = "加速"
        elif ratio < 0.8:
            profile.power_trend = "衰竭"
        else:
            profile.power_trend = "持平"
    else:
        profile.power_trend = "数据不足"

    # ── 未完成笔分析 ──
    try:
        bars_ubi = analyzer.czsc.bars_ubi
    except Exception:
        bars_ubi = []

    if bars_ubi and len(bars_ubi) >= 2:
        profile.ubi_bar_count = len(bars_ubi)
        ubi_high = max(b.high for b in bars_ubi)
        ubi_low = min(b.low for b in bars_ubi)
        profile.ubi_power = ubi_high - ubi_low

        if profile.last_bi_power > 0:
            profile.ubi_vs_last_ratio = round(
                profile.ubi_power / profile.last_bi_power, 3
            )

        if profile.ubi_bar_count >= 3 and profile.ubi_vs_last_ratio > 0.5:
            profile.ubi_momentum = "强势延续"
        elif profile.ubi_bar_count < 3:
            profile.ubi_momentum = "刚起步"
        else:
            profile.ubi_momentum = "力度不足"

        # ── 连续阳/阴线 + 实体比 + 量能趋势 ──
        _analyze_ubi_bars(bars_ubi, profile)
    elif bars_ubi and len(bars_ubi) == 1:
        profile.ubi_bar_count = 1
        profile.ubi_momentum = "刚起步"

    # ── 综合评分 ──
    _calc_dynamics_score(profile)

    # ── 卖点预警 ──
    _calc_sell_warning(profile, bis)

    return profile


def _analyze_ubi_bars(bars_ubi, profile: BiDynamicsProfile):
    """分析 ubi 内 K 线特征: 连续阳/阴、实体比、量能趋势、涨幅递减"""
    # 连续阳/阴线（从最新往回数）
    cons_bull = 0
    cons_bear = 0
    for b in reversed(bars_ubi):
        if b.close > b.open:
            if cons_bear == 0:
                cons_bull += 1
            else:
                break
        elif b.close < b.open:
            if cons_bull == 0:
                cons_bear += 1
            else:
                break
        else:
            break
    profile.consecutive_bullish = cons_bull
    profile.consecutive_bearish = cons_bear

    # 平均实体比
    body_ratios = []
    for b in bars_ubi:
        hl = b.high - b.low
        if hl > 0:
            body_ratios.append(abs(b.close - b.open) / hl)
    profile.avg_body_ratio = round(sum(body_ratios) / max(len(body_ratios), 1), 3)

    # 量能趋势（检查最近 bars 是否递增）
    if len(bars_ubi) >= 3:
        vols = [b.vol for b in bars_ubi[-3:]]
        if vols[-1] > vols[-2] > vols[-3]:
            profile.volume_expanding = True

    # 防假阳: 涨幅递减 + 量萎缩
    if len(bars_ubi) >= 3:
        gains = []
        vol_shrinking = True
        for i in range(1, len(bars_ubi)):
            b = bars_ubi[i]
            gains.append((b.close - b.open) / max(b.open, 1e-9))
            if i >= 2 and b.vol >= bars_ubi[i - 1].vol:
                vol_shrinking = False

        if len(gains) >= 3:
            recent = gains[-3:]
            if all(g > 0 for g in recent):
                if recent[-1] < recent[-2] < recent[-3]:
                    profile.gain_diminishing = True

        if profile.gain_diminishing and vol_shrinking:
            profile.fake_positive = True


def _calc_dynamics_score(profile: BiDynamicsProfile):
    """综合评分 [-100, +100]"""
    w = FUSION_WEIGHTS
    score = 0.0
    parts = []
    is_up = profile.last_bi_direction == "Up"

    # ── 笔力度加速/衰竭 ──
    if profile.power_trend == "加速":
        accel = w.get("dynamics_accel_bonus", 45)
        if is_up:
            score += accel
            parts.append(f"加速+{accel}")
        else:
            score -= accel
            parts.append(f"空加速-{accel}")
    elif profile.power_trend == "衰竭":
        exhaust = w.get("dynamics_exhaust_bonus", 40)
        if is_up:
            # 上升笔衰竭 → 见顶信号
            score -= exhaust * 0.6
            parts.append(f"上衰竭-{exhaust * 0.6:.0f}")
        else:
            # 下降笔衰竭 → 见底信号（抄底机会）
            score += exhaust
            parts.append(f"下衰竭+{exhaust}")

    # ── ubi 强势延续 ──
    if profile.ubi_momentum == "强势延续":
        ubi_bonus = w.get("dynamics_ubi_strong", 35)
        if is_up or (not is_up and profile.ubi_vs_last_ratio > 0.5):
            # 向上笔后 ubi 延续 = 多头强；向下笔后 ubi 反弹强 = 可能反转
            direction = 1 if is_up else -1
            # 向下笔后，ubi 是反向（向上）的，所以是正面信号
            if not is_up:
                direction = 1
            score += ubi_bonus * direction
            parts.append(f"ubi{'+' if direction > 0 else '-'}{ubi_bonus}")

    # ── 连续阳/阴线 ──
    cons_bonus = w.get("dynamics_consecutive", 25)
    if profile.consecutive_bullish >= 3:
        score += cons_bonus
        parts.append(f"连阳{profile.consecutive_bullish}+{cons_bonus}")
    elif profile.consecutive_bullish == 2:
        score += cons_bonus * 0.6
        parts.append(f"连阳2+{cons_bonus * 0.6:.0f}")
    elif profile.consecutive_bearish >= 3:
        score -= cons_bonus
        parts.append(f"连阴{profile.consecutive_bearish}-{cons_bonus}")

    # ── 量能递增 ──
    if profile.volume_expanding:
        vol_bonus = w.get("dynamics_volume_expand", 20)
        if is_up or profile.consecutive_bullish >= 2:
            score += vol_bonus
            parts.append(f"放量+{vol_bonus}")
        else:
            score -= vol_bonus * 0.5
            parts.append(f"放量跌-{vol_bonus * 0.5:.0f}")

    # ── 防假阳折扣 ──
    if profile.fake_positive and score > 0:
        old = score
        score *= 0.5
        parts.append(f"假阳×0.5({old:.0f}→{score:.0f})")

    # 限制范围
    score = max(-100, min(100, score))
    profile.dynamics_score = round(score, 1)

    # 信号标签
    if score >= 50:
        profile.signal = "笔加速做多"
    elif score >= 25:
        profile.signal = "动量偏多"
    elif score <= -50:
        if is_up:
            profile.signal = "笔衰竭见顶"
        else:
            profile.signal = "笔加速做空"
    elif score <= -25:
        profile.signal = "动量偏空"
    elif profile.power_trend == "衰竭" and not is_up:
        profile.signal = "笔衰竭见底"
    else:
        profile.signal = "中性"

    profile.detail = " | ".join(parts) if parts else "数据不足"


def _calc_sell_warning(profile: BiDynamicsProfile, bis):
    """卖点预警: 判断已确认多头是否即将见顶"""
    warnings = []
    sell_score = 0.0

    # 条件1: ubi 力度已超上一笔 1.5x → 笔即将完成
    if profile.ubi_vs_last_ratio > 1.5:
        sell_score += 70
        warnings.append("笔即将完成")

    # 条件2: 涨幅递减 + 量萎缩 → 力度见顶
    if profile.gain_diminishing and not profile.volume_expanding:
        sell_score += 60
        warnings.append("力度见顶")

    # 条件3: power_ratio 从加速转衰竭
    if len(bis) >= 5:
        # 比较倒数第3笔(同向)和倒数第1笔
        same_dir_bis = [b for b in bis if b.direction == bis[-1].direction]
        if len(same_dir_bis) >= 3:
            prev_ratio = same_dir_bis[-2].power_price / max(same_dir_bis[-3].power_price, 1e-9)
            curr_ratio = same_dir_bis[-1].power_price / max(same_dir_bis[-2].power_price, 1e-9)
            if prev_ratio > 1.3 and curr_ratio < 1.0:
                sell_score += 80
                warnings.append("加速转衰竭")

    # 条件4: ubi 内假阳
    if profile.fake_positive:
        sell_score += 45
        warnings.append("假阳")

    profile.sell_warning_score = min(100, sell_score)
    profile.sell_warning = "|".join(warnings) if warnings else ""


# ── 多级别融合 ──

FREQ_WEIGHTS = {"日线": 0.5, "30分钟": 0.35, "15分钟": 0.15}


def analyze_multi_freq_dynamics(
    analyzers: Dict[str, object],
) -> Dict[str, BiDynamicsProfile]:
    """
    对所有级别的 analyzer 计算笔动力学。

    :param analyzers: {freq_str: SymbolAnalyzer}
    :return: {freq_str: BiDynamicsProfile}
    """
    results = {}
    for freq_str, analyzer in analyzers.items():
        results[freq_str] = analyze_bi_dynamics(analyzer)
    return results


def merge_dynamics_score(profiles: Dict[str, BiDynamicsProfile]) -> float:
    """多级别动力学融合评分"""
    total = 0.0
    weight_sum = 0.0
    for freq, profile in profiles.items():
        w = FREQ_WEIGHTS.get(freq, 0.1)
        total += profile.dynamics_score * w
        weight_sum += w
    if weight_sum > 0:
        return round(total / weight_sum * 1.0, 1)
    return 0.0


def get_best_sell_warning(profiles: Dict[str, BiDynamicsProfile]) -> Dict:
    """从多级别中取最高卖点预警"""
    best_score = 0.0
    best_warning = ""
    best_freq = ""
    for freq, p in profiles.items():
        if p.sell_warning_score > best_score:
            best_score = p.sell_warning_score
            best_warning = p.sell_warning
            best_freq = freq
    return {
        "score": best_score,
        "warning": best_warning,
        "freq": best_freq,
    }
