# -*- coding: utf-8 -*-
"""
RSS 资讯抓取层

无外部依赖，使用内置 xml.etree.ElementTree 解析 RSS/Atom。

信息源（网友评价最高的免费渠道）:
1. Seeking Alpha   — 深度个股/板块分析，社区公认最佳
2. CNBC Markets    — 实时市场新闻，覆盖最全面
3. MarketWatch     — 综合市场资讯，周报口碑好
4. Finviz          — 板块热力图+新闻
5. 华尔街见闻       — 中文快讯/分析，中文覆盖最好
6. Nasdaq          — 官方市场新闻
7. Reuters         — 全球宏观视角
"""
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import List, Optional
from urllib.request import Request, urlopen

_log = logging.getLogger("signals.rss")

# ─────────────────────────────────────────────────────────
# RSS 源配置
# ─────────────────────────────────────────────────────────

RSS_FEEDS = {
    "seeking_alpha": {
        "name": "Seeking Alpha",
        "url": "https://seekingalpha.com/feed.xml",
        "category": "analysis",
        "icon": "📊",
    },
    "cnbc_markets": {
        "name": "CNBC Markets",
        "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
        "category": "news",
        "icon": "📺",
    },
    "marketwatch": {
        "name": "MarketWatch",
        "url": "https://feeds.marketwatch.com/marketwatch/topstories/",
        "category": "news",
        "icon": "📰",
    },
    "finviz": {
        "name": "Finviz News",
        "url": "https://finviz.com/news_export.ashx?v=1",
        "category": "news",
        "icon": "🗺️",
    },
    "wallstreetcn": {
        "name": "华尔街见闻",
        "url": "https://dedicated.wallstreetcn.com/rss.xml",
        "category": "analysis_cn",
        "icon": "🇨🇳",
    },
    "nasdaq": {
        "name": "Nasdaq Market News",
        "url": "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
        "category": "news",
        "icon": "🏛️",
    },
    "reuters_markets": {
        "name": "Reuters Markets",
        "url": "https://www.reutersagency.com/feed/?best-topics=business-finance",
        "category": "macro",
        "icon": "🌐",
    },
}

# 分类中文名
CATEGORY_LABELS = {
    "analysis": "深度分析",
    "analysis_cn": "中文分析",
    "news": "市场新闻",
    "macro": "宏观视角",
}

# ─────────────────────────────────────────────────────────
# 缓存基础设施 (同 social_fetcher.py 模式)
# ─────────────────────────────────────────────────────────

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / ".data" / "cache"


def _cache_path(name: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"rss_{name}.json"


def _save_cache(name: str, data: dict):
    try:
        path = _cache_path(name)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_cache(name: str, max_age: float = 3600) -> Optional[dict]:
    """加载缓存, 默认1小时过期"""
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
# 去重持久化 (已推送/已见条目)
# ─────────────────────────────────────────────────────────

_SEEN_MAX = 2000


def _load_seen() -> set:
    path = _cache_path("seen")
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("guids", []))
    except Exception:
        return set()


def _save_seen(seen: set):
    guids = list(seen)[-_SEEN_MAX:]
    _save_cache("seen", {"ts": time.time(), "guids": guids})


# ─────────────────────────────────────────────────────────
# 数据模型
# ─────────────────────────────────────────────────────────

@dataclass
class RSSEntry:
    title: str
    link: str
    summary: str
    source: str
    category: str
    published: str
    guid: str
    icon: str = ""


# ─────────────────────────────────────────────────────────
# XML 解析 (内置, 无外部依赖)
# ─────────────────────────────────────────────────────────

_USER_AGENT = "Mozilla/5.0 (compatible; SignalsBot/1.0)"

# Atom 命名空间
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _strip_html(text: str) -> str:
    """去除 HTML 标签"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("\n", " ").strip()


def _truncate(text: str, max_len: int = 200) -> str:
    text = _strip_html(text)
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _parse_date(date_str: str) -> str:
    """尝试解析 RSS/Atom 日期格式"""
    if not date_str:
        return datetime.now().isoformat()
    # RFC 2822 (RSS pubDate)
    try:
        return parsedate_to_datetime(date_str).isoformat()
    except Exception:
        pass
    # ISO 8601 (Atom updated/published)
    try:
        # 去除尾部 Z 或时区偏移的简单处理
        clean = date_str.replace("Z", "+00:00")
        return datetime.fromisoformat(clean).isoformat()
    except Exception:
        pass
    return datetime.now().isoformat()


def _xml_text(elem, tag: str, ns: dict = None) -> str:
    """安全获取 XML 子元素文本"""
    child = elem.find(tag, ns) if ns else elem.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _parse_rss_xml(xml_bytes: bytes, feed_cfg: dict, max_entries: int) -> List[RSSEntry]:
    """解析 RSS 2.0 或 Atom feed"""
    root = ET.fromstring(xml_bytes)
    entries = []

    # RSS 2.0: <rss><channel><item>...
    items = root.findall(".//item")
    if items:
        for item in items[:max_entries]:
            title = _xml_text(item, "title")
            link = _xml_text(item, "link")
            desc = _xml_text(item, "description")
            pub = _xml_text(item, "pubDate")
            guid = _xml_text(item, "guid") or link or title
            entries.append(RSSEntry(
                title=title or "无标题",
                link=link,
                summary=_truncate(desc),
                source=feed_cfg["name"],
                category=feed_cfg["category"],
                published=_parse_date(pub),
                guid=guid,
                icon=feed_cfg.get("icon", ""),
            ))
        return entries

    # Atom: <feed><entry>...
    atom_entries = root.findall("atom:entry", _ATOM_NS)
    if not atom_entries:
        # 尝试无命名空间
        atom_entries = root.findall("entry")
    if not atom_entries:
        # 尝试带默认命名空间
        ns_match = re.match(r"\{(.+?)\}", root.tag)
        if ns_match:
            ns = {"ns": ns_match.group(1)}
            atom_entries = root.findall("ns:entry", ns)

    for entry in atom_entries[:max_entries]:
        # Atom 可能有命名空间，需要灵活匹配
        title = ""
        link = ""
        summary = ""
        published = ""
        guid = ""
        for child in entry:
            local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if local == "title":
                title = (child.text or "").strip()
            elif local == "link":
                link = child.get("href", "") or (child.text or "").strip()
            elif local in ("summary", "content"):
                summary = (child.text or "").strip()
            elif local in ("published", "updated"):
                published = published or (child.text or "").strip()
            elif local == "id":
                guid = (child.text or "").strip()

        entries.append(RSSEntry(
            title=title or "无标题",
            link=link,
            summary=_truncate(summary),
            source=feed_cfg["name"],
            category=feed_cfg["category"],
            published=_parse_date(published),
            guid=guid or link or title,
            icon=feed_cfg.get("icon", ""),
        ))

    return entries


# ─────────────────────────────────────────────────────────
# 抓取逻辑
# ─────────────────────────────────────────────────────────

def _fetch_one_feed(feed_key: str, feed_cfg: dict, max_entries: int = 10) -> List[RSSEntry]:
    """抓取单个 RSS 源，带缓存"""
    cached = _load_cache(f"feed_{feed_key}", max_age=3600)
    if cached:
        return [RSSEntry(**e) for e in cached.get("entries", [])]

    entries = []
    try:
        _log.info("抓取 RSS: %s (%s)", feed_cfg["name"], feed_cfg["url"])
        req = Request(feed_cfg["url"], headers={"User-Agent": _USER_AGENT})
        with urlopen(req, timeout=15) as resp:
            xml_bytes = resp.read()
        entries = _parse_rss_xml(xml_bytes, feed_cfg, max_entries)
    except Exception as exc:
        _log.warning("RSS 抓取异常: %s — %s", feed_cfg["name"], exc)
        return []

    _save_cache(f"feed_{feed_key}", {
        "ts": time.time(),
        "entries": [asdict(e) for e in entries],
    })
    return entries


def fetch_all(max_per_feed: int = 10, only_new: bool = True) -> List[RSSEntry]:
    """
    抓取所有 RSS 源，返回条目列表（默认仅返回新条目）。
    """
    all_entries: List[RSSEntry] = []

    for key, cfg in RSS_FEEDS.items():
        try:
            entries = _fetch_one_feed(key, cfg, max_entries=max_per_feed)
            all_entries.extend(entries)
        except Exception as exc:
            _log.warning("跳过 %s: %s", cfg["name"], exc)

    all_entries.sort(key=lambda e: e.published, reverse=True)

    if not only_new:
        return all_entries

    seen = _load_seen()
    new_entries = [e for e in all_entries if e.guid not in seen]
    seen.update(e.guid for e in new_entries)
    _save_seen(seen)

    return new_entries


# ─────────────────────────────────────────────────────────
# 格式化输出
# ─────────────────────────────────────────────────────────

def format_text(entries: List[RSSEntry], limit: int = 30) -> str:
    """格式化为纯文本（微信不渲染 Markdown），按分类分组。"""
    if not entries:
        return "📭 暂无新资讯"

    entries = entries[:limit]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"📰 RSS 市场资讯 ({now})", ""]

    by_cat: dict = {}
    for e in entries:
        by_cat.setdefault(e.category, []).append(e)

    for cat, cat_entries in by_cat.items():
        label = CATEGORY_LABELS.get(cat, cat)
        lines.append(f"【{label}】")
        for e in cat_entries:
            lines.append(f"  {e.icon} [{e.source}] {e.title}")
            if e.summary:
                lines.append(f"    {e.summary[:100]}")
            lines.append(f"    🔗 {e.link}")
            lines.append("")
        lines.append("")

    lines.append(f"共 {len(entries)} 条新资讯")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────
# 入口函数 (由 run.py 调用)
# ─────────────────────────────────────────────────────────

def run_rss(args):
    """RSS 资讯订阅：抓取 + 显示 + 可选推送。"""
    from rich.console import Console
    console = Console()

    console.print("\n  🔄 正在抓取 RSS 资讯...\n", style="cyan")

    only_new = not getattr(args, "all", False)
    entries = fetch_all(only_new=only_new)

    text = format_text(entries)
    console.print(text)

    if getattr(args, "push", False) and entries:
        try:
            from signals import notify
            notify.send_text(text)
            console.print("\n  ✅ 已推送到飞书/微信\n", style="green")
        except Exception as exc:
            console.print(f"\n  ⚠️ 推送失败: {exc}\n", style="yellow")
