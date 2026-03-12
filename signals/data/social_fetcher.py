# -*- coding: utf-8 -*-
"""
社交舆情数据获取层

数据源（按可用性排序）:
1. 东财千股千评 stock_comment_em()    — 5168股, 综合得分/关注指数/机构参与度
2. 微博舆情 stock_js_weibo_report()   — 宏观情绪率 (50股, 短期热度)
3. 东财概念板块 stock_board_concept_*  — 主题→标的映射 (468概念)

注: 东财人气榜/飙升榜/关键词接口(emappdata.eastmoney.com)存在SSL问题,
    暂不使用, 后续环境修复后可作为补充数据源。
"""
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from signals.data.fetcher import no_proxy, em_call_with_retry

_log = logging.getLogger("signals.social")

# ─────────────────────────────────────────────────────────
# 缓存基础设施 (同 industry.py 模式)
# ─────────────────────────────────────────────────────────

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / ".data" / "cache"


def _cache_path(name: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"social_{name}.json"


def _save_cache(name: str, data: dict):
    try:
        path = _cache_path(name)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_cache(name: str, max_age: float = 7200) -> Optional[dict]:
    """加载缓存, 默认2小时过期"""
    path = _cache_path(name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - data.get("ts", 0) > max_age:
            return None
        return data
    except Exception:
        return None


# ─────────────────────────────────────────────────────────
# 数据模型
# ─────────────────────────────────────────────────────────

@dataclass
class SocialHeatSnapshot:
    """单股社交热度快照"""
    symbol: str
    name: str = ""
    # 千股千评数据
    comment_score: float = 0.0       # 综合得分 (0-100)
    comment_rank: int = 0            # 千评排名 (越小越好)
    focus_index: float = 0.0         # 关注指数
    institution_pct: float = 0.0     # 机构参与度 (0-1)
    # 微博舆情
    weibo_rate: float = 0.0          # 微博情绪率 (-1 ~ +1)
    # 股吧深度数据
    guba_heat: float = 0.0           # 股吧热度 (0-100)
    guba_post_count: int = 0         # 股吧帖子数
    guba_sentiment: float = 0.0      # 股吧标题情绪 (-1 ~ +1)
    # NLP 情绪
    nlp_sentiment: float = 0.0       # NLP情绪分 (-100 ~ +100)
    nlp_label: str = ""              # "偏乐观"/"中性"/"偏悲观" 等
    # 产业链
    chain_name: str = ""             # 所属产业链
    chain_position: str = ""         # "上游"/"中游"/"下游"
    chain_role: str = ""             # 产业链角色描述
    # 综合
    heat_score: float = 0.0          # 0-100 综合热度评分
    heat_grade: str = ""             # "爆热"/"热门"/"温和"/"冷门"
    concepts: List[str] = field(default_factory=list)  # 关联概念 ["华为昇腾","算力"]
    tag: str = ""                    # 简短标签 "千评#38 综合72"
    data_time: str = ""


@dataclass
class WeiboMacroSentiment:
    """微博宏观情绪"""
    top_stocks: List[Tuple[str, float]] = field(default_factory=list)  # [(name, rate), ...]
    avg_rate: float = 0.0
    temperature: str = ""  # "偏热"/"正常"/"偏冷"
    data_time: str = ""


@dataclass
class ConceptTheme:
    """概念主题"""
    name: str
    code: str = ""
    stocks: List[Dict] = field(default_factory=list)  # [{代码, 名称, 涨跌幅}, ...]
    stock_count: int = 0


# ─────────────────────────────────────────────────────────
# 千股千评 (核心数据源, 5168股全覆盖)
# ─────────────────────────────────────────────────────────

_comment_cache: Optional[pd.DataFrame] = None
_comment_cache_ts: float = 0.0


def fetch_comment_data(force: bool = False) -> pd.DataFrame:
    """
    获取东财千股千评数据 (全市场5000+股)。
    返回DataFrame: 代码, 名称, 综合得分, 关注指数, 机构参与度, 目前排名 等
    内存缓存2h + 磁盘缓存兜底。
    """
    global _comment_cache, _comment_cache_ts
    now = time.time()

    # 内存缓存
    if not force and _comment_cache is not None and (now - _comment_cache_ts) < 7200:
        return _comment_cache

    # 磁盘缓存
    if not force:
        cached = _load_cache("comment_em")
        if cached and "records" in cached:
            df = pd.DataFrame(cached["records"])
            _comment_cache = df
            _comment_cache_ts = cached.get("ts", now)
            _log.info(f"千股千评 从缓存加载 {len(df)} 条")
            return df

    # 网络获取
    import akshare as ak
    try:
        with no_proxy():
            df = ak.stock_comment_em()
        _log.info(f"千股千评 获取成功: {len(df)} 条")

        # 保存缓存
        records = df.to_dict("records")
        _save_cache("comment_em", {"ts": time.time(), "records": records})

        _comment_cache = df
        _comment_cache_ts = time.time()
        return df
    except Exception as e:
        _log.warning(f"千股千评 获取失败: {e}")
        # 尝试过期缓存
        cached = _load_cache("comment_em", max_age=86400 * 7)
        if cached and "records" in cached:
            return pd.DataFrame(cached["records"])
        return pd.DataFrame()


def get_stock_comment(symbol: str, df: Optional[pd.DataFrame] = None) -> dict:
    """
    获取单个标的的千股千评数据。
    symbol: 如 "SZ.002261" 或 "002261"
    返回 dict: {综合得分, 关注指数, 机构参与度, 目前排名}
    """
    if df is None:
        df = fetch_comment_data()
    if df.empty:
        return {}

    # 提取6位代码
    code = symbol.split(".")[-1] if "." in symbol else symbol
    if len(code) != 6:
        return {}

    row = df[df["代码"] == code]
    if row.empty:
        return {}

    r = row.iloc[0]
    return {
        "code": code,
        "name": str(r.get("名称", "")),
        "score": float(r.get("综合得分", 0)),
        "rank": int(r.get("目前排名", 0)),
        "focus_index": float(r.get("关注指数", 0)),
        "institution_pct": float(r.get("机构参与度", 0)),
    }


# ─────────────────────────────────────────────────────────
# 微博舆情 (宏观情绪)
# ─────────────────────────────────────────────────────────

def fetch_weibo_sentiment() -> WeiboMacroSentiment:
    """获取微博舆情数据 (jin10聚合, 50股情绪率)"""
    cached = _load_cache("weibo_sentiment")
    if cached and "stocks" in cached:
        return WeiboMacroSentiment(
            top_stocks=[(s["name"], s["rate"]) for s in cached["stocks"]],
            avg_rate=cached.get("avg_rate", 0),
            temperature=cached.get("temperature", ""),
            data_time=cached.get("data_time", ""),
        )

    import akshare as ak
    try:
        with no_proxy():
            df = ak.stock_js_weibo_report(time_period="CNHOUR12")

        stocks = [(str(row["name"]), float(row["rate"])) for _, row in df.iterrows()]
        avg = df["rate"].mean() if len(df) > 0 else 0.0

        # 温度判定
        if avg > 0.3:
            temp = "偏热"
        elif avg < -0.3:
            temp = "偏冷"
        else:
            temp = "正常"

        result = WeiboMacroSentiment(
            top_stocks=stocks,
            avg_rate=round(avg, 3),
            temperature=temp,
            data_time=time.strftime("%Y-%m-%d %H:%M"),
        )

        # 缓存
        _save_cache("weibo_sentiment", {
            "ts": time.time(),
            "stocks": [{"name": n, "rate": r} for n, r in stocks],
            "avg_rate": result.avg_rate,
            "temperature": result.temperature,
            "data_time": result.data_time,
        })

        _log.info(f"微博舆情 获取成功: {len(stocks)} 股, 均值={avg:.3f}, {temp}")
        return result
    except Exception as e:
        _log.warning(f"微博舆情 获取失败: {e}")
        return WeiboMacroSentiment()


# ─────────────────────────────────────────────────────────
# 概念板块 (主题→标的映射)
# ─────────────────────────────────────────────────────────

_concept_list_cache: Optional[pd.DataFrame] = None
_concept_list_ts: float = 0.0


def fetch_concept_list(force: bool = False) -> pd.DataFrame:
    """获取东财全部概念板块列表 (468+)"""
    global _concept_list_cache, _concept_list_ts
    now = time.time()

    if not force and _concept_list_cache is not None and (now - _concept_list_ts) < 14400:
        return _concept_list_cache

    # 磁盘缓存 (4h)
    if not force:
        cached = _load_cache("concept_list", max_age=14400)
        if cached and "records" in cached:
            df = pd.DataFrame(cached["records"])
            _concept_list_cache = df
            _concept_list_ts = cached.get("ts", now)
            return df

    import akshare as ak

    # 检查东财熔断状态（云端 push2 被封时避免无效重试）
    em_ok = True
    try:
        from signals.layers.industry import _EM_CIRCUIT_OPEN
        em_ok = not _EM_CIRCUIT_OPEN
    except ImportError:
        pass

    if not em_ok:
        _log.info("概念板块列表: 东财已熔断，使用缓存")
        cached = _load_cache("concept_list", max_age=86400 * 7)
        if cached and "records" in cached:
            df = pd.DataFrame(cached["records"])
            _concept_list_cache = df
            _concept_list_ts = cached.get("ts", now)
            return df
        return pd.DataFrame()

    try:
        df = em_call_with_retry(ak.stock_board_concept_name_em, retries=2, delay=1.0)
        _save_cache("concept_list", {"ts": time.time(), "records": df.to_dict("records")})
        _concept_list_cache = df
        _concept_list_ts = time.time()
        _log.info(f"概念板块列表 获取成功: {len(df)} 个概念")
        return df
    except Exception as e:
        _log.warning(f"概念板块列表 获取失败: {e}")
        cached = _load_cache("concept_list", max_age=86400 * 7)
        if cached and "records" in cached:
            return pd.DataFrame(cached["records"])
        return pd.DataFrame()


def search_concepts(keyword: str) -> List[Dict]:
    """
    搜索概念板块 (模糊匹配)。
    返回 [{板块名称, 板块代码}, ...]
    """
    df = fetch_concept_list()
    if df.empty:
        return []
    matches = df[df["板块名称"].str.contains(keyword, na=False)]
    return [{"name": row["板块名称"], "code": row["板块代码"]}
            for _, row in matches.iterrows()]


def fetch_concept_stocks(concept_name: str) -> ConceptTheme:
    """
    获取概念板块成分股。
    concept_name: 如 "华为昇腾", "算力概念", "AI芯片"
    """
    cache_key = f"concept_stocks_{concept_name.replace(' ', '_')}"
    cached = _load_cache(cache_key, max_age=7200)
    if cached and "stocks" in cached:
        return ConceptTheme(
            name=concept_name,
            code=cached.get("code", ""),
            stocks=cached["stocks"],
            stock_count=len(cached["stocks"]),
        )

    import akshare as ak

    # 检查东财熔断状态（云端 push2 被封时避免无效重试）
    em_ok = True
    try:
        from signals.layers.industry import _EM_CIRCUIT_OPEN
        em_ok = not _EM_CIRCUIT_OPEN
    except ImportError:
        pass

    if not em_ok:
        _log.info(f"概念成分股 [{concept_name}] 东财已熔断，跳过")
        cached = _load_cache(cache_key, max_age=86400 * 7)
        if cached and "stocks" in cached:
            return ConceptTheme(
                name=concept_name, code=cached.get("code", ""),
                stocks=cached["stocks"], stock_count=len(cached["stocks"]),
            )
        return ConceptTheme(name=concept_name)

    try:
        df = em_call_with_retry(
            ak.stock_board_concept_cons_em, symbol=concept_name,
            retries=2, delay=0.5,
        )
        stocks = [
            {
                "code": str(row["代码"]),
                "name": str(row["名称"]),
                "price": float(row.get("最新价", 0)),
                "change_pct": float(row.get("涨跌幅", 0)),
            }
            for _, row in df.iterrows()
        ]
        # 获取板块代码
        concept_df = fetch_concept_list()
        code = ""
        if not concept_df.empty:
            match = concept_df[concept_df["板块名称"] == concept_name]
            if not match.empty:
                code = str(match.iloc[0]["板块代码"])

        result = ConceptTheme(
            name=concept_name, code=code,
            stocks=stocks, stock_count=len(stocks),
        )
        _save_cache(cache_key, {
            "ts": time.time(), "code": code, "stocks": stocks,
        })
        _log.info(f"概念成分股 [{concept_name}] 获取成功: {len(stocks)} 只")
        return result
    except Exception as e:
        _log.warning(f"概念成分股 [{concept_name}] 获取失败: {e}")
        # 扩大缓存时效到 7 天作为兜底
        cached = _load_cache(cache_key, max_age=86400 * 7)
        if cached and "stocks" in cached:
            return ConceptTheme(
                name=concept_name,
                code=cached.get("code", ""),
                stocks=cached["stocks"],
                stock_count=len(cached["stocks"]),
            )
        return ConceptTheme(name=concept_name)


# ─────────────────────────────────────────────────────────
# 聚合查询
# ─────────────────────────────────────────────────────────

def fetch_social_heat(symbol: str, deep: bool = False) -> SocialHeatSnapshot:
    """
    获取单股社交热度综合快照。
    聚合千股千评 + 微博 + (可选)股吧深度 + NLP情绪 + 产业链 数据。

    :param deep: True 时启用股吧爬虫 + NLP情绪分析（较慢，约2-4s）
    """
    snap = SocialHeatSnapshot(symbol=symbol)

    # 千股千评
    comment = get_stock_comment(symbol)
    if comment:
        snap.name = comment.get("name", "")
        snap.comment_score = comment.get("score", 0)
        snap.comment_rank = comment.get("rank", 0)
        snap.focus_index = comment.get("focus_index", 0)
        snap.institution_pct = comment.get("institution_pct", 0)

    # 微博 (宏观层面, 检查是否在Top50)
    weibo = fetch_weibo_sentiment()
    name = snap.name or symbol
    for wname, wrate in weibo.top_stocks:
        if wname == name or wname in name:
            snap.weibo_rate = wrate
            break

    # 产业链标注
    try:
        from signals.core.chain_map import get_chain_position
        code = symbol.split(".")[-1] if "." in symbol else symbol
        pos = get_chain_position(code)
        if pos:
            snap.chain_name = pos.chain_name
            snap.chain_position = pos.position
            snap.chain_role = pos.role
    except Exception:
        pass

    # 深度模式: 股吧 + NLP
    if deep:
        _enrich_deep(snap)

    # 综合热度评分
    snap.heat_score = _compute_heat_score(snap)
    snap.heat_grade = _heat_grade(snap.heat_score)
    snap.data_time = time.strftime("%Y-%m-%d %H:%M")

    # 构建tag
    parts = []
    if snap.comment_rank > 0:
        parts.append(f"千评#{snap.comment_rank}")
    if snap.comment_score > 0:
        parts.append(f"综合{snap.comment_score:.0f}")
    if snap.chain_name:
        parts.append(f"{snap.chain_name}/{snap.chain_position}")
    snap.tag = " ".join(parts)

    return snap


def _enrich_deep(snap: SocialHeatSnapshot):
    """深度数据增强：股吧爬虫 + NLP 情绪"""
    code = snap.symbol.split(".")[-1] if "." in snap.symbol else snap.symbol
    if len(code) != 6:
        return

    # 股吧爬虫
    try:
        from signals.data.scrapers.tieba import TiebaScraper
        scraper = TiebaScraper()
        result = scraper.search(code, limit=20)
        if result.posts:
            snap.guba_heat = result.heat_index
            snap.guba_post_count = len(result.posts)
            # 平均标题情绪
            sentiments = [p.sentiment_hint for p in result.posts]
            snap.guba_sentiment = round(sum(sentiments) / len(sentiments), 2)

            # NLP 情绪分析（用帖子标题）
            try:
                from signals.core.sentiment_nlp import analyze_sentiment, classify_sentiment
                titles = [p.title for p in result.posts if p.title]
                if titles:
                    snap.nlp_sentiment = analyze_sentiment(titles)
                    snap.nlp_label = classify_sentiment(snap.nlp_sentiment)
            except Exception:
                pass
    except Exception as e:
        _log.debug(f"深度数据增强失败 [{code}]: {e}")


def fetch_social_heat_batch(symbols: List[str],
                            max_workers: int = 1) -> Dict[str, SocialHeatSnapshot]:
    """
    批量获取社交热度。
    先批量加载千股千评+微博(单次调用), 再逐股聚合。
    """
    comment_df = fetch_comment_data()
    weibo = fetch_weibo_sentiment()

    results = {}
    for sym in symbols:
        snap = SocialHeatSnapshot(symbol=sym)

        # 千股千评
        comment = get_stock_comment(sym, df=comment_df)
        if comment:
            snap.name = comment.get("name", "")
            snap.comment_score = comment.get("score", 0)
            snap.comment_rank = comment.get("rank", 0)
            snap.focus_index = comment.get("focus_index", 0)
            snap.institution_pct = comment.get("institution_pct", 0)

        # 微博
        name = snap.name or sym
        for wname, wrate in weibo.top_stocks:
            if wname == name or wname in name:
                snap.weibo_rate = wrate
                break

        snap.heat_score = _compute_heat_score(snap)
        snap.heat_grade = _heat_grade(snap.heat_score)
        snap.data_time = time.strftime("%Y-%m-%d %H:%M")
        results[sym] = snap

    return results


# ─────────────────────────────────────────────────────────
# 评分逻辑
# ─────────────────────────────────────────────────────────

def _compute_heat_score(snap: SocialHeatSnapshot) -> float:
    """计算综合热度 (0-100)"""
    score = 0.0

    # 有深度数据时权重调整
    has_deep = snap.guba_heat > 0

    if has_deep:
        # 深度模式权重分配
        # 千评综合得分 — 权重35%
        if snap.comment_score > 0:
            score += snap.comment_score * 0.35
        # 关注指数 — 权重15%
        if snap.focus_index > 0:
            focus_norm = min(max((snap.focus_index - 50) / 45.0, 0), 1) * 100
            score += focus_norm * 0.15
        # 千评排名 — 权重10%
        if snap.comment_rank > 0:
            rank_norm = max(1.0 - snap.comment_rank / 5168.0, 0) * 100
            score += rank_norm * 0.10
        # 微博情绪 — 权重5%
        if snap.weibo_rate != 0:
            weibo_norm = (snap.weibo_rate + 1) / 2 * 100
            score += weibo_norm * 0.05
        # 股吧热度 — 权重25%
        score += snap.guba_heat * 0.25
        # NLP情绪绝对值 — 权重10%（越极端说明讨论越激烈）
        if snap.nlp_sentiment != 0:
            nlp_abs = min(abs(snap.nlp_sentiment), 100)
            score += nlp_abs * 0.10
    else:
        # 基础模式（与原逻辑一致）
        if snap.comment_score > 0:
            score += snap.comment_score * 0.5
        if snap.focus_index > 0:
            focus_norm = min(max((snap.focus_index - 50) / 45.0, 0), 1) * 100
            score += focus_norm * 0.25
        if snap.comment_rank > 0:
            rank_norm = max(1.0 - snap.comment_rank / 5168.0, 0) * 100
            score += rank_norm * 0.15
        if snap.weibo_rate != 0:
            weibo_norm = (snap.weibo_rate + 1) / 2 * 100
            score += weibo_norm * 0.10

    return round(min(score, 100), 1)


def _heat_grade(score: float) -> str:
    """热度等级"""
    if score >= 75:
        return "爆热"
    elif score >= 50:
        return "热门"
    elif score >= 25:
        return "温和"
    else:
        return "冷门"
