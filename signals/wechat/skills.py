# -*- coding: utf-8 -*-
"""
WeChat Skills — 直接调用分析引擎，无需 Web 服务

行业分析和盘后复盘直接调底层函数，不走 HTTP。
"""
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

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
        ...

    def match(self, text: str) -> tuple[bool, dict]:
        for trigger in self.triggers:
            if trigger in text:
                idx = text.index(trigger) + len(trigger)
                remainder = text[idx:].strip()
                return True, {"query": remainder}
        return False, {}


# ─────────────────────────────────────────────────────
# Skill 1: 行业板块分析（直接调引擎）
# ─────────────────────────────────────────────────────

class IndustryAnalysisSkill(BaseSkill):
    """行业板块综合分析 — 涨幅榜 + 综合榜 + 超跌 + 概念排行"""
    name = "industry_analysis"
    description = "行业板块综合分析（涨幅 + 综合评分 + 超跌 + 概念排行）"
    triggers = ["行业", "板块", "排行"]
    usage = "行业 / 板块排行"

    def execute(self, raw_input: str, params: dict) -> SkillResult:
        try:
            from signals.layers.industry import get_industry_representatives
        except ImportError as e:
            return SkillResult(ok=False, text="", error=f"导入失败: {e}")

        try:
            result = get_industry_representatives()
            # 返回 5 或 6 个值，兼容两种签名
            if len(result) == 6:
                gain_list, composite_list, merged_list, concepts, oversold_list, sentiment_stats = result
            else:
                gain_list, composite_list, merged_list, concepts, oversold_list = result
                sentiment_stats = {}
        except Exception as e:
            _log.error("行业分析失败: %s", e)
            return SkillResult(ok=False, text="", error=f"行业数据获取失败: {e}")

        lines = []

        # 涨幅榜 Top 10
        if gain_list:
            lines.append("📊 行业涨幅榜 Top 10\n")
            for i, ind in enumerate(gain_list[:10], 1):
                pct = ind.gain_pct
                emoji = "🔴" if pct > 0 else "🟢"
                zt_str = f" 涨停{ind.zt_count}" if ind.zt_count else ""
                lines.append(f"{i}. {emoji} {ind.name}  {pct:+.2f}%{zt_str}")

        # 综合榜 Top 10
        if composite_list:
            lines.append("\n🏆 综合评分 Top 10\n")
            for i, ind in enumerate(composite_list[:10], 1):
                lines.append(f"{i}. {ind.name}  综合{ind.composite_score:.0f}  {ind.gain_pct:+.2f}%")

        # 超跌板块
        if oversold_list:
            oversold_top = [x for x in oversold_list[:5] if x.oversold_score > 0]
            if oversold_top:
                lines.append("\n🔻 超跌反弹候选\n")
                for ind in oversold_top:
                    lines.append(f"  · {ind.name}  超跌分{ind.oversold_score:.0f}")

        # 市场统计
        if isinstance(sentiment_stats, dict) and sentiment_stats:
            zt = sentiment_stats.get("zt_total", 0)
            dt = sentiment_stats.get("dt_total", 0)
            # 计算红盘比例
            red_pct = 0
            name_df = sentiment_stats.get("name_df")
            if name_df is not None:
                try:
                    import pandas as pd
                    for col in ['涨跌幅', '涨跌幅(%)', '涨幅', '涨幅(%)', '最新涨跌幅']:
                        if col in name_df.columns:
                            vals = pd.to_numeric(name_df[col], errors='coerce')
                            total = vals.count()
                            red = (vals > 0).sum()
                            red_pct = round(red / total * 100) if total > 0 else 0
                            break
                except Exception:
                    pass
            lines.append(f"\n📈 涨停{zt}  跌停{dt}  上涨占比{red_pct:.0f}%")

        # 概念排行
        if "概念" in raw_input and concepts:
            lines.append("\n🔥 概念板块 Top 10\n")
            for i, c in enumerate(concepts[:10], 1):
                up_total = c.up_count + c.down_count
                up_ratio = c.up_count / up_total if up_total > 0 else 0
                lines.append(
                    f"{i}. {c.name}  {c.gain_pct:+.2f}%  "
                    f"综合{c.composite_score:.0f}  上涨{up_ratio:.0%}"
                )

        if not lines:
            return SkillResult(ok=False, text="", error="未获取到行业数据")

        return SkillResult(ok=True, text="\n".join(lines))


# ─────────────────────────────────────────────────────
# Skill 2: 盘后复盘（直接调引擎）
# ─────────────────────────────────────────────────────

class ReviewSkill(BaseSkill):
    """盘后复盘 — L1 指数 + L2 行业 + L3 个股三层联动"""
    name = "review"
    description = "盘后复盘（指数 + 行业 + 标的三层联动分析）"
    triggers = ["复盘", "盘后", "review"]
    usage = "复盘 / 盘后复盘"

    def execute(self, raw_input: str, params: dict) -> SkillResult:
        query = params.get("query", "").strip()
        start_date = query if query else "yesterday"

        lines = [f"📋 盘后复盘 — {start_date}\n"]
        timing = {}

        # ── L1: 指数复盘 ──
        try:
            _log.info("[复盘] L1 开始 — 指数分析")
            t0 = time.monotonic()
            from signals.layers.index_screener import IndexScreener
            screener = IndexScreener()
            ctx = screener.run_review(start_date)
            timing["L1"] = round(time.monotonic() - t0, 1)
            _log.info("[复盘] L1 完成 — %.1fs", timing["L1"])

            if ctx:
                if ctx.direction:
                    lines.append(f"🌍 方向: {ctx.direction}")
                if ctx.sentiment_phase:
                    lines.append(f"🎭 情绪: {ctx.sentiment_phase}")

                # 主要指数
                main_names = {"沪深300", "上证50", "创业板指"}
                main_idx = [r for r in (ctx.reports or []) if r.name in main_names]
                if main_idx:
                    lines.append("\n━━ 主要指数 ━━")
                    for idx in main_idx:
                        change = getattr(idx, 'change_pct', 0)
                        trend = getattr(idx, 'trend', '')
                        emoji = "🔴" if change > 0 else "🟢"
                        lines.append(f"{emoji} {idx.name}  {change:+.2f}%  {trend}")
        except Exception as e:
            _log.error("[复盘] L1 失败: %s", e)
            lines.append(f"⚠️ 指数分析失败: {e}")
            ctx = None

        # ── L2: 行业排行 + 轮动 ──
        gain_list, composite_list, merged_list = [], [], []
        try:
            _log.info("[复盘] L2 开始 — 行业排行")
            t0 = time.monotonic()
            from signals.layers.industry import get_industry_representatives
            from datetime import datetime
            import config
            pool_date = datetime.now().strftime("%Y%m%d")
            result = get_industry_representatives(config.RANK_TOP_N, date_str=pool_date)
            if len(result) == 6:
                gain_list, composite_list, merged_list, concepts, oversold_list, _ = result
            else:
                gain_list, composite_list, merged_list, concepts, oversold_list = result
            timing["L2"] = round(time.monotonic() - t0, 1)
            _log.info("[复盘] L2 完成 — %.1fs (%d 行业)", timing["L2"], len(merged_list))

            if gain_list:
                lines.append("\n🏭 行业涨幅 Top 5")
                for ind in gain_list[:5]:
                    emoji = "🔴" if ind.gain_pct > 0 else "🟢"
                    lines.append(f"  {emoji} {ind.name} {ind.gain_pct:+.2f}%")

            # 轮动研判
            if ctx and (gain_list or composite_list):
                try:
                    from signals.core.rotation import detect_rotation_stage, suggest_allocation
                    rot = detect_rotation_stage(gain_list, composite_list)
                    _, alloc_str = suggest_allocation(rot, ctx.sentiment_phase)
                    if rot.stage:
                        lines.append(f"\n🔄 轮动: {rot.stage}")
                    if alloc_str:
                        lines.append(f"💡 建议: {alloc_str}")
                except Exception:
                    pass
        except Exception as e:
            _log.error("[复盘] L2 失败: %s", e)
            lines.append(f"\n⚠️ 行业分析失败: {e}")

        # ── L3: 个股复盘 ──
        try:
            _log.info("[复盘] L3 开始 — 个股分析")
            t0 = time.monotonic()
            import config
            ranking_stocks = []
            for r in merged_list:
                ranking_stocks.extend(r.pool_codes)
            ranking_stocks = list(dict.fromkeys(ranking_stocks))
            all_symbols = list(dict.fromkeys(config.WHITELIST + ranking_stocks))
            _log.info("[复盘] L3 标的: %d 只", len(all_symbols))

            from signals.layers.review_screener import review_stock_daily
            scored = review_stock_daily(all_symbols, start_date)

            # 注入名称
            try:
                from signals.core.stock_names import get_resolver
                resolver = get_resolver()
                if merged_list:
                    resolver.inject_from_rankings(merged_list)
                for s in scored:
                    if not s.name:
                        s.name = resolver.get_name(s.symbol)
            except Exception:
                pass

            timing["L3"] = round(time.monotonic() - t0, 1)
            _log.info("[复盘] L3 完成 — %.1fs (%d 个股)", timing["L3"], len(scored))

            if scored:
                lines.append("\n🎯 标的信号 Top 5")
                for s in scored[:5]:
                    display = f"{s.name}({s.symbol})" if s.name else s.symbol
                    direction = getattr(s, 'direction', '')
                    total_score = getattr(s, 'total_score', 0)
                    lines.append(f"  {display}  评分{total_score:.0f}  {direction}")
        except Exception as e:
            _log.error("[复盘] L3 失败: %s", e)
            lines.append(f"\n⚠️ 个股分析失败: {e}")

        total = sum(timing.values())
        lines.append(f"\n⏱ 耗时 {total:.0f}s (L1:{timing.get('L1', 0):.0f}s L2:{timing.get('L2', 0):.0f}s L3:{timing.get('L3', 0):.0f}s)")

        return SkillResult(ok=True, text="\n".join(lines))


# ─────────────────────────────────────────────────────
# Skill 3: 帮助
# ─────────────────────────────────────────────────────

class HelpSkill(BaseSkill):
    """帮助"""
    name = "help"
    description = "显示可用指令列表"
    triggers = ["帮助", "help", "指令", "命令", "?", "？"]
    usage = "帮助 / help"

    def execute(self, raw_input: str, params: dict) -> SkillResult:
        lines = [
            "🐲 隆小侠 — 可用指令\n",
            "🏭 行业 / 板块  — 行业板块分析",
            "📋 复盘 / 盘后  — 盘后复盘分析",
            "",
            "以下由 AI 直接回答:",
            "📊 分析 <股票>  — 个股分析",
            "📈 回测 <代码>  — 信号回测",
            "🔥 热点 / 主题  — 热门概念",
            "💬 舆情 <股票>  — 社交热度",
            "📋 计划  — 盘前计划",
            "📅 周策略  — 周末策略",
            "❓ 帮助  — 显示本菜单",
            "\n💡 任何其他问题直接问即可",
        ]
        return SkillResult(ok=True, text="\n".join(lines))


# ─────────────────────────────────────────────────────
# Skill 注册表
# ─────────────────────────────────────────────────────

_BUILTIN_SKILLS: list[BaseSkill] = [
    HelpSkill(),
    IndustryAnalysisSkill(),
    ReviewSkill(),
]

_custom_skills: list[BaseSkill] = []


def register_skill(skill: BaseSkill):
    """注册自定义 Skill"""
    _custom_skills.append(skill)
    _log.info("注册自定义 Skill: %s", skill.name)


def get_all_skills() -> list[BaseSkill]:
    """返回所有已注册的 Skills（内置 + 自定义）"""
    return _BUILTIN_SKILLS + _custom_skills
