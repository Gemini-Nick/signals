# -*- coding: utf-8 -*-
"""Seed HK/US identities, versioned memberships, and compact replay snapshots."""
from __future__ import annotations

from datetime import datetime, timedelta
from math import isfinite
import os
from typing import Any, Iterable

from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.global_market_universe import (
    load_global_market_universe,
    market_metadata,
    market_universe,
    normalize_market,
)
from signals.core.market_time import naive_market_now
from signals.data.bar_quality import validate_ohlcv_bar

DAILY_FREQS = ("日线", "daily", "D", "1d")
KR_CONTEXT_FLAG = "SECTOR_TRANSITION_KR_CONTEXT_ENABLED"


def kr_context_enabled() -> bool:
    return os.getenv(KR_CONTEXT_FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


def _requested_foundation_markets(markets: Iterable[str] | None = None) -> tuple[str, ...]:
    requested = ("HK", "US") if markets is None else tuple(markets)
    output: list[str] = []
    for value in requested:
        market = normalize_market(value)
        if market == "A" or (market == "KR" and not kr_context_enabled()):
            continue
        if market not in output:
            output.append(market)
    return tuple(output)


def _security_id(symbol: str) -> str:
    return f"security:{symbol.upper()}"


def _pair_by_symbol() -> dict[str, dict[str, str]]:
    pairs = load_global_market_universe()["a_h_pairs"]
    return {symbol: pair for pair in pairs for symbol in (pair["a_symbol"], pair["h_symbol"])}


def _select_session_date(
    valid: dict[tuple[str, str], dict[str, Any]],
    market: str,
) -> str:
    available_days = sorted({day for _, day in valid})
    session_date = available_days[-1]
    if market == "HK" and len(available_days) > 1:
        latest_count = sum(1 for _, day in valid if day == session_date)
        latest_symbols = {symbol for symbol, day in valid if day == session_date}
        core_symbols = {item["symbol"] for item in market_universe("HK")}
        core_minimum = min(5, len(core_symbols))
        if len(latest_symbols & core_symbols) >= core_minimum:
            return session_date
        previous_date = available_days[-2]
        previous_count = sum(1 for _, day in valid if day == previous_date)
        # HK shards can expose a small, newer partial day before the full-market
        # close sync finishes. Do not publish that shard as a complete market.
        if previous_count >= 500 and latest_count < max(500, int(previous_count * 0.8)):
            return previous_date
    return session_date


def security_master_documents(
    *,
    as_of: str,
    now: datetime | None = None,
    markets: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    now = now or naive_market_now("A")
    pair_map = _pair_by_symbol()
    docs: list[dict[str, Any]] = []
    for market in _requested_foundation_markets(markets):
        metadata = market_metadata(market)
        for item in market_universe(market):
            symbol = item["symbol"]
            pair = pair_map.get(symbol)
            linked = []
            issuer_id = f"issuer:{symbol.lower().replace('.', ':')}"
            if pair:
                other = pair["a_symbol"] if market == "HK" else pair["h_symbol"]
                linked = [_security_id(other)]
                issuer_id = pair["issuer_id"]
            docs.append(
                {
                    "_id": _security_id(symbol),
                    "security_id": _security_id(symbol),
                    "market": market,
                    "exchange": item["exchange"],
                    "symbol": symbol,
                    "raw_code": item["raw_code"],
                    "name": item["name"],
                    "issuer_id": issuer_id,
                    "primary_listing_id": _security_id(symbol),
                    "linked_listing_ids": linked,
                    "currency": metadata["currency"],
                    "timezone": metadata["timezone"],
                    "listing_status": "listed",
                    "asset_type": item["instrument_kind"],
                    "proxy_for": item.get("proxy_for") or None,
                    "data_sources": ["global_market_universe"],
                    "as_of": as_of,
                    "updated_at": now,
                }
            )
    return docs


def universe_membership_documents(
    *,
    effective_date: str,
    now: datetime | None = None,
    markets: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    now = now or naive_market_now("A")
    version = load_global_market_universe()["version"]
    docs: list[dict[str, Any]] = []
    for market in _requested_foundation_markets(markets):
        for rank, item in enumerate(market_universe(market), start=1):
            docs.append(
                {
                    "_id": f"{version}:{market}:{item['symbol']}",
                    "universe_version": version,
                    "market": market,
                    "symbol": item["symbol"],
                    "security_id": _security_id(item["symbol"]),
                    "role": item["role"],
                    "group": item.get("group") or None,
                    "instrument_kind": item["instrument_kind"],
                    "proxy_for": item.get("proxy_for") or None,
                    "priority": int(item.get("priority") or 0),
                    "fixed": True,
                    "rank": rank,
                    "effective_date": effective_date,
                    "updated_at": now,
                }
            )
    return docs


def seed_global_market_foundation(
    db: Database,
    *,
    as_of: str,
    now: datetime | None = None,
    markets: Iterable[str] | None = None,
) -> dict[str, int]:
    requested_markets = _requested_foundation_markets(markets)
    security_docs = security_master_documents(as_of=as_of, now=now, markets=requested_markets)
    membership_docs = universe_membership_documents(effective_date=as_of, now=now, markets=requested_markets)
    if security_docs:
        db["security_master"].bulk_write(
            [UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True) for doc in security_docs],
            ordered=False,
        )
    if membership_docs:
        db["market_universe_membership"].bulk_write(
            [UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True) for doc in membership_docs],
            ordered=False,
        )
    pair_updates = []
    for pair in load_global_market_universe()["a_h_pairs"] if "HK" in requested_markets else []:
        pair_updates.append(
            UpdateOne(
                {"market": "A", "symbol": pair["a_symbol"]},
                {
                    "$set": {"issuer_id": pair["issuer_id"], "linked_listing_ids": [_security_id(pair["h_symbol"])]},
                    "$setOnInsert": {
                        "security_id": _security_id(pair["a_symbol"]),
                        "market": "A",
                        "symbol": pair["a_symbol"],
                        "name": pair["name"],
                        "currency": "CNY",
                        "asset_type": "stock",
                    },
                },
                upsert=True,
            )
        )
    if pair_updates:
        db["security_master"].bulk_write(pair_updates, ordered=False)
    return {"security_master": len(security_docs), "market_universe_membership": len(membership_docs)}


def _latest_valid_bars(db: Database, market: str) -> tuple[str, list[dict[str, Any]]]:
    symbols = [item["symbol"] for item in market_universe(market)]
    candidates: list[dict[str, Any]] = []
    collections = ("bars", "index_bars")
    for collection in collections:
        try:
            query: dict[str, Any] = {"meta.symbol": {"$in": symbols}, "meta.freq": {"$in": list(DAILY_FREQS)}}
            latest = db[collection].find_one(query, {"dt": 1}, sort=[("dt", -1)]) or {}
            latest_dt = latest.get("dt")
            if latest_dt is None:
                continue
            latest_day = latest_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            current_query = {**query, "dt": {"$gte": latest_day, "$lt": latest_day + timedelta(days=1)}}
            current = list(db[collection].find(current_query, {"_id": 0}))
            candidates.extend(current)
            cursor_day = latest_day
            for _ in range(2):
                previous = db[collection].find_one(
                    {**query, "dt": {"$lt": cursor_day}},
                    {"dt": 1},
                    sort=[("dt", -1)],
                ) or {}
                previous_dt = previous.get("dt")
                if previous_dt is None:
                    break
                previous_day = previous_dt.replace(hour=0, minute=0, second=0, microsecond=0)
                candidates.extend(
                    list(
                        db[collection].find(
                            {**query, "dt": {"$gte": previous_day, "$lt": previous_day + timedelta(days=1)}},
                            {"_id": 0},
                        )
                    )
                )
                cursor_day = previous_day
        except Exception:
            continue
    valid: dict[tuple[str, str], dict[str, Any]] = {}
    for doc in candidates:
        accepted, _ = validate_ohlcv_bar(doc, allow_zero_volume=True)
        if not accepted:
            continue
        symbol = str((doc.get("meta") or {}).get("symbol") or "").upper()
        try:
            day = doc["dt"].date().isoformat()
        except AttributeError:
            day = str(doc.get("dt") or "")[:10]
        if symbol and day:
            valid.setdefault((symbol, day), doc)
    if not valid:
        return "", []
    session_date = _select_session_date(valid, market)
    rows: list[dict[str, Any]] = []
    for (symbol, day), doc in valid.items():
        if day != session_date:
            continue
        item = dict(doc)
        previous_days = sorted(
            previous_day
            for previous_symbol, previous_day in valid
            if previous_symbol == symbol and previous_day < session_date
        )
        if previous_days:
            item["_previous_close"] = valid[(symbol, previous_days[-1])].get("close")
        rows.append(item)
    return session_date, rows


def _raw_bar_documents(
    raw_bars: list[Any],
    *,
    symbol: str,
    market: str,
    source: str,
    feed: str | None,
) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for bar in raw_bars:
        freq = getattr(getattr(bar, "freq", None), "value", None) or str(getattr(bar, "freq", ""))
        doc = {
            "dt": getattr(bar, "dt", None),
            "meta": {
                "symbol": symbol,
                "freq": freq,
                "market": market,
                "asset_type": "stock",
                "source": source,
                "feed": feed,
            },
            "open": getattr(bar, "open", None),
            "high": getattr(bar, "high", None),
            "low": getattr(bar, "low", None),
            "close": getattr(bar, "close", None),
            "vol": getattr(bar, "vol", None),
            "amount": getattr(bar, "amount", 0),
        }
        accepted, _ = validate_ohlcv_bar(doc, allow_zero_volume=False)
        if accepted:
            docs.append(doc)
    return docs


def _write_bar_docs(db: Database, docs: list[dict[str, Any]]) -> int:
    if not docs:
        return 0
    from signals.sync.modules.index_daily import _replace_exact_bar_docs

    return _replace_exact_bar_docs(db["bars"], docs)


def _append_new_bar_docs(db: Database, docs: list[dict[str, Any]]) -> int:
    """Append only bars newer than each symbol's persisted tail.

    The fallback refresh only needs to advance the compact replay snapshot.
    Replacing a full adjusted history is both unnecessary and expensive on a
    remote time-series collection.
    """
    if not docs:
        return 0
    symbols = sorted({str((doc.get("meta") or {}).get("symbol") or "") for doc in docs} - {""})
    latest_by_symbol = {
        str(row["_id"]): row.get("latest")
        for row in db["bars"].aggregate([
            {"$match": {"meta.symbol": {"$in": symbols}, "meta.freq": {"$in": list(DAILY_FREQS)}}},
            {"$group": {"_id": "$meta.symbol", "latest": {"$max": "$dt"}}},
        ])
    }
    pending: list[dict[str, Any]] = []
    seen: set[tuple[str, Any]] = set()
    for doc in docs:
        item = dict(doc)
        item.pop("_id", None)
        symbol = str((item.get("meta") or {}).get("symbol") or "")
        dt = item.get("dt")
        latest = latest_by_symbol.get(symbol)
        key = (symbol, dt)
        if symbol and dt is not None and (latest is None or dt > latest) and key not in seen:
            seen.add(key)
            pending.append(item)
    if not pending:
        return 0
    result = db["bars"].insert_many(pending, ordered=False)
    return len(getattr(result, "inserted_ids", []) or [])


def _hydrate_us_yfinance(db: Database) -> dict[str, Any]:
    from signals.data.fetcher import YFinanceSource

    source = YFinanceSource()
    written = 0
    covered = 0
    errors: list[str] = []
    for item in market_universe("US"):
        symbol = item["symbol"]
        try:
            docs = _raw_bar_documents(
                source.get_us_daily(symbol, period="1y"),
                symbol=symbol,
                market="US",
                source="yfinance",
                feed="delayed",
            )
            if docs:
                covered += 1
                written += _append_new_bar_docs(db, docs)
        except Exception as exc:
            errors.append(f"{symbol}:{exc}")
    return {
        "status": "available" if covered else "unavailable",
        "provider": "yfinance",
        "daily_written": written,
        "minute_written": 0,
        "covered_symbols": covered,
        "coverage_scope": "core_universe",
        "errors": errors[:8],
    }


def _hydrate_hk_tencent(db: Database) -> dict[str, Any]:
    from signals.sync.modules.hk_stock_daily import (
        _docs_from_hk_daily_df,
        _fetch_tencent_hk_daily_df,
    )

    end = naive_market_now("HK").strftime("%Y%m%d")
    start = (naive_market_now("HK") - timedelta(days=45)).strftime("%Y%m%d")
    docs_by_code: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for item in market_universe("HK"):
        code = item["raw_code"]
        try:
            frame = _fetch_tencent_hk_daily_df(code, start, end)
            docs = _docs_from_hk_daily_df(code, frame, "website_tencent_hk", end_date=end)
            if docs:
                docs_by_code[code] = docs
        except Exception as exc:
            errors.append(f"{item['symbol']}:{exc}")
    written = _append_new_bar_docs(db, [doc for docs in docs_by_code.values() for doc in docs])
    return {
        "status": "available" if docs_by_code else "unavailable",
        "provider": "tencent",
        "daily_written": written,
        "minute_written": 0,
        "covered_symbols": len(docs_by_code),
        "coverage_scope": "core_universe",
        "errors": errors[:8],
    }


def hydrate_global_core_bars(db: Database) -> dict[str, Any]:
    """Best-effort provider bridge.

    Credentials and OpenD are optional. Missing providers return unavailable and
    never erase already persisted bars.
    """
    status: dict[str, Any] = {
        "US": {"status": "unavailable", "provider": "alpaca", "daily_written": 0, "minute_written": 0},
        "HK": {"status": "unavailable", "provider": "futu", "daily_written": 0, "minute_written": 0},
    }

    api_key = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
    if api_key and secret_key:
        try:
            from czsc import Freq
            from signals.data.alpaca_source import AlpacaSource

            feed = os.getenv("ALPACA_DATA_FEED", "iex").strip().lower() or "iex"
            source = AlpacaSource(api_key, secret_key, feed=feed)
            errors: list[str] = []
            for item in market_universe("US"):
                symbol = item["symbol"]
                try:
                    daily = _raw_bar_documents(
                        source.get_us_daily(symbol, lookback_days=365 * 5),
                        symbol=symbol,
                        market="US",
                        source="alpaca",
                        feed=feed,
                    )
                    minute = _raw_bar_documents(
                        source.get_us_minute(symbol, Freq.F30, lookback_days=60),
                        symbol=symbol,
                        market="US",
                        source="alpaca",
                        feed=feed,
                    )
                    status["US"]["daily_written"] += _write_bar_docs(db, daily)
                    status["US"]["minute_written"] += _write_bar_docs(db, minute)
                except Exception as exc:
                    errors.append(f"{symbol}:{exc}")
            status["US"].update(
                {
                    "status": "available" if status["US"]["daily_written"] else "unavailable",
                    "feed": source.feed,
                    "coverage_scope": "core_universe",
                    "errors": errors[:8],
                }
            )
        except Exception as exc:
            status["US"]["reason"] = str(exc)
    else:
        status["US"]["reason"] = "ALPACA_API_KEY / ALPACA_SECRET_KEY not configured"

    if not status["US"]["daily_written"]:
        try:
            status["US"] = _hydrate_us_yfinance(db)
        except Exception as exc:
            status["US"].setdefault("fallback_reason", str(exc))

    futu_source = None
    try:
        from czsc import Freq
        from signals.data.fetcher import FutuSource

        futu_source = FutuSource(
            host=os.getenv("FUTU_HOST", "127.0.0.1"),
            port=int(os.getenv("FUTU_PORT", "11111")),
        ).connect(timeout=2.0)
        errors = []
        for item in market_universe("HK"):
            symbol = item["symbol"]
            try:
                daily = _raw_bar_documents(
                    futu_source.get_history_kline(symbol, Freq.D, lookback_days=365 * 5),
                    symbol=symbol,
                    market="HK",
                    source="futu",
                    feed="opend",
                )
                minute = _raw_bar_documents(
                    futu_source.get_history_kline(symbol, Freq.F30, lookback_days=60),
                    symbol=symbol,
                    market="HK",
                    source="futu",
                    feed="opend",
                )
                status["HK"]["daily_written"] += _write_bar_docs(db, daily)
                status["HK"]["minute_written"] += _write_bar_docs(db, minute)
            except Exception as exc:
                errors.append(f"{symbol}:{exc}")
        status["HK"].update(
            {
                "status": "available" if status["HK"]["daily_written"] else "unavailable",
                "coverage_scope": "core_universe",
                "errors": errors[:8],
            }
        )
    except Exception as exc:
        status["HK"]["reason"] = str(exc)
    finally:
        if futu_source is not None:
            try:
                futu_source.close()
            except Exception:
                pass
    if not status["HK"]["daily_written"]:
        try:
            status["HK"] = _hydrate_hk_tencent(db)
        except Exception as exc:
            status["HK"].setdefault("fallback_reason", str(exc))
    return status


def build_market_daily_snapshot(db: Database, market: str, *, now: datetime | None = None) -> dict[str, Any]:
    market = normalize_market(market)
    if market == "A":
        raise ValueError("global foundation snapshots are only materialized for HK/US/KR")
    if market == "KR" and not kr_context_enabled():
        raise RuntimeError(f"KR context disabled: {KR_CONTEXT_FLAG}=false")
    now = now or naive_market_now(market)
    metadata = market_metadata(market)
    session_date, bars = _latest_valid_bars(db, market)
    universe = {item["symbol"]: item for item in market_universe(market)}
    rows: list[dict[str, Any]] = []
    for doc in bars:
        meta = doc.get("meta") or {}
        symbol = str(meta.get("symbol") or "").upper()
        item = universe.get(symbol)
        if not item and market == "HK":
            item = {
                "symbol": symbol,
                "name": symbol,
                "role": "market_member",
                "group": "",
                "instrument_kind": "stock",
                "proxy_for": "",
            }
        if not item or not symbol:
            continue
        change_pct = doc.get("change_pct", doc.get("pct_chg"))
        try:
            change_pct = float(change_pct)
            if not isfinite(change_pct):
                change_pct = None
        except (TypeError, ValueError):
            change_pct = None
        if change_pct is None:
            try:
                previous_close = float(doc.get("_previous_close"))
                close = float(doc.get("close"))
                if previous_close > 0 and isfinite(previous_close) and isfinite(close):
                    change_pct = round((close / previous_close - 1) * 100, 4)
            except (TypeError, ValueError):
                change_pct = None
        rows.append(
            {
                "symbol": symbol,
                "name": item["name"],
                "role": item["role"],
                "group": item.get("group") or None,
                "instrument_kind": item["instrument_kind"],
                "proxy_for": item.get("proxy_for") or None,
                "close": float(doc["close"]),
                "change_pct": change_pct,
                "amount": float(doc.get("amount") or 0),
                "source": str(meta.get("source") or doc.get("source") or ""),
            }
        )
    stocks = [row for row in rows if row["instrument_kind"] == "stock"]
    known_changes = [row for row in stocks if row["change_pct"] is not None]
    dynamic = sorted(
        known_changes,
        key=lambda row: (float(row.get("amount") or 0), abs(float(row.get("change_pct") or 0))),
        reverse=True,
    )[:8]
    effective_scope = metadata["coverage_scope"]
    if market == "HK" and stocks and len(stocks) < 500:
        effective_scope = "core_universe"
    if market == "US" and not stocks:
        effective_scope = "index_only"
    snapshot = {
        "_id": f"{market}:{session_date or 'unavailable'}",
        "schema_version": "1.0",
        **metadata,
        "session_date": session_date or None,
        "as_of": now.isoformat(timespec="seconds"),
        "session_state": "complete" if session_date and rows else "unavailable",
        "universe_version": load_global_market_universe()["version"],
        "coverage_scope": effective_scope,
        "universe_size": (
            len(stocks)
            if market == "HK"
            else (len(rows) if effective_scope == "index_only" else len(universe))
        ),
        "covered_count": len(stocks),
        "breadth": {
            "scope": effective_scope,
            "sample_count": len(known_changes),
            "up": sum(1 for row in known_changes if row["change_pct"] > 0),
            "down": sum(1 for row in known_changes if row["change_pct"] < 0),
            "flat": sum(1 for row in known_changes if row["change_pct"] == 0),
        },
        "liquidity": {
            "currency": metadata["currency"],
            "amount": sum(float(row.get("amount") or 0) for row in rows),
            "scope": effective_scope,
        },
        "indices": [row for row in rows if row["role"] == "index" and row.get("role") != "risk_gauge"],
        "risk_gauges": [row for row in rows if row.get("role") == "risk_gauge"],
        "fixed_representatives": [row for row in rows if row["role"] in {"anchor", "ai_chain"}],
        "dynamic_representatives": dynamic,
        "updated_at": now,
    }
    db["market_daily_snapshots"].replace_one({"_id": snapshot["_id"]}, snapshot, upsert=True)
    return snapshot


def latest_market_snapshot(db: Database, market: str, *, session_date: str | None = None) -> dict[str, Any] | None:
    market = normalize_market(market)
    query: dict[str, Any] = {"market": market, "session_state": "complete"}
    if market == "HK":
        query["$or"] = [
            {"coverage_scope": {"$ne": "full_market"}},
            {"covered_count": {"$gte": 500}},
        ]
    if session_date:
        query["session_date"] = {"$lte": session_date}
    return db["market_daily_snapshots"].find_one(
        query,
        {"_id": 0, "updated_at": 0},
        sort=[("session_date", -1), ("updated_at", -1)],
    )


def sync_global_market_foundation(
    db: Database,
    proxy_url: str | None = None,
    *,
    markets: Iterable[str] | None = None,
) -> dict[str, Any]:
    del proxy_url
    now = naive_market_now("A")
    as_of = now.date().isoformat()
    requested_markets = _requested_foundation_markets(markets)
    seeded = seed_global_market_foundation(db, as_of=as_of, now=now, markets=requested_markets)
    providers = hydrate_global_core_bars(db) if any(market in {"HK", "US"} for market in requested_markets) else {}
    snapshots = [
        build_market_daily_snapshot(db, market, now=naive_market_now(market))
        for market in requested_markets
    ]
    return {
        **seeded,
        "requested_markets": list(requested_markets),
        "providers": providers,
        "snapshots": snapshots,
        "inserted": sum(seeded.values()) + sum(
            int(item.get("daily_written") or 0) + int(item.get("minute_written") or 0)
            for item in providers.values()
        ) + len(snapshots),
    }
