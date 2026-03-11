# -*- coding: utf-8 -*-
"""
信号回放引擎 — 逐根K线回放信号变化，生成时间线（出现/消失）

用于复盘时为个股增加"过程维度"：
  - 信号何时首次出现？
  - 信号在哪根K线消失？
  - 最终保留了哪些信号？
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from czsc import Freq, RawBar

from signals.core.analyzer import SymbolAnalyzer
from signals.core.detectors import SignalEvent, detect_all_signals


@dataclass
class SignalChange:
    """一次信号变化事件"""
    dt: datetime
    symbol: str
    freq: str           # "日线" / "30分钟"
    signal_type: str    # "一买" / "二买" / "三买" / "背驰买" 等
    action: str         # "appear" / "disappear"
    price: float
    confidence: float
    bar_index: int      # 第几根K线时发生


class SignalReplayer:
    """逐根K线回放，跟踪信号出现/消失"""

    def __init__(self, symbol: str, freq: Freq, warmup: int = 30):
        """
        :param symbol: 标的代码
        :param freq: K线级别
        :param warmup: 热身期根数，前 N 根只建立结构不检测信号
        """
        self._symbol = symbol
        self._freq = freq
        self._analyzer: Optional[SymbolAnalyzer] = None
        self._prev_signals: Dict[str, SignalEvent] = {}  # signal_type → SignalEvent
        self._timeline: List[SignalChange] = []
        self._bar_count = 0
        self._warmup = warmup

    def feed_bar(self, bar: RawBar):
        """喂入一根K线，检测信号变化"""
        self._bar_count += 1

        if self._analyzer is None:
            self._analyzer = SymbolAnalyzer(self._symbol, self._freq, [bar])
        else:
            self._analyzer.update(bar)

        if self._bar_count < self._warmup:
            return  # 热身期不检测

        # 检测当前所有信号
        try:
            current_signals = detect_all_signals(self._analyzer.czsc, self._symbol)
        except Exception:
            return  # 检测失败不影响回放

        current_map = {s.signal_type: s for s in current_signals}

        # 新出现的信号
        for sig_type, sig in current_map.items():
            if sig_type not in self._prev_signals:
                self._timeline.append(SignalChange(
                    dt=bar.dt, symbol=self._symbol, freq=sig.freq,
                    signal_type=sig_type, action="appear",
                    price=bar.close, confidence=sig.confidence,
                    bar_index=self._bar_count,
                ))

        # 消失的信号
        for sig_type, prev_sig in self._prev_signals.items():
            if sig_type not in current_map:
                self._timeline.append(SignalChange(
                    dt=bar.dt, symbol=self._symbol, freq=prev_sig.freq,
                    signal_type=sig_type, action="disappear",
                    price=bar.close, confidence=0.0,
                    bar_index=self._bar_count,
                ))

        self._prev_signals = current_map

    @property
    def timeline(self) -> List[SignalChange]:
        return self._timeline

    @property
    def final_signals(self) -> List[SignalEvent]:
        return list(self._prev_signals.values())


def replay_stock(symbol: str, bars: List[RawBar],
                 freq: Freq = Freq.D, warmup: int = 30) -> List[SignalChange]:
    """
    一键回放：传入完整K线列表，返回信号时间线。

    :param symbol: 标的代码
    :param bars: 完整K线列表（按时间升序）
    :param freq: K线级别
    :param warmup: 热身期根数
    :return: 信号变化事件列表
    """
    replayer = SignalReplayer(symbol, freq, warmup=warmup)
    for bar in bars:
        replayer.feed_bar(bar)
    return replayer.timeline
