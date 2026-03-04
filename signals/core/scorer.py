# -*- coding: utf-8 -*-
from dataclasses import dataclass
from typing import List

from .detectors import SignalEvent

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
}


@dataclass
class ScoredSymbol:
    symbol: str
    total_score: float
    signal_count: int
    signals: List[SignalEvent]
    details: str


def score_signals(symbol: str, signals: List[SignalEvent]) -> ScoredSymbol:
    """对单个标的的所有信号计算综合评分。"""
    if not signals:
        return ScoredSymbol(
            symbol=symbol, total_score=0.0,
            signal_count=0, signals=[], details="无信号",
        )

    total = 0.0
    for sig in signals:
        base = SIGNAL_WEIGHTS.get(sig.signal_type, 0)
        total += base * sig.confidence

    # 多级别共振加分：同方向信号出现在多个级别
    buy_freqs = {s.freq for s in signals if "买" in s.signal_type}
    sell_freqs = {s.freq for s in signals if "卖" in s.signal_type}
    if len(buy_freqs) > 1:
        total += 15
    if len(sell_freqs) > 1:
        total -= 15

    details_lines = [
        f"  [{s.freq}] {s.signal_type} conf={s.confidence:.2f} @ {s.price:.2f}  {s.details}"
        for s in signals
    ]
    if len(buy_freqs) > 1:
        details_lines.append(f"  [共振+15] 买信号出现在 {buy_freqs}")
    if len(sell_freqs) > 1:
        details_lines.append(f"  [共振-15] 卖信号出现在 {sell_freqs}")

    return ScoredSymbol(
        symbol=symbol,
        total_score=round(total, 1),
        signal_count=len(signals),
        signals=signals,
        details="\n".join(details_lines),
    )
