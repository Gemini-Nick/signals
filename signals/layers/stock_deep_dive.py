# -*- coding: utf-8 -*-
"""
个股深度分析模块 — 缠论+均线+量价 三维度综合报告。

用法:
    dive = StockDeepDive("SZ.002759")
    print(dive.to_text())

包含:
  1. 多级别缠论结构（日线/30M/15M 笔+中枢+信号）
  2. 均线维度（MA 趋势+支撑阻力）
  3. 量价分析（缩放量趋势+量价配合度）
  4. 历史高低点支撑阻力
  5. 完全分类（3 个互斥情景）
  6. 综合评分+风控+操作建议
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Tuple

from czsc import Freq, Direction

from signals.core.analyzer import SymbolAnalyzer
from signals.core.detectors import detect_all_signals, SignalEvent
from signals.core.scorer import score_signals, ScoredSymbol
from signals.core.ma_levels import compute_ma_levels, MAContext
from signals.core.risk import (
    compute_risk_for_signal, RiskInfo,
    compute_layered_position, LayeredPosition,
    format_layered_position,
)
from signals.data.fetcher import AKShareSource, detect_market


# ─────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────

@dataclass
class VolumeProfile:
    """量价分析结果。"""
    avg_20: float               # 20日均量
    recent_volumes: List[float] # 近5日成交量
    trend: str                  # "缩量" / "温和缩量" / "温和放量" / "放量" / "巨量"
    ratio: float                # 量比 (当日/20日均)
    price_vol_match: str        # "量价齐升" / "放量下跌" / "缩量企稳" / "缩量上涨"
    detail: str                 # 可读描述


@dataclass
class PivotPoint:
    """历史关键高低点。"""
    dt: datetime
    price: float
    pivot_type: str             # "high" / "low"
    significance: float         # 振幅百分比
    role: str                   # "支撑" / "阻力" / "已突破" / "已跌破"


@dataclass
class Scenario:
    """完全分类单个情景。"""
    name: str                   # "看多（向上延伸）" / "看空（向下延伸）" / "震荡（中枢运行）"
    trigger: str                # 触发条件
    probability_hint: str       # "偏高" / "中等" / "偏低"
    action: str                 # 操作建议
    target_prices: List[float]  # 目标/止损价位
    rationale: str              # 依据


@dataclass
class TimeframeAnalysis:
    """单级别分析汇总。"""
    freq: str                   # "日线" / "30分钟" / "15分钟"
    bi_count: int
    last_direction: str         # "向上" / "向下"
    trend: str                  # "上涨趋势" / "下跌趋势" / "中枢震荡"
    signals: List[SignalEvent]
    zs_range: Optional[Tuple[float, float]]  # (zd, zg) if exists
    latest_bis: list            # 最近N笔信息 [(sdt, edt, dir, low, high, power), ...]


# ─────────────────────────────────────────────────────────
# 主类
# ─────────────────────────────────────────────────────────

class StockDeepDive:
    """
    单只股票深度分析。

    初始化即自动完成: 数据获取 → CZSC分析 → 量价 → 支撑阻力 → 完全分类。

    用法::

        dive = StockDeepDive("SZ.002759")
        print(dive.to_text())
    """

    def __init__(self, symbol: str, daily_lookback: int = 300):
        self.symbol = symbol
        self.market = detect_market(symbol)
        self._ak = AKShareSource()

        # 原始数据
        self.daily_bars = []
        self.f30_bars = []
        self.f15_bars = []

        # 分析结果
        self.tf_analyses: Dict[str, TimeframeAnalysis] = {}
        self.all_signals: List[SignalEvent] = []
        self.ma_context: Optional[MAContext] = None
        self.scored: Optional[ScoredSymbol] = None
        self.risk_info: Optional[RiskInfo] = None
        self.layered_pos: Optional[LayeredPosition] = None
        self.volume: Optional[VolumeProfile] = None
        self.pivots: List[PivotPoint] = []
        self.scenarios: List[Scenario] = []

        self._errors: List[str] = []
        self._data_sources: Dict[str, str] = {}
        self._daily_vol_scale = 1  # 东财返回"手"需×100转"股"

        # 执行
        self._fetch_data(daily_lookback)
        self._run_analysis()

    # ─────────────────────────────────────────────────────
    # 数据获取（含降级）
    # ─────────────────────────────────────────────────────

    def _fetch_data(self, lookback: int):
        now = datetime.now()
        sdt = (now - timedelta(days=lookback)).strftime("%Y-%m-%d")
        edt = now.strftime("%Y-%m-%d")

        self.daily_bars = self._fetch_daily(sdt, edt)
        self.f30_bars = self._fetch_minute(Freq.F30)
        self.f15_bars = self._fetch_minute(Freq.F15)

    def _fetch_daily(self, sdt: str, edt: str):
        """日线数据获取，根据市场类型路由，含降级链。"""
        if self.market == "A":
            return self._fetch_daily_a(sdt, edt)
        elif self.market == "HK":
            return self._fetch_daily_hk(sdt)
        elif self.market == "US":
            return self._fetch_daily_us(sdt)
        else:
            self._errors.append("不支持的市场类型: {}".format(self.market))
            return []

    def _fetch_daily_a(self, sdt: str, edt: str):
        """A股日线: 东财 → Sina 降级。"""
        try:
            bars = self._ak.get_a_daily(self.symbol, sdt, edt)
            if bars:
                self._data_sources["日线"] = "东财"
                self._daily_vol_scale = 100  # 东财成交量单位是"手"，×100转"股"
                return bars
        except Exception as e:
            self._errors.append("日线(东财)失败: {}".format(e.__class__.__name__))

        try:
            bars = self._ak.get_a_daily_sina(self.symbol, sdt, edt)
            if bars:
                self._data_sources["日线"] = "Sina(降级)"
                self._daily_vol_scale = 1  # Sina成交量单位是"股"
                return bars
        except Exception as e:
            self._errors.append("日线(Sina)也失败: {}".format(e.__class__.__name__))

        return []

    def _fetch_daily_hk(self, sdt: str):
        """港股日线: AKShare 全量历史 → 按日期截取。"""
        try:
            bars = self._ak.get_hk_daily(self.symbol)
            if bars:
                cutoff = datetime.strptime(sdt, "%Y-%m-%d")
                bars = [b for b in bars if b.dt >= cutoff]
                self._data_sources["日线"] = "AKShare(港股)"
                self._daily_vol_scale = 1
                return bars
        except Exception as e:
            self._errors.append("港股日线失败: {}".format(e.__class__.__name__))
        return []

    def _fetch_daily_us(self, sdt: str):
        """美股日线: AKShare 全量历史 → 按日期截取。"""
        try:
            bars = self._ak.get_us_daily(self.symbol)
            if bars:
                cutoff = datetime.strptime(sdt, "%Y-%m-%d")
                bars = [b for b in bars if b.dt >= cutoff]
                self._data_sources["日线"] = "AKShare(美股)"
                self._daily_vol_scale = 1
                return bars
        except Exception as e:
            self._errors.append("美股日线失败: {}".format(e.__class__.__name__))
        return []

    def _fetch_minute(self, freq: Freq):
        """分钟线: A股 Sina→东财降级，港美股暂不支持。
        只有当所有数据源都失败时才报错给用户，中间降级不展示。"""
        import logging
        _log = logging.getLogger(__name__)
        label = freq.value
        if self.market != "A":
            self._errors.append("{}线暂不支持{}市场".format(label, self.market))
            return []

        try:
            bars = self._ak.get_a_minute(self.symbol, freq)
            if bars:
                self._data_sources[label] = "Sina"
                return bars
        except Exception as e:
            _log.debug("%s(Sina)失败: %s — %s", label, e.__class__.__name__, e)

        try:
            bars = self._ak.get_a_minute_em(self.symbol, freq, max_retries=2)
            if bars:
                self._data_sources[label] = "东财(降级)"
                return bars
        except Exception as e:
            _log.debug("%s(东财)也失败: %s — %s", label, e.__class__.__name__, e)

        # 所有数据源都失败，才报错给用户
        self._errors.append("{}数据暂不可用".format(label))
        return []

    # ─────────────────────────────────────────────────────
    # CZSC 分析 + 评分 + 风控
    # ─────────────────────────────────────────────────────

    def _run_analysis(self):
        if not self.daily_bars:
            self._errors.append("无日线数据，无法分析")
            return

        from signals.layers.index_analyzer import (
            _determine_trend, _last_direction, _find_latest_zs,
        )

        self.all_signals = []

        for freq, bars, label in [
            (Freq.D,   self.daily_bars, "日线"),
            (Freq.F30, self.f30_bars,   "30分钟"),
            (Freq.F15, self.f15_bars,   "15分钟"),
        ]:
            if not bars:
                continue
            analyzer = SymbolAnalyzer(self.symbol, freq, bars)
            signals = detect_all_signals(analyzer.czsc, self.symbol)
            self.all_signals.extend(signals)

            trend = _determine_trend(analyzer)
            last_dir = _last_direction(analyzer)
            zs = _find_latest_zs(analyzer)

            # 提取最近笔信息
            bis = analyzer.finished_bis
            latest_bis = []
            for bi in bis[-8:]:
                try:
                    d = "↑" if bi.direction == Direction.Up else "↓"
                except Exception:
                    d = "?"
                latest_bis.append((
                    bi.sdt.strftime("%m-%d"),
                    bi.edt.strftime("%m-%d"),
                    d, bi.low, bi.high, bi.power_price,
                ))

            self.tf_analyses[label] = TimeframeAnalysis(
                freq=label,
                bi_count=len(bis),
                last_direction=last_dir,
                trend=trend,
                signals=signals,
                zs_range=(zs.zd, zs.zg) if zs else None,
                latest_bis=latest_bis,
            )

        # 均线
        self.ma_context = compute_ma_levels(self.daily_bars, self.symbol)

        # 量价分析（需要在评分之前，量比用于量价确认加减分）
        self.volume = self._analyze_volume(
            self.daily_bars, vol_scale=self._daily_vol_scale)
        vol_ratio = self.volume.ratio if self.volume else 0.0

        # 综合评分（含量价确认）
        self.scored = score_signals(
            self.symbol, self.all_signals,
            enable_decay=True, ma_context=self.ma_context,
            volume_ratio=vol_ratio,
        )

        # 异常检测 + 信号融合
        self.anomaly = None
        self.fused = None
        try:
            from signals.core.anomaly import compute_anomaly_profile
            from signals.core.fusion import fuse_scores
            self.anomaly = compute_anomaly_profile(self.symbol, self.daily_bars)
            if self.scored and self.anomaly:
                self.fused = fuse_scores(self.scored, self.anomaly)
                self.scored.anomaly_profile = self.anomaly
                self.scored.fused_score = self.fused
                self.scored.fused_total = self.fused.fused_total
        except Exception:
            pass  # 异常检测失败不影响主流程

        # 风控
        buy_sigs = [s for s in self.all_signals if "买" in s.signal_type]
        if buy_sigs and self.scored:
            best = max(buy_sigs, key=lambda s: s.confidence)
            self.risk_info = compute_risk_for_signal(best)
            self.layered_pos = compute_layered_position(
                self.scored, self.ma_context,
            )

        # 关键高低点
        self.pivots = self._find_pivot_points(self.daily_bars)

        # 完全分类
        self.scenarios = self._classify_scenarios()

    # ─────────────────────────────────────────────────────
    # 量价分析
    # ─────────────────────────────────────────────────────

    @staticmethod
    def _analyze_volume(bars, lookback: int = 20,
                        vol_scale: int = 1) -> Optional[VolumeProfile]:
        """量价分析：趋势 + 量比 + 量价配合度。vol_scale: 东财手→股需×100。"""
        if len(bars) < lookback:
            return None

        recent = bars[-lookback:]
        vols = [b.vol * vol_scale for b in recent]
        avg_20 = sum(vols) / len(vols)

        last_5_vols = [b.vol * vol_scale for b in bars[-5:]]
        last_vol = bars[-1].vol * vol_scale
        ratio = last_vol / avg_20 if avg_20 > 0 else 0

        # 趋势: 近5日均量 vs 前15日均量
        avg_5 = sum(last_5_vols) / 5 if last_5_vols else 0
        avg_prior = sum(vols[:-5]) / max(len(vols) - 5, 1)
        vol_change = avg_5 / avg_prior if avg_prior > 0 else 1.0

        if vol_change < 0.6:
            trend = "缩量"
        elif vol_change < 0.9:
            trend = "温和缩量"
        elif vol_change < 1.3:
            trend = "温和放量"
        elif vol_change < 2.0:
            trend = "放量"
        else:
            trend = "巨量"

        # 量价配合
        price_up = bars[-1].close > bars[-2].close if len(bars) >= 2 else False
        price_down = bars[-1].close < bars[-2].close if len(bars) >= 2 else False
        vol_up = ratio > 1.2
        vol_down = ratio < 0.8

        if price_up and vol_up:
            match = "量价齐升"
        elif price_down and vol_up:
            match = "放量下跌"
        elif price_down and vol_down:
            match = "缩量企稳"
        elif price_up and vol_down:
            match = "缩量上涨"
        else:
            match = "无明显特征"

        # 自适应单位：亿股(>1亿) / 万股(<1亿)
        avg_display = avg_20 / 1e8
        if avg_display >= 1.0:
            vol_str = "{:.2f}亿股".format(avg_display)
        else:
            vol_str = "{:.0f}万股".format(avg_20 / 10000)

        detail = ("20日均量 {}，近5日{}(变化 {:.0%})，"
                  "量比 {:.2f}，{}").format(
                      vol_str, trend, vol_change, ratio, match)

        return VolumeProfile(
            avg_20=avg_20,
            recent_volumes=last_5_vols,
            trend=trend,
            ratio=ratio,
            price_vol_match=match,
            detail=detail,
        )

    # ─────────────────────────────────────────────────────
    # 历史高低点支撑阻力
    # ─────────────────────────────────────────────────────

    @staticmethod
    def _find_pivot_points(bars, window: int = 5,
                           min_significance: float = 3.0) -> List[PivotPoint]:
        """
        检测历史关键高低点。

        算法: bar[i] 的 high 是 [i-window, i+window] 内最大值则为局部高点。
        过滤: 邻域振幅 > min_significance%。
        去重: 3% 价格范围内只保留最显著的。
        """
        if len(bars) < window * 2 + 1:
            return []

        raw_pivots = []
        current_price = bars[-1].close

        for i in range(window, len(bars) - window):
            bar = bars[i]
            neighborhood = bars[i - window: i + window + 1]
            max_high = max(b.high for b in neighborhood)
            min_low = min(b.low for b in neighborhood)

            # 局部高点
            if bar.high == max_high and bar.high != bar.low:
                amplitude = (bar.high - min_low) / min_low * 100 if min_low > 0 else 0
                if amplitude >= min_significance:
                    role = "阻力" if bar.high > current_price else "已突破"
                    raw_pivots.append(PivotPoint(
                        dt=bar.dt, price=bar.high,
                        pivot_type="high", significance=amplitude,
                        role=role,
                    ))

            # 局部低点
            if bar.low == min_low and bar.high != bar.low:
                amplitude = (max_high - bar.low) / bar.low * 100 if bar.low > 0 else 0
                if amplitude >= min_significance:
                    role = "支撑" if bar.low < current_price else "已跌破"
                    raw_pivots.append(PivotPoint(
                        dt=bar.dt, price=bar.low,
                        pivot_type="low", significance=amplitude,
                        role=role,
                    ))

        # 去重: 3%范围内保留 significance 最大的
        pivots = _dedup_pivots(raw_pivots, merge_pct=3.0)

        # 按时间降序，取最近10个
        pivots.sort(key=lambda p: p.dt, reverse=True)
        return pivots[:10]

    # ─────────────────────────────────────────────────────
    # 完全分类
    # ─────────────────────────────────────────────────────

    def _classify_scenarios(self) -> List[Scenario]:
        """生成 3 个互斥场景: 看多 / 看空 / 震荡。"""
        daily_tf = self.tf_analyses.get("日线")
        if not daily_tf:
            return [Scenario(
                name="数据不足", trigger="等待日线数据",
                probability_hint="--", action="观望",
                target_prices=[], rationale="无日线数据",
            )]

        current_price = self.daily_bars[-1].close
        ma_trend = self.ma_context.trend_summary if self.ma_context else "未知"
        vol_trend = self.volume.trend if self.volume else "未知"
        vol_match = self.volume.price_vol_match if self.volume else ""

        nearest_sup = self._find_nearest_level("支撑", current_price)
        nearest_res = self._find_nearest_level("阻力", current_price)

        scenarios = []

        # ── 场景1: 看多 ──────────────────────────────────
        bull_evidence = []
        bull_prob = "中等"

        if daily_tf.trend == "上涨趋势":
            bull_evidence.append("日线上涨趋势")
            bull_prob = "偏高"
        if ma_trend == "多头排列":
            bull_evidence.append("均线多头")
            bull_prob = "偏高"
        buy_sigs = [s.signal_type for s in daily_tf.signals if "买" in s.signal_type]
        if buy_sigs:
            bull_evidence.append("信号:" + ",".join(buy_sigs))
        if vol_trend in ("温和放量", "放量") and vol_match == "量价齐升":
            bull_evidence.append("量价配合")
        # 30M/15M 共振
        for label in ["30分钟", "15分钟"]:
            tf = self.tf_analyses.get(label)
            if tf:
                sub_buys = [s.signal_type for s in tf.signals if "买" in s.signal_type]
                if sub_buys:
                    bull_evidence.append("{}:{}".format(label, ",".join(sub_buys)))

        bull_targets = []
        if nearest_res:
            bull_targets.append(nearest_res)
        if daily_tf.zs_range and daily_tf.zs_range[1] > current_price:
            bull_targets.append(daily_tf.zs_range[1])
        # 若阻力位不足，取近期高点
        if not bull_targets:
            recent_highs = [p.price for p in self.pivots
                            if p.pivot_type == "high" and p.price > current_price]
            if recent_highs:
                bull_targets.append(min(recent_highs))

        if daily_tf.trend == "下跌趋势" and ma_trend == "空头排列":
            bull_prob = "偏低"

        trigger = ("，".join(bull_evidence) if bull_evidence
                   else "价格站稳 {:.2f}".format(nearest_sup) if nearest_sup
                   else "放量突破近期高点")

        scenarios.append(Scenario(
            name="看多（向上延伸）",
            trigger=trigger,
            probability_hint=bull_prob,
            action="逢回调买入" if bull_prob == "偏高" else "等待突破确认后买入",
            target_prices=bull_targets[:2],
            rationale="；".join(bull_evidence) if bull_evidence else "综合结构判断",
        ))

        # ── 场景2: 看空 ──────────────────────────────────
        bear_evidence = []
        bear_prob = "中等"

        if daily_tf.trend == "下跌趋势":
            bear_evidence.append("日线下跌趋势")
            bear_prob = "偏高"
        if ma_trend == "空头排列":
            bear_evidence.append("均线空头压制")
            bear_prob = "偏高"
        sell_sigs = [s.signal_type for s in daily_tf.signals if "卖" in s.signal_type]
        if sell_sigs:
            bear_evidence.append("信号:" + ",".join(sell_sigs))
        if vol_match == "放量下跌":
            bear_evidence.append("放量杀跌")

        bear_targets = []
        if nearest_sup:
            bear_targets.append(nearest_sup)
        if daily_tf.zs_range and daily_tf.zs_range[0] < current_price:
            bear_targets.append(daily_tf.zs_range[0])
        # 若支撑不足，取近期低点
        if not bear_targets:
            recent_lows = [p.price for p in self.pivots
                           if p.pivot_type == "low" and p.price < current_price]
            if recent_lows:
                bear_targets.append(max(recent_lows))

        if daily_tf.trend == "上涨趋势" and ma_trend == "多头排列":
            bear_prob = "偏低"

        trigger = ("，".join(bear_evidence) if bear_evidence
                   else "跌破 {:.2f}".format(nearest_sup) if nearest_sup
                   else "放量跌破近期低点")

        scenarios.append(Scenario(
            name="看空（向下延伸）",
            trigger=trigger,
            probability_hint=bear_prob,
            action="减仓或观望" if bear_prob == "偏高" else "设止损防守",
            target_prices=bear_targets[:2],
            rationale="；".join(bear_evidence) if bear_evidence else "综合结构判断",
        ))

        # ── 场景3: 震荡 ──────────────────────────────────
        neutral_targets = []
        neutral_evidence = []
        if daily_tf.zs_range:
            neutral_targets = [daily_tf.zs_range[0], daily_tf.zs_range[1]]
            neutral_evidence.append("中枢[{:.1f}~{:.1f}]".format(
                daily_tf.zs_range[0], daily_tf.zs_range[1]))
        elif nearest_sup and nearest_res:
            neutral_targets = [nearest_sup, nearest_res]
        if ma_trend == "交织":
            neutral_evidence.append("均线交织")
        if daily_tf.trend == "中枢震荡":
            neutral_evidence.append("结构震荡")

        scenarios.append(Scenario(
            name="震荡（中枢内运行）",
            trigger="价格在支撑-阻力区间内波动，无方向性突破",
            probability_hint="中等",
            action="区间内高抛低吸，突破方向跟随",
            target_prices=neutral_targets[:2],
            rationale="；".join(neutral_evidence) if neutral_evidence else "综合结构判断",
        ))

        return scenarios

    def _find_nearest_level(self, role: str, price: float) -> Optional[float]:
        """找最近的支撑/阻力位（pivot + MA 合并），并过滤方向。"""
        candidates = []
        for p in self.pivots:
            if p.role == role:
                candidates.append(p.price)
        if self.ma_context:
            if role == "支撑":
                candidates.extend(lv.value for lv in self.ma_context.support_levels[:3])
            elif role == "阻力":
                candidates.extend(lv.value for lv in self.ma_context.resistance_levels[:3])
        # 过滤方向：支撑必须在价格下方，阻力必须在价格上方
        if role == "支撑":
            candidates = [c for c in candidates if c < price]
        elif role == "阻力":
            candidates = [c for c in candidates if c > price]
        if not candidates:
            return None
        return min(candidates, key=lambda x: abs(x - price))

    # ─────────────────────────────────────────────────────
    # 报告生成
    # ─────────────────────────────────────────────────────

    def to_text(self) -> str:
        """生成终端友好的8段式文本报告。"""
        SEP = "═" * 56
        THIN = "─" * 56
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        lines = []
        lines.append("")
        lines.append(SEP)
        lines.append("  个股深度分析  {}  {}".format(self.symbol, now))
        lines.append(SEP)

        # ── 1. 基本面貌 ────────────────────────────────
        if self.daily_bars:
            last = self.daily_bars[-1]
            prev = self.daily_bars[-2] if len(self.daily_bars) > 1 else last
            chg = (last.close - prev.close) / prev.close * 100

            # 近期高低
            recent_90 = self.daily_bars[-min(60, len(self.daily_bars)):]
            hi = max(b.high for b in recent_90)
            lo = min(b.low for b in recent_90)
            drawdown = (1 - last.close / hi) * 100

            lines.append("")
            lines.append("  ▸ 基本面貌")
            lines.append("    当前价: {:.2f}  涨跌: {:+.2f}%  日期: {}".format(
                last.close, chg, last.dt.strftime("%Y-%m-%d")))
            dd_str = "-{:.1f}%".format(drawdown) if drawdown > 0.05 else "持平"
            lines.append("    近60日: 最高 {:.2f}  最低 {:.2f}  距高点 {}".format(
                hi, lo, dd_str))
            src_str = "  ".join("{}:{}".format(k, v)
                                for k, v in self._data_sources.items())
            lines.append("    数据源: {}".format(src_str))
        else:
            lines.append("")
            lines.append("  ▸ 基本面貌: 无数据")

        # ── 2. 缠论维度 ────────────────────────────────
        lines.append("")
        lines.append("  ▸ 缠论维度（多级别笔结构）")
        lines.append("    {:<8} {:>4} {:<4} {:<8} {:<18} {}".format(
            "级别", "笔数", "方向", "趋势", "中枢", "信号"))
        lines.append("    " + THIN[:50])

        for label in ["日线", "30分钟", "15分钟"]:
            tf = self.tf_analyses.get(label)
            if not tf:
                lines.append("    {:<8} 数据不足".format(label))
                continue
            zs_str = ("[{:.2f}~{:.2f}]".format(tf.zs_range[0], tf.zs_range[1])
                      if tf.zs_range else "─")
            sig_str = ", ".join(s.signal_type for s in tf.signals) or "无"
            lines.append("    {:<8} {:>4} {:<4} {:<8} {:<18} {}".format(
                label, tf.bi_count, tf.last_direction,
                tf.trend, zs_str, sig_str))

        # 近期笔结构（日线）
        daily_tf = self.tf_analyses.get("日线")
        if daily_tf and daily_tf.latest_bis:
            lines.append("")
            lines.append("    日线近期笔:")
            for sdt, edt, d, lo, hi, pwr in daily_tf.latest_bis[-6:]:
                lines.append("      {}→{} {} {:.2f}~{:.2f}  力度={:.2f}".format(
                    sdt, edt, d, lo, hi, pwr))

        # ── 3. 均线维度 ────────────────────────────────
        if self.ma_context:
            lines.append("")
            lines.append("  ▸ 均线维度  趋势: {}".format(
                self.ma_context.trend_summary))
            for lv in self.ma_context.levels:
                arrow = {"上方": "▲", "下方": "▼", "贴合": "◆"}.get(
                    lv.position, "?")
                lines.append("    {} {:<8} {:.2f}  距离 {:+.1f}%".format(
                    arrow, lv.name, lv.value, lv.distance_pct))

        # ── 4. 量价分析 ────────────────────────────────
        if self.volume:
            lines.append("")
            lines.append("  ▸ 量价分析")
            lines.append("    {}".format(self.volume.detail))

        # ── 5. 关键支撑阻力 ────────────────────────────
        if self.pivots:
            lines.append("")
            lines.append("  ▸ 关键支撑阻力（历史高低点）")
            supports = [p for p in self.pivots if p.role == "支撑"][:4]
            resistances = [p for p in self.pivots if p.role == "阻力"][:4]
            broken = [p for p in self.pivots if p.role == "已跌破"][:2]
            if supports:
                sup_str = "  ".join("{:.2f}({})".format(
                    p.price, p.dt.strftime("%m-%d")) for p in supports)
                lines.append("    ▲ 支撑: {}".format(sup_str))
            if resistances:
                res_str = "  ".join("{:.2f}({})".format(
                    p.price, p.dt.strftime("%m-%d")) for p in resistances)
                lines.append("    ▼ 阻力: {}".format(res_str))
            if broken:
                brk_str = "  ".join("{:.2f}({})".format(
                    p.price, p.dt.strftime("%m-%d")) for p in broken)
                lines.append("    ✕ 已跌破: {}".format(brk_str))

        # ── 6. 综合评分 ────────────────────────────────
        lines.append("")
        lines.append("  ▸ 综合评分")
        if self.scored:
            lines.append("    技术分: {:.1f}  方向: {}  信号数: {}".format(
                self.scored.total_score, self.scored.direction,
                self.scored.signal_count))
            if self.scored.ma_confirmation:
                lines.append("    均线确认: {}".format(
                    self.scored.ma_confirmation))
            if self.scored.details:
                for dl in self.scored.details.strip().split("\n"):
                    if dl.strip():
                        lines.append("    {}".format(dl.strip()))
            if self.risk_info:
                lines.append("    止损: {:.2f} ({:+.1f}%, {})  仓位: {:.1f}%".format(
                    self.risk_info.stop_loss, self.risk_info.stop_loss_pct,
                    self.risk_info.stop_source, self.risk_info.position_pct))
            if self.layered_pos:
                lines.append("    " + format_layered_position(self.layered_pos))
        else:
            lines.append("    无信号，评分 0")

        # ── 7. 完全分类 ────────────────────────────────
        if self.scenarios:
            lines.append("")
            lines.append("  ▸ 完全分类（三个场景）")
            lines.append("    " + THIN[:50])
            for i, sc in enumerate(self.scenarios, 1):
                lines.append("    场景{}: {}  概率: {}".format(
                    i, sc.name, sc.probability_hint))
                lines.append("      触发: {}".format(sc.trigger))
                lines.append("      操作: {}".format(sc.action))
                if sc.target_prices:
                    tp_str = " / ".join("{:.2f}".format(p)
                                        for p in sc.target_prices)
                    lines.append("      目标: {}".format(tp_str))
                lines.append("      依据: {}".format(sc.rationale))

        # ── 8. 操作建议 ────────────────────────────────
        lines.append("")
        lines.append("  ▸ 操作建议")
        lines.append("    {}".format(self._build_action_summary()))

        # 数据问题
        if self._errors:
            lines.append("")
            lines.append("  ⚠ 数据获取问题:")
            for err in self._errors[:5]:
                lines.append("    - {}".format(err))

        lines.append(SEP)
        return "\n".join(lines)

    def _build_action_summary(self) -> str:
        """一句话操作建议。"""
        if not self.scored:
            return "数据不足，无法给出建议"

        score = self.scored.total_score
        direction = self.scored.direction

        if direction == "偏多" and score >= 60:
            action = "积极关注，回调买入"
        elif direction == "偏多" and score >= 30:
            action = "关注中，等待信号加强"
        elif direction == "偏多":
            action = "弱偏多信号，轻仓试探或观望"
        elif direction == "偏空":
            action = "回避或减仓，等待底部结构完成"
        elif direction == "分歧":
            action = "观望，等待方向明确"
        else:
            action = "暂无明确方向，不预测只跟随"

        if self.risk_info:
            action += "  止损参考: {:.2f}".format(self.risk_info.stop_loss)

        return action


# ─────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────

def _dedup_pivots(pivots: List[PivotPoint],
                  merge_pct: float = 3.0) -> List[PivotPoint]:
    """去重: merge_pct% 价格范围内只保留 significance 最大的。"""
    if not pivots:
        return []
    # 按价格排序
    sorted_p = sorted(pivots, key=lambda p: p.price)
    result = []
    i = 0
    while i < len(sorted_p):
        group = [sorted_p[i]]
        j = i + 1
        while j < len(sorted_p):
            diff = (sorted_p[j].price - sorted_p[i].price) / sorted_p[i].price * 100
            if diff <= merge_pct:
                group.append(sorted_p[j])
                j += 1
            else:
                break
        # 保留 significance 最大的
        best = max(group, key=lambda p: p.significance)
        result.append(best)
        i = j
    return result
