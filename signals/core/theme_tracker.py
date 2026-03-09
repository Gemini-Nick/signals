# -*- coding: utf-8 -*-
"""
主题追踪器 (Theme Tracker)

将用户关注的投资主题（如"储能"、"算力"、"CLAW"）匹配到行业板块和概念板块，
输出命中板块及其涨跌状态。

复用 config.CONCEPT_TYPE_KEYWORDS 的模式，用关键词映射实现主题追踪。
"""
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd


# ─────────────────────────────────────────────────────────
# 主题 → 关键词映射
# ─────────────────────────────────────────────────────────

THEME_KEYWORD_MAP: dict = {
    "储能":   ["储能", "锂电池", "钠电池", "固态电池", "蓄能", "电池", "锂电"],
    "算力":   ["算力", "AI", "GPU", "服务器", "数据中心", "液冷", "光模块", "CPO",
               "交换机", "AI芯片"],
    "化工":   ["化工", "MDI", "纯碱", "钛白粉", "氟化工", "磷化工", "化学原料",
               "化学制品"],
    "CLAW":   ["算力", "AI", "低空", "机器人", "芯片", "半导体", "量子",
               "脑机", "无人驾驶"],
    "新能源": ["光伏", "风电", "新能源", "电池", "储能", "充电桩", "锂电"],
    "半导体": ["半导体", "芯片", "集成电路", "光刻", "封装", "存储", "先进封装"],
    "军工":   ["军工", "航天", "航空", "卫星", "商业航天", "导弹", "无人机"],
    "消费":   ["白酒", "乳品", "食品", "家电", "消费电子", "旅游", "免税"],
    "医药":   ["医药", "生物制药", "中药", "医疗器械", "CXO", "创新药"],
    "机器人": ["机器人", "人形机器人", "减速器", "伺服", "传感器"],
    "低空":   ["低空", "eVTOL", "飞行汽车", "通用航空", "无人机"],
    # 当前热点（舆论热度映射到底层旧概念体系）
    "OpenClaw": ["算力", "AI", "机器人", "低空", "芯片", "半导体", "量子",
                 "脑机", "无人驾驶", "光模块", "CPO", "服务器"],
    "龙虾":   ["算力", "AI", "机器人", "低空", "芯片", "半导体"],
    "电力":   ["电力", "电网", "特高压", "输变电", "配电", "智能电网"],
    "智驾":   ["智能驾驶", "无人驾驶", "自动驾驶", "车联网", "激光雷达"],
}


@dataclass
class ThemeHit:
    """单个主题的匹配结果"""
    theme: str                          # 主题名称
    matched_industries: List[str] = field(default_factory=list)  # 命中的行业板块
    matched_concepts: List[str] = field(default_factory=list)    # 命中的概念板块
    avg_change: Optional[float] = None  # 命中板块平均涨跌幅%
    status: str = "未知"                # "上涨" / "下跌" / "恐慌中" / "平盘"


def match_themes(
    themes: List[str],
    industry_name_df=None,
    concept_rankings: list = None,
    panic_level: str = "正常",
) -> List[ThemeHit]:
    """
    将用户主题匹配到行业/概念板块。

    :param themes: 用户关注的主题列表，如 ["储能", "算力"]
    :param industry_name_df: 东财行业板块 DataFrame (含板块名称+涨跌幅)
    :param concept_rankings: L2 概念排行 ConceptRanking/IndustryRanking 列表
    :param panic_level: 当前恐慌级别 ("恐慌"/"偏弱"/"正常")
    :return: ThemeHit 列表
    """
    results = []
    for theme in themes:
        theme_upper = theme.upper().strip()
        keywords = THEME_KEYWORD_MAP.get(theme_upper) or THEME_KEYWORD_MAP.get(theme.strip())
        if not keywords:
            # 没有预定义映射，把主题名本身作为关键词
            keywords = [theme.strip()]

        hit = ThemeHit(theme=theme.strip())

        # 匹配行业板块
        if industry_name_df is not None and not industry_name_df.empty:
            name_col = _find_name_col(industry_name_df)
            change_col = _find_change_col(industry_name_df)

            if name_col:
                for _, row in industry_name_df.iterrows():
                    board_name = str(row.get(name_col, ""))
                    if any(kw in board_name for kw in keywords):
                        hit.matched_industries.append(board_name)

                # 计算命中板块的平均涨跌幅
                if hit.matched_industries and change_col:
                    matched_rows = industry_name_df[
                        industry_name_df[name_col].isin(hit.matched_industries)
                    ]
                    changes = pd.to_numeric(matched_rows[change_col], errors='coerce')
                    if not changes.empty:
                        hit.avg_change = round(changes.mean(), 2)

        # 匹配概念板块
        if concept_rankings:
            for cr in concept_rankings:
                concept_name = getattr(cr, 'name', '') or getattr(cr, 'display_name', '')
                if any(kw in concept_name for kw in keywords):
                    hit.matched_concepts.append(concept_name)

        # 确定状态
        if hit.avg_change is not None:
            if panic_level == "恐慌":
                hit.status = "恐慌中"
            elif hit.avg_change > 0.5:
                hit.status = "上涨"
            elif hit.avg_change < -0.5:
                hit.status = "下跌"
            else:
                hit.status = "平盘"
        elif hit.matched_industries or hit.matched_concepts:
            hit.status = "已匹配"

        if hit.matched_industries or hit.matched_concepts:
            results.append(hit)

    return results


def format_theme_hits(hits: List[ThemeHit]) -> str:
    """格式化主题追踪结果为一行文本"""
    if not hits:
        return ""
    parts = []
    for h in hits:
        change_str = f"{h.avg_change:+.1f}%" if h.avg_change is not None else "N/A"
        status_str = f"({h.status})" if h.status not in ("未知", "已匹配") else ""
        parts.append(f"{h.theme}→{change_str}{status_str}")
    return " | ".join(parts)


def _find_name_col(df: pd.DataFrame) -> Optional[str]:
    """找板块名称列（兼容东财/THS/自定义列名）"""
    for col in ['板块名称', '板块', '名称', '行业', '概念名称', 'name',
                '行业名称', '板块名', '概念']:
        if col in df.columns:
            return col
    # 兜底: 取第一个 object 类型列
    for col in df.columns:
        if df[col].dtype == 'object':
            return col
    return None


def _find_change_col(df: pd.DataFrame) -> Optional[str]:
    """找涨跌幅列（兼容东财/THS/自定义列名）"""
    for col in ['涨跌幅', '涨跌幅(%)', '涨幅', '涨幅(%)', '最新涨跌幅',
                '最新涨幅', 'change_pct', '涨幅(%)']:
        if col in df.columns:
            return col
    return None
