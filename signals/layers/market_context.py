# -*- coding: utf-8 -*-
"""
MarketContext: 聚合指数的 IndexReport，形成大市研判结论。
- overall_direction: "偏多" / "偏空" / "分化"
- growth_vs_value: 成长 vs 价值相对强弱
- gate_industry_scan: 是否建议进行行业扫描
- sentiment_phase: 情绪周期（恐慌/修复/亢奋/回落）
- divergence_score: 大小盘分化度（>0 避险，<0 风险偏好）
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional
from .index_report import IndexReport


# ─────────────────────────────────────────────────────
# 情绪周期
# ─────────────────────────────────────────────────────

class SentimentPhase(Enum):
    PANIC    = "恐慌"
    REPAIR   = "修复"
    EUPHORIA = "亢奋"
    COOLING  = "回落"
    UNKNOWN  = "未知"


_SENTIMENT_EMOJI = {
    SentimentPhase.PANIC:    "🔴",
    SentimentPhase.REPAIR:   "🟡",
    SentimentPhase.EUPHORIA: "🟢",
    SentimentPhase.COOLING:  "🟠",
    SentimentPhase.UNKNOWN:  "⚪",
}

# 防守型指数 vs 进攻型指数（A股子集，用于分化度计算）
_SHIELD_INDICES = {"上证50", "超大盘"}
_SWORD_INDICES  = {"创业板指", "科创50", "中证1000"}

# 仓位建议映射（含逆向情绪提示）
_POSITION_MAP = {
    ("偏空", "恐慌"):  "半仓防守🛡+半仓现金💰 等待恐慌释放后埋伏 ⟵ 逆向：恐慌=底部信号，准备抄底清单",
    ("偏空", "修复"):  "卖出埋伏仓位获利 → 加仓防守🛡（跷跷板切换）",
    ("偏空", "回落"):  "减持半仓防守 → 半仓防守+半仓现金",
    ("偏空", "亢奋"):  "减仓进攻⚔（反弹非反转），保留防守🛡底仓 ⟵ 逆向：偏空+亢奋=反弹卖点",
    ("偏多", "亢奋"):  "减仓进攻⚔ → 增持防守🛡对冲（防大跌） ⟵ 逆向：亢奋=离场信号，切勿追高",
    ("偏多", "修复"):  "维持进攻⚔为主 + 关注超跌反弹方向",
    ("偏多", "恐慌"):  "恐慌即机会 → 逢低加仓进攻⚔ ⟵ 逆向：偏多+恐慌=黄金坑",
    ("偏多", "回落"):  "正常回调 → 持仓等待，不追高",
    ("分化", "恐慌"):  "防守🛡为主+少量现金💰，等方向明确 ⟵ 逆向：恐慌中观察谁先企稳",
    ("分化", "修复"):  "均衡配置 → 关注跷跷板轮动",
    ("分化", "亢奋"):  "均衡配置 → 适度减仓锁利 ⟵ 逆向：亢奋分化=轮动加速",
    ("分化", "回落"):  "均衡配置 → 关注防守🛡进攻⚔跷跷板轮动",
}


def calc_divergence(reports: List[IndexReport]) -> float:
    """
    大小盘分化度 = 防守指数得分 - 进攻指数得分。
    > 0: 资金流向防守（避险模式）
    < 0: 资金流向进攻（风险偏好上升）
    ≈ 0: 均衡
    """
    def _score_group(names: set) -> float:
        total = 0.0
        for r in reports:
            if r.name in names and r.data_available:
                if r.daily_trend == "上涨趋势":
                    total += 2
                elif r.daily_trend == "下跌趋势":
                    total -= 2
                if r.has_buy_signal:
                    total += 1
                if r.has_sell_signal:
                    total -= 1
        return total

    return _score_group(_SHIELD_INDICES) - _score_group(_SWORD_INDICES)


def detect_sentiment_phase(
    divergence: float,
    zt_total: int = 0,
    dt_total: int = 0,
    lianban_max: int = 0,
    bank_avg_gain: float = 0.0,
) -> SentimentPhase:
    """
    情绪周期识别（三维融合：分化度 + 涨跌停 + 指标股）。

    :param divergence:     大小盘分化度（>0 避险，<0 风险偏好）
    :param zt_total:       全市场涨停数
    :param dt_total:       全市场跌停数
    :param lianban_max:    最高连板数
    :param bank_avg_gain:  四大行平均涨幅%
    """
    dt_ratio = dt_total / max(zt_total, 1)

    # 恐慌：跌停远多于涨停 + 指数普跌
    if dt_ratio > 1.5 and divergence < 0:
        return SentimentPhase.PANIC

    # 亢奋：连板高度高 + 跌停少 + 涨停多
    if lianban_max >= 5 and dt_ratio < 0.2 and zt_total > 20:
        return SentimentPhase.EUPHORIA

    # 修复：跌停减少 + 防守资金外流（四大行跌）+ 小盘反弹
    if divergence < -2 and bank_avg_gain < 0 and zt_total > dt_total:
        return SentimentPhase.REPAIR

    # 回落：防守强（四大行涨）+ 进攻弱 + 赚钱效应下降
    if divergence > 2 and bank_avg_gain > 0.5:
        return SentimentPhase.COOLING

    # 无明确涨跌停数据时，仅靠分化度判断
    if zt_total == 0 and dt_total == 0:
        if divergence > 2:
            return SentimentPhase.COOLING
        elif divergence < -2:
            return SentimentPhase.REPAIR
        return SentimentPhase.UNKNOWN

    # 默认
    if dt_ratio > 0.8:
        return SentimentPhase.COOLING
    return SentimentPhase.REPAIR


def get_position_suggestion(direction: str, phase: SentimentPhase) -> str:
    """根据大市方向+情绪周期返回仓位建议。"""
    return _POSITION_MAP.get(
        (direction, phase.value),
        "均衡配置 → 关注市场变化"
    )


# ─────────────────────────────────────────────────────────
# P3-5: 高低风格切换检测
# ─────────────────────────────────────────────────────────

@dataclass
class StyleSwitch:
    """高低风格切换检测结果"""
    detected: bool = False
    direction: str = ""         # "低切高" / "高切低"
    evidence: str = ""          # 证据描述
    confidence: str = ""        # "强" / "中" / "弱"
    suggestion: str = ""        # 操作建议


# 高位指数（大盘价值）vs 低位指数（小盘成长）
_HIGH_INDICES = {"上证50", "沪深300", "超大盘"}
_LOW_INDICES = {"科创50", "中证1000", "创业板指"}


def detect_style_switch(reports: List["IndexReport"]) -> Optional[StyleSwitch]:
    """
    检测高低风格切换：
    - 低切高：低位指数（小盘/成长）连跌后反弹，高位指数同时走弱
    - 高切低：高位指数（大盘/价值）走弱，资金流向防守/大盘

    需要 IndexReport 有 recent_5d_return 字段。
    """
    high_reports = [r for r in reports if r.name in _HIGH_INDICES and r.data_available]
    low_reports = [r for r in reports if r.name in _LOW_INDICES and r.data_available]

    if not high_reports or not low_reports:
        return None

    # 获取5日收益率（如果 IndexReport 有此字段）
    def _get_5d_return(r):
        return getattr(r, 'recent_5d_return', None)

    high_returns = [_get_5d_return(r) for r in high_reports if _get_5d_return(r) is not None]
    low_returns = [_get_5d_return(r) for r in low_reports if _get_5d_return(r) is not None]

    if not high_returns or not low_returns:
        return None

    avg_high = sum(high_returns) / len(high_returns)
    avg_low = sum(low_returns) / len(low_returns)

    # 今日涨跌（用 daily_trend 和最新价判断当日表现）
    high_today_bullish = sum(1 for r in high_reports if r.daily_trend == "上涨趋势")
    low_today_bullish = sum(1 for r in low_reports if r.daily_trend == "上涨趋势")

    # 低切高：低位指数5日连跌(avg_low < -3%) + 今日反弹(low_today_bullish > 0) + 高位指数走弱
    if avg_low < -3.0 and low_today_bullish >= 1 and avg_high > avg_low + 2:
        # 找出反弹最强的低位指数
        best_low = max(low_reports, key=lambda r: _get_5d_return(r) or -999)
        best_low_name = best_low.name
        best_low_ret = _get_5d_return(best_low) or 0

        # 找出最弱的高位指数
        worst_high = min(high_reports, key=lambda r: _get_5d_return(r) or 999)

        evidence = f"{best_low_name}近5日{best_low_ret:+.1f}%后企稳反弹"
        confidence = "强" if avg_low < -5.0 else "中"
        suggestion = f"关注超跌{best_low_name}相关板块反弹机会"

        return StyleSwitch(
            detected=True,
            direction="低切高",
            evidence=evidence,
            confidence=confidence,
            suggestion=suggestion,
        )

    # 高切低：高位指数连涨(avg_high > 3%) + 今日走弱 + 低位补涨
    if avg_high > 3.0 and high_today_bullish == 0 and low_today_bullish >= 1:
        evidence = f"大盘价值股近5日+{avg_high:.1f}%后走弱,小盘成长补涨"
        confidence = "强" if avg_high > 5.0 else "中"
        suggestion = "大盘价值股获利了结,关注小盘成长补涨"

        return StyleSwitch(
            detected=True,
            direction="高切低",
            evidence=evidence,
            confidence=confidence,
            suggestion=suggestion,
        )

    return None


# ─────────────────────────────────────────────────────────
# 数据类
# ─────────────────────────────────────────────────────────

@dataclass
class MarketContext:
    """
    指数聚合后的大市研判结果。
    """
    reports: List[IndexReport]             # 所有指数报告
    overall_direction: str = "分化"        # "偏多" / "偏空" / "分化"
    direction_strength: float = 0.0        # [-1.0, +1.0] 方向强度连续值
    structural_divergence: str = ""        # "大强小弱" / "小强大弱" / ""
    buy_indices: List[str] = field(default_factory=list)   # 出现买信号的指数名称
    sell_indices: List[str] = field(default_factory=list)  # 出现卖信号的指数名称
    bullish_indices: List[str] = field(default_factory=list)  # 上涨趋势指数
    bearish_indices: List[str] = field(default_factory=list)  # 下跌趋势指数
    growth_vs_value: str = "均衡"          # "成长" / "价值" / "均衡"
    recommended_style: str = "均衡"        # 建议风格
    recommended_industries: List[str] = field(default_factory=list)  # 推断的强势板块
    gate_industry_scan: bool = True        # 偏多或中性才进行行业扫描
    summary: str = ""                      # 2-3 行综合判断
    # 情绪周期相关
    sentiment_phase: str = "未知"          # 恐慌/修复/亢奋/回落
    divergence_score: float = 0.0          # 大小盘分化度（>0避险，<0风险偏好）
    position_suggestion: str = ""          # 仓位建议
    shield_sectors: List[str] = field(default_factory=list)   # 防守板块
    sword_sectors: List[str] = field(default_factory=list)    # 进攻板块
    # 轮动阶段
    rotation_stage: str = ""              # "科技领涨" / "顺周期领涨" / "消费领涨" / "混沌"
    rotation_detail: str = ""             # 分布详情
    allocation_suggestion: str = ""       # "配置建议（科技领涨+修复）: 科技40% | ..."
    # 盘中恐慌评估
    panic_score: float = 0.0             # 恐慌指数 (0-100)
    panic_level: str = ""                # "恐慌"/"偏弱"/"正常"
    panic_detail: str = ""               # 恐慌分项明细
    bottom_candidates: List[str] = field(default_factory=list)  # 抄底候选板块名
    theme_summary: str = ""              # 主题追踪摘要
    # P3-5: 风格切换
    style_switch: Optional[StyleSwitch] = None
    # P3-4: 轮动持续时间和速度（从 RotationStage 传入）
    rotation_duration: int = 0
    rotation_velocity: str = ""
    rotation_peak_warning: bool = False
    rotation_peak_detail: str = ""

    # ─────────────────────────────────────────────────────
    # 情绪周期更新（L2 数据可用后调用）
    # ─────────────────────────────────────────────────────

    def update_sentiment(
        self,
        zt_total: int = 0,
        dt_total: int = 0,
        lianban_max: int = 0,
        bank_avg_gain: float = 0.0,
        shield_sectors: List[str] = None,
        sword_sectors: List[str] = None,
    ):
        """
        L2 数据加载完成后，用涨跌停/连板/指标股数据更新情绪周期。
        L1 阶段 build_market_context 已基于分化度做了初步判断，
        此方法用更丰富的数据重新计算。
        """
        self.divergence_score = calc_divergence(self.reports)
        phase = detect_sentiment_phase(
            divergence=self.divergence_score,
            zt_total=zt_total,
            dt_total=dt_total,
            lianban_max=lianban_max,
            bank_avg_gain=bank_avg_gain,
        )
        self.sentiment_phase = phase.value
        self.position_suggestion = get_position_suggestion(
            self.overall_direction, phase)
        if shield_sectors is not None:
            self.shield_sectors = shield_sectors
        if sword_sectors is not None:
            self.sword_sectors = sword_sectors

    # ─────────────────────────────────────────────────────
    # P3-7: 决策简报
    # ─────────────────────────────────────────────────────

    def build_decision_brief(
        self,
        rhythm_alerts: list = None,
        analog_ref: dict = None,
    ) -> dict:
        """
        构建决策简报结构化 dict（P3-7）。

        :param rhythm_alerts: P3-3 衰竭/休整的板块列表 [{name, phase, score, hint}]
        :param analog_ref: P3-6 缓存的历史匹配结果 dict
        :return: 决策简报 dict
        """
        from datetime import datetime

        # 关键情景分叉（来自 P3-2，从 reports 中提取 urgency=="接近" 的）
        key_scenarios = []
        for r in self.reports:
            if not r.data_available:
                continue
            branches = getattr(r, 'scenario_branches', None) or []
            for b in branches:
                if b.urgency == "接近":
                    key_scenarios.append({
                        "index_name": r.name,
                        "current_price": round(r.latest_price, 2) if r.latest_price else 0,
                        "level_name": b.level_name,
                        "level_price": b.level_price,
                        "distance_pct": b.distance_pct,
                        "is_support": b.is_support,
                        "hold": b.hold,
                        "break": b.break_,
                    })

        # 风格切换
        style_data = None
        if self.style_switch and self.style_switch.detected:
            style_data = {
                "detected": True,
                "direction": self.style_switch.direction,
                "evidence": self.style_switch.evidence,
                "confidence": self.style_switch.confidence,
                "suggestion": self.style_switch.suggestion,
            }

        # 轮动状态
        rotation_status = {
            "stage": self.rotation_stage,
            "duration": self.rotation_duration,
            "velocity": self.rotation_velocity,
            "peak_warning": self.rotation_peak_warning,
            "peak_detail": self.rotation_peak_detail,
        }

        # 自动生成操作建议（最多3条）
        action_items = self._generate_action_items(
            rhythm_alerts=rhythm_alerts or [],
            analog_ref=analog_ref,
        )

        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "direction": self.overall_direction,
            "sentiment": self.sentiment_phase,
            "direction_strength": self.direction_strength,
            "position_suggestion": self.position_suggestion,
            "key_scenarios": key_scenarios,
            "style_switch": style_data,
            "rotation_status": rotation_status,
            "rhythm_alerts": rhythm_alerts or [],
            "analog_ref": analog_ref,
            "action_items": action_items,
        }

    def _generate_action_items(
        self,
        rhythm_alerts: list,
        analog_ref: dict = None,
    ) -> list:
        """自动生成操作建议（最多3条）"""
        items = []

        # 1. 板块节奏兑现提醒
        exhaust_names = [
            a for a in rhythm_alerts
            if a.get("phase") in ("衰竭", "休整")
        ]
        if exhaust_names:
            names = [f"{a['name']}({a['phase']}{a.get('score', 0):.0f})"
                     for a in exhaust_names[:2]]
            items.append(f"{'、'.join(names)} 减持兑现")

        # 2. 轮动峰值警告
        if self.rotation_peak_warning and self.rotation_peak_detail:
            items.append(self.rotation_peak_detail)

        # 3. 风格切换建议
        if self.style_switch and self.style_switch.detected:
            items.append(self.style_switch.suggestion)

        # 4. 超跌+恐慌=抄底
        if self.sentiment_phase == "恐慌" and self.bottom_candidates:
            candidates = "、".join(self.bottom_candidates[:3])
            items.append(f"超跌关注: {candidates} (恐慌释放后分批)")

        # 5. 历史匹配参考
        if analog_ref and isinstance(analog_ref, dict):
            results = analog_ref.get("results", {})
            for idx_name, analogs in results.items():
                if analogs and len(analogs) > 0:
                    best = analogs[0]
                    sim = best.get("similarity", 0)
                    ret30 = best.get("next_30d_return", 0)
                    if sim > 0.8:
                        items.append(
                            f"历史参考: 与{best.get('match_end', '')}相似{sim:.0%},"
                            f"后30日{ret30:+.1f}%"
                        )
                    break

        return items[:3]

    # ─────────────────────────────────────────────────────
    # 格式化工具（内部）
    # ─────────────────────────────────────────────────────

    @staticmethod
    def _trend_arrow(trend: str) -> str:
        """趋势 → 单字符箭头"""
        return {"上涨趋势": "↑", "下跌趋势": "↓", "中枢震荡": "→",
                "结构未成型": "?", "数据不足": "-", "未知": "-"}.get(trend, "?")

    @staticmethod
    def _fmt_signals(r: "IndexReport") -> str:
        """
        信号列：精简展示，合并同类信号
        例：二买(日+30M)  背驰卖(15M)  三级共振
        """
        sigs = []
        for label, sig in [("日", r.daily_latest_signal),
                            ("30", r.f30_latest_signal),
                            ("15", r.f15_latest_signal)]:
            if sig != "无":
                sigs.append((label, sig))

        if not sigs:
            return "无信号"

        # 按信号类型分组
        groups: dict = {}
        for label, sig in sigs:
            groups.setdefault(sig, []).append(label)

        parts = []
        for sig, labels in groups.items():
            if len(labels) >= 3:
                parts.append(f"{sig}★三级")
            elif len(labels) == 2:
                parts.append(f"{sig}({'+'.join(labels)})")
            else:
                parts.append(f"{sig}({labels[0]})")
        return "  ".join(parts)

    @staticmethod
    def _fmt_zs(r: "IndexReport") -> str:
        """中枢：优先 30M，否则日线"""
        zs = r.f30_zs or r.daily_zs
        return f"[{zs.zd:.0f}~{zs.zg:.0f}]" if zs else ""

    def _fmt_price(self, r: "IndexReport") -> str:
        return f"{r.latest_price:.2f}" if r.latest_price else ""

    def _index_lines(self) -> List[str]:
        """每行：名称 | 日↑ 30→ 15↓ | 信号 | 中枢 | 价格 + 关键价位"""
        lines = []
        for r in self.reports:
            if not r.data_available:
                lines.append(f"  {'─' * 4} {r.name}  数据不可用")
                continue
            name  = r.name.ljust(5)
            trend = (f"{self._trend_arrow(r.daily_trend)}"
                     f"{self._trend_arrow(r.f30_trend)}"
                     f"{self._trend_arrow(r.f15_trend)}")
            sigs  = self._fmt_signals(r)
            zs    = self._fmt_zs(r)
            price = self._fmt_price(r)
            lines.append(f"  {name}  {trend}  {sigs:<22}  {zs:<14}  {price}")
            # 均线关键位（第二行）
            if r.ma_context:
                try:
                    from signals.core.ma_levels import format_key_levels
                    kl = format_key_levels(r.ma_context)
                    if kl:
                        lines.append(f"  {'':5}{kl}")
                except Exception:
                    pass
        return lines

    # ─────────────────────────────────────────────────────
    # 终端输出
    # ─────────────────────────────────────────────────────

    def print_report(self):
        """打印到终端"""
        text = self.to_text()
        print(text)

    # ─────────────────────────────────────────────────────
    # 文本格式（终端 + 飞书通用）
    # ─────────────────────────────────────────────────────

    def to_text(self) -> str:
        """
        生成可阅读文本，同时适用于终端打印和飞书发送。
        格式示例：
        ════════════════════════════════════════
          📊 大盘研判  2026-03-03 17:18
        ════════════════════════════════════════
          指数     趋势        主要信号
          ─────────────────────────────────────
          上证50   ↑↓↑  二买(日+15)      [3077~3080]  3047
          沪深300  →↑↑  背驰卖(日) 二买(30+15)  [4704~4726]  4729
          科创50   ↑↑↑  二买(30+15)★三级  [1453~1486]  1465
          ...
        ────────────────────────────────────────
          综合: 偏多  |  风格: 成长
          ⭐ 三级共振: 科创50
          🔔 买点: 上证50、沪深300 等7只
          📌 推荐关注: 新能源、金融、白酒消费
        ════════════════════════════════════════
        """
        from datetime import datetime
        now_dt = datetime.now()
        now = now_dt.strftime("%Y-%m-%d %H:%M")
        today_str = now_dt.strftime("%Y-%m-%d")

        # 数据截止时间（取可用指数中最新的一根）
        avail = [r for r in self.reports if r.data_available]
        daily_dts = [r.daily_last_dt for r in avail if r.daily_last_dt]
        f15_dts   = [r.f15_last_dt   for r in avail if r.f15_last_dt]
        daily_cutoff = max(daily_dts).strftime("%Y-%m-%d") if daily_dts else "未知"
        f15_cutoff   = max(f15_dts).strftime("%Y-%m-%d %H:%M") if f15_dts else "未知"

        # 数据新鲜度检查：盘后(15:30+)日线应已更新到今天
        daily_stale = False
        minute_stale = False
        if daily_dts and daily_cutoff < today_str and now_dt.hour >= 16:
            daily_stale = True
        if f15_dts:
            f15_date = max(f15_dts).strftime("%Y-%m-%d")
            if f15_date < today_str and now_dt.hour >= 16:
                minute_stale = True

        lines: List[str] = []
        SEP = "═" * 42
        lines.append("\n" + SEP)
        lines.append(f"  📊 大盘研判  {now}")
        lines.append(f"  日线截至: {daily_cutoff}  |  分钟线截至: {f15_cutoff}")

        # 数据滞后警告（日线源：新浪，通常收盘后 17:00~18:00 更新）
        if daily_stale or minute_stale:
            stale_parts = []
            if daily_stale:
                stale_parts.append(f"日线({daily_cutoff})")
            if minute_stale:
                stale_parts.append(f"分钟线({max(f15_dts).strftime('%Y-%m-%d')})")
            lines.append(f"  ⚠️  数据尚未更新到今日: {', '.join(stale_parts)}")
            lines.append(f"  ⚠️  以下分析基于滞后数据，仅供参考（日线源通常 17:00~18:00 更新）")

        lines.append(f"  日↑↓→  30M↑↓→  15M↑↓→  |  信号(日/30M/15M)")
        lines.append("─" * 42)
        lines.extend(self._index_lines())
        lines.append("─" * 42)

        # 综合结论 + 情绪周期
        dir_emoji = {"偏多": "📈", "偏空": "📉", "分化": "↔️"}.get(
            self.overall_direction, "")
        strength_str = f"强度 {self.direction_strength:+.2f}" if self.direction_strength else ""
        phase_emoji = _SENTIMENT_EMOJI.get(
            SentimentPhase(self.sentiment_phase), "⚪") if self.sentiment_phase != "未知" else ""
        phase_str = f"  |  {phase_emoji}{self.sentiment_phase}" if self.sentiment_phase != "未知" else ""
        div_str = f"  |  分化度: {self.divergence_score:+.1f}" if self.divergence_score != 0 else ""
        lines.append(f"  {dir_emoji} 综合: {self.overall_direction} ({strength_str})  |  风格: {self.growth_vs_value}{phase_str}{div_str}")

        # 结构性分化
        if self.structural_divergence:
            lines.append(f"  🔀 结构: {self.structural_divergence}")

        # 仓位建议
        if self.position_suggestion:
            lines.append(f"  💡 {self.position_suggestion}")

        # 轮动阶段 + 配置建议
        if self.rotation_stage:
            lines.append(f"  🔄 {self.rotation_detail}" if self.rotation_detail
                         else f"  🔄 轮动: {self.rotation_stage}")
        if self.allocation_suggestion:
            lines.append(f"  📦 {self.allocation_suggestion}")

        # 攻防板块
        if self.shield_sectors or self.sword_sectors:
            parts = []
            if self.shield_sectors:
                parts.append(f"🛡防守: {'、'.join(self.shield_sectors[:3])}")
            if self.sword_sectors:
                parts.append(f"⚔进攻: {'、'.join(self.sword_sectors[:3])}")
            lines.append(f"  {' | '.join(parts)}")

        # 盘中情绪评估（始终显示）
        if self.panic_level:
            p_icons = {"恐慌": "🔴", "偏弱": "🟡", "正常": "🟢"}
            p_icon = p_icons.get(self.panic_level, "⚪")
            lines.append(f"  {p_icon} 盘中情绪: {self.panic_score:.0f}/100 ({self.panic_level})")
            if self.panic_detail:
                lines.append(f"     {self.panic_detail}")
            if self.bottom_candidates:
                lines.append(f"  🎯 抄底候选: {'、'.join(self.bottom_candidates[:5])}")
        if self.theme_summary:
            lines.append(f"  🏷 {self.theme_summary}")

        # 三级共振（最强信号）
        aligned = [r.name for r in self.reports
                   if r.data_available and r.three_level_aligned]
        if aligned:
            lines.append(f"  ⭐ 三级共振: {'  '.join(aligned)}")

        if not self.gate_industry_scan:
            lines.append("  ⛔ 市场偏空，建议观望")

        lines.append(SEP)
        return "\n".join(lines)

    def to_feishu_text(self) -> str:
        """
        飞书纯文本格式（去掉 emoji 和 box-drawing 字符，改用 ASCII 分隔符，
        确保在飞书不同客户端上显示一致）。
        """
        from datetime import datetime
        now_dt = datetime.now()
        now = now_dt.strftime("%Y-%m-%d %H:%M")
        today_str = now_dt.strftime("%Y-%m-%d")

        avail = [r for r in self.reports if r.data_available]
        daily_dts = [r.daily_last_dt for r in avail if r.daily_last_dt]
        f15_dts   = [r.f15_last_dt   for r in avail if r.f15_last_dt]
        daily_cutoff = max(daily_dts).strftime("%Y-%m-%d") if daily_dts else "未知"
        f15_cutoff   = max(f15_dts).strftime("%Y-%m-%d %H:%M") if f15_dts else "未知"

        lines: List[str] = []
        lines.append(f"【大盘研判 {now}】")
        lines.append(f"日线截至: {daily_cutoff}  分钟线截至: {f15_cutoff}")

        # 数据滞后警告
        stale_parts = []
        if daily_dts and daily_cutoff < today_str and now_dt.hour >= 16:
            stale_parts.append(f"日线({daily_cutoff})")
        if f15_dts:
            f15_date = max(f15_dts).strftime("%Y-%m-%d")
            if f15_date < today_str and now_dt.hour >= 16:
                stale_parts.append(f"分钟线({f15_date})")
        if stale_parts:
            lines.append(f"[!] 数据尚未更新到今日: {', '.join(stale_parts)}，分析基于滞后数据（日线源通常 17:00~18:00 更新）")
        strength_str = f"强度{self.direction_strength:+.2f}" if self.direction_strength else ""
        struct_str = f"  结构:{self.structural_divergence}" if self.structural_divergence else ""
        phase_str = f"  情绪: {self.sentiment_phase}" if self.sentiment_phase != "未知" else ""
        lines.append(f"综合: {self.overall_direction}({strength_str})  风格: {self.growth_vs_value}{struct_str}{phase_str}")
        lines.append("─" * 36)
        lines.append("指数     趋势   主要信号")

        for r in self.reports:
            if not r.data_available:
                lines.append(f"{r.name}  数据不可用")
                continue
            trend = (f"{self._trend_arrow(r.daily_trend)}"
                     f"{self._trend_arrow(r.f30_trend)}"
                     f"{self._trend_arrow(r.f15_trend)}")
            sigs  = self._fmt_signals(r)
            price = self._fmt_price(r)
            zs    = self._fmt_zs(r)
            # 三级共振特别标注
            star = " [★三级共振]" if r.three_level_aligned else ""
            lines.append(f"{r.name.ljust(5)}  {trend}  {sigs}{star}  {zs}  {price}")
            # 均线关键位
            if r.ma_context:
                try:
                    from signals.core.ma_levels import format_key_levels
                    kl = format_key_levels(r.ma_context)
                    if kl:
                        lines.append(f"       {kl}")
                except Exception:
                    pass

        lines.append("─" * 36)

        # 仓位建议
        if self.position_suggestion:
            lines.append(f"仓位: {self.position_suggestion}")

        # 轮动阶段 + 配置建议
        if self.rotation_stage:
            lines.append(f"轮动: {self.rotation_stage}" +
                         (f" ({self.rotation_detail})" if self.rotation_detail else ""))
        if self.allocation_suggestion:
            lines.append(self.allocation_suggestion)

        # 盘中情绪评估（始终显示）
        if self.panic_level:
            lines.append(f"盘中情绪: {self.panic_score:.0f}/100 ({self.panic_level})")
            if self.panic_detail:
                lines.append(f"  {self.panic_detail}")
            if self.bottom_candidates:
                lines.append(f"抄底候选: {'、'.join(self.bottom_candidates[:5])}")
        if self.theme_summary:
            lines.append(f"主题: {self.theme_summary}")

        # 三级共振
        aligned = [r.name for r in self.reports
                   if r.data_available and r.three_level_aligned]
        if aligned:
            lines.append(f"三级共振: {'  '.join(aligned)}")

        if not self.gate_industry_scan:
            lines.append("市场偏空，建议观望")

        return "\n".join(lines)

    # ─────────────────────────────────────────────────────
    # 飞书交互式卡片（支持 collapsible_panel 折叠）
    # ─────────────────────────────────────────────────────

    def to_feishu_card(self, perf_summary: dict = None,
                       l2_gain: list = None, l2_composite: list = None) -> dict:
        """
        生成飞书 Card JSON (msg_type=interactive)。
        - 主体：L1 指数研判 + L2 行业排行（始终可见）
        - 折叠：性能分析 & 数据源健康度（collapsible_panel，默认收起）

        :param perf_summary: build_perf_summary() 返回的 dict，None 则不显示性能面板
        :param l2_gain: L2 涨幅榜 top 3 IndustryRanking 列表
        :param l2_composite: L2 综合榜 top 3 IndustryRanking 列表
        """
        from datetime import datetime
        now_dt = datetime.now()
        now = now_dt.strftime("%Y-%m-%d %H:%M")
        today_str = now_dt.strftime("%Y-%m-%d")

        elements = []

        # ── L1 指数研判（lark_md 表格）──
        l1_lines = []
        avail = [r for r in self.reports if r.data_available]
        daily_dts = [r.daily_last_dt for r in avail if r.daily_last_dt]
        f15_dts = [r.f15_last_dt for r in avail if r.f15_last_dt]
        daily_cutoff = max(daily_dts).strftime("%Y-%m-%d") if daily_dts else "未知"
        f15_cutoff = max(f15_dts).strftime("%Y-%m-%d %H:%M") if f15_dts else "未知"

        l1_lines.append(f"日线截至: {daily_cutoff}  |  分钟线截至: {f15_cutoff}")

        # 数据滞后警告
        stale_parts = []
        if daily_dts and daily_cutoff < today_str and now_dt.hour >= 16:
            stale_parts.append(f"日线({daily_cutoff})")
        if f15_dts:
            f15_date = max(f15_dts).strftime("%Y-%m-%d")
            if f15_date < today_str and now_dt.hour >= 16:
                stale_parts.append(f"分钟线({f15_date})")
        if stale_parts:
            l1_lines.append(f"⚠️ 数据尚未更新到今日: {', '.join(stale_parts)}，分析基于滞后数据（日线源通常 17:00~18:00 更新）")

        l1_lines.append("")
        for r in self.reports:
            if not r.data_available:
                l1_lines.append(f"**{r.name}**  数据不可用")
                continue
            trend = (f"{self._trend_arrow(r.daily_trend)}"
                     f"{self._trend_arrow(r.f30_trend)}"
                     f"{self._trend_arrow(r.f15_trend)}")
            sigs = self._fmt_signals(r)
            zs = self._fmt_zs(r)
            price = self._fmt_price(r)
            star = " **★三级共振**" if r.three_level_aligned else ""
            l1_lines.append(f"**{r.name}**  {trend}  {sigs}{star}  {zs}  {price}")
            # 均线关键位
            if r.ma_context:
                try:
                    from signals.core.ma_levels import format_key_levels
                    kl = format_key_levels(r.ma_context)
                    if kl:
                        l1_lines.append(f"  {kl}")
                except Exception:
                    pass
        l1_lines.append("")

        # 综合结论 + 情绪周期
        dir_emoji = {"偏多": "📈", "偏空": "📉", "分化": "↔️"}.get(
            self.overall_direction, "")
        strength_str = f"强度 {self.direction_strength:+.2f}" if self.direction_strength else ""
        struct_str = f"  |  结构: {self.structural_divergence}" if self.structural_divergence else ""
        phase_emoji = _SENTIMENT_EMOJI.get(
            SentimentPhase(self.sentiment_phase), "⚪") if self.sentiment_phase != "未知" else ""
        phase_str = f"  |  {phase_emoji}**{self.sentiment_phase}**" if self.sentiment_phase != "未知" else ""
        l1_lines.append(f"{dir_emoji} **综合: {self.overall_direction}** ({strength_str})  |  风格: {self.growth_vs_value}{struct_str}{phase_str}")

        # 仓位建议
        if self.position_suggestion:
            l1_lines.append(f"💡 {self.position_suggestion}")

        # 轮动阶段 + 配置建议
        if self.rotation_stage:
            rot_str = f"🔄 **{self.rotation_stage}**"
            if self.rotation_detail:
                rot_str += f"（{self.rotation_detail}）"
            l1_lines.append(rot_str)
        if self.allocation_suggestion:
            l1_lines.append(f"📦 {self.allocation_suggestion}")

        # 攻防板块
        if self.shield_sectors or self.sword_sectors:
            parts = []
            if self.shield_sectors:
                parts.append(f"🛡防守: {', '.join(self.shield_sectors[:3])}")
            if self.sword_sectors:
                parts.append(f"⚔进攻: {', '.join(self.sword_sectors[:3])}")
            l1_lines.append(" | ".join(parts))

        aligned = [r.name for r in self.reports
                   if r.data_available and r.three_level_aligned]
        if aligned:
            l1_lines.append(f"⭐ 三级共振: {'  '.join(aligned)}")
        if not self.gate_industry_scan:
            l1_lines.append("⛔ 市场偏空，建议观望")

        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(l1_lines)}
        })

        # ── L2 行业排行（含属性标签+概念标签）──
        if l2_gain or l2_composite:
            elements.append({"tag": "hr"})
            l2_lines = []
            if l2_gain:
                top3 = ", ".join(
                    f"**{r.display_name}**({r.gain_pct:+.1f}%)"
                    for r in l2_gain[:3])
                l2_lines.append(f"📊 涨幅榜: {top3}")
            if l2_composite:
                top3 = ", ".join(
                    f"**{r.display_name}**({r.composite_score:.0f}分)"
                    for r in l2_composite[:3])
                l2_lines.append(f"🏆 综合榜: {top3}")
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(l2_lines)}
            })

        # ── 性能折叠面板（仅 perf_summary 存在时）──
        if perf_summary:
            perf_lines = []
            ps = perf_summary
            perf_lines.append(
                f"⏱ 总计 **{ps['total_s']}s**  |  "
                f"API {ps['api_count']}次  |  "
                f"✅{ps['ok_count']} ❌{ps['fail_count']}  |  "
                f"内存 {ps['mem_mb']}MB"
            )
            perf_lines.append(
                f"L1: {ps['l1_s']}s ({ps['l1_s']/ps['total_s']*100:.0f}%)  |  "
                f"L2: {ps['l2_s']}s ({ps['l2_s']/ps['total_s']*100:.0f}%)"
            )

            # 数据源状态
            for src, d in ps.get("sources", {}).items():
                fail_rate = d["fail"] / d["total"] if d["total"] else 0
                if fail_rate > 0.3:
                    status = "❌"
                elif fail_rate > 0.1:
                    status = "⚠️"
                else:
                    status = "✅"
                avg = d["time"] / d["total"] if d["total"] else 0
                perf_lines.append(
                    f"{status} {src}: {d['ok']}/{d['total']} "
                    f"(均 {avg:.1f}s)")

            # 失败明细
            for f in ps.get("failures", [])[:3]:
                perf_lines.append(f"  ✗ {f[:80]}")

            elements.append({
                "tag": "collapsible_panel",
                "expanded": False,
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "📊 性能分析 & 数据源健康度"
                    }
                },
                "elements": [{
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "\n".join(perf_lines)
                    }
                }]
            })

        # ── 组装卡片 ──
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"🐲 大盘研判  {now}"
                },
                "template": "blue"
            },
            "elements": elements
        }
        return card


# ─────────────────────────────────────────────────────────
# 强势板块推断
# ─────────────────────────────────────────────────────────

# 指数名称 → 关联板块映射（基于市场惯例）
_INDEX_SECTOR_MAP = {
    "上证50":   ["金融", "银行", "地产", "白酒消费"],
    "沪深300":  ["白酒消费", "金融", "医药"],
    "创业板指": ["新能源", "医药生物", "科技成长"],
    "科创50":   ["半导体", "科技硬件", "新能源"],
    "超大盘":   ["金融", "能源", "基建"],
    "中证500":  ["中小盘制造", "材料", "化工"],
    "中证1000": ["小盘成长", "科技", "新能源中游"],
    "恒生科技": ["互联网", "港股科技"],
    "标普500":  ["美股大盘", "科技巨头"],
    "纳斯达克": ["美股科技", "AI/云计算"],
    "道琼斯":   ["美股工业", "美股金融"],
}

# 成长型指数 vs 价值型指数
_GROWTH_INDICES = {"创业板指", "科创50", "中证1000", "恒生科技", "纳斯达克"}
_VALUE_INDICES  = {"上证50", "超大盘", "沪深300", "道琼斯"}

# 指数重要性权重（大盘核心 > 小盘辅助）
_INDEX_WEIGHTS = {
    "沪深300": 1.5, "上证50": 1.2, "标普500": 1.5,
    "纳斯达克": 1.2, "道琼斯": 1.0,
    "创业板指": 1.0, "中证500": 0.8, "科创50": 0.8,
    "中证1000": 0.7, "超大盘": 0.7, "恒生科技": 0.8,
}


def infer_strong_sectors(ctx: "MarketContext") -> List[str]:
    """
    根据指数结构推断强势板块方向。
    逻辑：哪些指数处于上涨趋势或有买信号，提取其关联板块并去重。
    """
    sector_scores: dict = {}
    for r in ctx.reports:
        if not r.data_available:
            continue
        score = 0
        if r.daily_trend == "上涨趋势":
            score += 2
        if r.f30_trend == "上涨趋势":
            score += 2
        if r.has_buy_signal:
            score += 3
        if score > 0:
            for sector in _INDEX_SECTOR_MAP.get(r.name, []):
                sector_scores[sector] = sector_scores.get(sector, 0) + score

    # 按得分排序，取 Top 5
    sorted_sectors = sorted(sector_scores.items(), key=lambda x: -x[1])
    return [s for s, _ in sorted_sectors[:5]]


# ─────────────────────────────────────────────────────────
# 主聚合函数
# ─────────────────────────────────────────────────────────

def build_market_context(
    reports: List[IndexReport],
    zt_total: int = 0,
    dt_total: int = 0,
    lianban_max: int = 0,
    bank_avg_gain: float = 0.0,
    shield_sectors: List[str] = None,
    sword_sectors: List[str] = None,
) -> MarketContext:
    """
    聚合逻辑：
    - 买信号指数数量 ≥ 上涨趋势指数数量的一半 → 偏多
    - 卖信号 or 下跌趋势指数占多数 → 偏空
    - 否则 → 分化
    - 创业/科创/1000 强 vs 50/300 强 → 成长 vs 价值判断
    - gate_industry_scan: 偏多或分化才进行行业扫描
    - sentiment_phase: 情绪周期识别（恐慌/修复/亢奋/回落）

    :param reports:       IndexReport 列表
    :param zt_total:      全市场涨停数（来自 Layer 2，可选）
    :param dt_total:      全市场跌停数（来自 Layer 2，可选）
    :param lianban_max:   最高连板数（来自 Layer 2，可选）
    :param bank_avg_gain: 四大行平均涨幅%（可选）
    :param shield_sectors: 当日强势防守板块名列表（可选）
    :param sword_sectors:  当日强势进攻板块名列表（可选）
    """
    available = [r for r in reports if r.data_available]
    n = len(available)
    if n == 0:
        return MarketContext(
            reports=reports,
            overall_direction="数据不可用",
            gate_industry_scan=False,
            summary="所有指数数据不可用，请检查数据源连接。",
        )

    buy_indices    = [r.name for r in available if r.has_buy_signal]
    sell_indices   = [r.name for r in available if r.has_sell_signal]
    bullish_indices = [r.name for r in available if r.daily_trend == "上涨趋势"]
    bearish_indices = [r.name for r in available if r.daily_trend == "下跌趋势"]

    # 大市方向判断（加权评分，替代简单计数）
    bullish_score = (sum(_INDEX_WEIGHTS.get(n, 1.0) for n in bullish_indices)
                     + sum(_INDEX_WEIGHTS.get(n, 1.0) * 0.5 for n in buy_indices))
    bearish_score = (sum(_INDEX_WEIGHTS.get(n, 1.0) for n in bearish_indices)
                     + sum(_INDEX_WEIGHTS.get(n, 1.0) * 0.5 for n in sell_indices))
    total_weight = sum(_INDEX_WEIGHTS.get(r.name, 1.0) for r in available)

    # 方向强度：[-1.0, +1.0] 连续值
    direction_strength = (bullish_score - bearish_score) / (total_weight + 1e-9)
    direction_strength = max(-1.0, min(1.0, direction_strength))

    if direction_strength > 0.3:
        overall_direction = "偏多"
    elif direction_strength < -0.3:
        overall_direction = "偏空"
    else:
        overall_direction = "分化"

    # 结构性分化检测（大盘 vs 小盘）
    large_cap_bullish = sum(1 for r in available
                           if r.name in _VALUE_INDICES and r.is_bullish)
    small_cap_bullish = sum(1 for r in available
                           if r.name in _GROWTH_INDICES and r.is_bullish)
    if large_cap_bullish >= 3 and small_cap_bullish <= 1:
        structural_divergence = "大强小弱"
    elif small_cap_bullish >= 3 and large_cap_bullish <= 1:
        structural_divergence = "小强大弱"
    else:
        structural_divergence = ""

    # 成长 vs 价值
    growth_bullish = sum(1 for r in available
                        if r.name in _GROWTH_INDICES and r.is_bullish)
    value_bullish  = sum(1 for r in available
                        if r.name in _VALUE_INDICES  and r.is_bullish)

    if growth_bullish > value_bullish + 1:
        growth_vs_value = "成长"
        recommended_style = "成长"
    elif value_bullish > growth_bullish + 1:
        growth_vs_value = "价值"
        recommended_style = "价值"
    else:
        growth_vs_value = "均衡"
        recommended_style = "均衡"

    # 是否进行行业扫描（偏空时观望）
    gate_industry_scan = overall_direction in ("偏多", "分化")

    # 构建临时 context 用于推断板块
    ctx_temp = MarketContext(
        reports=reports,
        overall_direction=overall_direction,
        direction_strength=direction_strength,
        structural_divergence=structural_divergence,
        buy_indices=buy_indices,
        sell_indices=sell_indices,
        bullish_indices=bullish_indices,
        bearish_indices=bearish_indices,
        growth_vs_value=growth_vs_value,
        recommended_style=recommended_style,
        gate_industry_scan=gate_industry_scan,
    )
    recommended_industries = infer_strong_sectors(ctx_temp)

    # ── 情绪周期检测 ──
    divergence = calc_divergence(reports)
    phase = detect_sentiment_phase(
        divergence=divergence,
        zt_total=zt_total,
        dt_total=dt_total,
        lianban_max=lianban_max,
        bank_avg_gain=bank_avg_gain,
    )
    pos_suggestion = get_position_suggestion(overall_direction, phase)

    # 生成综合摘要
    summary_parts = [
        f"大市{overall_direction}（强度 {direction_strength:+.2f}），{growth_vs_value}风格占优。",
    ]
    if structural_divergence:
        summary_parts.append(f"结构: {structural_divergence}。")
    if bullish_indices:
        summary_parts.append(f"上涨趋势：{'、'.join(bullish_indices[:3])}{'等' if len(bullish_indices) > 3 else ''}。")
    if bearish_indices:
        summary_parts.append(f"下跌趋势：{'、'.join(bearish_indices[:3])}{'等' if len(bearish_indices) > 3 else ''}。")
    if buy_indices:
        summary_parts.append(f"买信号：{'、'.join(buy_indices)}。")
    if sell_indices:
        summary_parts.append(f"卖信号：{'、'.join(sell_indices)}。")
    if recommended_industries and gate_industry_scan:
        summary_parts.append(f"建议关注：{', '.join(recommended_industries[:3])}。")
    if not gate_industry_scan:
        summary_parts.append("市场偏空，建议观望。")
    summary_parts.append(f"情绪: {phase.value}。")

    summary = " ".join(summary_parts)

    # 防守/进攻板块（从指数趋势推断，可被外部参数覆盖）
    shield = (shield_sectors if shield_sectors is not None
              else [r.name for r in available
                    if r.name in _SHIELD_INDICES and r.daily_trend == "上涨趋势"])
    sword = (sword_sectors if sword_sectors is not None
             else [r.name for r in available
                   if r.name in _SWORD_INDICES and r.daily_trend == "上涨趋势"])

    # P3-5: 风格切换检测
    style_sw = None
    try:
        style_sw = detect_style_switch(reports)
    except Exception:
        pass

    return MarketContext(
        reports=reports,
        overall_direction=overall_direction,
        direction_strength=direction_strength,
        structural_divergence=structural_divergence,
        buy_indices=buy_indices,
        sell_indices=sell_indices,
        bullish_indices=bullish_indices,
        bearish_indices=bearish_indices,
        growth_vs_value=growth_vs_value,
        recommended_style=recommended_style,
        recommended_industries=recommended_industries,
        gate_industry_scan=gate_industry_scan,
        summary=summary,
        sentiment_phase=phase.value,
        divergence_score=divergence,
        position_suggestion=pos_suggestion,
        shield_sectors=shield,
        sword_sectors=sword,
        style_switch=style_sw,
    )
