# -*- coding: utf-8 -*-
"""
🐲 MongoDB 统一降级读写模块

所有实时数据获取点共享此模块：
- 实时源成功 → save_snapshot() 写入 MongoDB
- 实时源失败 → get_latest_docs() 从 MongoDB 读前一交易日数据

用法:
    from signals.data.mongo_fallback import get_latest_docs, get_latest_df, save_snapshot

    # 实时获取成功后保存快照
    save_snapshot("board_ranking", docs, dedup={"dt": today, "source": "ths"})

    # 实时获取失败时降级读取
    docs = get_latest_docs("board_ranking", query={"source": "ths"})
    df = get_latest_df("board_ranking", query={"source": "ths"})
"""
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger("signals.data.mongo_fallback")

_db = None
_db_lock = threading.Lock()
_db_checked = False


def get_db():
    """
    获取 MongoDB Database 实例（单例）。
    DB_ENABLED=False 或连接失败时返回 None。
    """
    global _db, _db_checked
    if _db is not None:
        return _db
    if _db_checked:
        return None

    with _db_lock:
        if _db is not None:
            return _db
        if _db_checked:
            return None

        try:
            import config
            if not config.DB_ENABLED:
                _db_checked = True
                return None

            from pymongo import MongoClient
            client = MongoClient(
                config.MONGO_URL,
                maxPoolSize=5,
                serverSelectionTimeoutMS=3000,
                connectTimeoutMS=3000,
            )
            # 验证连接
            client.admin.command("ping")
            _db = client[config.MONGO_DB_NAME]
            logger.info("[MongoDB] 降级模块已连接")
            return _db
        except Exception as e:
            _db_checked = True
            logger.warning(f"[MongoDB] 连接失败，降级功能不可用: {e}")
            return None


# ─── 核心：交易日/非交易日智能数据获取 ──────────────

def is_any_market_live() -> bool:
    """判断当前是否有任何市场在交易（A/H/美股）。"""
    try:
        from signals.core.market_hours import get_active_markets
        return len(get_active_markets()) > 0
    except Exception:
        # market_hours 不可用时按工作日 9:00-16:00 北京时间简单判断
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        return now.weekday() < 5 and 9 <= now.hour < 16


def smart_fetch(
    realtime_fetchers: list,
    mongo_collection: str,
    mongo_query: Optional[Dict] = None,
    mongo_sort: str = "dt",
    save_collection: Optional[str] = None,
    save_dedup: Optional[Dict] = None,
    market: str = "A",
) -> Any:
    """
    统一数据获取入口 — 贯穿所有功能的核心逻辑。

    规则：
      - 交易日盘中 → 依次尝试 realtime_fetchers（东财/新浪/腾讯等，第一序列平等）
                      成功后自动写入 MongoDB
                      全部失败 → MongoDB 前一交易日
      - 非交易日/盘后 → 直接从 MongoDB 读取前一交易日数据
                        MongoDB 无数据 → 尝试实时源作为兜底

    Args:
        realtime_fetchers: 实时数据获取函数列表 [fn1, fn2, ...]
                           每个 fn() 返回 DataFrame/list/dict，失败返回 None 或抛异常
        mongo_collection: MongoDB 集合名
        mongo_query: MongoDB 查询条件
        mongo_sort: MongoDB 排序字段
        save_collection: 成功后保存到的集合名（默认同 mongo_collection）
        save_dedup: 保存时的去重条件
        market: 关注的市场 ("A" / "HK" / "US" / "all")

    Returns:
        数据（DataFrame / list / dict），或 None
    """
    save_col = save_collection or mongo_collection

    # 判断当前是否盘中
    live = _is_market_live(market)

    if live:
        # ── 盘中：实时源优先 ──
        for fetcher in realtime_fetchers:
            try:
                result = fetcher()
                if result is not None:
                    if isinstance(result, pd.DataFrame) and result.empty:
                        continue
                    if isinstance(result, (list, dict)) and not result:
                        continue
                    # 成功 → 写入 MongoDB
                    _auto_save(result, save_col, save_dedup)
                    return result
            except Exception as e:
                logger.debug(f"[smart_fetch] {fetcher.__name__} 失败: {e}")
                continue

        # 全部实时源失败 → MongoDB 降级
        logger.info(f"[smart_fetch] 盘中实时源全部失败，降级到 MongoDB ({mongo_collection})")
        return _read_mongo(mongo_collection, mongo_query, mongo_sort)

    else:
        # ── 非交易日/盘后：MongoDB 优先 ──
        result = _read_mongo(mongo_collection, mongo_query, mongo_sort)
        if result is not None:
            return result

        # MongoDB 无数据 → 尝试实时源兜底
        logger.info(f"[smart_fetch] 非交易日 MongoDB 无数据，尝试实时源 ({mongo_collection})")
        for fetcher in realtime_fetchers:
            try:
                result = fetcher()
                if result is not None:
                    if isinstance(result, pd.DataFrame) and result.empty:
                        continue
                    if isinstance(result, (list, dict)) and not result:
                        continue
                    _auto_save(result, save_col, save_dedup)
                    return result
            except Exception:
                continue

        return None


def get_last_trading_day(market: str = "A") -> str:
    """
    获取最近的交易日日期字符串（YYYY-MM-DD）。

    规则：
    - 工作日 9:30 之后 → 今天
    - 工作日 9:30 之前 → 上一个工作日
    - 周末/节假日 → 上一个工作日（周五）
    """
    from datetime import timedelta
    now = datetime.now()
    d = now

    # 如果是工作日且 A 股已开盘（9:30后），用今天
    if d.weekday() < 5 and d.hour >= 9 and (d.hour > 9 or d.minute >= 30):
        return d.strftime("%Y-%m-%d")

    # 否则回退到上一个工作日
    if d.weekday() < 5 and d.hour < 9:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def smart_fetch_with_meta(
    realtime_fetchers: list,
    mongo_collection: str,
    mongo_query: Optional[Dict] = None,
    mongo_sort: str = "dt",
    save_collection: Optional[str] = None,
    save_dedup: Optional[Dict] = None,
    market: str = "A",
) -> tuple:
    """
    带元信息的 smart_fetch — 返回 (data, meta_dict)。

    Args:
        realtime_fetchers: 带名称的元组列表 [("东财", fn1), ("新浪", fn2), ...]
        其余参数同 smart_fetch()

    Returns:
        (data, {"source": str, "data_date": str, "update_time": str})
        data 为 None 时 meta 仍有值
    """
    save_col = save_collection or mongo_collection
    live = _is_market_live(market)
    trading_day = get_last_trading_day(market)
    update_time = datetime.now().strftime("%m-%d %H:%M")

    meta = {"source": "未知", "data_date": trading_day, "update_time": update_time}

    if live:
        for item in realtime_fetchers:
            name, fetcher = item if isinstance(item, tuple) else (getattr(item, '__name__', '?'), item)
            try:
                result = fetcher()
                if result is not None:
                    if isinstance(result, pd.DataFrame) and result.empty:
                        continue
                    if isinstance(result, (list, dict)) and not result:
                        continue
                    _auto_save(result, save_col, save_dedup)
                    meta["source"] = name
                    return result, meta
            except Exception as e:
                logger.debug(f"[smart_fetch] {name} 失败: {e}")
                continue

        logger.info(f"[smart_fetch] 盘中实时源全部失败，降级到 MongoDB ({mongo_collection})")
        result = _read_mongo(mongo_collection, mongo_query, mongo_sort)
        meta["source"] = "MongoDB(历史)"
        return result, meta

    else:
        result = _read_mongo(mongo_collection, mongo_query, mongo_sort)
        if result is not None:
            meta["source"] = "MongoDB(历史)"
            return result, meta

        logger.info(f"[smart_fetch] 非交易日 MongoDB 无数据，尝试实时源 ({mongo_collection})")
        for item in realtime_fetchers:
            name, fetcher = item if isinstance(item, tuple) else (getattr(item, '__name__', '?'), item)
            try:
                result = fetcher()
                if result is not None:
                    if isinstance(result, pd.DataFrame) and result.empty:
                        continue
                    if isinstance(result, (list, dict)) and not result:
                        continue
                    _auto_save(result, save_col, save_dedup)
                    meta["source"] = name
                    return result, meta
            except Exception:
                continue

        return None, meta


def _is_market_live(market: str) -> bool:
    """判断指定市场是否在交易。"""
    try:
        from signals.core.market_hours import get_active_markets, Market
        active = get_active_markets()
        if market == "all":
            return len(active) > 0
        market_map = {"A": Market.A, "HK": Market.HK, "US": Market.US}
        return market_map.get(market, Market.A) in active
    except Exception:
        return is_any_market_live()


def _read_mongo(collection, query, sort_field):
    """从 MongoDB 读取最新数据，返回 DataFrame 或 None。"""
    docs = get_latest_docs(collection, query, sort_field)
    if docs:
        return pd.DataFrame(docs)
    return None


def _auto_save(result, collection, dedup):
    """实时数据成功后自动保存到 MongoDB。"""
    try:
        if isinstance(result, pd.DataFrame) and not result.empty:
            docs = result.to_dict("records")
            save_snapshot(collection, docs, dedup)
        elif isinstance(result, list) and result:
            save_snapshot(collection, result, dedup)
    except Exception as e:
        logger.debug(f"[smart_fetch] 自动保存失败: {e}")


# ─── 读取：获取最近一个交易日的数据 ──────────────────


def get_latest_docs(
    collection: str,
    query: Optional[Dict] = None,
    sort_field: str = "dt",
    limit: int = 0,
) -> List[Dict]:
    """
    从 MongoDB 读取最近一个交易日的文档列表。

    Args:
        collection: 集合名 (board_ranking / concept_ranking / kline_cache / ...)
        query: 额外过滤条件 (如 {"source": "ths"})
        sort_field: 排序字段（默认 "dt"）
        limit: 限制返回数量（0 = 不限）

    Returns:
        文档列表（去掉 _id），失败返回空列表
    """
    db = get_db()
    if db is None:
        return []

    try:
        col = db[collection]
        q = dict(query) if query else {}

        # 先找到最新日期
        latest = col.find_one(q, sort=[(sort_field, -1)])
        if not latest:
            logger.debug(f"[MongoDB] {collection} 无数据 (query={q})")
            return []

        latest_dt = latest[sort_field]

        # 查询该日期的所有文档
        q[sort_field] = latest_dt
        cursor = col.find(q, {"_id": 0}).sort(sort_field, -1)
        if limit > 0:
            cursor = cursor.limit(limit)

        docs = list(cursor)
        logger.info(
            f"[MongoDB 降级] 从 {collection} 读取 {len(docs)} 条 "
            f"(日期: {latest_dt})"
        )
        return docs

    except Exception as e:
        logger.warning(f"[MongoDB] 读取 {collection} 失败: {e}")
        return []


def get_latest_df(
    collection: str,
    query: Optional[Dict] = None,
    columns: Optional[List[str]] = None,
    sort_field: str = "dt",
) -> Optional[pd.DataFrame]:
    """
    读取最近一个交易日数据，返回 DataFrame。

    Args:
        collection: 集合名
        query: 过滤条件
        columns: 需要的列（None = 全部）
        sort_field: 排序字段

    Returns:
        DataFrame 或 None
    """
    docs = get_latest_docs(collection, query, sort_field)
    if not docs:
        return None

    df = pd.DataFrame(docs)
    if columns:
        available = [c for c in columns if c in df.columns]
        if available:
            df = df[available]
    return df


def get_kline_docs(
    collection: str,
    code: str,
    freq: str = "daily",
    limit: int = 0,
) -> List[Dict]:
    """
    读取 K 线历史数据（按 code+freq 查询，返回全部历史而非单日）。

    Args:
        collection: 集合名 (kline_cache / bars)
        code: 股票代码
        freq: 频率 (daily / weekly)
        limit: 限制返回数量

    Returns:
        K 线记录列表，按时间正序
    """
    db = get_db()
    if db is None:
        return []

    try:
        col = db[collection]
        q = {"code": code, "freq": freq}
        cursor = col.find(q, {"_id": 0}).sort("dt", 1)
        if limit > 0:
            cursor = cursor.limit(limit)

        docs = list(cursor)
        if docs:
            logger.info(
                f"[MongoDB 降级] 从 {collection} 读取 {code} "
                f"{freq} K线 {len(docs)} 根"
            )
        return docs

    except Exception as e:
        logger.warning(f"[MongoDB] 读取 K 线 {code} 失败: {e}")
        return []


# ─── 写入：保存实时数据快照 ──────────────────────────


def save_snapshot(
    collection: str,
    docs: List[Dict],
    dedup: Optional[Dict] = None,
) -> bool:
    """
    保存实时数据快照到 MongoDB。

    Args:
        collection: 集合名
        docs: 文档列表
        dedup: 去重查询条件 (如 {"dt": today, "source": "ths"})
               匹配到则先删再插，避免重复

    Returns:
        是否成功
    """
    db = get_db()
    if db is None or not docs:
        return False

    try:
        col = db[collection]
        if dedup:
            col.delete_many(dedup)
        col.insert_many(docs, ordered=False)
        logger.debug(
            f"[MongoDB] 保存 {len(docs)} 条到 {collection}"
        )
        return True

    except Exception as e:
        logger.warning(f"[MongoDB] 写入 {collection} 失败: {e}")
        return False


def save_kline(
    collection: str,
    code: str,
    freq: str,
    records: List[Dict],
) -> bool:
    """
    保存 K 线数据到 MongoDB（按 code+freq 去重后 upsert）。

    Args:
        collection: 集合名 (kline_cache)
        code: 股票代码
        freq: 频率 (daily / weekly)
        records: K 线记录列表 [{dt, open, high, low, close, vol}, ...]

    Returns:
        是否成功
    """
    db = get_db()
    if db is None or not records:
        return False

    try:
        col = db[collection]
        # 删除旧数据，整体替换
        col.delete_many({"code": code, "freq": freq})
        docs = []
        for r in records:
            doc = {**r, "code": code, "freq": freq}
            # 确保 dt 是字符串或 datetime
            docs.append(doc)
        col.insert_many(docs, ordered=False)
        logger.debug(
            f"[MongoDB] 保存 {code} {freq} K线 {len(docs)} 根"
        )
        return True

    except Exception as e:
        logger.warning(f"[MongoDB] 写入 K 线 {code} 失败: {e}")
        return False
