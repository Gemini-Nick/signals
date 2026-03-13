# -*- coding: utf-8 -*-
"""
行业 / 概念板块主题聚合分析 — 先分主题再聚合评分

算法：主题关键词分类 → groupby 聚合 → 加权综合评分 → Top N
替代旧的 sklearn AgglomerativeClustering（纯数值聚类会把涨幅相近但
主题不同的板块分到一组，导致标签失准）。

用法：
    from signals.core.clustering import cluster_industries, cluster_concepts
    result = cluster_industries(top_n=3)
"""
import re
import logging
from datetime import datetime

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)

# ── 统一主题关键词词典（行业 + 概念共用） ─────────────────────
# 匹配规则：板块名包含任一关键词即归入该主题
# 多主题命中时取匹配关键词数最多的
THEME_KEYWORDS = {
    "化工": [
        "化工", "肥", "磷", "氮", "钾", "农化", "涂料", "钛白粉",
        "氟", "有机硅", "粘胶", "橡胶", "塑料",
    ],
    "周期资源": [
        "煤", "钢", "铁", "金属", "矿", "冶", "焦", "铝", "铜",
        "锌", "钨", "钴", "盐湖", "稀土", "黄金", "贵金属", "有色",
        "造纸",
    ],
    "新能源": [
        "光伏", "风电", "风能", "储能", "电池", "充电", "氢能", "燃料电池",
        "电解液", "正极", "负极", "隔膜", "钠电", "固态电池",
        "锂电", "锂矿", "锂",
    ],
    "科技硬件": [
        "半导体", "芯片", "电子", "元件", "PCB", "印制电路", "光学",
        "传感器", "LED", "光模块", "先进封装", "CPO", "存储",
        "消费电子",
    ],
    "AI软件": [
        "软件", "互联网", "IT", "计算机", "云计算", "大数据",
        "人工智能", "AI", "算力", "游戏", "大模型", "AIGC",
        "DeepSeek", "鸿蒙", "华为", "机器人", "无人驾驶",
        "具身智能", "脑机", "量子", "6G", "通信",
    ],
    "消费": [
        "食品", "酒", "饮料", "调味", "乳", "餐饮", "纺织", "服装",
        "零售", "商业", "旅游", "景区", "酒店", "奢侈品", "免税",
        "教育",
    ],
    "家居家电": [
        "家电", "卫浴", "厨卫", "家具", "家居", "照明", "灯具",
        "厨房", "装修",
    ],
    "医药生物": [
        "药", "医", "生物", "保健", "疫苗", "CXO", "CRO", "基因",
        "维生素", "抗癌", "中药", "仿制药",
    ],
    "金融": [
        "银行", "保险", "证券", "期货", "金融", "信托",
    ],
    "地产基建": [
        "房地产", "建筑", "建材", "装饰", "水泥", "钢结构", "玻璃",
        "陶瓷",
    ],
    "汽车交运": [
        "汽车", "车", "轮胎", "快递", "物流", "航运", "港口", "航空",
    ],
    "军工": [
        "军工", "国防", "航空航天", "船舶", "航天", "卫星", "低空",
    ],
    "电力公用": [
        "电网", "配电", "输变电", "电力", "电机", "水务", "燃气",
        "环保", "碳纤维", "固废", "污水", "核污", "核电",
    ],
    "农牧": [
        "种植", "养殖", "饲料", "畜牧", "农业", "水产", "渔业",
        "生态农业", "生物育种", "乡村振兴",
    ],
    "传媒文娱": [
        "影视", "传媒", "广告", "出版", "文化", "动漫", "迪士尼",
    ],
}

# ROTATION_LINE_MAP 精确映射 → 主题
_ROTATION_TO_THEME = {
    "科技": "科技硬件",
    "顺周期": "周期资源",
    "消费": "消费",
    "新能源": "新能源",
    "主题": "军工",
    "公用": "电力公用",
}

# ROTATION_LINE_MAP 精确映射的例外：某些行业虽然被分到"顺周期"等大类，
# 但在主题聚合中更适合归入专门主题
_ROTATION_OVERRIDE = {
    "银行": "金融", "保险": "金融", "多元金融": "金融", "证券": "金融",
    "家用电器": "家居家电", "照明设备": "家居家电",
    "化学原料": "化工", "化学制品": "化工", "农化制品": "化工",
    "电子化学品": "化工",
    "化学制药": "医药生物", "生物制药": "医药生物", "中药": "医药生物",
    "医疗器械": "医药生物", "医疗服务": "医药生物", "医药商业": "医药生物",
    "种植业与林业": "农牧", "养殖业": "农牧", "水产": "农牧",
    "影视院线": "传媒文娱", "广告营销": "传媒文娱",
    "旅游景区": "消费", "酒店餐饮": "消费", "教育": "消费",
    "船舶制造": "军工",
}


# ── 主题分类 ─────────────────────────────────────────────────

def _classify_board(name: str) -> str:
    """将板块名称归入主题类别（行业 + 概念通用）。

    优先级：
    1. ROTATION_LINE_MAP 精确匹配（78 个行业）
    2. THEME_KEYWORDS 关键词匹配（取命中数最多的主题）
    3. 未匹配 → "其他"
    """
    # 1. 精确匹配（先查例外表，再查轮动线大类）
    if name in _ROTATION_OVERRIDE:
        return _ROTATION_OVERRIDE[name]
    rot = config.ROTATION_LINE_MAP.get(name, "")
    if rot:
        return _ROTATION_TO_THEME.get(rot, "其他")

    # 2. 关键词匹配 — 取命中关键词数最多的主题
    best_theme, best_cnt = "其他", 0
    for theme, keywords in THEME_KEYWORDS.items():
        cnt = sum(1 for kw in keywords if kw in name)
        if cnt > best_cnt:
            best_theme, best_cnt = theme, cnt
    return best_theme


# ── 强度标签 ────────────────────────────────────────────────

_STRENGTH_LABELS = [
    (2.0, "强势"),
    (1.0, "偏强"),
    (0.0, "中性"),
    (-1.0, "偏弱"),
    (float("-inf"), "弱势"),
]


def _strength_label(avg_gain: float) -> str:
    for threshold, label in _STRENGTH_LABELS:
        if avg_gain >= threshold:
            return label
    return "弱势"


# ── 数据加载与预处理 ────────────────────────────────────────

def _load_industry_df() -> pd.DataFrame:
    """
    获取行业数据：东财优先（4D 特征完整）→ CSV 缓存兜底。
    不使用 THS 作为行业数据源（THS 概念板块单独聚类展示）。
    """
    import os
    from signals.layers.industry import (
        _fetch_board_industry_name_em,
        _load_board_industry_cache,
    )

    # 1. 东财优先 — 有换手率/上涨下跌家数（4D 特征完整）
    em_df = _fetch_board_industry_name_em()
    if em_df is not None and not em_df.empty:
        logger.info("聚类：东财数据 %d 个行业（4D）", len(em_df))
        return em_df

    # 2. CSV 缓存兜底 — gen_cache.py 预生成的东财快照
    cache_df = _load_board_industry_cache()
    if cache_df is not None and not cache_df.empty:
        logger.info("聚类：CSV 缓存 %d 个行业", len(cache_df))
        return cache_df

    # 3. 硬路径兜底
    csv_path = os.path.join(os.path.dirname(__file__), "../../.cache/board_industry.csv")
    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path)
        logger.info("聚类：硬路径 CSV 缓存 %d 个行业", len(df))
        return df

    logger.warning("聚类：无行业数据")
    return pd.DataFrame()


def _dedup_boards(df: pd.DataFrame) -> pd.DataFrame:
    """去重 Ⅱ/Ⅲ 同名板块（涨幅差 < 0.05 的保留排名靠前的）。"""
    if "板块名称" not in df.columns:
        return df

    df = df.copy()
    df["_base_name"] = df["板块名称"].apply(lambda x: re.sub(r'[ⅡⅢⅣ]$', '', str(x).strip()))

    # 找出需要去重的
    change_col = _find_change_col(df)
    if not change_col:
        return df.drop_duplicates(subset="_base_name", keep="first").drop(columns="_base_name")

    keep_idx = []
    for _, group in df.groupby("_base_name"):
        if len(group) == 1:
            keep_idx.append(group.index[0])
        else:
            vals = pd.to_numeric(group[change_col], errors="coerce")
            if vals.max() - vals.min() < 0.05:
                keep_idx.append(group.index[0])  # 涨幅接近，保留第一个
            else:
                keep_idx.extend(group.index.tolist())  # 涨幅差异大，保留全部
    return df.loc[keep_idx].drop(columns="_base_name").reset_index(drop=True)


def _find_change_col(df: pd.DataFrame) -> str:
    """找到涨跌幅列名。"""
    for col in ["涨跌幅", "涨跌幅(%)", "涨幅", "涨幅(%)", "最新涨跌幅"]:
        if col in df.columns:
            return col
    return ""


# ── 排名综合得分 ───────────────────────────────────────────

def _rank_clusters(cluster_stats: list) -> list:
    """计算综合得分并排序。"""
    if not cluster_stats:
        return []

    # min-max 归一化
    def _norm(values):
        arr = np.array(values, dtype=float)
        mn, mx = arr.min(), arr.max()
        if mx - mn < 1e-9:
            return np.full_like(arr, 0.5)
        return (arr - mn) / (mx - mn)

    gains = [c["avg_gain"] for c in cluster_stats]
    breadths = [c["avg_breadth"] for c in cluster_stats]
    turnovers = [c["avg_turnover"] for c in cluster_stats]
    spreads = [1 - c["avg_leader_spread"] for c in cluster_stats]  # 越低越好，取反
    sizes = [c["size"] for c in cluster_stats]

    n_gains = _norm(gains)
    n_breadths = _norm(breadths)
    n_turnovers = _norm(turnovers)
    n_spreads = _norm(spreads)
    n_sizes = _norm(sizes)

    for i, c in enumerate(cluster_stats):
        c["score"] = round(
            0.40 * n_gains[i] +
            0.20 * n_breadths[i] +
            0.20 * n_turnovers[i] +
            0.10 * n_spreads[i] +
            0.10 * n_sizes[i], 4)

    return sorted(cluster_stats, key=lambda c: c["score"], reverse=True)


# ── 行业板块主题聚合 ──────────────────────────────────────

def cluster_industries(top_n: int = 3, **_kw) -> dict:
    """
    行业板块主题聚合分析。

    流程：给每个板块打主题标签 → groupby 主题聚合 → 综合评分排名。
    保证同一主题内的板块主题一致，不会出现卫浴混入化工的情况。

    :param top_n: 返回 Top N 强势主题
    :return: {top: [...], all_clusters: [...], meta: {...}}
    """
    df = _load_industry_df()
    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        return {"top": [], "all_clusters": [], "meta": {"error": "无行业数据"}}

    # 去重
    df = _dedup_boards(df)
    total_boards = len(df)

    # 列名探测
    name_col = "板块名称" if "板块名称" in df.columns else df.columns[1]
    change_col = _find_change_col(df)
    if not change_col:
        return {"top": [], "all_clusters": [], "meta": {"error": "找不到涨跌幅列"}}

    df["_pct_change"] = pd.to_numeric(df[change_col], errors="coerce").fillna(0)

    # 广度
    has_breadth = "上涨家数" in df.columns and "下跌家数" in df.columns
    if has_breadth:
        up = pd.to_numeric(df["上涨家数"], errors="coerce").fillna(0)
        down = pd.to_numeric(df["下跌家数"], errors="coerce").fillna(0)
        total = up + down
        df["_breadth"] = np.where(total > 0, up / total, 0.5)
    else:
        df["_breadth"] = 0.5

    # 换手率
    if "换手率" in df.columns:
        df["_turnover"] = pd.to_numeric(df["换手率"], errors="coerce").fillna(0)
    else:
        df["_turnover"] = 0.0

    # 领涨差
    leader_col = "领涨股票-涨跌幅" if "领涨股票-涨跌幅" in df.columns else (
        "领涨股-涨跌幅" if "领涨股-涨跌幅" in df.columns else "")
    if leader_col:
        leader_val = pd.to_numeric(df[leader_col], errors="coerce").fillna(0)
        df["_leader_spread"] = leader_val - df["_pct_change"]
    else:
        df["_leader_spread"] = 0.0

    leader_name_col = "领涨股票" if "领涨股票" in df.columns else (
        "领涨股" if "领涨股" in df.columns else "")

    # 数据源标识
    source = "东财" if "换手率" in df.columns else "CSV缓存"

    # ── 核心：主题分类 + groupby 聚合 ──
    df["_theme"] = df[name_col].apply(_classify_board)

    cluster_stats = []
    for cid, (theme, group) in enumerate(df.groupby("_theme", sort=False)):
        if len(group) < 2:
            continue  # 跳过只有 1 个板块的主题

        avg_gain = group["_pct_change"].mean()
        avg_breadth = group["_breadth"].mean()
        avg_turnover = group["_turnover"].mean()
        avg_leader_spread = group["_leader_spread"].mean()

        # 成员详情
        member_list = []
        for _, row in group.iterrows():
            m = {
                "name": str(row[name_col]),
                "gain_pct": round(float(row["_pct_change"]), 2),
            }
            if has_breadth:
                m["up_count"] = int(row.get("上涨家数", 0)) if "上涨家数" in group.columns else 0
                m["down_count"] = int(row.get("下跌家数", 0)) if "下跌家数" in group.columns else 0
            if leader_name_col and leader_name_col in group.columns:
                m["leader"] = str(row[leader_name_col])
            if leader_col and leader_col in group.columns:
                m["leader_gain"] = round(float(pd.to_numeric(row[leader_col], errors="coerce") or 0), 2)
            member_list.append(m)

        # 按涨幅排序
        member_list.sort(key=lambda m: m["gain_pct"], reverse=True)

        # 标签 = 主题（强度）
        strength = _strength_label(avg_gain)
        label = f"{theme}（{strength}）"

        cluster_stats.append({
            "cluster_id": cid,
            "label": label,
            "size": len(group),
            "avg_gain": round(avg_gain, 2),
            "avg_breadth": round(avg_breadth, 3),
            "avg_turnover": round(avg_turnover, 2),
            "avg_leader_spread": round(avg_leader_spread, 2),
            "members": member_list,
        })

    # 排名
    ranked = _rank_clusters(cluster_stats)

    now = datetime.now()
    return {
        "top": ranked[:top_n],
        "all_clusters": ranked,
        "meta": {
            "date": now.strftime("%Y-%m-%d"),
            "timestamp": now.isoformat(),
            "total_boards": total_boards,
            "deduped_boards": len(df),
            "n_themes": len(ranked),
            "valid_clusters": len(ranked),
            "source": source,
        },
    }


# ── 概念板块主题聚合 ──────────────────────────────────────

def cluster_concepts(top_n: int = 3, **_kw) -> dict:
    """
    概念板块主题聚合分析（数据源：新浪/东财/THS 降级链）。

    使用同一套 THEME_KEYWORDS 做主题分类，替代旧的 3 类
    CONCEPT_TYPE_KEYWORDS（防守/进攻/周期 覆盖率太低）。

    :param top_n: 返回 Top N 强势主题
    :return: {top: [...], all_clusters: [...], meta: {...}}
    """
    from signals.layers.industry import get_concept_rankings

    try:
        rankings = get_concept_rankings(top_n=80)
    except Exception as e:
        logger.error("概念聚类：获取数据失败: %s", e)
        return {"top": [], "all_clusters": [], "meta": {"error": f"概念数据获取失败: {e}"}}

    if not rankings or len(rankings) < 3:
        return {"top": [], "all_clusters": [], "meta": {
            "error": f"概念数据不足({len(rankings) if rankings else 0}条)"}}

    # 构建 DataFrame
    rows = []
    for r in rankings:
        rows.append({
            "name": r.name,
            "gain_pct": r.gain_pct,
            "leading_stock": r.leading_stock,
            "leading_gain": r.leading_gain,
            "up_count": r.up_count,
            "down_count": r.down_count,
            "turnover_rate": r.turnover_rate,
            "score": r.composite_score,
        })
    df = pd.DataFrame(rows)

    df["_pct_change"] = df["gain_pct"]

    # 广度
    if df["up_count"].sum() > 0:
        total = df["up_count"] + df["down_count"]
        df["_breadth"] = np.where(total > 0, df["up_count"] / total, 0.5)
    else:
        df["_breadth"] = 0.5

    # 换手率
    if df["turnover_rate"].sum() > 0:
        df["_turnover"] = df["turnover_rate"]
    else:
        df["_turnover"] = 0.0

    # 领涨差
    if df["leading_gain"].abs().sum() > 0:
        df["_leader_spread"] = df["leading_gain"] - df["gain_pct"]
    else:
        df["_leader_spread"] = 0.0

    # 数据源标识
    source = "概念(新浪/东财)"
    if rankings and hasattr(rankings[0], "tag") and rankings[0].tag == "ths":
        source = "概念(THS)"

    # ── 核心：主题分类 + groupby 聚合 ──
    df["_theme"] = df["name"].apply(_classify_board)

    cluster_stats = []
    for cid, (theme, group) in enumerate(df.groupby("_theme", sort=False)):
        if len(group) < 2:
            continue

        avg_gain = group["_pct_change"].mean()
        avg_breadth = group["_breadth"].mean()
        avg_turnover = group["_turnover"].mean()

        member_list = []
        for _, row in group.iterrows():
            m = {
                "name": row["name"],
                "gain_pct": round(float(row["gain_pct"]), 2),
                "type": _classify_board(row["name"]),  # 用主题替代旧 sector_type
            }
            if row["leading_stock"]:
                m["leader"] = row["leading_stock"]
            if row["leading_gain"] != 0:
                m["leader_gain"] = round(float(row["leading_gain"]), 2)
            member_list.append(m)
        member_list.sort(key=lambda m: m["gain_pct"], reverse=True)

        strength = _strength_label(avg_gain)
        label = f"{theme}（{strength}）"

        cluster_stats.append({
            "cluster_id": cid,
            "label": label,
            "size": len(group),
            "avg_gain": round(avg_gain, 2),
            "avg_breadth": round(avg_breadth, 3),
            "avg_turnover": round(avg_turnover, 2),
            "avg_leader_spread": 0,
            "members": member_list,
        })

    ranked = _rank_clusters(cluster_stats)
    now = datetime.now()

    return {
        "top": ranked[:top_n],
        "all_clusters": ranked,
        "meta": {
            "date": now.strftime("%Y-%m-%d"),
            "timestamp": now.isoformat(),
            "total_concepts": len(df),
            "n_themes": len(ranked),
            "valid_clusters": len(ranked),
            "source": source,
        },
    }
