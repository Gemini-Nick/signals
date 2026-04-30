# -*- coding: utf-8 -*-
"""Read-only minute cache readiness probe for the terminal."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.macro_universe import macro_a_index_symbols
from signals.core.market_time import naive_market_now

logger = logging.getLogger("signals.sync.minute_readiness")

MINUTE_FREQS = ["5分钟", "15分钟", "30分钟"]

_FREQ_ALIASES = {
    "5": "5分钟",
    "5m": "5分钟",
    "5min": "5分钟",
    "5分钟": "5分钟",
    "15": "15分钟",
    "15m": "15分钟",
    "15min": "15分钟",
    "15分钟": "15分钟",
    "30": "30分钟",
    "30m": "30分钟",
    "30min": "30分钟",
    "30分钟": "30分钟",
}


def _symbol_candidates(symbol: str) -> list[str]:
    raw = str(symbol or "").strip()
    if not raw:
        return []
    candidates = [raw, raw.upper(), raw.lower()]
    pure = raw.split(".", 1)[-1] if "." in raw else raw
    if pure.isdigit() and len(pure) == 6:
        market = "SH" if pure.startswith(("5", "6", "9")) else "SZ" if pure.startswith(("0", "1", "2", "3")) else "BJ"
        candidates.extend([pure, f"{market}.{pure}", f"{market.lower()}{pure}"])
    if raw.lower().startswith(("sh", "sz")) and len(raw) == 8:
        candidates.append(raw[2:])
    return list(dict.fromkeys(candidates))


def _latest_bar(db: Database, collection: str, symbol: str, freq: str) -> tuple[int, Any, str]:
    candidates = _symbol_candidates(symbol)
    if not candidates:
        return 0, None, ""
    query = {"meta.symbol": {"$in": candidates}, "meta.freq": freq}
    count = db[collection].count_documents(query)
    latest = db[collection].find_one(query, {"dt": 1, "meta.source": 1}, sort=[("dt", -1)]) or {}
    return int(count), latest.get("dt"), (latest.get("meta") or {}).get("source", "")


def _normalize_minute_freqs(values: Any) -> list[str]:
    if isinstance(values, str):
        raw_values = [part.strip() for part in values.split(",")]
    else:
        raw_values = list(values or [])
    freqs: list[str] = []
    for value in raw_values:
        key = str(value or "").strip()
        if not key:
            continue
        freq = _FREQ_ALIASES.get(key.lower(), key)
        if freq in MINUTE_FREQS and freq not in freqs:
            freqs.append(freq)
    return freqs or list(MINUTE_FREQS)


def _stock_selection_meta(db: Database) -> dict[str, Any]:
    return db["sync_log"].find_one(
        {"_id": "stock_minute:selection:_meta"},
        {
            "selected_symbols": 1,
            "priority_symbols": 1,
            "pinned_symbols": 1,
            "minute_freqs": 1,
            "result.minute_freqs": 1,
            "last_run": 1,
        },
    ) or {}


def _same_market_day(value: Any, now: datetime) -> bool:
    if isinstance(value, datetime):
        return value.date() == now.date()
    if isinstance(value, str):
        return value[:10] == now.date().isoformat()
    return False


def _stock_freqs(meta: dict[str, Any], now: datetime) -> list[str]:
    values = meta.get("minute_freqs") or (meta.get("result") or {}).get("minute_freqs")
    if not values:
        return list(MINUTE_FREQS)
    last_run = meta.get("last_run")
    if last_run and not _same_market_day(last_run, now):
        return list(MINUTE_FREQS)
    return _normalize_minute_freqs(values)


def _stock_symbols(db: Database, meta: dict[str, Any] | None = None) -> list[str]:
    meta = meta if meta is not None else _stock_selection_meta(db)
    symbols = list(meta.get("selected_symbols") or [])
    for symbol in meta.get("pinned_symbols") or []:
        if symbol not in symbols:
            symbols.append(symbol)
    for symbol in meta.get("priority_symbols") or []:
        if symbol not in symbols:
            symbols.append(symbol)
    terminal_pool = db["terminal_stock_pool"].find_one(
        {"pool": "terminal_stock_pool", "market": "A"},
        {"stocks": 1},
        sort=[("updated_at", -1)],
    ) or {}
    for item in terminal_pool.get("stocks") or []:
        symbol = item.get("raw_code") or item.get("symbol") if isinstance(item, dict) else item
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols[:120]


def _index_symbols() -> list[str]:
    return macro_a_index_symbols()


def _heat_names(db: Database, kind: str) -> list[str]:
    try:
        docs = list(db["board_heat_ticks"].find(
            {"kind": kind},
            {"name": 1, "rank_idx": 1},
        ).sort([("trade_minute", -1), ("rank_idx", 1)]).limit(80))
    except Exception:
        return []
    names: list[str] = []
    for doc in docs:
        name = str(doc.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names[:40]


def _probe_symbol_domain(
    db: Database,
    *,
    domain: str,
    collection: str,
    symbols: list[str],
    now,
    freqs: list[str] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    active_freqs = freqs or MINUTE_FREQS
    for symbol in symbols:
        for freq in active_freqs:
            count, latest_dt, source = _latest_bar(db, collection, symbol, freq)
            rows.append({
                "domain": domain,
                "symbol": symbol,
                "freq": freq,
                "count": count,
                "latest_dt": latest_dt,
                "source": source,
                "status": "ready" if count else "not_ready",
                "root_cause_class": "" if count else f"{domain}_minute_not_ready",
                "checked_at": now,
                "trade_date": now.date().isoformat(),
            })
    return rows


def _probe_heat_domain(db: Database, *, kind: str, names: list[str], now) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in names:
        count = db["board_heat_ticks"].count_documents({"kind": kind, "name": name})
        latest = db["board_heat_ticks"].find_one(
            {"kind": kind, "name": name},
            {"trade_minute": 1, "source": 1},
            sort=[("trade_minute", -1)],
        ) or {}
        for freq in MINUTE_FREQS:
            rows.append({
                "domain": kind,
                "symbol": name,
                "freq": freq,
                "count": int(count),
                "latest_dt": latest.get("trade_minute"),
                "source": latest.get("source", ""),
                "status": "ready" if count else "not_ready",
                "root_cause_class": "" if count else "board_heat_not_ready",
                "checked_at": now,
                "trade_date": now.date().isoformat(),
            })
    return rows


def sync_minute_readiness_probe(db: Database, proxy_url: str = None) -> dict:
    now = naive_market_now("A")
    rows = []
    stock_meta = _stock_selection_meta(db)
    stock_freqs = _stock_freqs(stock_meta, now)
    rows.extend(_probe_symbol_domain(
        db,
        domain="stock",
        collection="bars",
        symbols=_stock_symbols(db, stock_meta),
        now=now,
        freqs=stock_freqs,
    ))
    rows.extend(_probe_symbol_domain(db, domain="index", collection="index_bars", symbols=_index_symbols(), now=now))
    rows.extend(_probe_heat_domain(db, kind="industry", names=_heat_names(db, "industry"), now=now))
    rows.extend(_probe_heat_domain(db, kind="concept", names=_heat_names(db, "concept"), now=now))
    if not rows:
        return {"status": "degraded", "checked": 0, "not_ready": 0, "reason": "readiness_universe_empty"}

    trade_date = now.date().isoformat()
    db["minute_readiness"].delete_many({"trade_date": trade_date})
    ops = [
        UpdateOne(
            {
                "trade_date": row["trade_date"],
                "domain": row["domain"],
                "symbol": row["symbol"],
                "freq": row["freq"],
            },
            {"$set": row},
            upsert=True,
        )
        for row in rows
    ]
    db["minute_readiness"].bulk_write(ops, ordered=False)
    not_ready = sum(1 for row in rows if row["status"] != "ready")
    db["data_freshness"].update_one(
        {"domain": "readiness", "market": "A", "mode": "realtime", "collection": "minute_readiness"},
        {"$set": {
            "domain": "readiness",
            "market": "A",
            "mode": "realtime",
            "lane": "signal_lane",
            "collection": "minute_readiness",
            "freshness": "fresh" if not_ready == 0 else "partial",
            "latest_dt": now.isoformat(timespec="minutes"),
            "as_of": now.date().isoformat(),
            "updated_at": now,
            "stale_reason": "" if not_ready == 0 else "minute_cache_not_ready",
            "count": len(rows),
            "not_ready": not_ready,
        }},
        upsert=True,
    )
    logger.info("minute readiness: checked=%d not_ready=%d", len(rows), not_ready)
    return {
        "status": "ok" if not_ready == 0 else "partial",
        "checked": len(rows),
        "not_ready": not_ready,
        "inserted": len(rows),
    }
