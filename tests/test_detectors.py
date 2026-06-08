# -*- coding: utf-8 -*-
from dataclasses import dataclass
from datetime import datetime, timedelta

from czsc import Direction


@dataclass
class _Fx:
    fx: float


@dataclass
class _Bi:
    direction: Direction
    low: float
    high: float
    sdt: datetime
    edt: datetime

    @property
    def fx_a(self) -> _Fx:
        return _Fx(self.low if self.direction == Direction.Up else self.high)


class _Freq:
    value = "日线"


class _Czsc:
    freq = _Freq()

    def __init__(self, bis: list[_Bi]):
        self.finished_bis = bis


def _bi(index: int, direction: Direction, low: float, high: float) -> _Bi:
    start = datetime(2026, 1, 1) + timedelta(days=index * 2)
    return _Bi(direction=direction, low=low, high=high, sdt=start, edt=start + timedelta(days=1))


def test_second_buy_covers_higher_low_pullback_sequence():
    from signals.core.detectors import _detect_second_bs

    czsc = _Czsc([
        _bi(0, Direction.Down, 10, 18),
        _bi(1, Direction.Up, 10, 16),
        _bi(2, Direction.Down, 12, 17),
        _bi(3, Direction.Up, 12, 19),
        _bi(4, Direction.Down, 14, 18),
    ])

    signals = _detect_second_bs(czsc, "SZ.000001")

    assert any(signal.signal_type == "二买" for signal in signals)


def test_second_sell_covers_lower_high_rebound_sequence():
    from signals.core.detectors import _detect_second_bs

    czsc = _Czsc([
        _bi(0, Direction.Up, 12, 30),
        _bi(1, Direction.Down, 20, 30),
        _bi(2, Direction.Up, 14, 28),
        _bi(3, Direction.Down, 18, 28),
        _bi(4, Direction.Up, 16, 25),
    ])

    signals = _detect_second_bs(czsc, "SZ.000001")

    assert any(signal.signal_type == "二卖" for signal in signals)


def test_third_buy_requires_leave_and_pullback_above_zhongshu():
    from signals.core.detectors import _detect_third_bs

    czsc = _Czsc([
        _bi(0, Direction.Down, 10, 20),
        _bi(1, Direction.Up, 10, 18),
        _bi(2, Direction.Down, 12, 19),
        _bi(3, Direction.Up, 18, 24),
        _bi(4, Direction.Down, 20, 23),
    ])

    signals = _detect_third_bs(czsc, "SZ.000001")

    assert any(signal.signal_type == "三买" for signal in signals)


def test_third_sell_requires_leave_and_rebound_below_zhongshu():
    from signals.core.detectors import _detect_third_bs

    czsc = _Czsc([
        _bi(0, Direction.Up, 20, 30),
        _bi(1, Direction.Down, 22, 30),
        _bi(2, Direction.Up, 21, 28),
        _bi(3, Direction.Down, 16, 22),
        _bi(4, Direction.Up, 17, 20),
    ])

    signals = _detect_third_bs(czsc, "SZ.000001")

    assert any(signal.signal_type == "三卖" for signal in signals)
