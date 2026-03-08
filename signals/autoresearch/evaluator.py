# -*- coding: utf-8 -*-
"""
适应度评估器 — 基于权重-质量对齐度计算 fitness 标量。

核心洞察:
  BacktestReport 的 SQS 基于固定历史结果，不随权重变化。
  但「当前权重分配」与「信号类型实际质量」的对齐程度会变！

  Fitness = 权重对齐度 (50%) + 基线质量 (50%)

  权重对齐度:
    对每个有 SQS 的信号类型，计算:
      weight_share = abs(当前权重) / sum(所有权重)
      contribution = weight_share * SQS
    weighted_sqs = sum(contributions) → 越高说明权重分配越合理

  基线质量 (不随权重变化):
    overall_win_rate, profit_factor, expectancy
"""
from typing import Optional

from signals.core.backtest import SignalJournal, BacktestReport


def _norm(val: float, lo: float, hi: float) -> float:
    """线性归一化到 [0, 1]，超出范围截断。"""
    if hi <= lo:
        return 0.5
    return max(0.0, min(1.0, (val - lo) / (hi - lo)))


def _weighted_sqs(sqs: dict) -> float:
    """
    计算权重对齐的 SQS 分数。

    高 SQS 的信号类型权重越大 → 分数越高。
    """
    from signals.core.scorer import SIGNAL_WEIGHTS

    total_abs_weight = 0.0
    weighted_sum = 0.0

    for sig_type, score in sqs.items():
        w = abs(SIGNAL_WEIGHTS.get(sig_type, 0))
        if w == 0:
            continue
        total_abs_weight += w
        weighted_sum += w * score

    if total_abs_weight == 0:
        return sum(sqs.values()) / len(sqs) if sqs else 0

    return weighted_sum / total_abs_weight


def compute_fitness(journal: SignalJournal,
                    min_samples: int = 20) -> Optional[float]:
    """
    计算当前参数配置的 fitness 标量。

    Fitness = 0.50 * norm(weighted_sqs)   ← 随权重变化
            + 0.20 * norm(overall_wr)     ← 固定
            + 0.15 * norm(overall_pf)     ← 固定
            + 0.15 * norm(overall_exp)    ← 固定

    :param journal: 信号日志（需含已评估记录）
    :param min_samples: 最少样本数，不足则返回 None
    :return: fitness 分数 (0~100) 或 None
    """
    eval_records = journal.get_evaluated()
    if len(eval_records) < min_samples:
        return None

    report = BacktestReport(eval_records)
    sqs = report.signal_quality_score()
    if not sqs:
        return None

    # 权重对齐 SQS（随权重变化的核心指标）
    w_sqs = _weighted_sqs(sqs)

    # 基线统计（不随权重变化）
    by_type = report.by_signal_type()
    if not by_type:
        return None

    total_count = sum(s.count for s in by_type.values())
    if total_count == 0:
        return None

    overall_wr = sum(s.win_rate * s.count for s in by_type.values()) / total_count
    overall_pf = sum(s.profit_factor * s.count for s in by_type.values()) / total_count
    overall_exp = sum(s.expectancy * s.count for s in by_type.values()) / total_count

    fitness = 100.0 * (
        0.50 * _norm(w_sqs, 0, 80) +
        0.20 * _norm(overall_wr, 0, 80) +
        0.15 * _norm(min(overall_pf, 10), 0, 4.0) +
        0.15 * _norm(overall_exp, -5, 5)
    )

    return round(fitness, 4)


def compute_fitness_detail(journal: SignalJournal,
                           min_samples: int = 20) -> Optional[dict]:
    """
    计算 fitness 并返回详细分解（用于日志/调试）。
    """
    eval_records = journal.get_evaluated()
    if len(eval_records) < min_samples:
        return None

    report = BacktestReport(eval_records)
    sqs = report.signal_quality_score()
    if not sqs:
        return None

    w_sqs = _weighted_sqs(sqs)
    avg_sqs = sum(sqs.values()) / len(sqs)

    by_type = report.by_signal_type()
    if not by_type:
        return None

    total_count = sum(s.count for s in by_type.values())
    if total_count == 0:
        return None

    overall_wr = sum(s.win_rate * s.count for s in by_type.values()) / total_count
    overall_pf = sum(s.profit_factor * s.count for s in by_type.values()) / total_count
    overall_exp = sum(s.expectancy * s.count for s in by_type.values()) / total_count

    fitness = 100.0 * (
        0.50 * _norm(w_sqs, 0, 80) +
        0.20 * _norm(overall_wr, 0, 80) +
        0.15 * _norm(min(overall_pf, 10), 0, 4.0) +
        0.15 * _norm(overall_exp, -5, 5)
    )

    return {
        "fitness": round(fitness, 2),
        "weighted_sqs": round(w_sqs, 1),
        "avg_sqs": round(avg_sqs, 1),
        "win_rate": round(overall_wr, 1),
        "profit_factor": round(overall_pf, 2),
        "expectancy": round(overall_exp, 2),
        "sample_count": total_count,
    }
