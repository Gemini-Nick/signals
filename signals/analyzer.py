# -*- coding: utf-8 -*-
from typing import List, Optional
from czsc import CZSC, RawBar, Freq


class SymbolAnalyzer:
    """管理单个 (标的, 级别) 的 CZSC 实例，支持历史初始化和增量更新。"""

    def __init__(self, symbol: str, freq: Freq, bars: List[RawBar], max_bi_num: int = 50):
        """
        :param symbol: Futu 格式代码，如 "SH.601958"
        :param freq: czsc.Freq 枚举
        :param bars: 历史 RawBar 列表（建议 100+ 根）
        :param max_bi_num: 保留的最大笔数
        """
        self.symbol = symbol
        self.freq = freq
        self.czsc = CZSC(bars, max_bi_num=max_bi_num)
        self._last_dt = bars[-1].dt if bars else None

    def update(self, bar: RawBar):
        """增量喂入新 K 线，自动过滤重复 bar。"""
        if self._last_dt and bar.dt <= self._last_dt:
            return
        self.czsc.update(bar)
        self._last_dt = bar.dt

    def update_many(self, bars: List[RawBar]):
        """批量更新，只处理比当前更新的 bar。"""
        for bar in bars:
            self.update(bar)

    @property
    def bi_list(self):
        return self.czsc.bi_list

    @property
    def finished_bis(self):
        return self.czsc.finished_bis

    @property
    def fx_list(self):
        return self.czsc.fx_list

    @property
    def bars_raw(self):
        return self.czsc.bars_raw
