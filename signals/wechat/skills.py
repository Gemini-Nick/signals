# -*- coding: utf-8 -*-
"""
WeChat Skills — 仅保留需要调用 Web API 的技能

只有两类技能需要通过脚本调用（依赖运行中的 Web 服务）：
1. 行业板块分析 → GET /api/industry/ranking + /api/industry/concept-ranking
2. 盘后复盘     → POST /api/review/run → GET /api/review/status → GET /api/review/results

其余所有需求由 Claude Code 直接用自身能力回答。
"""
import json
import logging
import time
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from dataclasses import dataclass

_log = logging.getLogger("signals.wechat.skills")

WEB_BASE = "http://127.0.0.1:8000/api"


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


def _api_get(path: str, timeout: int = 30) -> dict | None:
    """GET 请求 Web API"""
    url = f"{WEB_BASE}{path}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        _log.error("API GET %s 失败: %s", path, e)
        return None


def _api_post(path: str, body: dict | None = None, timeout: int = 30) -> dict | None:
    """POST 请求 Web API"""
    url = f"{WEB_BASE}{path}"
    try:
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        _log.error("API POST %s 失败: %s", path, e)
        return None


# ─────────────────────────────────────────────────────
# Skill 1: 行业板块分析（调用 Web API）
# ─────────────────────────────────────────────────────

class IndustryAnalysisSkill(BaseSkill):
    """行业板块综合分析 — 涨幅榜 + 综合榜 + 超跌 + 概念排行"""
    name = "industry_analysis"
    description = "行业板块综合分析（涨幅 + 综合评分 + 超跌 + 概念排行）"
    triggers = ["行业", "板块", "排行"]
    usage = "行业 / 板块排行"

    def execute(self, raw_input: str, params: dict) -> SkillResult:
        # 行业排行
        ranking = _api_get("/industry/ranking")
        if not ranking:
            return SkillResult(ok=False, text="", error="Web 服务未启动或行业数据未就绪，请确认 python run.py --mode web 正在运行")

        # 如果还在 loading
        if ranking.get("loading"):
            return SkillResult(ok=False, text="", error="行业数据正在加载中，请稍后再试")

        lines = []

        # 涨幅榜 Top 10
        gain_list = ranking.get("gain_list", [])[:10]
        if gain_list:
            lines.append("📊 行业涨幅榜 Top 10\n")
            for i, ind in enumerate(gain_list, 1):
                pct = ind.get("gain_pct", 0)
                emoji = "🔴" if pct > 0 else "🟢"
                name = ind.get("display_name") or ind.get("name", "?")
                zt = ind.get("zt_count", 0)
                zt_str = f" 涨停{zt}" if zt else ""
                lines.append(f"{i}. {emoji} {name}  {pct:+.2f}%{zt_str}")

        # 综合榜 Top 10
        comp_list = ranking.get("composite_list", [])[:10]
        if comp_list:
            lines.append("\n🏆 综合评分 Top 10\n")
            for i, ind in enumerate(comp_list, 1):
                name = ind.get("display_name") or ind.get("name", "?")
                score = ind.get("composite_score", 0)
                pct = ind.get("gain_pct", 0)
                lines.append(f"{i}. {name}  综合{score:.0f}  {pct:+.2f}%")

        # 超跌板块
        oversold = ranking.get("oversold_list", [])[:5]
        if oversold:
            lines.append("\n🔻 超跌反弹候选\n")
            for ind in oversold:
                name = ind.get("display_name") or ind.get("name", "?")
                score = ind.get("oversold_score", 0)
                if score > 0:
                    lines.append(f"  · {name}  超跌分{score:.0f}")

        # 市场统计
        stats = ranking.get("stats", {})
        if stats:
            zt = stats.get("zt_total", 0)
            dt = stats.get("dt_total", 0)
            red_pct = stats.get("red_pct", 0)
            lines.append(f"\n📈 涨停{zt}  跌停{dt}  上涨占比{red_pct:.0f}%")

        # 概念排行
        query = params.get("query", "")
        if "概念" in raw_input or "概念" in query:
            concepts = _api_get("/industry/concept-ranking")
            if concepts and isinstance(concepts, list):
                lines.append("\n🔥 概念板块 Top 10\n")
                for i, c in enumerate(concepts[:10], 1):
                    name = c.get("name", "?")
                    score = c.get("composite_score", 0)
                    pct = c.get("change_pct", 0)
                    up_ratio = c.get("up_ratio", 0)
                    lines.append(
                        f"{i}. {name}  {pct:+.2f}%  "
                        f"综合{score:.0f}  上涨{up_ratio:.0%}"
                    )

        if not lines:
            return SkillResult(ok=False, text="", error="未获取到行业数据")

        return SkillResult(ok=True, text="\n".join(lines))


# ─────────────────────────────────────────────────────
# Skill 2: 盘后复盘（调用 Web API，异步轮询）
# ─────────────────────────────────────────────────────

class ReviewSkill(BaseSkill):
    """盘后复盘 — 触发三层联动分析，轮询等待完成"""
    name = "review"
    description = "盘后复盘（指数 + 行业 + 标的三层联动分析）"
    triggers = ["复盘", "盘后", "review"]
    usage = "复盘 / 盘后复盘 / 复盘 yesterday"

    def execute(self, raw_input: str, params: dict) -> SkillResult:
        query = params.get("query", "").strip()
        start_date = query if query else "yesterday"

        # 1) 触发复盘任务
        resp = _api_post("/review/run", {"start_date": start_date})
        if not resp or not resp.get("ok"):
            return SkillResult(ok=False, text="", error="复盘任务启动失败，请确认 Web 服务正在运行")

        label = resp.get("label", start_date)

        # 2) 轮询进度（最多等 5 分钟）
        max_wait = 300
        elapsed = 0
        while elapsed < max_wait:
            time.sleep(5)
            elapsed += 5
            status = _api_get("/review/status")
            if not status:
                continue
            if status.get("error"):
                return SkillResult(ok=False, text="", error=f"复盘出错: {status['error']}")
            if status.get("completed"):
                break
        else:
            return SkillResult(ok=False, text="", error="复盘超时（5分钟），请检查 Web 服务日志")

        # 3) 获取结果
        results = _api_get("/review/results", timeout=60)
        if not results:
            return SkillResult(ok=False, text="", error="获取复盘结果失败")

        return SkillResult(ok=True, text=_format_review(results, label))


def _format_review(r: dict, label: str) -> str:
    """将复盘 API 结果格式化为微信纯文本"""
    lines = [f"📋 盘后复盘 — {label}\n"]

    # 大盘环境
    banner = r.get("banner", {})
    if banner:
        direction = banner.get("direction", "")
        emotion = banner.get("emotion_stage", "")
        alloc = banner.get("allocation_suggestion", "")
        if direction:
            lines.append(f"🌍 方向: {direction}")
        if emotion:
            lines.append(f"🎭 情绪: {emotion}")
        if alloc:
            lines.append(f"💰 仓位: {alloc}")

    # 指数摘要（只取主要3个）
    idx_reports = r.get("index_reports", [])
    main_names = {"沪深300", "上证50", "创业板指"}
    main_idx = [x for x in idx_reports if x.get("name") in main_names]
    if main_idx:
        lines.append("\n━━ 主要指数 ━━")
        for idx in main_idx:
            name = idx.get("name", "?")
            trend = idx.get("trend", "")
            change = idx.get("change_pct", 0)
            emoji = "🔴" if change > 0 else "🟢"
            lines.append(f"{emoji} {name}  {change:+.2f}%  {trend}")

    # 行业涨幅 Top 5
    gain_list = r.get("gain_list", [])[:5]
    if gain_list:
        lines.append("\n🏭 行业涨幅 Top 5")
        for ind in gain_list:
            name = ind.get("display_name") or ind.get("name", "?")
            pct = ind.get("gain_pct", 0)
            emoji = "🔴" if pct > 0 else "🟢"
            lines.append(f"  {emoji} {name} {pct:+.2f}%")

    # 轮动研判
    rotation = r.get("rotation", {})
    if rotation:
        stage = rotation.get("stage", "")
        alloc = rotation.get("allocation", "")
        if stage:
            lines.append(f"\n🔄 轮动: {stage}")
        if alloc:
            lines.append(f"💡 建议: {alloc}")

    # 标的信号 Top 5
    scored = r.get("scored_symbols", [])[:5]
    if scored:
        lines.append("\n🎯 标的信号 Top 5")
        for s in scored:
            symbol = s.get("symbol", "?")
            name = s.get("name", "")
            score = s.get("total_score", 0)
            direction = s.get("direction", "")
            display = f"{name}({symbol})" if name else symbol
            lines.append(f"  {display}  评分{score:.0f}  {direction}")

    return "\n".join(lines)


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
            "🏭 行业 / 板块  — 行业板块分析（需 Web 服务）",
            "📋 复盘 / 盘后  — 盘后复盘分析（需 Web 服务）",
            "",
            "以下由 AI 直接回答，无需 Web 服务:",
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
