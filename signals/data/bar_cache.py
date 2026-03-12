# -*- coding: utf-8 -*-
"""
股票K线缓存 — 可插拔后端（本地磁盘 / Redis）

本地磁盘实现：当日有效的 JSON 文件缓存，自动清理过期文件。
未来切换 Redis 只需实现 BarCacheBackend 接口并调用 set_cache()。

用法：
    from signals.data.bar_cache import get_cache
    cache = get_cache()
    cached = cache.get("SH_600519_20260310")
    cache.set("SH_600519_20260310", records)

切换 Redis：
    from signals.data.bar_cache import set_cache
    set_cache(RedisBarCache(redis_url="redis://..."))
"""
import abc
import json
import os
from typing import Dict, List, Optional


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


# ── 模块级单例 ────────────────────────────────────────────
_DEFAULT_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", ".data", "cache", "stock_daily"
)
_cache: BarCacheBackend = DiskBarCache(_DEFAULT_DIR)


def get_cache() -> BarCacheBackend:
    """获取当前缓存后端实例。"""
    return _cache


def set_cache(backend: BarCacheBackend):
    """切换缓存后端（如 RedisBarCache）。"""
    global _cache
    _cache = backend
