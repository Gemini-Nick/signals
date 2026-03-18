# -*- coding: utf-8 -*-
"""
股票K线缓存 — 可插拔后端（本地磁盘 / MongoDB / Redis）

本地磁盘实现：当日有效的 JSON 文件缓存，自动清理过期文件。
MongoDB 实现：TTL 索引自动过期，替代手动清理。
切换后端只需实现 BarCacheBackend 接口并调用 set_cache()。

用法：
    from signals.data.bar_cache import get_cache
    cache = get_cache()
    cached = cache.get("SH_600519_20260310")
    cache.set("SH_600519_20260310", records)

切换 MongoDB：
    from signals.data.bar_cache import set_cache, MongoBarCache
    set_cache(MongoBarCache(mongo_db))
"""
import abc
import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("signals.data.bar_cache")


class BarCacheBackend(abc.ABC):
    """缓存后端协议 — 未来换 Redis 只需实现此接口。"""

    @abc.abstractmethod
    def get(self, key: str) -> Optional[List[Dict]]:
        """读取缓存，未命中返回 None。"""
        ...

    @abc.abstractmethod
    def set(self, key: str, records: List[Dict], ttl_seconds: int = 86400):
        """写入缓存，ttl_seconds 供 Redis 等有过期机制的后端使用。"""
        ...


class DiskBarCache(BarCacheBackend):
    """本地磁盘缓存（JSON 文件，当日有效）。"""

    def __init__(self, cache_dir: str):
        self._dir = cache_dir
        os.makedirs(self._dir, exist_ok=True)

    def _path(self, key: str) -> str:
        return os.path.join(self._dir, f"{key}.json")

    def get(self, key: str) -> Optional[List[Dict]]:
        path = self._path(key)
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None

    def set(self, key: str, records: List[Dict], ttl_seconds: int = 86400):
        try:
            with open(self._path(key), "w") as f:
                json.dump(records, f)
        except Exception:
            pass  # 写缓存失败不影响主流程

    def cleanup_old(self, today_tag: str):
        """删除非今日缓存文件（在每次 review 入口调用）。"""
        try:
            for name in os.listdir(self._dir):
                if name.endswith(".json") and today_tag not in name:
                    os.remove(os.path.join(self._dir, name))
        except Exception:
            pass


class MongoBarCache(BarCacheBackend):
    """
    MongoDB 缓存后端 — TTL 索引自动过期，替代 cleanup_old()。

    使用 bar_cache collection，文档结构：
    {
        _id: "SH_600519_20260310",
        records: [{dt, open, high, ...}, ...],
        created_at: ISODate(...)  // TTL 索引：24h 后自动删除
    }
    """

    def __init__(self, db):
        self._col = db["bar_cache"]
        # 确保 TTL 索引存在（幂等操作）
        try:
            self._col.create_index(
                "created_at", expireAfterSeconds=86400)
        except Exception:
            pass  # 索引已存在

    def get(self, key: str) -> Optional[List[Dict]]:
        try:
            doc = self._col.find_one({"_id": key})
            return doc["records"] if doc else None
        except Exception:
            return None

    def set(self, key: str, records: List[Dict], ttl_seconds: int = 86400):
        try:
            self._col.update_one(
                {"_id": key},
                {"$set": {
                    "records": records,
                    "created_at": datetime.utcnow(),
                }},
                upsert=True,
            )
        except Exception:
            pass  # 写缓存失败不影响主流程

    def cleanup_old(self, today_tag: str):
        """无需手动清理 — TTL 索引自动处理。保留方法签名兼容。"""
        pass


# ── 模块级单例 ────────────────────────────────────────────
_DEFAULT_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", ".data", "cache", "stock_daily"
)
_cache: BarCacheBackend = DiskBarCache(_DEFAULT_DIR)
_mongo_cache_initialized = False


def get_cache() -> BarCacheBackend:
    """获取当前缓存后端实例。DB_ENABLED 时自动切换为 MongoBarCache。"""
    global _cache, _mongo_cache_initialized
    if not _mongo_cache_initialized:
        _mongo_cache_initialized = True
        try:
            import config
            if config.DB_ENABLED:
                from .db_source import get_mongo_source
                src = get_mongo_source()
                if src and src.ping():
                    _cache = MongoBarCache(src._db)
                    logger.info("BarCache 已切换为 MongoBarCache")
        except Exception as e:
            logger.debug(f"MongoBarCache 初始化失败，保持 DiskBarCache: {e}")
    return _cache


def set_cache(backend: BarCacheBackend):
    """手动切换缓存后端。"""
    global _cache
    _cache = backend
