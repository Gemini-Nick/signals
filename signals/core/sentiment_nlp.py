# -*- coding: utf-8 -*-
"""
情绪关键词 NLP — 基于预置词库的中文股票舆情分析

返回 -100 ~ +100 的情绪分数:
  +100 = 极度乐观
  0    = 中性
  -100 = 极度悲观
"""
from typing import Dict, List, Tuple

# ── 情绪词库 ──────────────────────────────────────────
# (词, 权重) — 权重越大影响越大

_BULLISH_WORDS: List[Tuple[str, float]] = [
    # 强买入信号
    ("涨停", 3.0), ("封板", 2.5), ("连板", 3.0), ("龙头", 2.0),
    ("突破", 2.0), ("放量", 1.5), ("强势", 1.5), ("拉升", 2.0),
    ("翻倍", 2.5), ("暴涨", 2.5), ("起飞", 2.0), ("井喷", 2.5),
    # 看多
    ("利好", 2.0), ("看多", 1.5), ("做多", 1.5), ("加仓", 1.5),
    ("抄底", 1.5), ("建仓", 1.0), ("买入", 1.5), ("满仓", 2.0),
    ("上车", 1.5), ("进场", 1.0), ("入场", 1.0),
    # 技术面
    ("金叉", 1.5), ("站上", 1.0), ("企稳", 1.0), ("反弹", 1.0),
    ("反转", 1.5), ("底部", 1.0), ("支撑", 0.8), ("蓄力", 1.0),
    # 基本面
    ("业绩预增", 2.0), ("超预期", 1.5), ("景气", 1.0), ("高增长", 1.5),
    ("订单", 1.0), ("中标", 1.5), ("放量", 1.0), ("国产替代", 1.5),
    # 政策
    ("政策利好", 2.0), ("降准", 1.5), ("降息", 1.5), ("刺激", 1.0),
    ("补贴", 1.0), ("扩产", 1.0),
    # 情绪
    ("看好", 1.0), ("牛市", 2.0), ("爆发", 1.5), ("机会", 0.8),
    ("潜力", 0.8), ("低估", 1.0), ("价值洼地", 1.5),
]

_BEARISH_WORDS: List[Tuple[str, float]] = [
    # 强卖出信号
    ("跌停", 3.0), ("暴跌", 2.5), ("崩盘", 3.0), ("闪崩", 2.5),
    ("跳水", 2.0), ("断崖", 2.5), ("天地板", 3.0),
    # 看空
    ("利空", 2.0), ("看空", 1.5), ("做空", 1.5), ("减仓", 1.5),
    ("清仓", 2.0), ("割肉", 2.0), ("止损", 1.5), ("卖出", 1.5),
    ("出逃", 2.0), ("跑路", 2.0), ("逃命", 2.5),
    # 套牢
    ("套牢", 2.0), ("被套", 2.0), ("深套", 2.5), ("亏损", 1.5),
    ("亏钱", 1.5), ("血亏", 2.5), ("赔钱", 1.5), ("巨亏", 2.5),
    # 风险
    ("暴雷", 2.5), ("爆雷", 2.5), ("退市", 3.0), ("ST", 2.0),
    ("违规", 1.5), ("造假", 2.5), ("欺诈", 2.5), ("立案", 2.0),
    # 技术面
    ("死叉", 1.5), ("破位", 1.5), ("跌破", 1.5), ("下行", 1.0),
    ("阴跌", 1.5), ("缩量", 0.8), ("空头", 1.0),
    # 基本面
    ("业绩下滑", 2.0), ("亏损扩大", 2.0), ("需求疲弱", 1.5),
    ("产能过剩", 1.5), ("价格战", 1.5),
    # 情绪
    ("恐慌", 2.0), ("熊市", 2.0), ("寒冬", 1.5), ("危险", 1.5),
    ("风险", 1.0), ("泡沫", 1.5), ("高估", 1.0), ("韭菜", 1.5),
    ("收割", 1.5), ("骗局", 2.0), ("庄家", 1.0),
]

# 预编译词库为 dict
_BULL_DICT: Dict[str, float] = {w: s for w, s in _BULLISH_WORDS}
_BEAR_DICT: Dict[str, float] = {w: s for w, s in _BEARISH_WORDS}


def analyze_sentiment(texts: List[str]) -> float:
    """
    分析文本列表的情绪分数。

    :param texts: 文本列表（帖子标题、评论等）
    :return: -100 ~ +100 的情绪分数
    """
    if not texts:
        return 0.0

    total_bull = 0.0
    total_bear = 0.0
    total_texts = len(texts)

    for text in texts:
        if not text:
            continue
        for word, weight in _BULL_DICT.items():
            if word in text:
                total_bull += weight
        for word, weight in _BEAR_DICT.items():
            if word in text:
                total_bear += weight

    total = total_bull + total_bear
    if total == 0:
        return 0.0

    # 归一化到 -100 ~ +100
    raw = (total_bull - total_bear) / total * 100

    # 按文本量做衰减（少量文本不应给极端分数）
    confidence_factor = min(1.0, total_texts / 20.0)
    return round(raw * confidence_factor, 1)


def classify_sentiment(score: float) -> str:
    """
    将情绪分数分类为标签。

    :param score: -100 ~ +100
    :return: 标签字符串
    """
    if score >= 60:
        return "极度乐观"
    elif score >= 30:
        return "偏乐观"
    elif score >= 10:
        return "轻度乐观"
    elif score >= -10:
        return "中性"
    elif score >= -30:
        return "轻度悲观"
    elif score >= -60:
        return "偏悲观"
    else:
        return "极度悲观"


def extract_keywords(text: str) -> List[str]:
    """提取文本中出现的情绪关键词"""
    found = []
    for word in _BULL_DICT:
        if word in text:
            found.append(f"+{word}")
    for word in _BEAR_DICT:
        if word in text:
            found.append(f"-{word}")
    return found
