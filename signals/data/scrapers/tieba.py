# -*- coding: utf-8 -*-
"""
东方财富股吧爬虫 — 获取个股/概念讨论帖数据

数据源: 东方财富股吧 (guba.eastmoney.com)
- 个股吧: guba.eastmoney.com/list,SZ002261.html
- 话题搜索: so.eastmoney.com/News/s?keyword=xxx

抓取内容: 帖子标题、回复数、浏览数、发帖时间
用途: 计算个股讨论热度、标题情绪分析
"""
import logging
import re
from typing import List

from signals.data.scrapers.base import BaseScraper, ScrapedPost, ScrapeResult

_log = logging.getLogger("signals.scrapers.tieba")

# 简单的情绪关键词（用于标题快速判断）
_BULLISH_WORDS = {"涨停", "暴涨", "利好", "突破", "龙头", "翻倍", "起飞", "爆发",
                  "加仓", "抄底", "牛", "大阳", "新高", "金叉", "放量"}
_BEARISH_WORDS = {"跌停", "暴跌", "利空", "割肉", "套牢", "崩盘", "暴雷", "清仓",
                  "减仓", "跑路", "亏", "大阴", "新低", "死叉", "缩量"}


def _title_sentiment(title: str) -> float:
    """标题快速情绪判断 (-1 ~ +1)"""
    bull = sum(1 for w in _BULLISH_WORDS if w in title)
    bear = sum(1 for w in _BEARISH_WORDS if w in title)
    if bull + bear == 0:
        return 0.0
    return round((bull - bear) / (bull + bear), 2)


class TiebaScraper(BaseScraper):
    """
    东方财富股吧爬虫。

    通过 requests 抓取股吧帖子列表，提取标题、回复、浏览等数据。
    限速 2s/请求，缓存 2h。
    """

    def __init__(self):
        super().__init__(
            source_name="eastmoney_guba",
            min_interval=2.0,
            cache_ttl=7200,
            max_retries=2,
        )
        self._headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://guba.eastmoney.com/",
        }

    def _do_search(self, keyword: str, limit: int) -> ScrapeResult:
        """
        搜索股吧帖子。

        keyword 支持:
        - 股票代码: "002261" → 抓取该股吧
        - 关键词: "算力" → 搜索帖子
        """
        import requests
        from signals.data.fetcher import no_proxy

        posts: List[ScrapedPost] = []

        # 判断是股票代码还是关键词
        if re.match(r"^\d{6}$", keyword):
            posts = self._fetch_stock_guba(keyword, limit)
        else:
            posts = self._search_keyword(keyword, limit)

        return ScrapeResult(posts=posts, total_count=len(posts))

    def _fetch_stock_guba(self, code: str, limit: int) -> List[ScrapedPost]:
        """抓取个股股吧帖子列表"""
        import requests
        from signals.data.fetcher import no_proxy

        # 东财股吧 API (JSON)
        url = "https://guba.eastmoney.com/interface/GetData"
        params = {
            "path": "newtopic/api/Topic/TopicListByCode",
            "param": f"code={code}&ordertype=2&ps={min(limit, 30)}&p=1",
        }

        try:
            with no_proxy():
                resp = requests.get(url, params=params, headers=self._headers, timeout=10)
            data = resp.json()
            posts = []

            # 东财股吧返回结构可能变化，尝试多种解析路径
            topic_list = (data.get("re", []) or
                          data.get("data", {}).get("list", []) or
                          data.get("result", {}).get("data", {}).get("list", []))

            if not topic_list and isinstance(data, dict):
                # 尝试直接解析 HTML 兜底
                return self._parse_guba_html(code, limit)

            for item in topic_list[:limit]:
                title = str(item.get("post_title", item.get("title", "")))
                posts.append(ScrapedPost(
                    title=title,
                    author=str(item.get("post_user", {}).get("user_nickname", "")),
                    reply_count=int(item.get("reply_count", item.get("comment_count", 0))),
                    view_count=int(item.get("view_count", item.get("read_count", 0))),
                    post_time=str(item.get("post_publish_time", item.get("create_time", ""))),
                    source="eastmoney_guba",
                    sentiment_hint=_title_sentiment(title),
                ))
            return posts
        except Exception as e:
            _log.debug(f"股吧API获取失败 [{code}]: {e}, 尝试HTML解析")
            return self._parse_guba_html(code, limit)

    def _parse_guba_html(self, code: str, limit: int) -> List[ScrapedPost]:
        """HTML 兜底解析"""
        import requests
        from signals.data.fetcher import no_proxy

        url = f"https://guba.eastmoney.com/list,{code}.html"
        try:
            with no_proxy():
                resp = requests.get(url, headers=self._headers, timeout=10)
                resp.encoding = "utf-8"

            posts = []
            # 简单正则提取帖子列表
            # 东财股吧帖子行格式: <div class="listitem">...<span class="l3">title</span>...
            pattern = re.compile(
                r'class="l1[^"]*"[^>]*>(\d+)</.*?'
                r'class="l2[^"]*"[^>]*>(\d+)</.*?'
                r'class="l3[^"]*"[^>]*>.*?title="([^"]*)"',
                re.DOTALL,
            )
            matches = pattern.findall(resp.text)
            for view, reply, title in matches[:limit]:
                posts.append(ScrapedPost(
                    title=title.strip(),
                    reply_count=int(reply),
                    view_count=int(view),
                    source="eastmoney_guba",
                    sentiment_hint=_title_sentiment(title.strip()),
                ))
            return posts
        except Exception as e:
            _log.warning(f"股吧HTML解析失败 [{code}]: {e}")
            return []

    def _search_keyword(self, keyword: str, limit: int) -> List[ScrapedPost]:
        """搜索关键词相关帖子"""
        import requests
        from signals.data.fetcher import no_proxy

        url = "https://searchapi.eastmoney.com/bussiness/web/QuotationSearch"
        params = {
            "keyword": keyword,
            "type": "guba",
            "pageindex": 1,
            "pagesize": min(limit, 20),
        }

        try:
            with no_proxy():
                resp = requests.get(url, params=params, headers=self._headers, timeout=10)
            data = resp.json()
            posts = []

            items = data.get("result", {}).get("gubaList", []) or []
            for item in items[:limit]:
                title = str(item.get("Title", ""))
                # 清理 HTML 标签
                title = re.sub(r"<[^>]+>", "", title)
                posts.append(ScrapedPost(
                    title=title,
                    reply_count=int(item.get("ReplyCount", 0)),
                    view_count=int(item.get("ViewCount", 0)),
                    post_time=str(item.get("PostDate", "")),
                    source="eastmoney_guba",
                    url=str(item.get("Url", "")),
                    sentiment_hint=_title_sentiment(title),
                ))
            return posts
        except Exception as e:
            _log.warning(f"股吧搜索失败 [{keyword}]: {e}")
            return []

    def _compute_heat(self, result: ScrapeResult) -> float:
        """股吧特化热度计算"""
        if not result.posts:
            return 0.0

        # 股吧帖子回复/浏览量通常较高
        post_score = min(len(result.posts) / 30.0, 1.0) * 25
        reply_score = min(result.avg_reply / 100.0, 1.0) * 35
        view_score = min(result.avg_view / 10000.0, 1.0) * 25

        # 情绪极化度加分（正/负情绪越强烈，说明讨论越激烈）
        sentiments = [abs(p.sentiment_hint) for p in result.posts if p.sentiment_hint != 0]
        polar_score = 0.0
        if sentiments:
            polar_score = min(sum(sentiments) / len(sentiments) * 100, 100) * 0.15

        return round(min(post_score + reply_score + view_score + polar_score, 100), 1)
