# -*- coding: utf-8 -*-
"""
IndexAnalyzer: 为单个指数维护三级 CZSC 实例，生成 IndexReport。
三级联动：日线（趋势背景）+ 30分钟（中枢结构）+ 15分钟（买卖点）

数据说明：
- A股指数：日线来自 AKShare stock_zh_index_daily；
           30min/15min 来自 AKShare stock_zh_a_minute（限近5日）
- 港股指数（HK.800700）：三个周期均由 Futu 直接提供
"""
from typing import List, Optional
from czsc import RawBar, Freq, Direction

from signals.core.analyzer import SymbolAnalyzer
from .index_report import IndexReport, ZSLevel


# ─────────────────────────────────────────────────────────
# 趋势判断辅助
# ─────────────────────────────────────────────────────────

def _determine_trend(analyzer: SymbolAnalyzer) -> str:
    """根据最近4笔高低点的序列判断趋势"""
    bis = analyzer.finished_bis
    if len(bis) < 4:
        return "结构未成型"
    last4 = bis[-4:]
    highs = [b.high for b in last4]
    lows  = [b.low  for b in last4]
    high_up = highs[-1] > highs[0]
    low_up  = lows[-1]  > lows[0]
    high_dn = highs[-1] < highs[0]
    low_dn  = lows[-1]  < lows[0]
    if high_up and low_up:
        return "上涨趋势"
    if high_dn and low_dn:
        return "下跌趋势"
    return "中枢震荡"


def _last_direction(analyzer: SymbolAnalyzer) -> str:
    """最后一笔方向"""
    bis = analyzer.finished_bis
    if not bis:
        return "未知"
    try:
        return "向上" if bis[-1].direction == Direction.Up else "向下"
    except Exception:
        return "未知"


def _find_latest_zs(analyzer: SymbolAnalyzer) -> Optional[ZSLevel]:
    """取最近5笔中 b1/b3 的重叠区间作为中枢，无重叠返回 None。"""
    bis = analyzer.finished_bis
    if len(bis) < 5:
        return None
    last5 = bis[-5:]
    b1, b3 = last5[0], last5[2]
    zd = max(b1.low,  b3.low)
    zg = min(b1.high, b3.high)
    if zg <= zd:
        return None
    return ZSLevel(zd=zd, zg=zg, bi_count=5)


def _detect_signal(analyzer: SymbolAnalyzer) -> str:
    """
    缠论买卖点检测（通用，适用于日线/30M/15M）：
    - 三买：最近一笔向上，低点 > 中枢上沿
    - 二买：最近一笔向上，前回调低点 > 更早低点
    - 背驰买：最近一笔向上，power_price 递减
    - 三卖：最近一笔向下，高点 < 中枢下沿
    - 背驰卖：最近一笔向下，power_price 递减
    """
    bis = analyzer.finished_bis
    if len(bis) < 3:
        return "无"

    last = bis[-1]
    zs = _find_latest_zs(analyzer)

    try:
        last_dir = last.direction

        if last_dir == Direction.Up:
            if zs and last.low > zs.zg:
                return "三买"
            if len(bis) >= 4:
                prev_dn  = bis[-2]
                pprev_dn = bis[-4] if len(bis) >= 4 else None
                if (pprev_dn and prev_dn.direction == Direction.Down
                        and prev_dn.low > pprev_dn.low):
                    return "二买"
            if len(bis) >= 3:
                prev_up = bis[-3] if bis[-3].direction == Direction.Up else None
                if (prev_up and hasattr(last, 'power_price')
                        and hasattr(prev_up, 'power_price')
                        and last.power_price < prev_up.power_price * 0.7):
                    return "背驰买"
        else:  # Direction.Down
            if zs and last.high < zs.zd:
                return "三卖"
            if len(bis) >= 3:
                prev_dn = bis[-3] if bis[-3].direction == Direction.Down else None
                if (prev_dn and hasattr(last, 'power_price')
                        and hasattr(prev_dn, 'power_price')
                        and last.power_price < prev_dn.power_price * 0.7):
                    return "背驰卖"
    except Exception:
        pass

    return "无"


def _build_for(analyzer: SymbolAnalyzer):
    """从 analyzer 提取 (trend, last_dir, signal, zs, bi_count)"""
    return (
        _determine_trend(analyzer),
        _last_direction(analyzer),
        _detect_signal(analyzer),
        _find_latest_zs(analyzer),
        len(analyzer.finished_bis),
    )


# ─────────────────────────────────────────────────────────
# IndexAnalyzer
# ─────────────────────────────────────────────────────────

class IndexAnalyzer:
    """
    为单个指数维护三级 CZSC 实例：
      日线  (Freq.D)   → 趋势背景
      30分钟 (Freq.F30) → 中枢结构
      15分钟 (Freq.F15) → 精确买卖点

    若某个周期数据为空，对应维度标记为"数据不足"，不影响其他周期正常分析。
    """

    def __init__(self, name: str, symbol: str,
                 daily_bars: List[RawBar],
                 f30_bars: Optional[List[RawBar]] = None,
                 f15_bars: Optional[List[RawBar]] = None):
        self.name   = name
        self.symbol = symbol

        if not daily_bars:
            self._available = False
            self._daily = self._f30 = self._f15 = None
            return

        self._available = True
        self._daily = SymbolAnalyzer(symbol, Freq.D,   daily_bars, max_bi_num=100)
        self._f30   = (SymbolAnalyzer(symbol, Freq.F30, f30_bars,  max_bi_num=50)
                       if f30_bars else None)
        self._f15   = (SymbolAnalyzer(symbol, Freq.F15, f15_bars,  max_bi_num=50)
                       if f15_bars else None)

    def report(self) -> IndexReport:
        if not self._available or self._daily is None:
            return IndexReport(name=self.name, symbol=self.symbol,
                               data_available=False)

        latest_price   = (self._daily.bars_raw[-1].close
                          if self._daily.bars_raw else 0.0)
        daily_last_dt  = (self._daily.bars_raw[-1].dt
                          if self._daily.bars_raw else None)
        f15_last_dt    = (self._f15.bars_raw[-1].dt
                          if self._f15 and self._f15.bars_raw else None)

        # 快照：优先小级别（15M > 30M > 日线）
        snapshot_price = latest_price
        snapshot_dt = daily_last_dt
        snapshot_freq = "日线"

        if self._f15 and self._f15.bars_raw:
            snapshot_price = self._f15.bars_raw[-1].close
            snapshot_dt = self._f15.bars_raw[-1].dt
            snapshot_freq = "15M"
        elif self._f30 and self._f30.bars_raw:
            snapshot_price = self._f30.bars_raw[-1].close
            snapshot_dt = self._f30.bars_raw[-1].dt
            snapshot_freq = "30M"

        # 盘中涨跌幅：snapshot vs 前一交易日收盘
        intraday_change = None
        if snapshot_freq != "日线" and self._daily.bars_raw:
            prev_close = self._daily.bars_raw[-1].close
            if prev_close > 0:
                intraday_change = round(
                    (snapshot_price / prev_close - 1) * 100, 2)

        # 均线关键位（日线以上，不做分钟线 MA）
        ma_ctx = None
        try:
            from signals.core.ma_levels import compute_ma_levels
            if self._daily and self._daily.bars_raw:
                ma_ctx = compute_ma_levels(self._daily.bars_raw, self.symbol)
        except Exception:
            pass

        # 日线
        d_trend, d_dir, d_sig, d_zs, d_cnt = _build_for(self._daily)

        # 30分钟
        if self._f30:
            f30_trend, f30_dir, f30_sig, f30_zs, f30_cnt = _build_for(self._f30)
        else:
            f30_trend = f30_dir = "数据不足"
            f30_sig, f30_zs, f30_cnt = "无", None, 0

        # 15分钟
        if self._f15:
            f15_trend, f15_dir, f15_sig, f15_zs, f15_cnt = _build_for(self._f15)
        else:
            f15_trend = f15_dir = "数据不足"
            f15_sig, f15_zs, f15_cnt = "无", None, 0

        return IndexReport(
            name=self.name, symbol=self.symbol,
            # 日线
            daily_bi_count=d_cnt,
            daily_last_direction=d_dir,
            daily_trend=d_trend,
            daily_latest_signal=d_sig,
            daily_zs=d_zs,
            # 30分钟
            f30_bi_count=f30_cnt,
            f30_last_direction=f30_dir,
            f30_trend=f30_trend,
            f30_latest_signal=f30_sig,
            f30_zs=f30_zs,
            # 15分钟
            f15_bi_count=f15_cnt,
            f15_last_direction=f15_dir,
            f15_trend=f15_trend,
            f15_latest_signal=f15_sig,
            f15_zs=f15_zs,
            # 价格与时间
            latest_price=latest_price,
            daily_last_dt=daily_last_dt,
            f15_last_dt=f15_last_dt,
            data_available=True,
            # 快照
            snapshot_price=snapshot_price,
            snapshot_dt=snapshot_dt,
            snapshot_freq=snapshot_freq,
            intraday_change=intraday_change,
            # 均线关键位
            ma_context=ma_ctx,
            # P3-2: 情景分叉
            scenario_branches=self._build_scenarios(ma_ctx),
            # P3-5: 近5日收益率
            recent_5d_return=self._calc_recent_return(5),
        )

    def _build_scenarios(self, ma_ctx) -> list:
        """P3-2: 构建情景分叉"""
        if ma_ctx is None:
            return []
        try:
            from signals.core.ma_levels import build_scenario_branches
            import config
            custom = config.CUSTOM_KEY_LEVELS.get(self.name, {})
            return build_scenario_branches(ma_ctx, custom_levels=custom or None)
        except Exception:
            return []

    def _calc_recent_return(self, days: int = 5) -> float:
        """计算近 N 个交易日收益率 %"""
        if not self._daily or not self._daily.bars_raw:
            return 0.0
        bars = self._daily.bars_raw
        if len(bars) < days + 1:
            return 0.0
        old_close = bars[-(days + 1)].close
        new_close = bars[-1].close
        if old_close <= 0:
            return 0.0
        return round((new_close / old_close - 1) * 100, 2)
