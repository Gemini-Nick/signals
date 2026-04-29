# -*- coding: utf-8 -*-
"""Full-market A-share 30 minute cache preheat for hard-technical scans."""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime
from typing import Any

from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.sync.task_context import get_task_env

from .stock_minute import _index_codes, _insert_new_minute_docs, _pure_a_code, _sync_one_minute

logger = logging.getLogger("signals.sync.stock_30m_fullmarket")


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(get_task_env(name, os.getenv(name, str(default))) or default)
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    fallback = "true" if default else "false"
    return str(get_task_env(name, os.getenv(name, fallback)) or fallback).strip().lower() in {"1", "true", "yes", "on"}


def _symbols_with_daily(db: Database) -> list[str]:
    index_codes = _index_codes()
    raw_symbols = db["bars"].distinct("meta.symbol", {"meta.freq": {"$in": ["日线", "daily", "D", "1d"]}})
    return sorted({
        code
        for symbol in raw_symbols
        for code in [_pure_a_code(symbol)]
        if code and code not in index_codes
    })


def _shard_symbols(symbols: list[str], shard_index: int, shard_count: int) -> list[str]:
    return [symbol for idx, symbol in enumerate(symbols) if idx % shard_count == shard_index]


def _latest_30m_state(db: Database, symbols: list[str]) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    pipeline = [
        {"$match": {"meta.freq": "30分钟", "meta.symbol": {"$in": symbols}}},
        {"$group": {"_id": "$meta.symbol", "bar_count": {"$sum": 1}, "latest_dt": {"$max": "$dt"}}},
    ]
    state: dict[str, dict[str, Any]] = {}
    for row in db["bars"].aggregate(pipeline):
        code = _pure_a_code(row.get("_id"))
        if code:
            state[code] = {"bar_count": int(row.get("bar_count") or 0), "latest_dt": row.get("latest_dt")}
    return state


def _needs_refresh(state: dict[str, Any], *, min_bars: int, trade_date: str, require_today: bool) -> bool:
    if int(state.get("bar_count") or 0) < min_bars:
        return True
    if not require_today:
        return False
    latest = state.get("latest_dt")
    latest_date = latest.date().isoformat() if hasattr(latest, "date") else str(latest or "")[:10]
    return latest_date < trade_date


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "")[:10]
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _tail_count_for_state(
    state: dict[str, Any],
    *,
    min_bars: int,
    default_tail_count: int,
    trade_date: str,
) -> int:
    """Pull a full 30m window only for underfilled symbols; otherwise pull a small gap window."""
    if int(state.get("bar_count") or 0) < min_bars:
        return default_tail_count
    latest_date = _coerce_date(state.get("latest_dt"))
    current_date = _coerce_date(trade_date)
    if latest_date is None or current_date is None or latest_date >= current_date:
        return min(default_tail_count, _env_int("STOCK_30M_FULLMARKET_REFRESH_TAIL_COUNT", 40, minimum=8, maximum=160))
    bars_per_day = _env_int("STOCK_30M_FULLMARKET_BARS_PER_DAY", 8, minimum=4, maximum=16)
    overlap = _env_int("STOCK_30M_FULLMARKET_TAIL_OVERLAP", 16, minimum=8, maximum=160)
    missing_days = max(1, (current_date - latest_date).days)
    desired = missing_days * bars_per_day + overlap
    return min(default_tail_count, max(40, desired))


def sync_stock_30m_fullmarket(db: Database, proxy_url: str = None) -> dict:
    """Preheat 30 minute bars for one resumable full-market shard."""
    now = naive_market_now("A")
    trade_date = now.date().isoformat()
    shard_count = _env_int("STOCK_30M_FULLMARKET_SHARD_COUNT", 16, minimum=1, maximum=64)
    shard_index = _env_int("STOCK_30M_FULLMARKET_SHARD_INDEX", 0, minimum=0, maximum=shard_count - 1)
    shard_key = str(get_task_env("STOCK_30M_FULLMARKET_SHARD_KEY", f"shard_{shard_index:02d}") or f"shard_{shard_index:02d}")
    min_bars = _env_int("STOCK_30M_FULLMARKET_MIN_BARS", 260, minimum=20, maximum=1000)
    max_codes = _env_int("STOCK_30M_FULLMARKET_MAX_CODES_PER_RUN", 320, minimum=1, maximum=1000)
    tail_count = _env_int("STOCK_30M_FULLMARKET_TAIL_COUNT", max(min_bars, 320), minimum=min_bars, maximum=1200)
    require_today = _env_bool("STOCK_30M_FULLMARKET_REQUIRE_TODAY", True)
    call_interval = float(os.getenv("STOCK_30M_FULLMARKET_CALL_INTERVAL", os.getenv("STOCK_MINUTE_CALL_INTERVAL", "0.5")))

    universe = _symbols_with_daily(db)
    shard_symbols = _shard_symbols(universe, shard_index, shard_count)
    state = _latest_30m_state(db, shard_symbols)
    due = [
        symbol
        for symbol in shard_symbols
        if _needs_refresh(state.get(symbol, {}), min_bars=min_bars, trade_date=trade_date, require_today=require_today)
    ]
    selected = due[:max_codes]
    written = 0
    skipped_existing = 0
    empty = 0
    tail_count_total = 0
    errors: list[dict[str, str]] = []
    for symbol in selected:
        try:
            symbol_tail_count = _tail_count_for_state(
                state.get(symbol, {}),
                min_bars=min_bars,
                default_tail_count=tail_count,
                trade_date=trade_date,
            )
            tail_count_total += symbol_tail_count
            docs = _sync_one_minute(symbol, "30分钟", proxy_url, tail_count=symbol_tail_count, db=db)
            if not docs:
                empty += 1
            else:
                result = _insert_new_minute_docs(db["bars"], symbol, "30分钟", docs)
                written += int(result.get("written") or 0)
                skipped_existing += int(result.get("skipped_existing") or 0)
            time.sleep(call_interval)
        except Exception as exc:
            errors.append({"symbol": symbol, "error": f"{exc.__class__.__name__}: {str(exc)[:160]}"})

    remaining = max(0, len(due) - len(selected))
    status = "partial" if remaining or errors else "ok"
    db["data_freshness"].update_one(
        {"domain": "kline", "market": "A", "mode": "postmarket", "collection": "bars", "freq": "30分钟", "shard_key": shard_key},
        {"$set": {
            "domain": "kline",
            "market": "A",
            "mode": "postmarket",
            "lane": "postmarket",
            "collection": "bars",
            "freq": "30分钟",
            "shard_key": shard_key,
            "freshness": "fresh" if status == "ok" else "partial",
            "latest_dt": trade_date,
            "as_of": trade_date,
            "updated_at": now,
            "count": len(shard_symbols),
            "selected": len(selected),
            "remaining": remaining,
            "min_bars": min_bars,
            "tail_count_default": tail_count,
            "tail_count_avg": round(tail_count_total / len(selected), 2) if selected else 0,
            "require_today": require_today,
            "stale_reason": "" if status == "ok" else "stock_30m_fullmarket_incomplete",
        }},
        upsert=True,
    )
    logger.info(
        "stock 30m fullmarket shard=%s selected=%d remaining=%d written=%d errors=%d",
        shard_key,
        len(selected),
        remaining,
        written,
        len(errors),
    )
    return {
        "module": "stock_30m_fullmarket",
        "status": status,
        "shard_key": shard_key,
        "processed": len(selected),
        "selected": len(selected),
        "total": len(shard_symbols),
        "due": len(due),
        "remaining": remaining,
        "written": written,
        "skipped_existing": skipped_existing,
        "empty": empty,
        "tail_count_default": tail_count,
        "tail_count_avg": round(tail_count_total / len(selected), 2) if selected else 0,
        "errors": len(errors),
        "error_samples": errors[:5],
        "progress_pct": round(((len(due) - remaining) / len(due) * 100), 2) if due else 100.0,
        "coverage_pct": round(((len(shard_symbols) - remaining) / len(shard_symbols) * 100), 2) if shard_symbols else 100.0,
    }
