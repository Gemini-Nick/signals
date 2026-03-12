# -*- coding: utf-8 -*-
"""
多维信号融合框架 (Signal Fusion)

预测导向评分体系:
  动力学预测 60% + 结构确认 25% + 事后确认 15%（受市场环境系数调节）

核心逻辑:
  缠论原始分 + 笔动力学 + 板块动量 + 异常加减分×环境系数 = 融合总分

用法:
    from signals.core.fusion import fuse_scores
    fused = fuse_scores(scored, anomaly, dynamics=dyn, sector_mom=sec,
                        l2_stats=stats, market_ctx=ctx)
"""
from dataclasses import dataclass
from typing import Optional

from config import FUSION_WEIGHTS


@dataclass
class FusedScore:
    """多维融合评分结果"""
    raw_czsc_score: float        # 原始缠论分数
    anomaly_boost: float         # 异常维度加减分（已乘环境系数）
    convergence_bonus: float     # 多维收敛加分（已乘环境系数）
    capitulation_bonus: float    # 割肉指标加分（已乘环境系数）
    dynamics_boost: float = 0.0  # 笔动力学加减分
    sector_momentum: float = 0.0 # 板块动量加分
    regime_mult: float = 1.0     # 市场环境系数
    fused_total: float = 0.0     # 融合后总分
    dimension_count: int = 0     # 触发异常维度数
    confidence_level: str = "无" # "高"(≥3维) / "中"(2维) / "低"(1维) / "无"
    detail: str = ""             # 融合明细


def fused_sell_penalty(sell_warning_score: float, weights: dict) -> float:
    """sell_warning 越高，对 fused_total 的扣分越大。"""
    if sell_warning_score >= 80:
        return weights.get("sell_warning_extreme", 25)
    elif sell_warning_score >= 60:
        return weights.get("sell_warning_high", 15)
    return 0.0


def fuse_scores(
    scored,
    anomaly=None,
    dynamics=None,
    sector_mom=None,
    l2_stats: dict = None,
    market_ctx=None,
    weights: dict = None,
) -> FusedScore:
    """
    将缠论评分、笔动力学、板块动量、异常画像融合为综合评分。

    :param scored: ScoredSymbol (缠论评分结果)
    :param anomaly: AnomalyProfile (异常检测结果, 可为 None)
    :param dynamics: BiDynamicsProfile (笔动力学, 可为 None)
    :param sector_mom: SectorMomentumSignal (板块动量, 可为 None)
    :param l2_stats: L2 统计 (zt_total, dt_total, lianban_max)
    :param market_ctx: MarketContext (指数分析结果, 可为 None)
    :param weights: 融合权重, 默认 FUSION_WEIGHTS
    :return: FusedScore
    """
    w = weights or FUSION_WEIGHTS
    base = scored.total_score
    czsc_buy = scored.direction in ("偏多", "")

    anomaly_boost = 0.0
    convergence_bonus = 0.0
    capitulation_bonus = 0.0
    dimension_count = 0
    detail_parts = []

    # ════════════════════════════════════════════════════════
    # 事后确认维度（异常检测 + 割肉）— 受市场环境系数调节
    # ════════════════════════════════════════════════════════

    if anomaly and anomaly.items:
        dimension_count = anomaly.anomaly_count
        vol_item = anomaly.items.get("volume")
        gap_item = anomaly.items.get("gap")
        range_item = anomaly.items.get("range")
        body_item = anomaly.items.get("body")

        # ── 异常方向判定 ──
        body_bullish = body_item and body_item.is_anomaly and "大阳" in body_item.label
        body_bearish = body_item and body_item.is_anomaly and "大阴" in body_item.label
        anomaly_bullish = body_bullish or (czsc_buy and not body_bearish)

        # ── 实体异常 ──
        if body_item and body_item.is_anomaly:
            body_boost = w.get("anomaly_body_boost", 6)
            if body_bullish:
                anomaly_boost += body_boost
                detail_parts.append(f"大阳+{body_boost}")
            else:
                anomaly_boost -= body_boost
                detail_parts.append(f"大阴-{body_boost}")

        # ── 量能异常 ──
        if vol_item and vol_item.is_anomaly:
            if "放量" in vol_item.label:
                if anomaly_bullish:
                    boost = w.get("anomaly_volume_boost", 8)
                    anomaly_boost += boost
                    detail_parts.append(f"放量+{boost}")
                else:
                    penalty = abs(w.get("anomaly_volume_penalty", -5))
                    anomaly_boost -= penalty
                    detail_parts.append(f"放量卖-{penalty}")
            elif "缩量" in vol_item.label:
                if anomaly_bullish:
                    penalty = abs(w.get("anomaly_volume_penalty", -5))
                    anomaly_boost -= penalty
                    detail_parts.append(f"缩量买-{penalty}")

        # ── 跳空异常 ──
        if gap_item and gap_item.is_anomaly:
            gap_boost = w.get("anomaly_gap_boost", 5)
            if "高开" in gap_item.label:
                if anomaly_bullish:
                    anomaly_boost += gap_boost
                    detail_parts.append(f"跳高+{gap_boost}")
                else:
                    anomaly_boost += gap_boost * 0.3
                    detail_parts.append(f"跳高弱+{gap_boost * 0.3:.0f}")
            elif "低开" in gap_item.label:
                if not anomaly_bullish:
                    anomaly_boost += gap_boost
                    detail_parts.append(f"跳低+{gap_boost}")
                else:
                    anomaly_boost -= gap_boost * 0.5
                    detail_parts.append(f"跳低逆势-{gap_boost * 0.5:.0f}")

        # ── 振幅异常 ──
        if range_item and range_item.is_anomaly:
            range_boost = w.get("anomaly_range_boost", 3)
            anomaly_boost += range_boost if anomaly_bullish else -range_boost
            detail_parts.append(f"波动{'+'if anomaly_bullish else '-'}{range_boost}")

        # ── 多维收敛加分 ──
        if dimension_count >= 3:
            convergence_bonus = w.get("convergence_3dim", 10)
        elif dimension_count == 2:
            convergence_bonus = w.get("convergence_2dim", 6)
        elif dimension_count == 1:
            convergence_bonus = w.get("convergence_1dim", 3)

        if convergence_bonus > 0:
            detail_parts.append(f"收敛+{convergence_bonus:.0f}({dimension_count}维)")

        # ── 割肉指标加分 ──
        cap = anomaly.capitulation_score
        if cap >= 80:
            capitulation_bonus = w.get("capitulation_extreme", 12)
            detail_parts.append(f"割肉+{capitulation_bonus:.0f}(极度)")
        elif cap >= 60:
            capitulation_bonus = w.get("capitulation_high", 8)
            detail_parts.append(f"割肉+{capitulation_bonus:.0f}(恐慌)")
        elif cap >= 40:
            capitulation_bonus = w.get("capitulation_medium", 4)
            detail_parts.append(f"割肉+{capitulation_bonus:.0f}(偏弱)")

    # ── 市场环境系数（硬数据驱动，仅调节事后确认维度）──
    regime_mult = _calc_market_regime_mult(l2_stats or {}, market_ctx)
    anomaly_boost *= regime_mult
    convergence_bonus *= regime_mult
    capitulation_bonus *= regime_mult
    if regime_mult < 0.8:
        detail_parts.append(f"环境×{regime_mult:.2f}(存量市)")
    elif regime_mult > 0.9:
        detail_parts.append(f"环境×{regime_mult:.2f}(增量市)")

    # ════════════════════════════════════════════════════════
    # 预测维度（笔动力学 + 板块动量）— 不受环境系数影响
    # ════════════════════════════════════════════════════════

    dynamics_boost = 0.0
    if dynamics and abs(dynamics.dynamics_score) > 10:
        dynamics_boost = dynamics.dynamics_score
        detail_parts.append(f"动力学{dynamics_boost:+.0f}({dynamics.signal})")

    sector_momentum_boost = 0.0
    if sector_mom and sector_mom.momentum_score >= 20:
        # 只有个股方向与板块方向一致时才加分
        stock_dir = getattr(scored, "direction", "")
        sector_aligned = stock_dir not in ("偏空",)  # 偏空个股不享受板块加分
        if sector_aligned:
            if sector_mom.signal_level == "强":
                sector_momentum_boost = w.get("sector_momentum_strong", 30)
            elif sector_mom.signal_level == "中":
                sector_momentum_boost = w.get("sector_momentum_medium", 15)
            detail_parts.append(
                f"板块+{sector_momentum_boost:.0f}({sector_mom.concept_name})"
            )
        else:
            detail_parts.append(f"板块方向冲突(个股{stock_dir})")

    # ── 卖点预警折扣（sell_warning 参与融合）──
    sell_discount = 0.0
    if dynamics and hasattr(dynamics, 'sell_warning_score'):
        sw = dynamics.sell_warning_score or 0
        if sw >= 60:
            sell_discount = -fused_sell_penalty(sw, w)
            detail_parts.append(f"卖警{sell_discount:+.0f}(预警{sw})")

    # ════════════════════════════════════════════════════════
    # 融合总分
    # ════════════════════════════════════════════════════════

    fused_total = (
        base + anomaly_boost + convergence_bonus + capitulation_bonus
        + dynamics_boost + sector_momentum_boost + sell_discount
    )

    # 置信度分级（综合预测+确认维度）
    active_dims = dimension_count
    if dynamics and abs(dynamics.dynamics_score) > 10:
        active_dims += 1
    if sector_mom and sector_mom.momentum_score >= 20:
        active_dims += 1

    if active_dims >= 3:
        confidence_level = "高"
    elif active_dims >= 2:
        confidence_level = "中"
    elif active_dims >= 1:
        confidence_level = "低"
    else:
        confidence_level = "无"

    detail = " | ".join(detail_parts) if detail_parts else "纯缠论评分"

    return FusedScore(
        raw_czsc_score=round(base, 1),
        anomaly_boost=round(anomaly_boost, 1),
        convergence_bonus=round(convergence_bonus, 1),
        capitulation_bonus=round(capitulation_bonus, 1),
        dynamics_boost=round(dynamics_boost, 1),
        sector_momentum=round(sector_momentum_boost, 1),
        regime_mult=regime_mult,
        fused_total=round(fused_total, 1),
        dimension_count=dimension_count,
        confidence_level=confidence_level,
        detail=detail,
    )


# ════════════════════════════════════════════════════════
# 市场环境系数 — 硬数据驱动
# ════════════════════════════════════════════════════════

def _calc_market_regime_mult(l2_stats: dict, market_ctx=None) -> float:
    """
    市场环境系数 [0.15, 1.0]

    基于硬数据判断增量/存量市场，调整事后确认维度权重。
    增量市场（追涨有接盘者）→ 系数高；存量市场（追涨=接盘）→ 系数低。

    三维度: 涨跌停比(40%) + 全市场成交量趋势(40%) + 连板高度(20%)
    """
    if not l2_stats:
        return 1.0

    zt = l2_stats.get("zt_total", 0)
    dt = l2_stats.get("dt_total", 0)
    lianban = l2_stats.get("lianban_max", 0)

    # 无数据时默认中性
    if zt == 0 and dt == 0:
        return 1.0

    # ── 维度1: 涨跌停比 (40%) ──
    total = max(zt + dt, 1)
    zt_ratio = zt / total
    zt_score = max(0.0, min(1.0, (zt_ratio - 0.3) / 0.4))

    # ── 维度2: 全市场成交量趋势 (40%) ──
    vol_ratio = _get_market_volume_ratio(market_ctx)
    vol_score = max(0.0, min(1.0, (vol_ratio - 0.8) / 0.4))

    # ── 维度3: 连板高度 (20%) ──
    lianban_score = max(0.0, min(1.0, (lianban - 2) / 3))

    regime_score = zt_score * 0.4 + vol_score * 0.4 + lianban_score * 0.2

    # 映射到 [0.30, 1.0]（熊市保留 30% 信号价值）
    return round(0.30 + regime_score * 0.70, 2)


def _get_market_volume_ratio(market_ctx) -> float:
    """从指数日线数据计算今日成交额/5日均值"""
    if not market_ctx:
        return 1.0
    try:
        for report in (market_ctx.reports or []):
            if report.name == "沪深300" and hasattr(report, "analyzer"):
                analyzer = report.analyzer
                if hasattr(analyzer, "bars") and len(analyzer.bars) >= 6:
                    bars = analyzer.bars
                    today_amount = (
                        bars[-1].amount
                        if hasattr(bars[-1], "amount")
                        else 0
                    )
                    if today_amount > 0:
                        avg_5d = sum(
                            b.amount for b in bars[-6:-1]
                            if hasattr(b, "amount")
                        ) / 5
                        return today_amount / max(avg_5d, 1)
    except Exception:
        pass
    return 1.0
