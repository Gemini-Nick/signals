# -*- coding: utf-8 -*-
"""
多维信号融合框架 (Signal Fusion)

将缠论评分 + 统计异常检测结果融合为综合置信度评分。
独立于缠论和异常模块，可单独调用。

核心逻辑:
  缠论原始分 + 异常加减分 + 多维收敛加分 + 割肉指标加分 = 融合总分

用法:
    from signals.core.fusion import fuse_scores
    fused = fuse_scores(scored_symbol, anomaly_profile)
"""
from dataclasses import dataclass
from typing import Optional

from config import FUSION_WEIGHTS


@dataclass
class FusedScore:
    """多维融合评分结果"""
    raw_czsc_score: float        # 原始缠论分数
    anomaly_boost: float         # 异常维度加减分
    convergence_bonus: float     # 多维收敛加分
    capitulation_bonus: float    # 割肉指标加分
    fused_total: float           # 融合后总分
    dimension_count: int         # 触发异常维度数
    confidence_level: str        # "高"(≥3维) / "中"(2维) / "低"(1维) / "无"
    detail: str                  # 融合明细


def fuse_scores(scored, anomaly=None, weights: dict = None) -> FusedScore:
    """
    将缠论评分和异常画像融合为综合评分。

    :param scored: ScoredSymbol (缠论评分结果)
    :param anomaly: AnomalyProfile (异常检测结果, 可为 None)
    :param weights: 融合权重, 默认 FUSION_WEIGHTS
    :return: FusedScore
    """
    w = weights or FUSION_WEIGHTS
    base = scored.total_score
    is_buy = scored.direction in ("偏多", "")

    anomaly_boost = 0.0
    convergence_bonus = 0.0
    capitulation_bonus = 0.0
    dimension_count = 0
    detail_parts = []

    if anomaly and anomaly.items:
        dimension_count = anomaly.anomaly_count
        vol_item = anomaly.items.get("volume")
        gap_item = anomaly.items.get("gap")
        range_item = anomaly.items.get("range")
        body_item = anomaly.items.get("body")

        # ── 异常加减分 ──
        if vol_item and vol_item.is_anomaly:
            if "放量" in vol_item.label:
                if is_buy:
                    boost = w.get("anomaly_volume_boost", 15)
                    anomaly_boost += boost
                    detail_parts.append(f"放量+{boost}")
                else:
                    penalty = abs(w.get("anomaly_volume_penalty", -10))
                    anomaly_boost -= penalty
                    detail_parts.append(f"放量卖-{penalty}")
            elif "缩量" in vol_item.label:
                if is_buy:
                    penalty = abs(w.get("anomaly_volume_penalty", -10))
                    anomaly_boost -= penalty
                    detail_parts.append(f"缩量买-{penalty}")

        if gap_item and gap_item.is_anomaly:
            gap_boost = w.get("anomaly_gap_boost", 10)
            if "高开" in gap_item.label and is_buy:
                anomaly_boost += gap_boost
                detail_parts.append(f"跳高+{gap_boost}")
            elif "低开" in gap_item.label and not is_buy:
                anomaly_boost += gap_boost
                detail_parts.append(f"跳低+{gap_boost}")
            elif "低开" in gap_item.label and is_buy:
                anomaly_boost -= gap_boost * 0.5
                detail_parts.append(f"跳低逆势-{gap_boost * 0.5:.0f}")

        if range_item and range_item.is_anomaly:
            range_boost = w.get("anomaly_range_boost", 5)
            anomaly_boost += range_boost if is_buy else -range_boost
            detail_parts.append(f"波动±{range_boost}")

        # ── 多维收敛加分 ──
        # 仅在有异常时计算
        if dimension_count >= 3:
            convergence_bonus = w.get("convergence_3dim", 20)
        elif dimension_count == 2:
            convergence_bonus = w.get("convergence_2dim", 12)
        elif dimension_count == 1:
            convergence_bonus = w.get("convergence_1dim", 5)

        if convergence_bonus > 0:
            detail_parts.append(f"收敛+{convergence_bonus:.0f}({dimension_count}维)")

        # ── 割肉指标加分 ──
        cap = anomaly.capitulation_score
        if cap >= 80:
            capitulation_bonus = w.get("capitulation_extreme", 25)
            detail_parts.append(f"割肉+{capitulation_bonus:.0f}(极度)")
        elif cap >= 60:
            capitulation_bonus = w.get("capitulation_high", 15)
            detail_parts.append(f"割肉+{capitulation_bonus:.0f}(恐慌)")
        elif cap >= 40:
            capitulation_bonus = w.get("capitulation_medium", 8)
            detail_parts.append(f"割肉+{capitulation_bonus:.0f}(偏弱)")

    fused_total = base + anomaly_boost + convergence_bonus + capitulation_bonus

    # 置信度分级
    if dimension_count >= 3:
        confidence_level = "高"
    elif dimension_count >= 2:
        confidence_level = "中"
    elif dimension_count >= 1:
        confidence_level = "低"
    else:
        confidence_level = "无"

    detail = " | ".join(detail_parts) if detail_parts else "纯缠论评分"

    return FusedScore(
        raw_czsc_score=round(base, 1),
        anomaly_boost=round(anomaly_boost, 1),
        convergence_bonus=round(convergence_bonus, 1),
        capitulation_bonus=round(capitulation_bonus, 1),
        fused_total=round(fused_total, 1),
        dimension_count=dimension_count,
        confidence_level=confidence_level,
        detail=detail,
    )
