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

def _classify_board(name) -> str:
    """将板块名称归入主题类别（行业 + 概念通用）。

    优先级：
    1. ROTATION_LINE_MAP 精确匹配（78 个行业）
    2. THEME_KEYWORDS 关键词匹配（取命中数最多的主题）
    3. 未匹配 → "其他"
    """
    if not isinstance(name, str) or not name:
        return "其他"
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

def _fetch_sina_industry() -> pd.DataFrame:
    """新浪行业数据源 — 实时涨跌幅，列名标准化为东财格式。"""
    try:
        import akshare as ak
        df = ak.stock_sector_spot("行业")
        if df is None or df.empty:
            return pd.DataFrame()
        # 标准化列名为东财格式
        df = df.rename(columns={
            "板块": "板块名称",
            "涨跌幅": "涨跌幅",
            "总成交量": "总成交量",
            "总成交额": "总成交额",
            "公司家数": "公司家数",
        })
        # 加排名列
        df = df.sort_values("涨跌幅", ascending=False).reset_index(drop=True)
        df["排名"] = range(1, len(df) + 1)
        return df
    except Exception as e:
        logger.warning("新浪行业数据失败: %s", e)
        return pd.DataFrame()


def _load_industry_df(mode: str = "auto", as_of: str = None) -> tuple:
    """
    获取行业数据 — 多源合并模式。

    从 board_ths / board_em / board_sina 三个 MongoDB 集合读取，
    用 board_normalizer.merge_industry_sources() 合并互补字段。

    盘中时先尝试实时 API，成功后存入对应集合；
    非交易日/盘后直接从 MongoDB 读取。

    Returns:
        (DataFrame, fetch_meta) — fetch_meta 包含 source/data_date/update_time
    """
    from signals.data.gateway import get_board_rank
    from signals.data.models import DataRequest
    from signals.data.mongo_fallback import get_last_trading_day
    from datetime import datetime as _dt

    trading_day = get_last_trading_day()
    update_time = _dt.now().strftime("%m-%d %H:%M")

    response = get_board_rank(DataRequest(
        domain="board",
        mode=mode,  # realtime uses source snapshots, historical uses canonical.
        market="A",
        as_of=as_of,
        purpose="cluster" if mode == "realtime" else "",
    ))
    meta = {
        "source": response.source or "无数据",
        "data_date": response.as_of or trading_day,
        "update_time": update_time,
        "mode_used": response.mode_used,
        "freshness": response.freshness,
        "is_stale": response.is_stale,
        "errors": response.errors,
    }
    df = response.data
    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        logger.warning("聚类：所有数据源均无数据")
        return pd.DataFrame(), meta

    logger.info("聚类：获取到 %d 个行业 (源: %s, mode=%s)",
                len(df), meta["source"], meta["mode_used"])
    return df, meta


def _fetch_em_for_cluster():
    """东财行业排行（供聚类用）"""
    try:
        from signals.layers.industry import _fetch_board_industry_name_em
        return _fetch_board_industry_name_em()
    except Exception:
        return None


def _fetch_ths_for_cluster():
    """THS 行业排行（供聚类用）"""
    try:
        import akshare as ak
        return ak.stock_board_industry_summary_ths()
    except Exception:
        return None


def _try_realtime_fetch(collection, fetch_fn, normalize_fn):
    """尝试实时获取 → normalize → 存入 MongoDB。"""
    from signals.data.mongo_fallback import save_snapshot, get_last_trading_day
    try:
        raw = fetch_fn()
        if raw is None or (isinstance(raw, pd.DataFrame) and raw.empty):
            return
        normalized = normalize_fn(raw)
        if normalized is not None and not normalized.empty:
            trading_day = get_last_trading_day()
            normalized["dt"] = trading_day
            docs = normalized.to_dict("records")
            save_snapshot(collection, docs, dedup={"dt": trading_day})
            logger.info("聚类：%s 实时获取成功 (%d 条)", collection, len(docs))
    except Exception as e:
        logger.debug("聚类：%s 实时获取失败: %s", collection, e)


def _save_industry_snapshot(df: pd.DataFrame, source: str):
    """保存行业数据快照到 MongoDB"""
    from signals.data.mongo_fallback import save_snapshot
    try:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        docs = []
        for _, row in df.iterrows():
            doc = {"dt": today, "source": source}
            for col in df.columns:
                doc[col] = row[col]
            docs.append(doc)
        save_snapshot("board_ranking", docs, dedup={"dt": today, "source": source})
    except Exception as e:
        logger.debug("保存行业快照失败: %s", e)


def _dedup_boards(df: pd.DataFrame) -> pd.DataFrame:
    """去重 Ⅱ/Ⅲ 同名板块（涨幅差 < 0.05 的保留排名靠前的）。"""
    # 兼容新旧列名
    name_col = "board_name" if "board_name" in df.columns else (
        "板块名称" if "板块名称" in df.columns else None)
    if not name_col:
        return df

    df = df.copy()
    df["_base_name"] = df[name_col].apply(lambda x: re.sub(r'[ⅡⅢⅣ]$', '', str(x).strip()))

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

def cluster_industries(top_n: int = 3, mode: str = "auto", as_of: str = None, **_kw) -> dict:
    """
    行业板块主题聚合分析。

    流程：给每个板块打主题标签 → groupby 主题聚合 → 综合评分排名。
    保证同一主题内的板块主题一致，不会出现卫浴混入化工的情况。

    :param top_n: 返回 Top N 强势主题
    :return: {top: [...], all_clusters: [...], meta: {...}}
    """
    df_result = _load_industry_df(mode=mode, as_of=as_of)
    # _load_industry_df 返回 (df, fetch_meta) 元组
    if isinstance(df_result, tuple):
        df, fetch_meta = df_result
    else:
        df, fetch_meta = df_result, {}
    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        return {"top": [], "all_clusters": [], "meta": {"error": "无行业数据"}}

    # 去重
    df = _dedup_boards(df)
    total_boards = len(df)

    # 列名探测 — 兼容新统一格式 (board_name/change_pct) 和旧格式 (板块名称/涨跌幅)
    name_col = "board_name" if "board_name" in df.columns else (
        "板块名称" if "板块名称" in df.columns else df.columns[1])

    # 涨跌幅列
    if "change_pct" in df.columns:
        change_col = "change_pct"
    else:
        change_col = _find_change_col(df)
    if not change_col:
        return {"top": [], "all_clusters": [], "meta": {"error": "找不到涨跌幅列"}}

    df["_pct_change"] = pd.to_numeric(df[change_col], errors="coerce").fillna(0)

    # 广度 — 兼容 up_count/down_count (新) 和 上涨家数/下跌家数 (旧)
    up_col = "up_count" if "up_count" in df.columns else ("上涨家数" if "上涨家数" in df.columns else "")
    dn_col = "down_count" if "down_count" in df.columns else ("下跌家数" if "下跌家数" in df.columns else "")
    if up_col and dn_col:
        up = pd.to_numeric(df[up_col], errors="coerce").fillna(0)
        down = pd.to_numeric(df[dn_col], errors="coerce").fillna(0)
        total = up + down
        df["_breadth"] = np.where(total > 0, up / total, 0.5)
        has_breadth = True
    else:
        df["_breadth"] = 0.5
        has_breadth = False

    # 换手率 — 兼容 turnover_pct (新) 和 换手率 (旧)
    turnover_col = "turnover_pct" if "turnover_pct" in df.columns else (
        "换手率" if "换手率" in df.columns else "")
    if turnover_col:
        df["_turnover"] = pd.to_numeric(df[turnover_col], errors="coerce").fillna(0)
    else:
        df["_turnover"] = 0.0

    # 领涨差 — 兼容 leader_change_pct (新) 和 领涨股票-涨跌幅 (旧)
    leader_col = "leader_change_pct" if "leader_change_pct" in df.columns else (
        "领涨股票-涨跌幅" if "领涨股票-涨跌幅" in df.columns else (
            "领涨股-涨跌幅" if "领涨股-涨跌幅" in df.columns else ""))
    if leader_col:
        leader_val = pd.to_numeric(df[leader_col], errors="coerce").fillna(0)
        df["_leader_spread"] = leader_val - df["_pct_change"]
    else:
        df["_leader_spread"] = 0.0

    leader_name_col = "leader_name" if "leader_name" in df.columns else (
        "领涨股票" if "领涨股票" in df.columns else (
            "领涨股" if "领涨股" in df.columns else ""))

    # 数据源标识
    source = fetch_meta.get("source", "未知")

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
            if has_breadth and up_col and dn_col:
                _up = row.get(up_col, 0)
                _dn = row.get(dn_col, 0)
                m["up_count"] = int(_up) if pd.notna(_up) else 0
                m["down_count"] = int(_dn) if pd.notna(_dn) else 0
            if leader_name_col and leader_name_col in group.columns:
                m["leader"] = str(row[leader_name_col])
            if leader_col and leader_col in group.columns:
                _lg = pd.to_numeric(row[leader_col], errors="coerce")
                m["leader_gain"] = round(float(_lg), 2) if pd.notna(_lg) else 0.0
            member_list.append(m)

        # 按涨幅排序
        member_list.sort(key=lambda m: m["gain_pct"], reverse=True)

        # 标签 = 主题（强度）
        strength = _strength_label(avg_gain)
        label = f"{theme}（{strength}）"

        def _safe(v, decimals=2):
            return round(float(v), decimals) if pd.notna(v) else 0.0

        cluster_stats.append({
            "cluster_id": cid,
            "label": label,
            "size": len(group),
            "avg_gain": _safe(avg_gain),
            "avg_breadth": _safe(avg_breadth, 3),
            "avg_turnover": _safe(avg_turnover),
            "avg_leader_spread": _safe(avg_leader_spread),
            "members": member_list,
        })

    # 排名
    ranked = _rank_clusters(cluster_stats)

    now = datetime.now()
    trading_day = fetch_meta.get("data_date", now.strftime("%Y-%m-%d"))
    update_time = fetch_meta.get("update_time", now.strftime("%m-%d %H:%M"))
    return {
        "top": ranked[:top_n],
        "all_clusters": ranked,
        "meta": {
            "date": trading_day,
            "timestamp": now.isoformat(),
            "update_time": update_time,
            "total_boards": total_boards,
            "deduped_boards": len(df),
            "n_themes": len(ranked),
            "valid_clusters": len(ranked),
            "source": source,
        },
    }


# ── 概念板块主题聚合 ──────────────────────────────────────

def cluster_concepts(top_n: int = 3, mode: str = "auto", as_of: str = None, **_kw) -> dict:
    """
    概念板块主题聚合分析（数据源：新浪/东财/THS 降级链）。

    使用同一套 THEME_KEYWORDS 做主题分类，替代旧的 3 类
    CONCEPT_TYPE_KEYWORDS（防守/进攻/周期 覆盖率太低）。

    :param top_n: 返回 Top N 强势主题
    :return: {top: [...], all_clusters: [...], meta: {...}}
    """
    from signals.layers.industry import get_concept_rankings

    try:
        rankings = get_concept_rankings(top_n=80, mode=mode, as_of=as_of)
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

    # 数据源标识 — 从 rankings 的 tag 属性获取真实源
    from signals.data.mongo_fallback import is_any_market_live as _live_check
    _live = _live_check()
    source = "概念(未知)"
    if rankings:
        tag = getattr(rankings[0], "tag", None) or getattr(rankings[0], "source", None)
        if tag == "ths":
            source = "THS(实时API)" if _live else "THS(MongoDB历史)"
        elif tag == "sina":
            source = "新浪(实时API)" if _live else "新浪(MongoDB历史)"
        elif tag == "em":
            source = "东财(实时API)" if _live else "东财(MongoDB历史)"
        elif tag == "mongo":
            source = "MongoDB(历史)"
        else:
            source = "新浪/东财"

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
            _name = row["name"]
            if not isinstance(_name, str) or not _name:
                continue
            _gpct = row["gain_pct"]
            m = {
                "name": _name,
                "gain_pct": round(float(_gpct), 2) if pd.notna(_gpct) else 0.0,
                "type": _classify_board(_name),
            }
            _leader = row.get("leading_stock")
            if _leader and isinstance(_leader, str):
                m["leader"] = _leader
            _lg = row.get("leading_gain", 0)
            if pd.notna(_lg) and _lg != 0:
                m["leader_gain"] = round(float(_lg), 2)
            member_list.append(m)
        member_list.sort(key=lambda m: m["gain_pct"], reverse=True)

        def _safe(v, decimals=2):
            return round(float(v), decimals) if pd.notna(v) else 0.0

        strength = _strength_label(avg_gain if pd.notna(avg_gain) else 0)
        label = f"{theme}（{strength}）"

        cluster_stats.append({
            "cluster_id": cid,
            "label": label,
            "size": len(group),
            "avg_gain": _safe(avg_gain),
            "avg_breadth": _safe(avg_breadth, 3),
            "avg_turnover": _safe(avg_turnover),
            "avg_leader_spread": 0,
            "members": member_list,
        })

    ranked = _rank_clusters(cluster_stats)
    now = datetime.now()

    from signals.data.mongo_fallback import get_last_trading_day, get_db
    trading_day = get_last_trading_day()

    # 从 MongoDB 取概念数据的实际日期
    actual_concept_date = trading_day
    db = get_db()
    if db is not None:
        for col_name in ["concept_sina", "concept_ths", "concept_em"]:
            try:
                latest = db[col_name].find_one({}, sort=[("dt", -1)])
                if latest and "dt" in latest:
                    actual_concept_date = str(latest["dt"])
                    break
            except Exception:
                continue

    return {
        "top": ranked[:top_n],
        "all_clusters": ranked,
        "meta": {
            "date": actual_concept_date,
            "timestamp": now.isoformat(),
            "update_time": now.strftime("%m-%d %H:%M"),
            "total_concepts": len(df),
            "n_themes": len(ranked),
            "valid_clusters": len(ranked),
            "source": source,
        },
    }
