# -*- coding: utf-8 -*-
"""
MarketContext: 聚合 8 个指数的 IndexReport，形成大市研判结论。
- overall_direction: "偏多" / "偏空" / "分化"
- growth_vs_value: 成长 vs 价值相对强弱
- gate_industry_scan: 是否建议进行行业扫描
"""
from dataclasses import dataclass, field
from typing import List, Optional
from .index_report import IndexReport


# ─────────────────────────────────────────────────────────
# 数据类
# ─────────────────────────────────────────────────────────

@dataclass
class MarketContext:
    """
    8 个指数聚合后的大市研判结果。
    """
    reports: List[IndexReport]             # 所有指数报告
    overall_direction: str = "分化"        # "偏多" / "偏空" / "分化"
    buy_indices: List[str] = field(default_factory=list)   # 出现买信号的指数名称
    sell_indices: List[str] = field(default_factory=list)  # 出现卖信号的指数名称
    bullish_indices: List[str] = field(default_factory=list)  # 上涨趋势指数
    bearish_indices: List[str] = field(default_factory=list)  # 下跌趋势指数
    growth_vs_value: str = "均衡"          # "成长" / "价值" / "均衡"
    recommended_style: str = "均衡"        # 建议风格
    recommended_industries: List[str] = field(default_factory=list)  # 推断的强势板块
    gate_industry_scan: bool = True        # 偏多或中性才进行行业扫描
    summary: str = ""                      # 2-3 行综合判断

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
        """每行：名称 | 日↑ 30→ 15↓ | 信号 | 中枢 | 价格"""
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

        # 综合结论
        dir_emoji = {"偏多": "📈", "偏空": "📉", "分化": "↔️"}.get(
            self.overall_direction, "")
        lines.append(f"  {dir_emoji} 综合: {self.overall_direction}  |  风格: {self.growth_vs_value}")

        # 三级共振（最强信号）
        aligned = [r.name for r in self.reports
                   if r.data_available and r.three_level_aligned]
        if aligned:
            lines.append(f"  ⭐ 三级共振: {'  '.join(aligned)}")

        # 买点汇总
        if self.buy_indices:
            n = len(self.buy_indices)
            shown = "、".join(self.buy_indices[:4])
            suffix = f" 等{n}只" if n > 4 else ""
            lines.append(f"  🔔 买点: {shown}{suffix}")

        # 卖点汇总（有卖信号时提示）
        if self.sell_indices:
            lines.append(f"  ⚠️  卖点: {'、'.join(self.sell_indices)}")

        # 推荐板块
        if self.recommended_industries and self.gate_industry_scan:
            lines.append(f"  📌 推荐关注: {'、'.join(self.recommended_industries[:4])}")

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
        lines.append(f"综合: {self.overall_direction}  风格: {self.growth_vs_value}")
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

        lines.append("─" * 36)

        # 三级共振
        aligned = [r.name for r in self.reports
                   if r.data_available and r.three_level_aligned]
        if aligned:
            lines.append(f"三级共振: {'  '.join(aligned)}")

        if self.buy_indices:
            n = len(self.buy_indices)
            shown = "、".join(self.buy_indices[:5])
            suffix = f" 等{n}只" if n > 5 else ""
            lines.append(f"买点: {shown}{suffix}")

        if self.sell_indices:
            lines.append(f"注意卖点: {'、'.join(self.sell_indices)}")

        if self.recommended_industries and self.gate_industry_scan:
            lines.append(f"推荐关注: {'、'.join(self.recommended_industries[:4])}")

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
        l1_lines.append("")

        # 综合结论
        dir_emoji = {"偏多": "📈", "偏空": "📉", "分化": "↔️"}.get(
            self.overall_direction, "")
        l1_lines.append(f"{dir_emoji} **综合: {self.overall_direction}**  |  风格: {self.growth_vs_value}")

        aligned = [r.name for r in self.reports
                   if r.data_available and r.three_level_aligned]
        if aligned:
            l1_lines.append(f"⭐ 三级共振: {'  '.join(aligned)}")
        if self.buy_indices:
            n = len(self.buy_indices)
            shown = "、".join(self.buy_indices[:5])
            suffix = f" 等{n}只" if n > 5 else ""
            l1_lines.append(f"🔔 买点: {shown}{suffix}")
        if self.sell_indices:
            l1_lines.append(f"⚠️ 卖点: {'、'.join(self.sell_indices)}")
        if self.recommended_industries and self.gate_industry_scan:
            l1_lines.append(f"📌 推荐关注: {'、'.join(self.recommended_industries[:4])}")
        if not self.gate_industry_scan:
            l1_lines.append("⛔ 市场偏空，建议观望")

        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(l1_lines)}
        })

        # ── L2 行业排行 ──
        if l2_gain or l2_composite:
            elements.append({"tag": "hr"})
            l2_lines = []
            if l2_gain:
                top3 = ", ".join(
                    f"**{r.name}**({r.gain_pct:+.1f}%)" for r in l2_gain[:3])
                l2_lines.append(f"📊 涨幅榜: {top3}")
            if l2_composite:
                top3 = ", ".join(
                    f"**{r.name}**({r.composite_score:.0f}分)"
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

def build_market_context(reports: List[IndexReport]) -> MarketContext:
    """
    聚合逻辑：
    - 买信号指数数量 ≥ 上涨趋势指数数量的一半 → 偏多
    - 卖信号 or 下跌趋势指数占多数 → 偏空
    - 否则 → 分化
    - 创业/科创/1000 强 vs 50/300 强 → 成长 vs 价值判断
    - gate_industry_scan: 偏多或分化才进行行业扫描
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

    # 大市方向判断
    bullish_score = len(bullish_indices) + len(buy_indices) * 0.5
    bearish_score = len(bearish_indices) + len(sell_indices) * 0.5

    if bullish_score >= n * 0.5:
        overall_direction = "偏多"
    elif bearish_score >= n * 0.5:
        overall_direction = "偏空"
    else:
        overall_direction = "分化"

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
        buy_indices=buy_indices,
        sell_indices=sell_indices,
        bullish_indices=bullish_indices,
        bearish_indices=bearish_indices,
        growth_vs_value=growth_vs_value,
        recommended_style=recommended_style,
        gate_industry_scan=gate_industry_scan,
    )
    recommended_industries = infer_strong_sectors(ctx_temp)

    # 生成综合摘要
    summary_parts = [
        f"大市{overall_direction}，{growth_vs_value}风格占优。",
    ]
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

    summary = " ".join(summary_parts)

    return MarketContext(
        reports=reports,
        overall_direction=overall_direction,
        buy_indices=buy_indices,
        sell_indices=sell_indices,
        bullish_indices=bullish_indices,
        bearish_indices=bearish_indices,
        growth_vs_value=growth_vs_value,
        recommended_style=recommended_style,
        recommended_industries=recommended_industries,
        gate_industry_scan=gate_industry_scan,
        summary=summary,
    )
