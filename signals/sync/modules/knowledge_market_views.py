# -*- coding: utf-8 -*-
"""Publish research-note market views without turning them into trade signals."""
from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from pymongo.database import Database

from signals.core.market_time import naive_market_now

logger = logging.getLogger("signals.sync.knowledge_market_views")

VAULT_SUBDIRS = ("10 Knowledge", "10 Inbox/WeChat", "20 Sources", "30 Assets/Originals")
ALLOWED_EFFECTS = {"confirm", "downgrade", "block", "exit_priority", "context_only"}


def _pure_a_code(symbol: Any) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    pure = raw.split(".", 1)[-1] if "." in raw else raw
    pure = pure.replace("SH", "").replace("SZ", "").replace("BJ", "")
    return pure if pure.isdigit() and len(pure) == 6 else ""


def _prefixed_symbol(symbol: Any) -> str:
    code = _pure_a_code(symbol)
    if not code:
        return str(symbol or "").strip()
    if code.startswith(("6", "9")):
        return f"SH.{code}"
    if code.startswith(("4", "8")):
        return f"BJ.{code}"
    return f"SZ.{code}"


def _note_date(note) -> str:
    value = getattr(note, "date", "") or ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value)[:10]


def _sentiment_rank(sentiment: str) -> int:
    return {"看多": 2, "中性": 1, "看空": 0}.get(sentiment, 1)


def _notes_dir() -> str:
    import config

    configured = getattr(config, "NOTES_DIR", "notes")
    path = Path(configured)
    if not path.is_absolute():
        path = Path.cwd() / path
    return str(path)


def _vault_dir() -> Path:
    import config

    configured = os.getenv("SIGNALS_KNOWLEDGE_VAULT_DIR") or getattr(config, "KNOWLEDGE_VAULT_DIR", "")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Desktop" / "知识库"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    raw = text[3:end].strip()
    body = text[end + 4:].lstrip()
    meta: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, body


def _first_heading(body: str, fallback: str) -> str:
    for line in body.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip() or fallback
    return fallback


def _asset_paths(text: str, meta: dict[str, str]) -> list[str]:
    paths: list[str] = []
    for key in ("asset_path", "asset_paths", "source_paths", "derived_from"):
        value = meta.get(key)
        if value:
            paths.extend(item.strip() for item in value.split(",") if item.strip())
    paths.extend(re.findall(r"!\[\[([^\]]+)\]\]", text))
    paths.extend(re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text))
    seen: set[str] = set()
    output: list[str] = []
    for item in paths:
        if item and item not in seen:
            seen.add(item)
            output.append(item)
    return output[:12]


def _knowledge_docs(vault_dir: Path) -> list[dict[str, Any]]:
    if not vault_dir.exists():
        return []
    max_files = max(1, int(os.getenv("SIGNALS_KNOWLEDGE_MAX_FILES", "500")))
    max_chars = max(2000, int(os.getenv("SIGNALS_KNOWLEDGE_MAX_CHARS_PER_FILE", "120000")))
    docs: list[dict[str, Any]] = []
    for subdir in VAULT_SUBDIRS:
        root = vault_dir / subdir
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            if len(docs) >= max_files:
                return docs
            try:
                raw = path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
            except Exception:
                continue
            meta, body = _parse_frontmatter(raw)
            rel = str(path.relative_to(vault_dir))
            title = meta.get("title") or _first_heading(body, path.stem)
            author_focus = (meta.get("author_focus") or meta.get("owner_topic") or "").lower()
            if not author_focus:
                if "胖哥" in rel or "胖哥" in title or "胖哥" in body[:4000]:
                    author_focus = "pangge"
                elif "道长" in rel or "道长" in title or "道长" in body[:4000]:
                    author_focus = "daozhang"
            date_match = re.search(r"(20\d{2}-\d{2}-\d{2})", rel)
            docs.append({
                "title": title,
                "path": rel,
                "tier": subdir,
                "author_focus": author_focus,
                "topic": meta.get("topic", ""),
                "date": meta.get("date", "") or (date_match.group(1) if date_match else ""),
                "text": body,
                "asset_paths": _asset_paths(raw, meta),
            })
    return docs


def _source_refs(docs: list[dict[str, Any]], author: str = "", limit: int = 12) -> list[dict[str, Any]]:
    filtered = [doc for doc in docs if not author or doc.get("author_focus") == author]
    if not filtered:
        filtered = docs
    filtered.sort(key=lambda item: (item.get("tier") == "10 Knowledge", item.get("tier") == "10 Inbox/WeChat", item.get("date", "")), reverse=True)
    return [
        {
            "title": doc.get("title", ""),
            "path": doc.get("path", ""),
            "tier": doc.get("tier", ""),
            "author_focus": doc.get("author_focus", ""),
            "asset_paths": doc.get("asset_paths", [])[:4],
        }
        for doc in filtered[:limit]
    ]


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _strategy_rule_views(docs: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    if not docs:
        return []
    combined = "\n".join(str(doc.get("text", "")) for doc in docs)
    daozhang_text = "\n".join(str(doc.get("text", "")) for doc in docs if doc.get("author_focus") == "daozhang")
    pangge_text = "\n".join(str(doc.get("text", "")) for doc in docs if doc.get("author_focus") == "pangge")
    today = now.date().isoformat()
    views: list[dict[str, Any]] = []
    views.append({
        "view_id": "strategy:combined:market_rules",
        "target_type": "strategy_rule",
        "market": "A",
        "rule_scope": "market_style_risk",
        "market_stage": "阶段判断先于个股判断；指数阶段、风格结构和仓位动作必须一起看。",
        "style_rotation": "不能只看沪指，要同时看国证2000、创业板、科创50、超大盘等结构，区分主线、补涨和情绪题材。",
        "mainline_status": "科技、小票、医药、锂电、化工等方向按市场阶段和角色切换确认，普涨后进入分化时优先确定性。",
        "position_policy": "反弹后分化时先控仓位，高位强势偏兑现，低位确定性承接才提高参与。",
        "participation_rule": "硬技术触发是候选前提；产业链和知识库只能确认、降级、阻断或提供背景。",
        "risk_rule": "公开利好不等于可交易，必须看赔率、拥挤度、参与性和风报比。",
        "right_side_requirement": "趋势弱或阶段不明时不做左侧，盘中等待止跌、放量、5m/15m 右侧确认。",
        "forbidden_chase": "产业链一致高潮、涨幅和广度背离、公开利好拥挤时禁止追高。",
        "knowledge_effect": "context_only",
        "confirmation_role": "rule_view_only",
        "candidate_policy": "cannot_generate_trade_candidate_without_technical_signal",
        "sources": _source_refs(docs, limit=12),
        "asset_paths": [path for doc in docs[:20] for path in doc.get("asset_paths", [])][:20],
        "as_of": today,
        "updated_at": now,
        "freshness": "fresh",
    })
    if daozhang_text or _has_any(combined, ("道长", "国证2000", "超大盘", "科创50")):
        views.append({
            "view_id": "strategy:daozhang:market_and_rotation",
            "target_type": "strategy_rule",
            "market": "A",
            "rule_scope": "daozhang",
            "market_stage": "先看市场阶段，再看板块；下探、平衡、反弹确认对应不同仓位动作。",
            "style_rotation": "沪指修复不能代表全市场，要同步观察国证2000、创业板、科创50、超大盘。",
            "mainline_status": "科技/小票弹性可以做，但要受仓位、确定性和分化阶段约束。",
            "position_policy": "高位强势兑现，低位确定性承接；反弹后分化优先确定性和仓位控制。",
            "participation_rule": "板块观点只做背景，个股必须回到硬技术和右侧确认。",
            "risk_rule": "题材热度不能替代结构和关键位，分化阶段降低追高优先级。",
            "right_side_requirement": "低位承接和弹性机会都要等确认，不能静态幻想一把拿到底。",
            "forbidden_chase": "超大盘修复、科技波动或小票弹性都不能直接推导买入。",
            "knowledge_effect": "context_only",
            "sources": _source_refs(docs, "daozhang", 10),
            "as_of": today,
            "updated_at": now,
            "freshness": "fresh",
        })
    if pangge_text or _has_any(combined, ("胖哥", "右侧", "恒科", "风报比")):
        views.append({
            "view_id": "strategy:pangge:intraday_execution",
            "target_type": "strategy_rule",
            "market": "A",
            "rule_scope": "pangge",
            "market_stage": "阶段判断先于个股判断，盘中策略是等条件，不是猜方向。",
            "style_rotation": "规则变化要翻译成资金行为和二级映射，再判断能不能参与。",
            "mainline_status": "热点或公开利好必须落到赔率、拥挤度和参与性，不能只看叙事。",
            "position_policy": "趋势弱时收缩仓位，尤其港股/恒科阴跌时不做左侧。",
            "participation_rule": "先确认参与条件，再谈方向；关键位、跌破与否、右侧确认和风报比一起判断。",
            "risk_rule": "趋势不站队时等待止跌、放量或右侧确认；左侧冲动默认降级。",
            "right_side_requirement": "必须等 5m/15m 右侧确认、止跌或放量，不用盘中猜方向。",
            "forbidden_chase": "公开利好、便宜估值或单点消息不能直接交易。",
            "knowledge_effect": "context_only",
            "sources": _source_refs(docs, "pangge", 10),
            "as_of": today,
            "updated_at": now,
            "freshness": "fresh",
        })
    return views


def sync_knowledge_market_views(db: Database, proxy_url: str = None) -> dict:
    """Load reviewed research-note metadata into a separate confirmation/conflict plane."""
    from signals.research import load_all_notes

    now = naive_market_now("A")
    notes_dir = _notes_dir()
    notes = load_all_notes(notes_dir)
    vault_dir = _vault_dir()
    vault_docs = _knowledge_docs(vault_dir)
    stock_notes: dict[str, list[Any]] = defaultdict(list)
    sector_notes: dict[str, list[Any]] = defaultdict(list)
    for note in notes:
        for stock in getattr(note, "stocks", []) or []:
            symbol = _prefixed_symbol(stock)
            if symbol:
                stock_notes[symbol].append(note)
        for sector in getattr(note, "sectors", []) or []:
            if sector:
                sector_notes[str(sector)].append(note)

    inserted = 0
    for symbol, rows in stock_notes.items():
        rows.sort(key=lambda item: _note_date(item), reverse=True)
        latest = rows[0]
        sentiments = [getattr(item, "sentiment", "中性") for item in rows]
        dominant = max(sentiments, key=_sentiment_rank) if sentiments else "中性"
        raw_code = _pure_a_code(symbol)
        doc = {
            "view_id": f"stock:{symbol}",
            "target_type": "stock",
            "symbol": symbol,
            "raw_code": raw_code,
            "market": "A",
            "sentiment": dominant,
            "latest_sentiment": getattr(latest, "sentiment", "中性"),
            "confidence": float(getattr(latest, "confidence", 0.5) or 0.5),
            "confirmation_role": "confirm_conflict_degrade_only",
            "candidate_policy": "cannot_generate_trade_candidate_without_technical_signal",
            "knowledge_effect": "confirm" if getattr(latest, "sentiment", "中性") in {"看多", "看空"} else "context_only",
            "sources": [
                {
                    "title": getattr(item, "title", ""),
                    "date": _note_date(item),
                    "source": getattr(item, "source", ""),
                    "author": getattr(item, "author", ""),
                    "sentiment": getattr(item, "sentiment", "中性"),
                    "confidence": float(getattr(item, "confidence", 0.5) or 0.5),
                    "meta_path": getattr(item, "meta_path", ""),
                }
                for item in rows[:8]
            ],
            "catalysts": list(getattr(latest, "catalysts", []) or [])[:8],
            "as_of": _note_date(latest) or now.date().isoformat(),
            "updated_at": now,
            "freshness": "fresh" if not getattr(latest, "is_expired", False) else "stale",
        }
        db["knowledge_market_views"].update_one({"view_id": doc["view_id"]}, {"$set": doc}, upsert=True)
        inserted += 1

    for sector, rows in sector_notes.items():
        rows.sort(key=lambda item: _note_date(item), reverse=True)
        latest = rows[0]
        doc = {
            "view_id": f"sector:{sector}",
            "target_type": "sector",
            "sector": sector,
            "market": "A",
            "sentiment": getattr(latest, "sentiment", "中性"),
            "confirmation_role": "context_only",
            "candidate_policy": "sector_view_requires_chain_or_technical_confirmation",
            "knowledge_effect": "context_only",
            "sources": [
                {
                    "title": getattr(item, "title", ""),
                    "date": _note_date(item),
                    "source": getattr(item, "source", ""),
                    "sentiment": getattr(item, "sentiment", "中性"),
                    "meta_path": getattr(item, "meta_path", ""),
                }
                for item in rows[:8]
            ],
            "catalysts": list(getattr(latest, "catalysts", []) or [])[:8],
            "as_of": _note_date(latest) or now.date().isoformat(),
            "updated_at": now,
            "freshness": "fresh" if not getattr(latest, "is_expired", False) else "stale",
        }
        db["knowledge_market_views"].update_one({"view_id": doc["view_id"]}, {"$set": doc}, upsert=True)
        inserted += 1

    strategy_views = _strategy_rule_views(vault_docs, now)
    for doc in strategy_views:
        effect = str(doc.get("knowledge_effect") or "context_only")
        if effect not in ALLOWED_EFFECTS:
            doc["knowledge_effect"] = "context_only"
        db["knowledge_market_views"].update_one({"view_id": doc["view_id"]}, {"$set": doc}, upsert=True)
        inserted += 1

    db["data_freshness"].update_one(
        {"domain": "knowledge", "market": "A", "mode": "postmarket", "collection": "knowledge_market_views"},
        {"$set": {
            "domain": "knowledge",
            "market": "A",
            "mode": "postmarket",
            "lane": "postmarket",
            "collection": "knowledge_market_views",
            "freshness": "fresh",
            "latest_dt": now.date().isoformat(),
            "as_of": now.date().isoformat(),
            "updated_at": now,
            "stale_reason": "" if notes else "no_research_notes",
            "count": inserted,
            "stock_views": len(stock_notes),
            "sector_views": len(sector_notes),
            "strategy_rule_views": len(strategy_views),
            "notes_dir": notes_dir,
            "vault_dir": str(vault_dir),
            "vault_docs": len(vault_docs),
            "vault_subdirs": list(VAULT_SUBDIRS),
        }},
        upsert=True,
    )
    logger.info(
        "knowledge market views: notes=%d stock_views=%d sector_views=%d vault_docs=%d strategy_views=%d",
        len(notes), len(stock_notes), len(sector_notes), len(vault_docs), len(strategy_views),
    )
    return {
        "status": "ok",
        "inserted": inserted,
        "notes": len(notes),
        "stocks": len(stock_notes),
        "sectors": len(sector_notes),
        "vault_docs": len(vault_docs),
        "strategy_rule_views": len(strategy_views),
    }
