# -*- coding: utf-8 -*-
"""
Skill 注册表 — 定义可被微信 Agent 调用的分析技能

每个 Skill 包含:
- name: 技能标识
- triggers: 触发关键词列表
- execute(params): 执行分析，返回文本结果
"""
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

_log = logging.getLogger("signals.wechat.skills")


@dataclass
class SkillResult:
    """Skill 执行结果"""
    ok: bool
    text: str
    error: str = ""


class BaseSkill(ABC):
    """技能基类"""
    name: str = ""
    description: str = ""
    triggers: list[str] = []
    usage: str = ""

    @abstractmethod
    def execute(self, raw_input: str, params: dict) -> SkillResult:
        """执行技能，返回文本结果"""
        ...

    def match(self, text: str) -> tuple[bool, dict]:
        """
        判断文本是否匹配该技能。
        返回 (是否匹配, 提取的参数 dict)
        """
        for trigger in self.triggers:
            if trigger in text:
                # 提取 trigger 后面的参数文本
                idx = text.index(trigger) + len(trigger)
                remainder = text[idx:].strip()
                return True, {"query": remainder}
        return False, {}


# ─────────────────────────────────────────────────────
# 内置 Skills
# ─────────────────────────────────────────────────────

class StockAnalyzeSkill(BaseSkill):
    """个股深度分析"""
    name = "stock_analyze"
    description = "深度分析单只股票（缠论结构 + 量价 + 异常检测 + 完全分类）"
    triggers = ["分析", "看看", "诊股", "analyze"]
    usage = "分析 茅台 / 分析 600519 / 分析 SZ.002759"

    def execute(self, raw_input: str, params: dict) -> SkillResult:
        query = params.get("query", "").strip()
        if not query:
            return SkillResult(ok=False, text="", error="请提供股票名称或代码，如：分析 茅台")

        symbol = _resolve_symbol(query)
        if not symbol:
            return SkillResult(ok=False, text="", error=f"无法识别股票: {query}")

        try:
            from signals.layers.stock_deep_dive import StockDeepDive
            dive = StockDeepDive(symbol)
            return SkillResult(ok=True, text=dive.to_text())
        except Exception as e:
            _log.exception("StockDeepDive 失败: %s", symbol)
            return SkillResult(ok=False, text="", error=f"分析失败: {e}")


class MarketOverviewSkill(BaseSkill):
    """大盘研判"""
    name = "market_overview"
    description = "大盘方向 / 情绪周期 / 仓位建议"
    triggers = ["大盘", "指数", "市场", "行情"]
    usage = "大盘 / 指数 / 今日行情"

    def execute(self, raw_input: str, params: dict) -> SkillResult:
        try:
            from signals.web.services.engine import get_engine
            engine = get_engine()
            ctx = engine.get_market_context()
            if ctx is None:
                return SkillResult(ok=False, text="", error="引擎未就绪，请先启动 web 服务")
            return SkillResult(ok=True, text=ctx.to_text())
        except Exception as e:
            _log.exception("大盘研判失败")
            return SkillResult(ok=False, text="", error=f"大盘研判失败: {e}")


class IndustryRankingSkill(BaseSkill):
    """行业排行"""
    name = "industry_ranking"
    description = "行业板块强度排行（涨幅 + 综合评分 + 超跌）"
    triggers = ["行业", "板块", "排行"]
    usage = "行业排行 / 板块"

    def execute(self, raw_input: str, params: dict) -> SkillResult:
        try:
            from signals.layers.industry import run_industry_ranking
            result = run_industry_ranking()
            if not result:
                return SkillResult(ok=False, text="", error="行业数据获取失败")

            lines = ["📊 行业排行 Top 10\n"]
            gain_list = sorted(result, key=lambda x: x.gain_pct, reverse=True)[:10]
            for i, ind in enumerate(gain_list, 1):
                emoji = "🔴" if ind.gain_pct > 0 else "🟢"
                lines.append(
                    f"{i}. {emoji} {ind.display_name}  "
                    f"{ind.gain_pct:+.2f}%  "
                    f"综合{ind.composite_score:.0f}"
                )

            # 超跌板块
            oversold = sorted(result, key=lambda x: x.oversold_score, reverse=True)[:5]
            if oversold and oversold[0].oversold_score > 0:
                lines.append("\n🔻 超跌反弹候选:")
                for ind in oversold:
                    if ind.oversold_score > 0:
                        lines.append(f"  · {ind.display_name} 超跌分{ind.oversold_score:.0f}")

            return SkillResult(ok=True, text="\n".join(lines))
        except Exception as e:
            _log.exception("行业排行失败")
            return SkillResult(ok=False, text="", error=f"行业排行失败: {e}")


class BacktestSkill(BaseSkill):
    """信号回测"""
    name = "backtest"
    description = "对个股进行信号回测（胜率 / 期望 / MFE-MAE）"
    triggers = ["回测", "backtest"]
    usage = "回测 600519 / 回测 茅台 日线"

    def execute(self, raw_input: str, params: dict) -> SkillResult:
        query = params.get("query", "").strip()
        if not query:
            return SkillResult(ok=False, text="", error="请提供股票代码，如：回测 600519")

        # 解析频率
        freq = "daily"
        for kw, f in [("周线", "weekly"), ("日线", "daily")]:
            if kw in query:
                freq = f
                query = query.replace(kw, "").strip()

        # 解析代码
        code = _extract_code(query)
        if not code:
            return SkillResult(ok=False, text="", error=f"无法识别股票代码: {query}")

        try:
            from signals.web.api.backtest import _fetch_kline, _detect_market, _build_symbol
            from signals.core.signal_eval import evaluate_signals

            market = _detect_market(code)
            symbol = _build_symbol(code, market)
            df = _fetch_kline(code, market, freq)
            if df.empty:
                return SkillResult(ok=False, text="", error=f"无法获取K线数据: {code}")

            # 运行回测
            from signals.core.backtest_engine import run_signal_backtest
            result = run_signal_backtest(df, symbol, freq)

            lines = [f"📈 回测报告: {symbol} ({freq})\n"]
            if hasattr(result, "kpi") and result.kpi:
                kpi = result.kpi
                lines.append(f"信号总数: {kpi.get('total_signals', 0)}")
                lines.append(f"胜率: {kpi.get('win_rate', 0):.1%}")
                lines.append(f"期望收益: {kpi.get('expectancy', 0):.2%}")
                lines.append(f"平均T+5: {kpi.get('avg_ret_t5', 0):.2%}")
                lines.append(f"平均T+10: {kpi.get('avg_ret_t10', 0):.2%}")
                lines.append(f"MFE均值: {kpi.get('avg_mfe', 0):.2%}")
                lines.append(f"MAE均值: {kpi.get('avg_mae', 0):.2%}")
            else:
                lines.append("未检测到有效信号")

            return SkillResult(ok=True, text="\n".join(lines))
        except Exception as e:
            _log.exception("回测失败: %s", code)
            return SkillResult(ok=False, text="", error=f"回测失败: {e}")


class SocialHeatSkill(BaseSkill):
    """社交舆情"""
    name = "social_heat"
    description = "查询个股社交热度（舆情评分 + 概念标签）"
    triggers = ["舆情", "热度", "人气"]
    usage = "舆情 茅台 / 热度 600519"

    def execute(self, raw_input: str, params: dict) -> SkillResult:
        query = params.get("query", "").strip()
        if not query:
            return SkillResult(ok=False, text="", error="请提供股票名称或代码")

        symbol = _resolve_symbol(query)
        if not symbol:
            return SkillResult(ok=False, text="", error=f"无法识别股票: {query}")

        try:
            from signals.data.social_fetcher import fetch_social_heat
            heat = fetch_social_heat(symbol)
            if not heat:
                return SkillResult(ok=True, text=f"{symbol} 暂无社交热度数据")

            lines = [
                f"🔥 {symbol} 社交舆情",
                f"热度: {heat.heat_score:.1f} ({heat.heat_grade})",
                f"评论得分: {heat.comment_score:.1f} ({heat.comment_rank})",
                f"关注指数: {heat.focus_index:.1f}",
                f"机构占比: {heat.institution_pct:.1%}",
            ]
            if heat.concepts:
                lines.append(f"概念: {', '.join(heat.concepts[:5])}")
            if heat.tag:
                lines.append(f"标签: {heat.tag}")
            return SkillResult(ok=True, text="\n".join(lines))
        except Exception as e:
            _log.exception("舆情查询失败")
            return SkillResult(ok=False, text="", error=f"舆情查询失败: {e}")


class HotThemesSkill(BaseSkill):
    """热点主题"""
    name = "hot_themes"
    description = "当日热门概念主题 Top 15"
    triggers = ["热点", "主题", "概念"]
    usage = "热点 / 今日主题"

    def execute(self, raw_input: str, params: dict) -> SkillResult:
        try:
            from signals.core.theme_discovery import get_hot_themes
            themes = get_hot_themes(top_n=15)
            if not themes:
                return SkillResult(ok=True, text="暂无热门主题数据")

            lines = ["🔥 今日热门概念\n"]
            for i, t in enumerate(themes, 1):
                emoji = "🔴" if t.change_pct > 0 else "🟢"
                lines.append(
                    f"{i}. {emoji} {t.name}  "
                    f"{t.change_pct:+.2f}%  "
                    f"({t.stock_count}只)"
                )
            return SkillResult(ok=True, text="\n".join(lines))
        except Exception as e:
            _log.exception("热门主题获取失败")
            return SkillResult(ok=False, text="", error=f"热门主题获取失败: {e}")


class PlanSkill(BaseSkill):
    """盘前计划"""
    name = "plan"
    description = "生成盘前计划（主要指数三情景分析）"
    triggers = ["计划", "盘前"]
    usage = "盘前计划 / 计划"

    def execute(self, raw_input: str, params: dict) -> SkillResult:
        try:
            from signals.web.services.engine import get_engine
            from signals.core.planner import generate_plan
            engine = get_engine()
            if not engine.is_ready():
                return SkillResult(ok=False, text="", error="引擎未就绪，请先启动 web 服务")

            reports = engine.get_index_reports()
            main_indices = ["沪深300", "上证50", "创业板指"]
            lines = ["📋 盘前计划\n"]

            for r in reports:
                if not getattr(r, "data_available", False):
                    continue
                if r.name not in main_indices:
                    continue
                analyzer = engine.get_symbol_analyzer(r.name, "daily")
                if analyzer is None:
                    continue
                ma_ctx = getattr(r, "ma_context", None)
                plan = generate_plan(analyzer, ma_ctx)
                plan.name = r.name

                lines.append(f"━━ {plan.name} ━━")
                lines.append(f"趋势: {plan.trend}  结构: {plan.structure}")
                for sc in plan.scenarios:
                    lines.append(f"  【{sc.name}】({sc.probability_hint})")
                    lines.append(f"    触发: {sc.trigger}")
                    lines.append(f"    操作: {sc.action}")
                lines.append("")

            return SkillResult(ok=True, text="\n".join(lines))
        except Exception as e:
            _log.exception("盘前计划生成失败")
            return SkillResult(ok=False, text="", error=f"盘前计划生成失败: {e}")


class WeeklyStrategySkill(BaseSkill):
    """周策略"""
    name = "weekly_strategy"
    description = "生成周末策略（市场展望 + 仓位建议 + 关注/回避板块）"
    triggers = ["周策略", "周末策略", "本周"]
    usage = "周策略 / 本周策略"

    def execute(self, raw_input: str, params: dict) -> SkillResult:
        try:
            from signals.web.services.engine import get_engine
            from signals.core.weekly import generate_weekly
            engine = get_engine()
            reports = engine.get_index_reports()
            ctx = engine.get_market_context()
            rv = engine.review_state

            weekly = generate_weekly(
                index_reports=reports,
                market_context=ctx,
                rotation_stage=getattr(rv, "rotation_stage", "") or
                               getattr(ctx, "rotation_stage", "") if ctx else "",
                allocation=getattr(rv, "allocation_suggestion", "") or
                           getattr(ctx, "allocation_suggestion", "") if ctx else "",
            )

            lines = [
                f"📅 {weekly.week_label}\n",
                f"市场展望: {weekly.market_outlook}",
                f"仓位建议: {weekly.position_suggestion}",
                f"风格偏好: {weekly.style_suggestion}",
            ]
            if weekly.focus_sectors:
                lines.append(f"关注板块: {', '.join(weekly.focus_sectors)}")
            if weekly.avoid_sectors:
                lines.append(f"回避板块: {', '.join(weekly.avoid_sectors)}")
            if weekly.rotation_outlook:
                lines.append(f"轮动研判: {weekly.rotation_outlook}")

            return SkillResult(ok=True, text="\n".join(lines))
        except Exception as e:
            _log.exception("周策略生成失败")
            return SkillResult(ok=False, text="", error=f"周策略生成失败: {e}")


class HelpSkill(BaseSkill):
    """帮助"""
    name = "help"
    description = "显示可用指令列表"
    triggers = ["帮助", "help", "指令", "命令", "?", "？"]
    usage = "帮助 / help"

    def execute(self, raw_input: str, params: dict) -> SkillResult:
        lines = [
            "🐲 隆小侠 — 可用指令\n",
            "📊 分析 <股票名/代码>  — 个股深度分析",
            "📈 回测 <股票代码> [日线/周线]  — 信号回测",
            "🌍 大盘 / 指数  — 大盘研判",
            "🏭 行业 / 板块  — 行业排行",
            "🔥 热点 / 主题  — 热门概念",
            "💬 舆情 <股票名>  — 社交热度",
            "📋 计划  — 盘前计划",
            "📅 周策略  — 周末策略",
            "❓ 帮助  — 显示本菜单",
            "\n示例: 分析 茅台、回测 600519 日线、舆情 宁德时代",
        ]
        return SkillResult(ok=True, text="\n".join(lines))


# ─────────────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────────────

def _resolve_symbol(query: str) -> str | None:
    """
    将用户输入（股票名/代码/Futu代码）解析为 Futu 格式代码。
    支持: 茅台 / 600519 / SZ.002759
    """
    query = query.strip()

    # 已是 Futu 格式
    if re.match(r"^(SH|SZ|BJ|HK|US)\.\w+$", query, re.IGNORECASE):
        return query.upper()

    # 纯6位数字
    if re.match(r"^\d{6}$", query):
        if query.startswith("6"):
            return f"SH.{query}"
        elif query.startswith(("0", "3")):
            return f"SZ.{query}"
        elif query.startswith(("8", "4")):
            return f"BJ.{query}"
        return f"SZ.{query}"

    # 5位数字 = 港股
    if re.match(r"^\d{5}$", query):
        return f"HK.{query}"

    # 中文名称 → 代码
    try:
        from signals.layers.industry import _build_name_to_code_map, _code6_to_futu
        name_map = _build_name_to_code_map()

        # 精确匹配
        if query in name_map:
            code6 = name_map[query]
            return _code6_to_futu(code6)

        # 模糊匹配（包含关键词）
        for name, code6 in name_map.items():
            if query in name or name in query:
                return _code6_to_futu(code6)
    except Exception:
        _log.debug("名称映射查询失败: %s", query, exc_info=True)

    return None


def _extract_code(query: str) -> str | None:
    """从文本中提取6位股票代码"""
    m = re.search(r"\d{6}", query)
    if m:
        return m.group()

    # 尝试名称解析
    symbol = _resolve_symbol(query)
    if symbol:
        return symbol.split(".")[-1]

    return None


# ─────────────────────────────────────────────────────
# Skill 注册表
# ─────────────────────────────────────────────────────

_BUILTIN_SKILLS: list[BaseSkill] = [
    HelpSkill(),
    StockAnalyzeSkill(),
    MarketOverviewSkill(),
    IndustryRankingSkill(),
    BacktestSkill(),
    SocialHeatSkill(),
    HotThemesSkill(),
    PlanSkill(),
    WeeklyStrategySkill(),
]

_custom_skills: list[BaseSkill] = []


def register_skill(skill: BaseSkill):
    """注册自定义 Skill"""
    _custom_skills.append(skill)
    _log.info("注册自定义 Skill: %s", skill.name)


def get_all_skills() -> list[BaseSkill]:
    """返回所有已注册的 Skills（内置 + 自定义）"""
    return _BUILTIN_SKILLS + _custom_skills
