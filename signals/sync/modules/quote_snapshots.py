# -*- coding: utf-8 -*-
"""Build short-lived quote snapshots for the active pool."""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.sync.provider_limits import ProviderCoolingDown, provider_call

logger = logging.getLogger("signals.sync.quote_snapshots")
_EM_ENDPOINT = "https://push2delay.eastmoney.com/api/qt/stock/get"
_EM_FIELDS = "f43,f44,f45,f46,f47,f48,f49,f50,f57,f58,f60,f116,f117,f168,f169,f170,f171"


def _symbol_candidates(symbol: str) -> list[str]:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return []
    pure = raw.split(".", 1)[-1] if "." in raw else raw
    candidates = [raw, pure]
    if "." in raw and raw.split(".", 1)[0] in {"SH", "SZ", "BJ"}:
        prefix = raw.split(".", 1)[0]
        candidates.append(f"{prefix.lower()}{pure}")
    if len(raw) >= 8 and raw[:2] in {"SH", "SZ", "BJ"}:
        candidates.append(f"{raw[:2]}.{raw[2:]}")
    if pure.isdigit() and len(pure) == 6:
        candidates.extend([
            f"SH.{pure}" if pure.startswith(("5", "6", "9")) else f"SZ.{pure}",
            f"sh{pure}" if pure.startswith(("5", "6", "9")) else f"sz{pure}",
        ])
    return list(dict.fromkeys(candidates))


def _latest_bars(db: Database, symbol: str, limit: int = 2) -> list[dict]:
    docs = list(db["bars"].find(
        {
            "meta.symbol": {"$in": _symbol_candidates(symbol)},
            "meta.freq": {"$in": ["daily", "日线", "D", "1d"]},
        },
        {"_id": 0},
    ).sort("dt", -1).limit(limit))
    return docs


def _iter_strategy_snapshot_symbols() -> list[str]:
    symbols: list[str] = []
    try:
        from signals.strategy.snapshot import get_strategy_snapshot

        snapshot = get_strategy_snapshot()
    except Exception:
        return symbols

    for key in ("candidates", "warnings", "decision_queue", "buy_candidates", "sell_warnings"):
        rows = snapshot.get(key) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for field in ("symbol", "code", "raw_code"):
                value = row.get(field)
                if value:
                    symbols.append(str(value))
            metadata = row.get("metadata")
            if isinstance(metadata, dict):
                for field in ("symbol", "code", "raw_code"):
                    value = metadata.get(field)
                    if value:
                        symbols.append(str(value))
    return symbols


def _latest_pool_symbols(db: Database) -> list[str]:
    symbols = []

    def add(value: object) -> None:
        symbol = str(value or "").strip()
        if symbol and symbol not in symbols:
            symbols.append(symbol)

    doc = db["market_pools"].find_one({"pool": "active"}, {"symbols": 1}, sort=[("dt", -1), ("updated_at", -1)])
    if doc and doc.get("symbols"):
        for item in doc["symbols"]:
            add(item)

    for item in _iter_strategy_snapshot_symbols():
        add(item)

    for item in db["signals"].find({}, {"symbol": 1}).sort("signal_date", -1).limit(50):
        add(item.get("symbol"))
    if symbols:
        return symbols
    for item in db["bars"].aggregate([
        {"$sort": {"dt": -1}},
        {"$group": {"_id": "$meta.symbol", "latest_dt": {"$first": "$dt"}}},
        {"$limit": 50},
    ]):
        add(item.get("_id"))
    return symbols


def _write_provider_health(db: Database, ok: bool, latency_ms: float, error: str = "") -> None:
    now = naive_market_now("A")
    db["provider_health"].update_one(
        {"provider": "eastmoney", "endpoint": "push2delay.stock.get", "domain": "quote"},
        {"$set": {
            "provider": "eastmoney",
            "endpoint": "push2delay.stock.get",
            "domain": "quote",
            "last_success_at": now if ok else None,
            "last_error_at": None if ok else now,
            "last_error_type": "" if ok else error[:200],
            "avg_latency_ms": round(latency_ms, 1),
            "status": "ok" if ok else "degraded",
            "updated_at": now,
        }},
        upsert=True,
    )


def _write_data_freshness(db: Database, count: int, latest_dt: str | None, live_count: int, stale_count: int) -> None:
    now = naive_market_now("A")
    if count <= 0:
        freshness = "empty"
        stale_reason = "quote_snapshot_empty"
    elif live_count == count:
        freshness = "fresh"
        stale_reason = ""
    elif live_count > 0:
        freshness = "partial"
        stale_reason = f"live_partial_stale={stale_count}"
    else:
        freshness = "stale"
        stale_reason = "bars_latest_snapshot"
    db["data_freshness"].update_one(
        {"domain": "quote", "market": "A", "mode": "realtime", "collection": "quote_snapshots"},
        {"$set": {
            "domain": "quote",
            "market": "A",
            "mode": "realtime",
            "collection": "quote_snapshots",
            "freshness": freshness,
            "latest_dt": latest_dt,
            "as_of": latest_dt,
            "updated_at": now,
            "stale_reason": stale_reason,
            "count": count,
            "live_count": live_count,
            "stale_count": stale_count,
        }},
        upsert=True,
    )


def _secid_for_symbol(symbol: str) -> str:
    raw = str(symbol or "").strip().upper()
    if "." in raw:
        market, code = raw.split(".", 1)
        market_id = "1" if market == "SH" else "0"
        return f"{market_id}.{code}"
    code = raw.replace("SH", "").replace("SZ", "").replace("BJ", "")
    market_id = "1" if code.startswith(("5", "6", "9")) else "0"
    return f"{market_id}.{code}"


def _scale_price(value: object) -> float | None:
    try:
        if value in (None, "-", ""):
            return None
        return round(float(value) / 100.0, 4)
    except Exception:
        return None


def _scale_pct(value: object) -> float | None:
    try:
        if value in (None, "-", ""):
            return None
        return round(float(value) / 100.0, 4)
    except Exception:
        return None


def _quote_doc_from_em(symbol: str, payload: dict, now: datetime, trading_day: str) -> dict | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not data:
        return None
    price = _scale_price(data.get("f43"))
    if price is None or price <= 0:
        return None
    prev_close = _scale_price(data.get("f60"))
    change_pct = _scale_pct(data.get("f170"))
    return {
        "_id": f"{symbol}:latest",
        "symbol": symbol,
        "code": data.get("f57") or (symbol.split(".", 1)[-1] if "." in symbol else symbol),
        "name": data.get("f58") or "",
        "dt": trading_day,
        "snapshot_at": now,
        "source": "eastmoney_push2delay",
        "freshness": "fresh",
        "is_stale": False,
        "stale_reason": "",
        "open": _scale_price(data.get("f46")),
        "high": _scale_price(data.get("f44")),
        "low": _scale_price(data.get("f45")),
        "close": price,
        "price": price,
        "prev_close": prev_close,
        "change": _scale_price(data.get("f169")),
        "change_pct": change_pct,
        "turnover_pct": _scale_pct(data.get("f168")),
        "amplitude_pct": _scale_pct(data.get("f171")),
        "vol": int(float(data.get("f47") or 0) * 100),
        "amount": float(data.get("f48") or 0),
        "market_cap": float(data.get("f116") or 0),
        "float_market_cap": float(data.get("f117") or 0),
        "expires_at": now + timedelta(days=3),
    }


def _code_for_symbol(symbol: str) -> str:
    raw = str(symbol or "").strip().upper()
    if "." in raw:
        return raw.split(".", 1)[-1]
    if len(raw) >= 8 and raw[:2] in {"SH", "SZ", "BJ"}:
        return raw[2:]
    return raw.replace("SH", "").replace("SZ", "").replace("BJ", "")


def _quote_doc_from_fullmarket_spot(symbol: str, row: dict, now: datetime, trading_day: str) -> dict | None:
    price = row.get("price", row.get("latest"))
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    code = str(row.get("code") or _code_for_symbol(symbol))
    return {
        "_id": f"{symbol}:latest",
        "symbol": symbol,
        "code": code,
        "name": row.get("name") or "",
        "dt": trading_day,
        "snapshot_at": now,
        "source": "fullmarket_spot_snapshot",
        "freshness": "fresh",
        "is_stale": False,
        "stale_reason": "",
        "open": row.get("open"),
        "high": row.get("high"),
        "low": row.get("low"),
        "close": price,
        "price": price,
        "prev_close": row.get("prev_close"),
        "change": row.get("change"),
        "change_pct": row.get("change_pct"),
        "turnover_pct": row.get("turnover_pct"),
        "amplitude_pct": row.get("amplitude_pct"),
        "vol": int(float(row.get("vol") or 0) * 100),
        "amount": float(row.get("amount") or 0),
        "market_cap": float(row.get("market_cap") or 0),
        "float_market_cap": float(row.get("float_market_cap") or 0),
        "expires_at": now + timedelta(days=3),
    }


def _read_fullmarket_spot_quotes(db: Database, symbols: list[str], trading_day: str, now: datetime) -> dict[str, dict]:
    if not symbols:
        return {}
    date_key = str(trading_day or "").replace("-", "")[:8]
    by_symbol: dict[str, str] = {}
    codes: set[str] = set()
    normalized_symbols: set[str] = set()
    for symbol in symbols:
        code = _code_for_symbol(symbol)
        if code:
            by_symbol[code] = symbol
            codes.add(code)
        for candidate in _symbol_candidates(symbol):
            if "." in candidate:
                normalized_symbols.add(candidate.upper())

    def read_rows(date_key_value: str) -> list[dict]:
        return list(db["fullmarket_spot_snapshots"].find(
            {
                "date_key": date_key_value,
                "$or": [
                    {"code": {"$in": list(codes)}},
                    {"symbol": {"$in": list(normalized_symbols)}},
                ],
            },
            {
                "_id": 0,
                "code": 1,
                "symbol": 1,
                "trade_date": 1,
                "name": 1,
                "latest": 1,
                "price": 1,
                "change": 1,
                "change_pct": 1,
                "turnover_pct": 1,
                "amplitude_pct": 1,
                "vol": 1,
                "amount": 1,
                "open": 1,
                "high": 1,
                "low": 1,
                "prev_close": 1,
                "market_cap": 1,
                "float_market_cap": 1,
            },
        ))

    try:
        rows = read_rows(date_key)
        if not rows:
            latest = db["fullmarket_spot_snapshots"].find_one(
                {},
                {"date_key": 1},
                sort=[("snapshot_at", -1)],
            )
            latest_date_key = str((latest or {}).get("date_key") or "")
            if latest_date_key and latest_date_key != date_key:
                rows = read_rows(latest_date_key)
    except Exception as exc:
        logger.debug("读取 fullmarket_spot_snapshots 失败，改用逐股 quote: %s", exc)
        return {}

    docs: dict[str, dict] = {}
    symbol_set = set(symbols)
    for row in rows:
        code = str(row.get("code") or "")
        candidates = []
        if code and code in by_symbol:
            candidates.append(by_symbol[code])
        normalized = str(row.get("symbol") or "").upper()
        if normalized in symbol_set:
            candidates.append(normalized)
        for symbol in candidates:
            if symbol in docs:
                continue
            doc = _quote_doc_from_fullmarket_spot(symbol, row, now, str(row.get("trade_date") or trading_day))
            if doc:
                docs[symbol] = doc
    return docs


def _fetch_em_quote(db: Database, symbol: str, timeout: float = 5.0) -> tuple[dict | None, float, str]:
    start = time.monotonic()
    try:
        import requests

        def request_quote():
            session = requests.Session()
            session.trust_env = False
            try:
                return session.get(
                    _EM_ENDPOINT,
                    params={"secid": _secid_for_symbol(symbol), "fields": _EM_FIELDS},
                    timeout=timeout,
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
                )
            finally:
                session.close()

        response = provider_call(
            "eastmoney",
            "push2delay.stock.get",
            request_quote,
            db=db,
            domain="quote",
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("rc") not in (0, None):
            raise ValueError(f"rc={payload.get('rc')}")
        return payload, (time.monotonic() - start) * 1000, ""
    except ProviderCoolingDown as exc:
        return None, (time.monotonic() - start) * 1000, f"provider_cooling_down:{exc}"
    except Exception as exc:
        return None, (time.monotonic() - start) * 1000, str(exc)


def _fallback_doc_from_bars(db: Database, symbol: str, now: datetime) -> tuple[dict | None, str | None]:
    bars = _latest_bars(db, symbol)
    if not bars:
        return None, None
    latest = bars[0]
    prev = bars[1] if len(bars) > 1 else None
    close = latest.get("close")
    prev_close = prev.get("close") if prev else None
    change_pct = None
    try:
        if close is not None and prev_close:
            change_pct = (float(close) - float(prev_close)) / float(prev_close) * 100
    except Exception:
        change_pct = None
    dt = latest.get("dt")
    dt_str = str(dt.date()) if hasattr(dt, "date") else str(dt)[:10]
    doc = {
        "_id": f"{symbol}:latest",
        "symbol": symbol,
        "code": symbol.split(".", 1)[-1] if "." in symbol else symbol,
        "dt": dt,
        "snapshot_at": now,
        "source": "bars_latest",
        "freshness": "stale",
        "is_stale": True,
        "stale_reason": "bars_latest_snapshot",
        "open": latest.get("open"),
        "high": latest.get("high"),
        "low": latest.get("low"),
        "close": close,
        "price": close,
        "prev_close": prev_close,
        "change_pct": change_pct,
        "vol": latest.get("vol"),
        "amount": latest.get("amount"),
        "expires_at": now + timedelta(days=3),
    }
    return doc, dt_str


def sync_quote_snapshots(db: Database, proxy_url: str = None) -> dict:
    """Write quote snapshots for active-pool symbols.

    Runtime callers only read this collection. Eastmoney realtime requests are
    isolated here inside sync/backfill, with local bars as stale fallback.
    """
    del proxy_url
    now = naive_market_now("A")
    try:
        from signals.data.mongo_fallback import get_last_trading_day

        trading_day = get_last_trading_day()
    except Exception:
        trading_day = now.date().isoformat()
    symbols = _latest_pool_symbols(db)
    inserted = 0
    modified = 0
    processed = 0
    live_count = 0
    stale_count = 0
    errors = []
    latest_dt = None
    timeout = float(os.getenv("QUOTE_PROVIDER_TIMEOUT", "5"))
    max_workers = int(os.getenv("QUOTE_MAX_WORKERS", "2"))

    spot_docs = _read_fullmarket_spot_quotes(db, symbols, trading_day, now)
    request_symbols = [symbol for symbol in symbols if symbol not in spot_docs]
    quote_results = {}
    latencies = []
    if request_symbols:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_fetch_em_quote, db, symbol, timeout): symbol for symbol in request_symbols}
            for future in as_completed(futures):
                symbol = futures[future]
                payload, latency_ms, error = future.result()
                latencies.append(latency_ms)
                quote_results[symbol] = (payload, error)

    ops = []
    for symbol in symbols:
        doc = spot_docs.get(symbol)
        if doc:
            live_count += 1
            latest_dt = max(latest_dt or trading_day, trading_day)
            ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True))
            processed += 1
            continue

        payload, error = quote_results.get(symbol, (None, "not_requested"))
        doc = _quote_doc_from_em(symbol, payload or {}, now, trading_day)
        if doc:
            live_count += 1
            latest_dt = max(latest_dt or trading_day, trading_day)
        else:
            if error:
                errors.append((symbol, error[:160]))
            doc, dt_str = _fallback_doc_from_bars(db, symbol, now)
            if not doc:
                continue
            stale_count += 1
            latest_dt = max(latest_dt or dt_str, dt_str)

        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True))
        processed += 1

    if ops:
        result = db["quote_snapshots"].bulk_write(ops, ordered=False)
        inserted = int(result.upserted_count)
        modified = int(result.modified_count)

    avg_latency = sum(latencies) / len(latencies) if latencies else 0
    if not quote_results:
        _write_provider_health(db, False, avg_latency, "quote_snapshot_empty")
    _write_data_freshness(db, processed, latest_dt, live_count, stale_count)
    logger.info("quote snapshots: %d inserted, %d modified, %d live, %d stale", inserted, modified, live_count, stale_count)
    return {
        "inserted": inserted,
        "modified": modified,
        "count": processed,
        "live": live_count,
        "stale": stale_count,
        "spot_snapshot": len(spot_docs),
        "requested": len(request_symbols),
        "errors": len(errors),
        "target_collection": "quote_snapshots",
    }
