# -*- coding: utf-8 -*-
"""Publish research-note market views without turning them into trade signals."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from pymongo.database import Database

from signals.core.market_time import naive_market_now

logger = logging.getLogger("signals.sync.knowledge_market_views")


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


def sync_knowledge_market_views(db: Database, proxy_url: str = None) -> dict:
    """Load reviewed research-note metadata into a separate confirmation/conflict plane."""
    from signals.research import load_all_notes

    now = naive_market_now("A")
    notes_dir = _notes_dir()
    notes = load_all_notes(notes_dir)
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
            "notes_dir": notes_dir,
        }},
        upsert=True,
    )
    logger.info("knowledge market views: notes=%d stock_views=%d sector_views=%d", len(notes), len(stock_notes), len(sector_notes))
    return {
        "status": "ok",
        "inserted": inserted,
        "notes": len(notes),
        "stocks": len(stock_notes),
        "sectors": len(sector_notes),
    }

