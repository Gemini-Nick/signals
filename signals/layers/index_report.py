# -*- coding: utf-8 -*-
"""
指数研判报告数据类（三级联动：日线趋势 + 30M中枢 + 15M买卖点）
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List


@dataclass
class ZSLevel:
    """中枢区间"""
    zd: float       # 中枢下沿
    zg: float       # 中枢上沿
    bi_count: int   # 构成笔数


@dataclass
class IndexReport:
    """
    单个指数的缠论三级研判结果：
    - 日线：趋势背景（上涨/下跌/震荡）
    - 30分钟：中枢结构
    - 15分钟：精确买卖点
    """
    name: str                           # 指数名称，如 "沪深300"
    symbol: str                         # 数据源代码，如 "sh000300"

    # ── 日线维度（趋势背景）────────────────────────────────
    daily_bi_count: int = 0
    daily_last_direction: str = "未知"   # "向上" / "向下"
    daily_trend: str = "未知"            # "上涨趋势" / "下跌趋势" / "中枢震荡"
    daily_latest_signal: str = "无"      # "三买" / "二买" / "背驰卖" 等
    daily_zs: Optional[ZSLevel] = None  # 最近一个日线中枢

    # ── 30分钟维度（中枢结构）────────────────────────────
    f30_bi_count: int = 0
    f30_last_direction: str = "未知"
    f30_trend: str = "未知"
    f30_latest_signal: str = "无"
    f30_zs: Optional[ZSLevel] = None

    # ── 15分钟维度（买卖点）──────────────────────────────
    f15_bi_count: int = 0
    f15_last_direction: str = "未知"
    f15_trend: str = "未知"
    f15_latest_signal: str = "无"
    f15_zs: Optional[ZSLevel] = None

    # ── 价格、时间与摘要 ──────────────────────────────────
    latest_price: float = 0.0
    daily_last_dt: Optional[datetime] = None   # 日线最后一根 K 线的日期
    f15_last_dt: Optional[datetime] = None     # 15M 最后一根 K 线的时间
    data_available: bool = True
    summary: str = ""

    # ── 均线关键位（P0-1）──────────────────────────────────
    ma_context: Optional[object] = None        # MAContext，日线以上均线关键位

    # ── P3-2: 情景分叉 ──────────────────────────────────
    scenario_branches: list = field(default_factory=list)   # List[ScenarioBranch]

    # ── P3-5: 近5日收益率 ─────────────────────────────────
    recent_5d_return: Optional[float] = None   # 近5个交易日收益率 %

    def __post_init__(self):
        if not self.summary and self.data_available:
            all_signals = [self.daily_latest_signal,
                           self.f30_latest_signal,
                           self.f15_latest_signal]
            buy_signals  = [s for s in all_signals if "买" in s]
            sell_signals = [s for s in all_signals if "卖" in s]
            trend_str = (f"日:{self.daily_trend} "
                         f"30M:{self.f30_trend} "
                         f"15M:{self.f15_trend}")
            if buy_signals:
                self.summary = f"{self.name} | {trend_str} | {'、'.join(buy_signals)}"
            elif sell_signals:
                self.summary = f"{self.name} | {trend_str} | {'、'.join(sell_signals)}"
            else:
                self.summary = f"{self.name} | {trend_str} | 无明确信号"
        elif not self.data_available:
            self.summary = f"{self.name} | 数据不可用"

    @property
    def has_buy_signal(self) -> bool:
        return any("买" in s for s in [self.daily_latest_signal,
                                        self.f30_latest_signal,
                                        self.f15_latest_signal])

    @property
    def has_sell_signal(self) -> bool:
        return any("卖" in s for s in [self.daily_latest_signal,
                                        self.f30_latest_signal,
                                        self.f15_latest_signal])

    @property
    def is_bullish(self) -> bool:
        """日线或30分钟处于上涨趋势"""
        return (self.daily_trend == "上涨趋势"
                or self.f30_trend == "上涨趋势")

    @property
    def three_level_aligned(self) -> bool:
        """三级共振：日线上涨 + 30M上涨/震荡 + 15M有买信号"""
        return (self.daily_trend == "上涨趋势"
                and self.f30_trend in ("上涨趋势", "中枢震荡")
                and "买" in self.f15_latest_signal)
