# -*- coding: utf-8 -*-
"""
爬虫基类 — 统一数据结构 + 限速 + 缓存 + 重试
"""
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

_log = logging.getLogger("signals.scrapers")

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent.parent / ".data" / "cache" / "scrapers"


@dataclass
class ScrapedPost:
    """统一的帖子数据结构"""
    title: str = ""
    content: str = ""           # 帖子摘要/正文片段
    author: str = ""
    post_time: str = ""         # ISO格式 或 "3小时前" 等
    reply_count: int = 0
    view_count: int = 0
    like_count: int = 0
    source: str = ""            # "tieba" / "xueqiu" / "eastmoney"
    url: str = ""
    sentiment_hint: float = 0.0  # 标题情绪预估 (-1~+1)


@dataclass
class ScrapeResult:
    """爬取结果"""
    keyword: str = ""
    source: str = ""
    posts: List[ScrapedPost] = field(default_factory=list)
    total_count: int = 0        # 搜索结果总数（如果可获取）
    avg_reply: float = 0.0      # 平均回复数
    avg_view: float = 0.0       # 平均浏览数
    heat_index: float = 0.0     # 计算得到的热度指标 (0-100)
    fetch_time: str = ""
    error: str = ""


class BaseScraper(ABC):
    """
    爬虫基类。

    特性:
    - 请求限速 (min_interval 秒)
    - 磁盘缓存 (cache_ttl 秒)
    - 简单重试 (max_retries 次)
    """

    def __init__(self, source_name: str, min_interval: float = 2.0,
                 cache_ttl: int = 7200, max_retries: int = 2):
        self.source_name = source_name
        self.min_interval = min_interval
        self.cache_ttl = cache_ttl
        self.max_retries = max_retries
        self._last_request_time: float = 0.0

    def _rate_limit(self):
        """限速：两次请求间至少间隔 min_interval 秒"""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_time = time.time()

    def _cache_path(self, key: str) -> Path:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        safe_key = key.replace("/", "_").replace(" ", "_")[:80]
        return _CACHE_DIR / f"{self.source_name}_{safe_key}.json"

    def _load_cache(self, key: str) -> Optional[dict]:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - data.get("ts", 0) > self.cache_ttl:
                return None
            return data
        except Exception:
            return None

    def _save_cache(self, key: str, data: dict):
        try:
            data["ts"] = time.time()
            path = self._cache_path(key)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def search(self, keyword: str, limit: int = 20) -> ScrapeResult:
        """
        搜索关键词，返回结果。
        带缓存 + 重试。
        """
        # 检查缓存
        cached = self._load_cache(keyword)
        if cached and "posts" in cached:
            result = ScrapeResult(
                keyword=keyword,
                source=self.source_name,
                posts=[ScrapedPost(**p) for p in cached["posts"]],
                total_count=cached.get("total_count", 0),
                avg_reply=cached.get("avg_reply", 0),
                avg_view=cached.get("avg_view", 0),
                heat_index=cached.get("heat_index", 0),
                fetch_time=cached.get("fetch_time", ""),
            )
            _log.debug(f"[{self.source_name}] 缓存命中: {keyword}")
            return result

        # 网络请求 (带重试)
        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                self._rate_limit()
                result = self._do_search(keyword, limit)
                result.source = self.source_name
                result.keyword = keyword
                result.fetch_time = time.strftime("%Y-%m-%d %H:%M:%S")

                # 计算聚合指标
                if result.posts:
                    result.avg_reply = sum(p.reply_count for p in result.posts) / len(result.posts)
                    result.avg_view = sum(p.view_count for p in result.posts) / len(result.posts)
                    result.heat_index = self._compute_heat(result)

                # 保存缓存
                self._save_cache(keyword, {
                    "posts": [self._post_to_dict(p) for p in result.posts],
                    "total_count": result.total_count,
                    "avg_reply": result.avg_reply,
                    "avg_view": result.avg_view,
                    "heat_index": result.heat_index,
                    "fetch_time": result.fetch_time,
                })
                return result
            except Exception as e:
                last_error = str(e)
                _log.warning(f"[{self.source_name}] 搜索失败 (attempt {attempt+1}): {e}")
                if attempt < self.max_retries:
                    time.sleep(1.0 * (attempt + 1))

        return ScrapeResult(
            keyword=keyword,
            source=self.source_name,
            error=last_error,
            fetch_time=time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    @abstractmethod
    def _do_search(self, keyword: str, limit: int) -> ScrapeResult:
        """子类实现的实际搜索逻辑"""
        ...

    def _compute_heat(self, result: ScrapeResult) -> float:
        """计算热度指标 (0-100)，子类可覆盖"""
        if not result.posts:
            return 0.0
        # 基础热度 = f(帖子数, 回复数, 浏览数)
        post_score = min(len(result.posts) / 20.0, 1.0) * 30
        reply_score = min(result.avg_reply / 50.0, 1.0) * 40
        view_score = min(result.avg_view / 5000.0, 1.0) * 30
        return round(min(post_score + reply_score + view_score, 100), 1)

    @staticmethod
    def _post_to_dict(p: ScrapedPost) -> dict:
        return {
            "title": p.title,
            "content": p.content,
            "author": p.author,
            "post_time": p.post_time,
            "reply_count": p.reply_count,
            "view_count": p.view_count,
            "like_count": p.like_count,
            "source": p.source,
            "url": p.url,
            "sentiment_hint": p.sentiment_hint,
        }
