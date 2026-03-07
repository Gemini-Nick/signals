# -*- coding: utf-8 -*-
"""
轮动线检测：科技/顺周期/消费 三线轮动阶段识别。

依赖：config.ROTATION_LINE_MAP + Layer 2 双榜 Top N 行业数据。

用法：
    from signals.core.rotation import detect_rotation_stage, RotationStage
    stage = detect_rotation_stage(gain_list, composite_list)
"""
from dataclasses import dataclass
from typing import List, Optional, Tuple

import config


# 轮动线展示图标
_LINE_ICON = {
    "科技": "💡",
    "顺周期": "🔄",
    "消费": "🛒",
    "新能源": "⚡",
    "主题": "🎯",
    "公用": "🏛",
}


@dataclass
class RotationStage:
    """轮动阶段识别结果"""
    stage: str                 # "科技领涨" / "顺周期领涨" / "消费领涨" / "混沌"
    dominant_line: str         # "科技" / "顺周期" / "消费" / ""
    line_distribution: dict    # {"科技": 5, "顺周期": 3, "消费": 2, ...}
    detail: str                # 人类可读描述

    @property
    def icon(self) -> str:
        return _LINE_ICON.get(self.dominant_line, "")

    def format_line(self) -> str:
        """格式化输出：轮动阶段 + 分布"""
        if self.stage == "混沌":
            return f"轮动: 混沌（{self._dist_str()}）"
        return f"轮动: {self.icon}{self.stage}（{self._dist_str()}）"

    def _dist_str(self) -> str:
        """轮动线分布字符串：科技5/顺周期3/消费2"""
        parts = []
        for line in ("科技", "顺周期", "消费", "新能源", "主题", "公用"):
            cnt = self.line_distribution.get(line, 0)
            if cnt > 0:
                parts.append(f"{line}{cnt}")
        return "/".join(parts) if parts else "无数据"


def get_rotation_line(industry_name: str) -> str:
    """查询行业所属轮动线。未映射返回空字符串。"""
    return config.ROTATION_LINE_MAP.get(industry_name, "")


def _count_lines(rankings, top_n: int = 10) -> dict:
    """
    统计排行榜 Top N 行业中各轮动线占比。
    rankings: IndustryRanking 列表（已按排名排序）。
    """
    counts: dict = {}
    for r in rankings[:top_n]:
        line = get_rotation_line(r.name)
        if line:
            counts[line] = counts.get(line, 0) + 1
    return counts


def detect_rotation_stage(
    gain_list: list,
    composite_list: list,
    top_n: int = 10,
) -> RotationStage:
    """
    根据双榜 Top N 行业中各轮动线占比，识别当前轮动阶段。

    判定规则：
    - 某线在两榜合计中占比 >= 40%  → 该线领涨
    - 多线占比接近（差 < 15%）     → 混沌
    - 合并两榜去重后统计

    :param gain_list: 涨幅榜 IndustryRanking 列表
    :param composite_list: 综合榜 IndustryRanking 列表
    :param top_n: 取各榜前 N 名统计
    """
    # 合并两榜行业名（去重）
    gain_counts = _count_lines(gain_list, top_n)
    comp_counts = _count_lines(composite_list, top_n)

    # 合并计数
    merged: dict = {}
    all_lines = set(gain_counts.keys()) | set(comp_counts.keys())
    for line in all_lines:
        merged[line] = gain_counts.get(line, 0) + comp_counts.get(line, 0)

    total = sum(merged.values())
    if total == 0:
        return RotationStage(
            stage="混沌",
            dominant_line="",
            line_distribution=merged,
            detail="无行业轮动数据",
        )

    # 三大主线占比
    main_lines = ("科技", "顺周期", "消费")
    line_pcts = {line: merged.get(line, 0) / total * 100 for line in main_lines}

    # 找最强线
    sorted_lines = sorted(line_pcts.items(), key=lambda x: -x[1])
    top_line, top_pct = sorted_lines[0]
    second_line, second_pct = sorted_lines[1]

    # 判定
    if top_pct >= 40 and (top_pct - second_pct) >= 15:
        stage = f"{top_line}领涨"
        dominant = top_line
    elif top_pct >= 35:
        stage = f"{top_line}偏强"
        dominant = top_line
    else:
        stage = "混沌"
        dominant = ""

    detail_parts = [f"{l}: {merged.get(l, 0)}({p:.0f}%)"
                    for l, p in sorted_lines if p > 0]
    detail = " | ".join(detail_parts)

    return RotationStage(
        stage=stage,
        dominant_line=dominant,
        line_distribution=merged,
        detail=detail,
    )


# ─────────────────────────────────────────────────────────
# 板块配置比例建议
# ─────────────────────────────────────────────────────────

# 基于轮动阶段+情绪周期的配置模板
# key: (dominant_line, sentiment_phase)
# value: dict of line→pct (must sum to 100)
_ALLOCATION_TEMPLATES = {
    # ── 科技领涨 ──
    ("科技", "亢奋"):   {"科技": 30, "顺周期": 15, "消费": 15, "现金": 40},
    ("科技", "修复"):   {"科技": 40, "顺周期": 20, "消费": 10, "现金": 30},
    ("科技", "恐慌"):   {"科技": 20, "顺周期": 10, "消费": 10, "现金": 60},
    ("科技", "回落"):   {"科技": 25, "顺周期": 20, "消费": 15, "现金": 40},
    # ── 顺周期领涨 ──
    ("顺周期", "亢奋"): {"顺周期": 30, "消费": 15, "科技": 10, "现金": 45},
    ("顺周期", "修复"): {"顺周期": 40, "消费": 20, "科技": 10, "现金": 30},
    ("顺周期", "恐慌"): {"顺周期": 20, "消费": 10, "科技": 5, "现金": 65},
    ("顺周期", "回落"): {"顺周期": 25, "消费": 20, "科技": 10, "现金": 45},
    # ── 消费领涨 ──
    ("消费", "亢奋"):   {"消费": 30, "顺周期": 15, "科技": 10, "现金": 45},
    ("消费", "修复"):   {"消费": 40, "顺周期": 15, "科技": 15, "现金": 30},
    ("消费", "恐慌"):   {"消费": 15, "顺周期": 10, "科技": 5, "现金": 70},
    ("消费", "回落"):   {"消费": 25, "顺周期": 15, "科技": 15, "现金": 45},
}

# 混沌阶段的默认配置
_DEFAULT_ALLOCATION = {
    "亢奋":  {"科技": 15, "顺周期": 15, "消费": 15, "现金": 55},
    "修复":  {"科技": 20, "顺周期": 20, "消费": 20, "现金": 40},
    "恐慌":  {"科技": 10, "顺周期": 10, "消费": 10, "现金": 70},
    "回落":  {"科技": 15, "顺周期": 15, "消费": 15, "现金": 55},
    "未知":  {"科技": 15, "顺周期": 15, "消费": 15, "现金": 55},
}


def suggest_allocation(
    rotation: RotationStage,
    sentiment_phase: str = "未知",
) -> Tuple[dict, str]:
    """
    基于轮动阶段+情绪周期输出板块配置比例。

    Returns:
        (allocation_dict, formatted_string)
        allocation_dict: {"科技": 40, "顺周期": 20, ...}
        formatted_string: "科技40% | 顺周期20% | 消费10% | 现金30%"
    """
    key = (rotation.dominant_line, sentiment_phase)
    alloc = _ALLOCATION_TEMPLATES.get(key)
    if alloc is None:
        alloc = _DEFAULT_ALLOCATION.get(sentiment_phase,
                                         _DEFAULT_ALLOCATION["未知"])

    # 格式化
    parts = [f"{k}{v}%" for k, v in alloc.items() if v > 0]
    label = f"{rotation.stage}+{sentiment_phase}" if rotation.stage != "混沌" else f"混沌+{sentiment_phase}"
    formatted = f"配置建议（{label}）: {' | '.join(parts)}"

    return alloc, formatted
