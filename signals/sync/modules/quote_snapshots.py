# -*- coding: utf-8 -*-
"""Build short-lived quote snapshots for the active pool."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta

from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.sync.eastmoney_observer import observe_eastmoney
from signals.sync.provider_limits import ProviderCoolingDown, provider_call

logger = logging.getLogger("signals.sync.quote_snapshots")
_EM_ENDPOINT = "https://push2delay.eastmoney.com/api/qt/stock/get"
_EM_FIELDS = "f43,f44,f45,f46,f47,f48,f49,f50,f57,f58,f60,f116,f117,f168,f169,f170,f171"
_EM_ULIST_ENDPOINT = "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
_EM_ULIST_FIELDS = "f2,f3,f4,f5,f6,f7,f8,f12,f13,f14,f15,f16,f17,f18,f20,f21"
_MACRO_QUOTE_SYMBOLS = ("SH.000001", "SZ.399001", "SZ.399006", "SH.000300", "SH.000016")


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


def _write_provider_health(
    db: Database,
    ok: bool,
    latency_ms: float,
    error: str = "",
    *,
    endpoint: str = "push2delay.stock.get",
) -> None:
    now = naive_market_now("A")
    db["provider_health"].update_one(
        {"provider": "eastmoney", "endpoint": endpoint, "domain": "quote"},
        {"$set": {
            "provider": "eastmoney",
            "endpoint": endpoint,
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
    lane = os.getenv("SIGNALS_CURRENT_SYNC_LANE", "quote_lane")
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
            "lane": lane,
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


def _mark_non_current_quotes_stale(db: Database, trading_day: str, now: datetime) -> int:
    day_text = str(trading_day or "")[:10]
    if not day_text:
        return 0
    compact_day = day_text.replace("-", "")
    date_expr = {"$substr": [{"$toString": {"$ifNull": ["$dt", {"$ifNull": ["$trade_date", ""]}]}}, 0, 10]}
    try:
        result = db["quote_snapshots"].update_many(
            {
                "$expr": {
                    "$and": [
                        {"$ne": [date_expr, ""]},
                        {"$ne": [date_expr, day_text]},
                        {"$ne": [date_expr, compact_day]},
                    ],
                },
                "freshness": {"$ne": "stale"},
            },
            {"$set": {
                "freshness": "stale",
                "is_stale": True,
                "stale_reason": "non_current_quote_day",
                "stale_checked_at": now,
            }},
        )
        return int(getattr(result, "modified_count", 0) or 0)
    except Exception as exc:
        logger.debug("mark non-current quote snapshots stale failed: %s", exc)
        return 0


def _secid_for_symbol(symbol: str) -> str:
    raw = str(symbol or "").strip().upper()
    if "." in raw:
        market, code = raw.split(".", 1)
        market_id = "1" if market == "SH" else "0"
        return f"{market_id}.{code}"
    code = raw.replace("SH", "").replace("SZ", "").replace("BJ", "")
    market_id = "1" if code.startswith(("5", "6", "9")) else "0"
    return f"{market_id}.{code}"


def _normalize_quote_symbol(value: object) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        prefix, code = raw.split(".", 1)
        if prefix in {"SH", "SZ", "BJ"} and code:
            return f"{prefix}.{code}"
    if len(raw) >= 8 and raw[:2] in {"SH", "SZ", "BJ"}:
        return f"{raw[:2]}.{raw[2:]}"
    code = raw.replace("SH", "").replace("SZ", "").replace("BJ", "")
    if code.isdigit() and len(code) == 6:
        if code.startswith(("5", "6", "9")):
            return f"SH.{code}"
        if code.startswith(("4", "8")):
            return f"BJ.{code}"
        return f"SZ.{code}"
    return raw


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


def _quote_doc_from_ulist_row(symbol: str, row: dict, now: datetime, trading_day: str) -> dict | None:
    price = row.get("f2")
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    code = str(row.get("f12") or _code_for_symbol(symbol))
    return {
        "_id": f"{symbol}:latest",
        "symbol": symbol,
        "code": code,
        "name": str(row.get("f14") or ""),
        "dt": trading_day,
        "trade_date": trading_day,
        "snapshot_at": now,
        "source": "eastmoney_push2delay_ulist",
        "freshness": "fresh",
        "is_stale": False,
        "stale_reason": "",
        "open": row.get("f17"),
        "high": row.get("f15"),
        "low": row.get("f16"),
        "close": price,
        "price": price,
        "prev_close": row.get("f18"),
        "change": row.get("f4"),
        "change_pct": row.get("f3"),
        "turnover_pct": row.get("f8"),
        "amplitude_pct": row.get("f7"),
        "vol": int(float(row.get("f5") or 0) * 100),
        "amount": float(row.get("f6") or 0),
        "market_cap": row.get("f20"),
        "float_market_cap": row.get("f21"),
        "expires_at": now + timedelta(days=3),
    }


def _chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[idx:idx + size] for idx in range(0, len(values), size)]


def _hot_quote_symbols(db: Database) -> list[str]:
    symbols: list[str] = []

    def add(value: object) -> None:
        symbol = _normalize_quote_symbol(value)
        if symbol and symbol not in symbols:
            symbols.append(symbol)

    for symbol in _MACRO_QUOTE_SYMBOLS:
        add(symbol)

    try:
        doc = db["terminal_stock_pool"].find_one(
            {"pool": "terminal_stock_pool", "market": "A"},
            {"stocks": 1, "focus_stocks": 1, "risk_stocks": 1, "watch_stocks": 1},
            sort=[("updated_at", -1)],
        ) or {}
    except Exception:
        doc = {}
    for group in ("focus_stocks", "risk_stocks", "watch_stocks", "stocks"):
        for item in doc.get(group) or []:
            if not isinstance(item, dict):
                continue
            add(item.get("symbol") or item.get("code") or item.get("raw_code"))

    if len(symbols) < 20:
        for symbol in _latest_pool_symbols(db):
            add(symbol)

    try:
        limit = max(1, min(500, int(os.getenv("EASTMONEY_ULIST_MAX_SYMBOLS", "240"))))
    except (TypeError, ValueError):
        limit = 240
    return symbols[:limit]


def _fetch_eastmoney_ulist_docs(
    db: Database,
    symbols: list[str],
    now: datetime,
    trading_day: str,
) -> tuple[dict[str, dict], list[dict]]:
    if not symbols:
        return {}, []
    try:
        batch_size = max(1, min(100, int(os.getenv("EASTMONEY_ULIST_BATCH_SIZE", "80"))))
    except (TypeError, ValueError):
        batch_size = 80
    try:
        timeout = max(1.0, float(os.getenv("EASTMONEY_ULIST_TIMEOUT", "5")))
    except (TypeError, ValueError):
        timeout = 5.0

    docs: dict[str, dict] = {}
    observations: list[dict] = []
    for batch in _chunked(symbols, batch_size):
        secid_to_symbol = {_secid_for_symbol(symbol): symbol for symbol in batch}
        secids = ",".join(secid_to_symbol)
        started = time.monotonic()
        http_status = None
        rc = None
        error = ""
        returned_count = 0
        try:
            import requests

            def request_quote():
                session = requests.Session()
                session.trust_env = False
                try:
                    response = session.get(
                        _EM_ULIST_ENDPOINT,
                        params={
                            "fltt": "2",
                            "invt": "2",
                            "fields": _EM_ULIST_FIELDS,
                            "secids": secids,
                        },
                        timeout=timeout,
                        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
                    )
                    response.raise_for_status()
                    return response
                finally:
                    session.close()

            response = provider_call(
                "eastmoney",
                "push2delay.ulist.quote",
                request_quote,
                db=db,
                domain="quote",
            )
            http_status = response.status_code
            payload = response.json()
            rc = payload.get("rc")
            if rc not in (0, None):
                raise ValueError(f"rc={rc}")
            rows = (payload.get("data") or {}).get("diff") or []
            returned_count = len(rows)
            for row in rows:
                secid = f"{row.get('f13')}.{row.get('f12')}"
                symbol = secid_to_symbol.get(secid)
                if not symbol:
                    continue
                doc = _quote_doc_from_ulist_row(symbol, row, now, trading_day)
                if doc:
                    docs[symbol] = doc
        except ProviderCoolingDown as exc:
            error = f"provider_cooling_down:{exc}"
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
        elapsed_ms = (time.monotonic() - started) * 1000
        observations.append(observe_eastmoney(
            db,
            endpoint="push2delay.ulist.quote",
            domain="quote",
            request_count=1,
            requested_symbols=len(batch),
            returned_count=returned_count,
            elapsed_ms=elapsed_ms,
            http_status=http_status,
            rc=rc,
            error=error,
            batch_size=batch_size,
        ))
    return docs, observations


def _read_fullmarket_spot_quotes(
    db: Database,
    symbols: list[str],
    trading_day: str,
    now: datetime,
    *,
    allow_latest_fallback: bool = True,
) -> dict[str, dict]:
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
        if not rows and allow_latest_fallback:
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


def _read_fullmarket_no_price_symbols(db: Database, symbols: list[str], trading_day: str) -> set[str]:
    if not symbols:
        return set()
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
                normalized = candidate.upper()
                by_symbol[normalized] = symbol
                normalized_symbols.add(normalized)

    try:
        rows = db["fullmarket_spot_snapshots"].find(
            {
                "date_key": date_key,
                "$or": [
                    {"code": {"$in": list(codes)}},
                    {"symbol": {"$in": list(normalized_symbols)}},
                ],
            },
            {"_id": 0, "code": 1, "symbol": 1, "latest": 1, "price": 1},
        )
    except Exception as exc:
        logger.debug("读取 fullmarket_spot_snapshots 空报价状态失败: %s", exc)
        return set()

    no_price: set[str] = set()
    for row in rows:
        candidates = []
        code = str(row.get("code") or "")
        if code and code in by_symbol:
            candidates.append(by_symbol[code])
        normalized = str(row.get("symbol") or "").upper()
        if normalized in by_symbol:
            candidates.append(by_symbol[normalized])
        price = row.get("price", row.get("latest"))
        try:
            has_price = price not in (None, "", "-") and float(price) > 0
        except (TypeError, ValueError):
            has_price = False
        if has_price:
            continue
        no_price.update(symbol for symbol in candidates if symbol)
    return no_price


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
    spot_docs = _read_fullmarket_spot_quotes(db, symbols, trading_day, now, allow_latest_fallback=False)
    request_symbols = [symbol for symbol in symbols if symbol not in spot_docs]
    no_current_price_symbols = _read_fullmarket_no_price_symbols(db, request_symbols, trading_day)

    ops = []
    for symbol in symbols:
        doc = spot_docs.get(symbol)
        if doc:
            live_count += 1
            latest_dt = max(latest_dt or trading_day, trading_day)
            ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True))
            processed += 1
            continue

        stale_count += 1
        if symbol in no_current_price_symbols:
            ops.append(UpdateOne(
                {"_id": f"{symbol}:latest"},
                {"$set": {
                    "freshness": "stale",
                    "is_stale": True,
                    "stale_reason": "fullmarket_no_current_price",
                    "stale_checked_at": now,
                }},
                upsert=False,
            ))
            continue
        errors.append((symbol, "eastmoney_current_quote_missing"))
        ops.append(UpdateOne(
            {"_id": f"{symbol}:latest"},
            {"$set": {
                "freshness": "stale",
                "is_stale": True,
                "stale_reason": "eastmoney_current_quote_missing",
                "stale_checked_at": now,
            }},
            upsert=False,
        ))

    if ops:
        result = db["quote_snapshots"].bulk_write(ops, ordered=False)
        inserted = int(result.upserted_count)
        modified = int(result.modified_count)

    if spot_docs:
        _write_provider_health(db, True, 0, endpoint="fullmarket_spot_snapshot")
    elif not symbols:
        _write_provider_health(db, False, 0, "quote_symbol_universe_empty", endpoint="fullmarket_spot_snapshot")
    stale_marked = _mark_non_current_quotes_stale(db, trading_day, now)
    _write_data_freshness(db, len(symbols), latest_dt, live_count, stale_count)
    logger.info("quote snapshots: %d inserted, %d modified, %d live, %d stale", inserted, modified, live_count, stale_count)
    return {
        "inserted": inserted,
        "modified": modified,
        "count": len(symbols),
        "live": live_count,
        "stale": stale_count,
        "spot_snapshot": len(spot_docs),
        "requested": 0,
        "missing_current": len(request_symbols),
        "no_current_price": len(no_current_price_symbols),
        "errors": len(errors),
        "stale_marked": stale_marked,
        "target_collection": "quote_snapshots",
    }


def sync_eastmoney_ulist_quote(db: Database, proxy_url: str = None) -> dict:
    """Refresh hot quote symbols through Eastmoney's multi-code batch endpoint."""
    del proxy_url
    now = naive_market_now("A")
    try:
        from signals.data.mongo_fallback import get_last_trading_day

        trading_day = get_last_trading_day()
    except Exception:
        trading_day = now.date().isoformat()
    symbols = _hot_quote_symbols(db)
    docs, observations = _fetch_eastmoney_ulist_docs(db, symbols, now, trading_day)

    inserted = 0
    modified = 0
    if docs:
        ops = [UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True) for doc in docs.values()]
        result = db["quote_snapshots"].bulk_write(ops, ordered=False)
        inserted = int(result.upserted_count)
        modified = int(result.modified_count)
    stale_marked = _mark_non_current_quotes_stale(db, trading_day, now)

    errors = [item for item in observations if item.get("error")]
    avg_latency = (
        sum(float(item.get("elapsed_ms") or 0) for item in observations) / len(observations)
        if observations else 0
    )
    _write_provider_health(
        db,
        bool(docs),
        avg_latency,
        errors[0].get("error", "eastmoney_ulist_empty") if errors else "",
        endpoint="push2delay.ulist.quote",
    )
    missing = max(0, len(symbols) - len(docs))
    _write_data_freshness(db, len(symbols), trading_day if docs else None, len(docs), missing)
    status = "ok" if docs else "degraded"
    return {
        "module": "eastmoney_ulist_quote",
        "status": status,
        "inserted": inserted,
        "modified": modified,
        "count": len(symbols),
        "live": len(docs),
        "stale": missing,
        "batches": len(observations),
        "errors": len(errors),
        "stale_marked": stale_marked,
        "target_collection": "quote_snapshots",
    }
