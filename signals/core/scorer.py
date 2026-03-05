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

    # 多级别共振加分：根据共振的级别组合差异化加分
    # 日线+30M 共振比 30M+15M 更有价值（大级别确认小级别）
    buy_freqs = {s.freq for s in signals if "买" in s.signal_type}
    sell_freqs = {s.freq for s in signals if "卖" in s.signal_type}
    buy_bonus = _resonance_bonus(buy_freqs)
    sell_bonus = _resonance_bonus(sell_freqs)
    total += buy_bonus
    total -= sell_bonus

    details_lines = [
        f"  [{s.freq}] {s.signal_type} conf={s.confidence:.2f} @ {s.price:.2f}  {s.details}"
        for s in signals
    ]
    if buy_bonus > 0:
        details_lines.append(f"  [共振+{buy_bonus}] 买信号出现在 {buy_freqs}")
    if sell_bonus > 0:
        details_lines.append(f"  [共振-{sell_bonus}] 卖信号出现在 {sell_freqs}")

    return ScoredSymbol(
        symbol=symbol,
        total_score=round(total, 1),
        signal_count=len(signals),
        signals=signals,
        details="\n".join(details_lines),
    )
