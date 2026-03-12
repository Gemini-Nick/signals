# -*- coding: utf-8 -*-
"""
盘前计划生成器 — 完全分类状态机 + 目标位计算

基于当前 CZSC 结构（笔 + 中枢）+ MA 均线关键位，
生成 3 种完全分类情景，每种附带触发条件 + 操作建议 + 目标价位。
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from czsc import Direction


@dataclass
class Scenario:
    """一种完全分类的情景"""
    name: str              # "分类A: 上涨延续"
    probability_hint: str  # "偏高" / "中等" / "偏低"
    trigger: str           # "站稳3200" / "跌破3150"
    action: str            # "加仓至7成" / "减仓至3成"
    target_prices: List[float] = field(default_factory=list)
    stop_price: float = 0.0
    rationale: str = ""


@dataclass
class DailyPlan:
    """盘前计划"""
    symbol: str
    name: str
    current_price: float
    trend: str            # "上涨趋势" / "下跌趋势" / "震荡"
    structure: str        # 当前笔段结构描述
    scenarios: List[Scenario] = field(default_factory=list)
    key_levels: List[dict] = field(default_factory=list)  # 关键价位


class PlanGenerator:
    """
    基于 CZSC 结构 + MA 均线生成盘前计划。

    3 种完全分类情景:
    A. 趋势延续（顺方向）
    B. 回调/反弹确认（反方向但不破关键位）
    C. 趋势反转（关键位破位）
    """

    def generate(self, analyzer, ma_ctx=None) -> DailyPlan:
        """
        :param analyzer: SymbolAnalyzer 实例
        :param ma_ctx: MAContext 均线上下文（可选）
        :return: DailyPlan
        """
        bis = analyzer.finished_bis
        bars = analyzer.bars_raw
        symbol = analyzer.symbol
        current_price = bars[-1].close if bars else 0

        # 趋势判断
        trend = self._assess_trend(bis)
        structure = self._describe_structure(bis)

        # 提取中枢
        zs_zd, zs_zg = self._find_latest_zhongshu(bis)

        # 目标位计算
        targets_up, targets_down = self._compute_targets(
            bis, zs_zd, zs_zg, current_price, ma_ctx)

        # 关键价位
        key_levels = self._extract_key_levels(bis, zs_zd, zs_zg, ma_ctx)

        # 生成 3 种情景
        scenarios = self._build_scenarios(
            trend, bis, current_price,
            zs_zd, zs_zg, targets_up, targets_down, key_levels)

        return DailyPlan(
            symbol=symbol,
            name=symbol,
            current_price=round(current_price, 2),
            trend=trend,
            structure=structure,
            scenarios=scenarios,
            key_levels=key_levels,
        )

    def _assess_trend(self, bis) -> str:
        """基于笔端点判断趋势"""
        if len(bis) < 3:
            return "数据不足"
        # 最近3笔的高低点趋势
        highs = [b.high for b in bis[-3:] if b.direction == Direction.Up]
        lows = [b.low for b in bis[-3:] if b.direction == Direction.Down]

        if len(highs) >= 2 and highs[-1] > highs[-2]:
            if len(lows) >= 2 and lows[-1] > lows[-2]:
                return "上涨趋势"
            return "偏强震荡"
        elif len(lows) >= 2 and lows[-1] < lows[-2]:
            if len(highs) >= 2 and highs[-1] < highs[-2]:
                return "下跌趋势"
            return "偏弱震荡"
        return "震荡"

    def _describe_structure(self, bis) -> str:
        """描述当前笔段结构"""
        if len(bis) < 2:
            return "笔数不足"
        last = bis[-1]
        prev = bis[-2]
        dir_str = "向上笔" if last.direction == Direction.Up else "向下笔"
        return (f"最后一笔: {dir_str} "
                f"({last.low:.2f}→{last.high:.2f})，"
                f"共 {len(bis)} 笔完成")

    def _find_latest_zhongshu(self, bis) -> Tuple[float, float]:
        """从最近的笔中提取最近的有效中枢"""
        if len(bis) < 3:
            return 0, 0
        # 从最近往前搜索有效中枢
        for i in range(len(bis) - 3, max(len(bis) - 10, -1), -1):
            if i < 0:
                break
            b1, b2, b3 = bis[i], bis[i + 1], bis[i + 2]
            zd = max(b1.low, b3.low)
            zg = min(b1.high, b3.high)
            if zg > zd:
                return zd, zg
        return 0, 0

    def _compute_targets(self, bis, zs_zd, zs_zg, current_price, ma_ctx):
        """计算上下目标位"""
        targets_up = []
        targets_down = []

        if zs_zd > 0 and zs_zg > 0:
            zs_range = zs_zg - zs_zd
            # 中枢上方目标: ZG + 中枢高度
            targets_up.append(round(zs_zg + zs_range, 2))
            # 中枢下方目标: ZD - 中枢高度
            targets_down.append(round(zs_zd - zs_range, 2))

        # 从笔端点提取目标
        if len(bis) >= 3:
            up_bis = [b for b in bis[-6:] if b.direction == Direction.Up]
            down_bis = [b for b in bis[-6:] if b.direction == Direction.Down]
            if up_bis:
                prev_high = max(b.high for b in up_bis)
                if prev_high > current_price:
                    targets_up.append(round(prev_high, 2))
            if down_bis:
                prev_low = min(b.low for b in down_bis)
                if prev_low < current_price:
                    targets_down.append(round(prev_low, 2))

        # Fibonacci 扩展
        if len(bis) >= 2:
            last = bis[-1]
            swing = abs(last.high - last.low)
            if last.direction == Direction.Up:
                targets_up.append(round(last.high + swing * 0.618, 2))
            else:
                targets_down.append(round(last.low - swing * 0.618, 2))

        # MA 均线目标
        if ma_ctx:
            for lv in getattr(ma_ctx, 'resistance_levels', [])[:2]:
                if lv.value > current_price:
                    targets_up.append(round(lv.value, 2))
            for lv in getattr(ma_ctx, 'support_levels', [])[:2]:
                if lv.value < current_price:
                    targets_down.append(round(lv.value, 2))

        # 去重排序
        targets_up = sorted(set(targets_up))
        targets_down = sorted(set(targets_down), reverse=True)
        return targets_up[:3], targets_down[:3]

    def _extract_key_levels(self, bis, zs_zd, zs_zg, ma_ctx) -> List[dict]:
        """提取关键价位"""
        levels = []
        if zs_zd > 0:
            levels.append({"name": "中枢下沿", "price": round(zs_zd, 2), "type": "support"})
        if zs_zg > 0:
            levels.append({"name": "中枢上沿", "price": round(zs_zg, 2), "type": "resistance"})

        # 最近笔端点
        if len(bis) >= 2:
            last = bis[-1]
            if last.direction == Direction.Down:
                levels.append({"name": "最新低点", "price": round(last.low, 2), "type": "support"})
            else:
                levels.append({"name": "最新高点", "price": round(last.high, 2), "type": "resistance"})

        # MA 均线
        if ma_ctx:
            for lv in getattr(ma_ctx, 'key_levels', [])[:3]:
                pos_type = "support" if lv.position in ("下方", "贴合") else "resistance"
                levels.append({
                    "name": lv.name, "price": round(lv.value, 2), "type": pos_type,
                })

        return levels

    def _build_scenarios(self, trend, bis, price, zs_zd, zs_zg,
                         targets_up, targets_down, key_levels):
        """构建完全分类的 3 种情景"""
        scenarios = []

        # 找最近的支撑和阻力
        nearest_support = 0
        nearest_resistance = 0
        for lv in key_levels:
            if lv["type"] == "support" and lv["price"] < price:
                if not nearest_support or lv["price"] > nearest_support:
                    nearest_support = lv["price"]
            if lv["type"] == "resistance" and lv["price"] > price:
                if not nearest_resistance or lv["price"] < nearest_resistance:
                    nearest_resistance = lv["price"]

        if "上涨" in trend or "偏强" in trend:
            # 上涨趋势的完全分类
            scenarios.append(Scenario(
                name="分类A: 上涨延续",
                probability_hint="偏高",
                trigger=f"站稳 {nearest_support:.0f}" if nearest_support else "维持上涨结构",
                action="持仓待涨，可适度加仓",
                target_prices=targets_up,
                stop_price=nearest_support if nearest_support else 0,
                rationale="高低点同时抬升，趋势延续概率大",
            ))
            scenarios.append(Scenario(
                name="分类B: 正常回调",
                probability_hint="中等",
                trigger=f"回调至 {nearest_support:.0f} 附近企稳" if nearest_support else "回调不破前低",
                action="回调到位可补仓",
                target_prices=targets_up[:1],
                stop_price=nearest_support * 0.98 if nearest_support else 0,
                rationale="趋势中正常回调，关注支撑位表现",
            ))
            scenarios.append(Scenario(
                name="分类C: 趋势反转",
                probability_hint="偏低",
                trigger=f"跌破 {nearest_support:.0f}" if nearest_support else "低点破位",
                action="减仓观望，等结构明确",
                target_prices=targets_down,
                rationale="破位后趋势可能转空，先出再看",
            ))

        elif "下跌" in trend or "偏弱" in trend:
            # 下跌趋势的完全分类
            scenarios.append(Scenario(
                name="分类A: 下跌延续",
                probability_hint="偏高",
                trigger=f"反弹受阻于 {nearest_resistance:.0f}" if nearest_resistance else "维持下跌结构",
                action="空仓或轻仓观望",
                target_prices=targets_down,
                rationale="高低点同时降低，趋势延续",
            ))
            scenarios.append(Scenario(
                name="分类B: 反弹确认",
                probability_hint="中等",
                trigger=f"反弹至 {nearest_resistance:.0f} 附近" if nearest_resistance else "出现底部结构",
                action="反弹减仓为主",
                target_prices=[nearest_resistance] if nearest_resistance else [],
                rationale="下跌中反弹，高抛低吸",
            ))
            scenarios.append(Scenario(
                name="分类C: 反转向上",
                probability_hint="偏低",
                trigger=f"突破 {nearest_resistance:.0f}" if nearest_resistance else "底部买点出现",
                action="轻仓试探，确认后加仓",
                target_prices=targets_up,
                rationale="反转需多重确认：笔结构 + 量能 + MA 突破",
            ))

        else:
            # 震荡格局的完全分类
            range_mid = (nearest_support + nearest_resistance) / 2 if nearest_support and nearest_resistance else price
            scenarios.append(Scenario(
                name="分类A: 向上突破",
                probability_hint="中等",
                trigger=f"突破 {nearest_resistance:.0f}" if nearest_resistance else "放量突破",
                action="跟随突破，追踪强势板块",
                target_prices=targets_up,
                stop_price=nearest_resistance * 0.98 if nearest_resistance else 0,
                rationale="震荡格局向上突破，新一轮上涨",
            ))
            scenarios.append(Scenario(
                name="分类B: 继续震荡",
                probability_hint="偏高",
                trigger="维持区间内运行",
                action="高抛低吸，轻仓波段",
                target_prices=[],
                rationale="无方向选择前维持震荡",
            ))
            scenarios.append(Scenario(
                name="分类C: 向下破位",
                probability_hint="中等",
                trigger=f"跌破 {nearest_support:.0f}" if nearest_support else "放量破位",
                action="减仓至2成以下",
                target_prices=targets_down,
                rationale="震荡格局向下选择，进入下跌",
            ))

        return scenarios


def generate_plan(analyzer, ma_ctx=None) -> DailyPlan:
    """便捷函数: 生成盘前计划"""
    return PlanGenerator().generate(analyzer, ma_ctx)
