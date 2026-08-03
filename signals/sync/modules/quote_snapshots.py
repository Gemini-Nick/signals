# -*- coding: utf-8 -*-
"""Build short-lived quote snapshots for the active pool."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.macro_universe import macro_watchlist
from signals.core.market_time import naive_market_now
from signals.core.trading_dates import trading_day_key
from signals.sync.trade_date import a_share_task_trade_date
from signals.sync.eastmoney_observer import observe_eastmoney
from signals.sync.provider_limits import ProviderCoolingDown, provider_call
from signals.sync.task_context import get_task_env
from signals.sync.volume_units import CANONICAL_STOCK_VOLUME_UNIT, normalize_stock_volume

logger = logging.getLogger("signals.sync.quote_snapshots")
_EM_ENDPOINT = "https://push2delay.eastmoney.com/api/qt/stock/get"
_EM_FIELDS = "f43,f44,f45,f46,f47,f48,f49,f50,f57,f58,f60,f116,f117,f168,f169,f170,f171"
_EM_ULIST_ENDPOINT = "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
_EM_ULIST_FIELDS = "f2,f3,f4,f5,f6,f7,f8,f12,f13,f14,f15,f16,f17,f18,f20,f21,f124"
_MACRO_QUOTE_SYMBOLS = ("SH.000001", "SZ.399001", "SZ.399006", "SH.000300", "SH.000016")
QUOTE_TRADING_DAY_OPEN = dt_time(9, 15)


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


def _iter_strategy_snapshot_symbols(db: Database | None = None) -> list[str]:
    symbols: list[str] = []
    snapshot = None
    # The strategy lane already persists a Mongo read model.  Rebuilding it
    # from all gateway responses on every quote tick can take 10-20 seconds;
    # use the latest persisted snapshot first and only compute as a fallback.
    if db is not None:
        try:
            stored = db["strategy_snapshots"].find_one(
                {},
                {"snapshot": 1, "updated_at": 1},
                sort=[("updated_at", -1)],
            ) or {}
            nested = stored.get("snapshot")
            if isinstance(nested, dict):
                snapshot = nested
        except Exception:
            snapshot = None
    if snapshot is None:
        try:
            from signals.strategy.snapshot import get_strategy_snapshot

            snapshot = get_strategy_snapshot(db=db)
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


def _latest_pool_symbols(db: Database, strategy_symbols: list[str] | None = None) -> list[str]:
    symbols = []

    def add(value: object) -> None:
        symbol = str(value or "").strip()
        if symbol and symbol not in symbols:
            symbols.append(symbol)

    doc = db["market_pools"].find_one({"pool": "active"}, {"symbols": 1}, sort=[("dt", -1), ("updated_at", -1)])
    if doc and doc.get("symbols"):
        for item in doc["symbols"]:
            add(item)

    for item in strategy_symbols if strategy_symbols is not None else _iter_strategy_snapshot_symbols(db):
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


def _latest_chain_heat_representative_symbols(db: Database) -> list[str]:
    symbols: list[str] = []

    def add(value: object) -> None:
        symbol = _normalize_quote_symbol(value)
        if symbol and _is_a_quote_symbol(symbol) and symbol not in symbols:
            symbols.append(symbol)

    try:
        latest = db["chain_heat_snapshots"].find_one(
            {"market": "A"},
            {"trade_minute": 1},
            sort=[("trade_minute", -1), ("updated_at", -1)],
        ) or {}
        trade_minute = latest.get("trade_minute")
        if not trade_minute:
            return []
        limit = max(1, min(80, int(os.getenv("QUOTE_CHAIN_HEAT_NODE_LIMIT", "32"))))
        cursor = db["chain_heat_snapshots"].find(
            {"market": "A", "trade_minute": trade_minute},
            {
                "_id": 0,
                "leader_symbol": 1,
                "representatives": 1,
                "integrated_domains.representatives": 1,
            },
        ).sort("rank", 1).limit(limit)
        for row in cursor:
            add(row.get("leader_symbol"))
            for rep in row.get("representatives") or []:
                if isinstance(rep, dict):
                    add(rep.get("symbol") or rep.get("code") or rep.get("raw_code"))
            for domain in row.get("integrated_domains") or []:
                if not isinstance(domain, dict):
                    continue
                for rep in domain.get("representatives") or []:
                    if isinstance(rep, dict):
                        add(rep.get("symbol") or rep.get("code") or rep.get("raw_code"))
    except Exception:
        logger.debug("chain heat representative quote symbols unavailable", exc_info=True)
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
    lane = str(get_task_env("SIGNALS_CURRENT_SYNC_LANE", "quote_lane") or "quote_lane")
    date_key = str(latest_dt or "").replace("-", "")[:8]
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
            "date_key": date_key,
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
    raw = _normalize_quote_symbol(symbol)
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
            if code.isdigit() and len(code) == 6:
                if prefix == "BJ":
                    return f"{prefix}.{code}"
                if prefix == "SH" and code.startswith("000"):
                    return f"{prefix}.{code}"
                if prefix == "SZ" and code.startswith("399"):
                    return f"{prefix}.{code}"
                if code.startswith(("5", "6", "9")):
                    return f"SH.{code}"
                if code.startswith(("4", "8")):
                    return f"BJ.{code}"
                return f"SZ.{code}"
            return f"{prefix}.{code}"
    if len(raw) >= 8 and raw[:2] in {"SH", "SZ", "BJ"}:
        return f"{raw[:2]}.{raw[2:]}"
    code = raw.replace("SH", "").replace("SZ", "").replace("BJ", "")
    if code.isdigit() and len(code) == 6:
        if code.startswith("920"):
            return f"BJ.{code}"
        if code.startswith(("5", "6", "9")):
            return f"SH.{code}"
        if code.startswith(("4", "8")):
            return f"BJ.{code}"
        return f"SZ.{code}"
    return raw


def _is_a_quote_symbol(value: object) -> bool:
    normalized = _normalize_quote_symbol(value)
    return normalized.startswith(("SH.", "SZ.", "BJ."))


def _a_quote_symbols(symbols: list[str]) -> list[str]:
    filtered: list[str] = []
    for symbol in symbols:
        normalized = _normalize_quote_symbol(symbol)
        if normalized and _is_a_quote_symbol(normalized) and normalized not in filtered:
            filtered.append(normalized)
    return filtered


def _is_index_quote_symbol(symbol: str) -> bool:
    normalized = _normalize_quote_symbol(symbol)
    if "." not in normalized:
        return False
    prefix, code = normalized.split(".", 1)
    # Besides the broad SH.000/SZ.399 indexes, Eastmoney's custom/industry
    # indexes used by the index lane are commonly SH.93xxxx.  They do not
    # belong in the stock ulist quote channel either.
    return (prefix == "SH" and code.startswith(("000", "93"))) or (prefix == "SZ" and code.startswith("399"))


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


def _float_or_default(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, "-", ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _float_or_none(value: object) -> float | None:
    try:
        if value in (None, "-", ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _computed_change(price: object, prev_close: object, fallback: object = None) -> float | None:
    price_value = _float_or_none(price)
    prev_value = _float_or_none(prev_close)
    if price_value is not None and prev_value not in (None, 0):
        return round(price_value - prev_value, 4)
    fallback_value = _float_or_none(fallback)
    return round(fallback_value, 4) if fallback_value is not None else None


def _computed_change_pct(price: object, prev_close: object, fallback: object = None) -> float | None:
    price_value = _float_or_none(price)
    prev_value = _float_or_none(prev_close)
    if price_value is not None and prev_value not in (None, 0):
        return round((price_value - prev_value) / prev_value * 100.0, 4)
    fallback_value = _float_or_none(fallback)
    return round(fallback_value, 4) if fallback_value is not None else None


def _quote_doc_from_em(symbol: str, payload: dict, now: datetime, trading_day: str) -> dict | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not data:
        return None
    price = _scale_price(data.get("f43"))
    if price is None or price <= 0:
        return None
    prev_close = _scale_price(data.get("f60"))
    change = _computed_change(price, prev_close, _scale_price(data.get("f169")))
    change_pct = _computed_change_pct(price, prev_close, _scale_pct(data.get("f170")))
    vol, source_volume_unit = normalize_stock_volume(data.get("f47"), source_unit="hands")
    return {
        "_id": f"{symbol}:latest",
        "symbol": symbol,
        "code": data.get("f57") or (symbol.split(".", 1)[-1] if "." in symbol else symbol),
        "name": data.get("f58") or "",
        "dt": trading_day,
        "trade_date": trading_day,
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
        "change": change,
        "change_pct": change_pct,
        "turnover_pct": _scale_pct(data.get("f168")),
        "amplitude_pct": _scale_pct(data.get("f171")),
        "vol": vol,
        "volume_unit": CANONICAL_STOCK_VOLUME_UNIT,
        "source_vol": _float_or_default(data.get("f47")),
        "source_volume_unit": source_volume_unit,
        "amount": _float_or_default(data.get("f48")),
        "market_cap": _float_or_default(data.get("f116")),
        "float_market_cap": _float_or_default(data.get("f117")),
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
    prev_close = _float_or_none(row.get("prev_close"))
    change = _computed_change(price, prev_close, row.get("change"))
    change_pct = _computed_change_pct(price, prev_close, row.get("change_pct"))
    code = str(row.get("code") or _code_for_symbol(symbol))
    vol, source_volume_unit = normalize_stock_volume(
        row.get("vol"),
        source=row.get("source"),
        source_unit=row.get("volume_unit"),
        default_source_unit="hands",
    )
    return {
        "_id": f"{symbol}:latest",
        "symbol": symbol,
        "code": code,
        "name": row.get("name") or "",
        "dt": trading_day,
        "trade_date": trading_day,
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
        "prev_close": prev_close,
        "change": change,
        "change_pct": change_pct,
        "turnover_pct": row.get("turnover_pct"),
        "amplitude_pct": row.get("amplitude_pct"),
        "vol": vol,
        "volume_unit": CANONICAL_STOCK_VOLUME_UNIT,
        "source_vol": row.get("source_vol", row.get("vol")),
        "source_volume_unit": row.get("source_volume_unit") or source_volume_unit,
        "amount": float(row.get("amount") or 0),
        "market_cap": float(row.get("market_cap") or 0),
        "float_market_cap": float(row.get("float_market_cap") or 0),
        "expires_at": now + timedelta(days=3),
    }


def _quote_doc_from_ulist_row(symbol: str, row: dict, now: datetime, trading_day: str) -> dict | None:
    price = _float_or_default(row.get("f2"), default=0.0)
    if price <= 0:
        return None
    code = str(row.get("f12") or _code_for_symbol(symbol))
    prev_close = _float_or_default(row.get("f18"))
    change = _computed_change(price, prev_close, row.get("f4"))
    change_pct = _computed_change_pct(price, prev_close, row.get("f3"))
    vol, source_volume_unit = normalize_stock_volume(row.get("f5"), source_unit="hands")
    source_updated_at = None
    try:
        source_epoch = int(row.get("f124") or 0)
        if source_epoch > 0:
            source_updated_at = datetime.fromtimestamp(source_epoch, tz=ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        source_updated_at = None
    return {
        "_id": f"{symbol}:latest",
        "symbol": symbol,
        "code": code,
        "name": str(row.get("f14") or ""),
        "dt": trading_day,
        "trade_date": trading_day,
        "snapshot_at": now,
        "source_updated_at": source_updated_at,
        "source": "eastmoney_push2delay_ulist",
        "freshness": "fresh",
        "is_stale": False,
        "stale_reason": "",
        "open": _float_or_default(row.get("f17")),
        "high": _float_or_default(row.get("f15")),
        "low": _float_or_default(row.get("f16")),
        "close": price,
        "price": price,
        "prev_close": prev_close,
        "change": change,
        "change_pct": change_pct,
        "turnover_pct": _float_or_default(row.get("f8")),
        "amplitude_pct": _float_or_default(row.get("f7")),
        "vol": vol,
        "volume_unit": CANONICAL_STOCK_VOLUME_UNIT,
        "source_vol": _float_or_default(row.get("f5")),
        "source_volume_unit": source_volume_unit,
        "amount": _float_or_default(row.get("f6")),
        "market_cap": _float_or_default(row.get("f20")),
        "float_market_cap": _float_or_default(row.get("f21")),
        "expires_at": now + timedelta(days=3),
    }


def _chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[idx:idx + size] for idx in range(0, len(values), size)]


def _hot_quote_symbols(db: Database) -> list[str]:
    symbols: list[str] = []

    def add(value: object) -> None:
        symbol = _normalize_quote_symbol(value)
        if symbol and _is_a_quote_symbol(symbol) and symbol not in symbols:
            symbols.append(symbol)

    for symbol in _MACRO_QUOTE_SYMBOLS:
        add(symbol)

    try:
        strategy_symbols = _iter_strategy_snapshot_symbols(db)
    except TypeError:
        # Keep lightweight test/integration overrides that expose the old
        # zero-argument hook compatible.
        strategy_symbols = _iter_strategy_snapshot_symbols()
    for item in strategy_symbols:
        add(item)

    try:
        for item in db["terminal_manual_clues"].find(
            {"active": {"$ne": False}},
            {"symbol": 1, "raw_code": 1},
        ).sort("updated_at", -1).limit(80):
            add(item.get("symbol") or item.get("raw_code"))
    except Exception:
        logger.debug("manual clue quote symbols unavailable", exc_info=True)

    # Auto-selected hot-rank clues are rendered beside manual and terminal
    # pool clues, so they need the same live quote coverage.  They are not
    # guaranteed to be present in the previous postmarket pool or strategy
    # snapshot, especially before today's pool has been published.
    try:
        for item in db["hot_rank_clues"].find(
            {"active": True},
            {"symbol": 1, "raw_code": 1},
        ).sort([("score", -1), ("updated_at", -1)]).limit(80):
            add(item.get("symbol") or item.get("raw_code"))
    except Exception:
        logger.debug("hot-rank clue quote symbols unavailable", exc_info=True)

    try:
        for item in macro_watchlist():
            if isinstance(item, dict):
                add(item.get("symbol"))
    except Exception:
        logger.debug("macro quote symbols unavailable", exc_info=True)

    try:
        doc = db["terminal_stock_pool"].find_one(
            {"pool": "terminal_stock_pool", "market": "A"},
            {"stocks": 1, "focus_stocks": 1, "risk_stocks": 1, "watch_stocks": 1, "clue_stocks": 1},
            sort=[("updated_at", -1)],
        ) or {}
    except Exception:
        doc = {}
    for group in ("focus_stocks", "risk_stocks", "watch_stocks", "clue_stocks", "stocks"):
        for item in doc.get(group) or []:
            if not isinstance(item, dict):
                continue
            add(item.get("symbol") or item.get("code") or item.get("raw_code"))
    for symbol in _latest_chain_heat_representative_symbols(db):
        add(symbol)
    for symbol in _latest_pool_symbols(db, strategy_symbols=strategy_symbols):
        add(symbol)

    try:
        limit = max(1, min(500, int(os.getenv("EASTMONEY_ULIST_MAX_SYMBOLS", "500"))))
    except (TypeError, ValueError):
        limit = 500
    return symbols[:limit]


def _realtime_quote_symbols(db: Database) -> list[str]:
    """Return symbols that belong to the quote lane, excluding indices.

    Macro/index symbols are intentionally present in the hot universe, but
    their canonical source is ``index_bars``/``index_minute``.  Eastmoney's
    stock ulist endpoint does not guarantee all index codes, so sending them
    through the stock quote lane creates false degraded tails without adding
    any usable quote data.
    """
    return [
        symbol
        for symbol in _a_quote_symbols(_hot_quote_symbols(db))
        if not _is_index_quote_symbol(symbol)
    ]


def _fetch_eastmoney_ulist_docs(
    db: Database,
    symbols: list[str],
    now: datetime,
    trading_day: str,
) -> tuple[dict[str, dict], list[dict]]:
    symbols = _a_quote_symbols(symbols)
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
    symbols = [symbol for symbol in _a_quote_symbols(symbols) if not _is_index_quote_symbol(symbol)]
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
                "volume_unit": 1,
                "source_vol": 1,
                "source_volume_unit": 1,
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
    symbols = [symbol for symbol in _a_quote_symbols(symbols) if not _is_index_quote_symbol(symbol)]
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


def _current_quote_snapshot_fallback_allowed(now: datetime) -> bool:
    current = now.time()
    return not (
        dt_time(9, 30) <= current < dt_time(11, 30)
        or dt_time(13, 0) <= current < dt_time(15, 0)
    )


def _current_quote_snapshot_doc(row: dict, symbol: str, trading_day: str, now: datetime) -> dict | None:
    day_text = str(trading_day or "")[:10]
    compact_day = day_text.replace("-", "")
    row_day = str(row.get("trade_date") or row.get("dt") or "")[:10]
    if row_day not in {day_text, compact_day}:
        return None
    price = _float_or_none(row.get("price", row.get("close")))
    if price is None or price <= 0:
        return None
    doc = dict(row)
    doc.update({
        "_id": f"{symbol}:latest",
        "symbol": symbol,
        "code": doc.get("code") or _code_for_symbol(symbol),
        "dt": day_text,
        "trade_date": day_text,
        "snapshot_at": doc.get("snapshot_at") or now,
        "freshness": "fresh",
        "is_stale": False,
        "stale_reason": "",
        "price": price,
        "close": doc.get("close", price),
        "expires_at": now + timedelta(days=3),
    })
    return doc


def _read_current_quote_snapshot_docs(
    db: Database,
    symbols: list[str],
    trading_day: str,
    now: datetime,
) -> dict[str, dict]:
    if not _current_quote_snapshot_fallback_allowed(now):
        return {}
    docs: dict[str, dict] = {}
    for symbol in _a_quote_symbols(symbols):
        try:
            row = db["quote_snapshots"].find_one({"_id": f"{symbol}:latest"}, {"_id": 0})
        except Exception:
            row = None
        if not row:
            continue
        doc = _current_quote_snapshot_doc(row, symbol, trading_day, now)
        if doc:
            docs[symbol] = doc
    return docs


def _fetch_em_quote(db: Database, symbol: str, timeout: float = 5.0) -> tuple[dict | None, float, str]:
    symbol = _normalize_quote_symbol(symbol)
    if not _is_a_quote_symbol(symbol):
        return None, 0.0, "non_a_quote_symbol"
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
        trading_day = a_share_task_trade_date(now=now)
    except Exception:
        trading_day = now.date().isoformat()
    symbols = _realtime_quote_symbols(db)
    inserted = 0
    modified = 0
    processed = 0
    live_count = 0
    stale_count = 0
    errors = []
    latest_dt = None
    spot_docs = _read_fullmarket_spot_quotes(db, symbols, trading_day, now, allow_latest_fallback=False)
    request_symbols = [symbol for symbol in symbols if symbol not in spot_docs]
    ulist_docs, observations = _fetch_eastmoney_ulist_docs(db, request_symbols, now, trading_day)
    errors.extend((item.get("endpoint") or "push2delay.ulist.quote", item.get("error")) for item in observations if item.get("error"))
    current_snapshot_docs = _read_current_quote_snapshot_docs(
        db,
        [symbol for symbol in request_symbols if symbol not in ulist_docs],
        trading_day,
        now,
    )
    missing_symbols = [
        symbol
        for symbol in request_symbols
        if symbol not in ulist_docs and symbol not in current_snapshot_docs
    ]
    no_current_price_symbols = _read_fullmarket_no_price_symbols(db, request_symbols, trading_day)

    ops = []
    for symbol in symbols:
        doc = spot_docs.get(symbol) or ulist_docs.get(symbol) or current_snapshot_docs.get(symbol)
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
        "ulist": len(ulist_docs),
        "current_snapshot_fallback": len(current_snapshot_docs),
        "requested": len(request_symbols),
        "missing_current": len(missing_symbols),
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
        trading_day = a_share_task_trade_date(now=now)
    except Exception:
        trading_day = now.date().isoformat()
    symbols = _realtime_quote_symbols(db)
    # fullmarket_spot_snapshots is the canonical all-market watermark.  The
    # ulist lane is only a bounded supplement for symbols absent from that
    # watermark; otherwise the same hot symbols are fetched twice in one live
    # pass and the provider/quote_snapshots writes are duplicated.
    canonical_docs = _read_fullmarket_spot_quotes(
        db,
        symbols,
        trading_day,
        now,
        allow_latest_fallback=False,
    )
    missing_symbols = [symbol for symbol in symbols if symbol not in canonical_docs]
    fetched_docs, observations = _fetch_eastmoney_ulist_docs(
        db,
        missing_symbols,
        now,
        trading_day,
    )
    docs = {**canonical_docs, **fetched_docs}

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
        endpoint="push2delay.ulist.quote" if observations else "fullmarket_spot_snapshot",
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
        "canonical": len(canonical_docs),
        "requested": len(missing_symbols),
        "target_collection": "quote_snapshots",
    }
