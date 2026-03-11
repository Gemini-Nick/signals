# -*- coding: utf-8 -*-
"""
主题→标的发现引擎

输入关键词 (如 "昇腾", "算力", "机器人"), 自动发现关联标的:
1. 匹配东财概念板块 → 获取成分股
2. 聚合千股千评数据 → 按热度+综合得分排序
3. (可选) 交叉技术面信号
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from signals.data.social_fetcher import (
    fetch_comment_data,
    fetch_concept_list,
    fetch_concept_stocks,
    fetch_weibo_sentiment,
    get_stock_comment,
    search_concepts,
)

_log = logging.getLogger("signals.theme")


@dataclass
class ThemeStock:
    """主题关联标的"""
    symbol: str                       # "SZ.002261"
    name: str = ""                    # "拓维信息"
    code: str = ""                    # "002261"
    change_pct: float = 0.0           # 今日涨跌幅
    price: float = 0.0                # 最新价
    # 千股千评
    comment_score: float = 0.0        # 综合得分
    comment_rank: int = 0             # 排名
    focus_index: float = 0.0          # 关注指数
    institution_pct: float = 0.0      # 机构参与度
    # 聚合
    relevance_score: float = 0.0      # 综合关联度评分 (0-100)
    concepts: List[str] = field(default_factory=list)  # 所属概念 ["华为昇腾","算力概念"]
    heat_grade: str = ""              # "爆热"/"热门"/"温和"/"冷门"


@dataclass
class ThemeDiscoveryResult:
    """主题发现结果"""
    theme: str                        # 搜索关键词
    matched_concepts: List[str] = field(default_factory=list)   # 匹配到的概念名
    discovered_stocks: List[ThemeStock] = field(default_factory=list)  # 关联标的 (已排序)
    total_stocks: int = 0
    sentiment_summary: str = ""       # "整体看多"/"分歧"/"整体看空"


@dataclass
class HotTheme:
    """热门主题"""
    name: str                         # 概念名
    code: str = ""                    # 板块代码
    change_pct: float = 0.0           # 板块涨跌幅
    stock_count: int = 0              # 成分股数


def discover_theme(keyword: str) -> ThemeDiscoveryResult:
    """
    主题标的发现: 输入关键词 → 返回关联标的排名。

    流程:
    1. keyword 模糊匹配东财概念板块
    2. 获取所有匹配概念的成分股
    3. 合并去重, 聚合千股千评数据
    4. 按 relevance_score 排序
    """
    result = ThemeDiscoveryResult(theme=keyword)

    # 1. 搜索匹配概念
    concepts = search_concepts(keyword)
    if not concepts:
        _log.info(f"主题 [{keyword}] 未匹配到任何概念板块")
        return result

    result.matched_concepts = [c["name"] for c in concepts]
    _log.info(f"主题 [{keyword}] 匹配概念: {result.matched_concepts}")

    # 2. 获取成分股 (合并多个概念)
    all_stocks: Dict[str, dict] = {}  # code → stock_info
    stock_concepts: Dict[str, List[str]] = {}  # code → [concept_name, ...]

    for concept in concepts[:5]:  # 最多5个概念, 避免太慢
        theme = fetch_concept_stocks(concept["name"])
        for s in theme.stocks:
            code = s["code"]
            if code not in all_stocks:
                all_stocks[code] = s
                stock_concepts[code] = []
            stock_concepts[code].append(concept["name"])

    if not all_stocks:
        return result

    # 3. 聚合千股千评
    comment_df = fetch_comment_data()

    discovered = []
    for code, info in all_stocks.items():
        # 标准化 symbol
        prefix = "SH" if code.startswith(("6", "5")) else \
                 "SZ" if code.startswith(("0", "3")) else "BJ"
        symbol = f"{prefix}.{code}"

        stock = ThemeStock(
            symbol=symbol,
            name=info.get("name", ""),
            code=code,
            change_pct=info.get("change_pct", 0),
            price=info.get("price", 0),
            concepts=stock_concepts.get(code, []),
        )

        # 千股千评数据
        comment = get_stock_comment(code, df=comment_df)
        if comment:
            stock.comment_score = comment.get("score", 0)
            stock.comment_rank = comment.get("rank", 0)
            stock.focus_index = comment.get("focus_index", 0)
            stock.institution_pct = comment.get("institution_pct", 0)

        # 综合关联度
        stock.relevance_score = _compute_relevance(stock)
        stock.heat_grade = _heat_grade(stock.relevance_score)

        discovered.append(stock)

    # 4. 排序
    discovered.sort(key=lambda s: s.relevance_score, reverse=True)
    result.discovered_stocks = discovered
    result.total_stocks = len(discovered)

    # 5. 情绪汇总
    positive = sum(1 for s in discovered if s.change_pct > 0)
    negative = sum(1 for s in discovered if s.change_pct < 0)
    total = len(discovered)
    if total > 0:
        ratio = positive / total
        if ratio > 0.65:
            result.sentiment_summary = "整体看多"
        elif ratio < 0.35:
            result.sentiment_summary = "整体看空"
        else:
            result.sentiment_summary = "分歧"

    _log.info(f"主题 [{keyword}] 发现 {len(discovered)} 只标的, {result.sentiment_summary}")
    return result


def get_hot_themes(top_n: int = 15) -> List[HotTheme]:
    """
    获取当日热门主题 (按涨跌幅排序的概念板块Top N)。
    """
    df = fetch_concept_list()
    if df.empty:
        return []

    # 按涨跌幅排序
    if "涨跌幅" in df.columns:
        sorted_df = df.nlargest(top_n, "涨跌幅")
    else:
        sorted_df = df.head(top_n)

    themes = []
    for _, row in sorted_df.iterrows():
        themes.append(HotTheme(
            name=str(row.get("板块名称", "")),
            code=str(row.get("板块代码", "")),
            change_pct=float(row.get("涨跌幅", 0)),
            stock_count=int(row.get("成份股数量", 0)) if "成份股数量" in row.index else 0,
        ))

    return themes


def get_surge_stocks(top_n: int = 10) -> List[dict]:
    """
    获取千评综合得分最高的飙升标的 (关注指数高+得分高)。
    用作Dashboard"飙升关注"区域。
    """
    df = fetch_comment_data()
    if df.empty:
        return []

    # 按综合得分排序, 取Top N
    if "综合得分" in df.columns:
        top = df.nlargest(top_n, "综合得分")
    else:
        return []

    results = []
    for _, row in top.iterrows():
        code = str(row.get("代码", ""))
        prefix = "SH" if code.startswith(("6", "5")) else \
                 "SZ" if code.startswith(("0", "3")) else "BJ"
        results.append({
            "symbol": f"{prefix}.{code}",
            "name": str(row.get("名称", "")),
            "code": code,
            "score": float(row.get("综合得分", 0)),
            "focus_index": float(row.get("关注指数", 0)),
            "change_pct": float(row.get("涨跌幅", 0)),
        })

    return results


# ─────────────────────────────────────────────────────────
# 评分辅助
# ─────────────────────────────────────────────────────────

def _compute_relevance(stock: ThemeStock) -> float:
    """计算主题关联度 (0-100)"""
    score = 0.0

    # 千评综合得分 (权重40%)
    if stock.comment_score > 0:
        score += stock.comment_score * 0.4

    # 关注指数 (权重20%)
    if stock.focus_index > 0:
        focus_norm = min(max((stock.focus_index - 50) / 45.0, 0), 1) * 100
        score += focus_norm * 0.2

    # 多概念关联加分 (权重20%)
    concept_bonus = min(len(stock.concepts) * 15, 100)
    score += concept_bonus * 0.2

    # 涨跌幅方向 (权重10%)
    if stock.change_pct > 3:
        score += 10
    elif stock.change_pct > 0:
        score += 5

    # 机构参与度 (权重10%)
    if stock.institution_pct > 0:
        score += stock.institution_pct * 100 * 0.1

    return round(min(score, 100), 1)


def _heat_grade(score: float) -> str:
    if score >= 75:
        return "爆热"
    elif score >= 50:
        return "热门"
    elif score >= 25:
        return "温和"
    else:
        return "冷门"
