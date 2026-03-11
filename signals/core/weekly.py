# -*- coding: utf-8 -*-
"""
周末策略生成器 — 宏观事件剧本 + 技术结构 + 仓位建议

输入: 指数报告 + 轮动阶段 + 宏观日历
输出: 下周操作策略
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import logging

_log = logging.getLogger(__name__)


@dataclass
class EventPlaybook:
    """单个宏观事件的剧本"""
    event_name: str
    event_date: str
    scenarios: Dict[str, str] = field(default_factory=dict)  # "鹰派" → "减仓防守"
    affected_sectors: List[str] = field(default_factory=list)


@dataclass
class WeeklyPlan:
    """周度策略"""
    week_label: str           # "2026-W11 (03/09 ~ 03/13)"
    market_outlook: str       # 大盘展望
    position_suggestion: str  # 仓位建议
    focus_sectors: List[str] = field(default_factory=list)    # 关注板块
    avoid_sectors: List[str] = field(default_factory=list)    # 回避板块
    events: List[EventPlaybook] = field(default_factory=list) # 宏观事件
    key_levels: List[dict] = field(default_factory=list)      # 关键价位
    style_suggestion: str = ""  # 风格建议
    rotation_outlook: str = ""  # 轮动展望


class WeeklyStrategy:
    """
    周末策略生成器。

    整合:
    1. 本周指数分析结果（趋势 + 信号）
    2. 轮动阶段 + 配置建议
    3. 宏观事件日历（AKShare）
    """

    def generate(self, index_reports=None, market_context=None,
                 rotation_stage: str = "", allocation: str = "") -> WeeklyPlan:
        """
        :param index_reports: IndexReport 列表
        :param market_context: MarketContext
        :param rotation_stage: 轮动阶段描述
        :param allocation: 配置建议
        :return: WeeklyPlan
        """
        # 周标签
        from datetime import datetime, timedelta
        now = datetime.now()
        # 下周一
        days_until_monday = (7 - now.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        next_monday = now + timedelta(days=days_until_monday)
        next_friday = next_monday + timedelta(days=4)
        week_num = next_monday.isocalendar()[1]
        week_label = (f"{next_monday.year}-W{week_num:02d} "
                      f"({next_monday.strftime('%m/%d')} ~ "
                      f"{next_friday.strftime('%m/%d')})")

        # 大盘展望
        market_outlook = self._build_outlook(index_reports, market_context)

        # 仓位建议
        position = self._suggest_position(market_context, rotation_stage)

        # 板块建议
        focus, avoid = self._sector_suggestion(market_context)

        # 宏观事件
        events = self._fetch_events(next_monday, next_friday)

        # 关键价位
        key_levels = self._extract_weekly_levels(index_reports)

        # 风格建议
        style = ""
        if market_context:
            style = getattr(market_context, "recommended_style", "")

        return WeeklyPlan(
            week_label=week_label,
            market_outlook=market_outlook,
            position_suggestion=position or allocation,
            focus_sectors=focus,
            avoid_sectors=avoid,
            events=events,
            key_levels=key_levels,
            style_suggestion=style,
            rotation_outlook=rotation_stage,
        )

    def _build_outlook(self, reports, ctx) -> str:
        """基于指数分析生成大盘展望"""
        if not ctx:
            return "无指数分析数据"

        direction = ctx.overall_direction
        sentiment = getattr(ctx, "sentiment_phase", "")
        parts = [f"大盘方向 {direction}"]
        if sentiment:
            parts.append(f"情绪 {sentiment}")

        # 统计买卖信号
        buy_count = 0
        sell_count = 0
        for r in (reports or []):
            if not getattr(r, "data_available", False):
                continue
            if getattr(r, "has_buy_signal", False):
                buy_count += 1
            if getattr(r, "has_sell_signal", False):
                sell_count += 1

        if buy_count > sell_count:
            parts.append(f"买入信号 {buy_count} 个 > 卖出信号 {sell_count} 个，偏多")
        elif sell_count > buy_count:
            parts.append(f"卖出信号 {sell_count} 个 > 买入信号 {buy_count} 个，偏空")
        else:
            parts.append("买卖信号均衡，方向不明")

        return "。".join(parts)

    def _suggest_position(self, ctx, rotation_stage) -> str:
        """仓位建议"""
        if not ctx:
            return "5成（默认中性仓位）"
        pos = getattr(ctx, "position_suggestion", "")
        if pos:
            return pos
        direction = ctx.overall_direction
        if direction == "偏多":
            return "6-8成"
        elif direction == "偏空":
            return "2-3成"
        return "4-5成"

    def _sector_suggestion(self, ctx):
        """板块建议"""
        focus = []
        avoid = []
        if ctx:
            sword = getattr(ctx, "sword_sectors", [])
            shield = getattr(ctx, "shield_sectors", [])
            if sword:
                focus.extend(sword[:5])
            if shield:
                # 偏空时关注防守板块
                if ctx.overall_direction == "偏空":
                    focus.extend(shield[:3])
                else:
                    avoid.extend(shield[:3])
        return focus, avoid

    def _fetch_events(self, start_date, end_date) -> List[EventPlaybook]:
        """获取宏观事件日历"""
        events = []
        try:
            import akshare as ak
            df = ak.stock_em_macro_event()
            if df is not None and not df.empty:
                # 过滤日期范围内的事件
                for _, row in df.head(10).iterrows():
                    event_name = str(row.get("事件", row.iloc[0]))
                    event_date = str(row.get("日期", row.iloc[1] if len(row) > 1 else ""))
                    events.append(EventPlaybook(
                        event_name=event_name,
                        event_date=event_date,
                        scenarios=self._event_scenarios(event_name),
                    ))
        except Exception as e:
            _log.debug("宏观事件获取失败: %s", e)
            # 降级: 使用常见事件模板
            events = self._default_events()

        return events[:5]

    def _event_scenarios(self, event_name: str) -> Dict[str, str]:
        """根据事件名称生成情景剧本"""
        if "CPI" in event_name or "通胀" in event_name:
            return {"超预期": "利空成长股，利好周期", "符合预期": "影响有限", "低于预期": "利好成长股"}
        if "利率" in event_name or "降息" in event_name or "加息" in event_name:
            return {"鸽派": "利好成长+科技", "中性": "维持现状", "鹰派": "利空成长，利好银行"}
        if "GDP" in event_name or "经济" in event_name:
            return {"强劲": "利好顺周期", "疲弱": "利空周期股，利好防守"}
        if "就业" in event_name or "非农" in event_name:
            return {"强劲": "经济韧性，维持", "疲弱": "衰退担忧，防守"}
        return {"利好": "积极应对", "中性": "维持不变", "利空": "谨慎应对"}

    def _default_events(self) -> List[EventPlaybook]:
        """默认事件（当 AKShare 不可用时）"""
        return [
            EventPlaybook(
                event_name="常规交易周",
                event_date="",
                scenarios={"偏多": "跟随趋势", "偏空": "控制仓位", "震荡": "高抛低吸"},
            )
        ]

    def _extract_weekly_levels(self, reports) -> List[dict]:
        """从指数报告中提取周线关键价位"""
        levels = []
        if not reports:
            return levels

        # 取主要指数（沪深300、上证50）的关键位
        main_indices = ["沪深300", "上证50", "创业板指"]
        for r in reports:
            if not getattr(r, "data_available", False):
                continue
            if r.name not in main_indices:
                continue
            ma_ctx = getattr(r, "ma_context", None)
            if ma_ctx:
                for lv in getattr(ma_ctx, "key_levels", [])[:2]:
                    levels.append({
                        "index": r.name,
                        "name": lv.name,
                        "price": round(lv.value, 2),
                        "distance_pct": round(lv.distance_pct, 2),
                    })
        return levels[:8]


def generate_weekly(index_reports=None, market_context=None,
                    rotation_stage: str = "", allocation: str = "") -> WeeklyPlan:
    """便捷函数: 生成周末策略"""
    return WeeklyStrategy().generate(
        index_reports, market_context, rotation_stage, allocation)
