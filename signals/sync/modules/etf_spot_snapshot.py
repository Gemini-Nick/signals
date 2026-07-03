# -*- coding: utf-8 -*-
"""All-market ETF Eastmoney spot snapshot cache."""
from __future__ import annotations

import logging
import math
import os
import time
from datetime import time as dt_time, timedelta

from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.etf_universe import etf_symbol_for_code, fetch_eastmoney_etf_spot_rows, normalize_etf_code
from signals.core.market_time import naive_market_now
from signals.core.trading_dates import trading_day_key
from signals.sync.eastmoney_observer import observe_eastmoney
from signals.sync.provider_limits import provider_call
from signals.sync.task_context import get_task_env
from signals.sync.volume_units import CANONICAL_STOCK_VOLUME_UNIT, normalize_stock_volume

logger = logging.getLogger("signals.sync.etf_spot_snapshot")

QUOTE_TRADING_DAY_OPEN = dt_time(9, 15)
ETF_SPOT_PAGE_SIZE = 100


def _timeout() -> float:
    try:
        return max(3.0, float(os.getenv("ETF_SPOT_TIMEOUT", os.getenv("SIGNALS_ETF_UNIVERSE_TIMEOUT", "8"))))
    except (TypeError, ValueError):
        return 8.0


def _number(value, default: float | None = None) -> float | None:
    if value in (None, "", "-"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _computed_change(latest, prev_close) -> float | None:
    latest_value = _number(latest)
    prev_value = _number(prev_close)
    if latest_value is not None and prev_value not in (None, 0):
        return round(latest_value - prev_value, 4)
    return None


def _computed_change_pct(latest, prev_close, fallback=None) -> float | None:
    latest_value = _number(latest)
    prev_value = _number(prev_close)
    if latest_value is not None and prev_value not in (None, 0):
        return round((latest_value - prev_value) / prev_value * 100.0, 4)
    fallback_value = _number(fallback)
    return round(fallback_value, 4) if fallback_value is not None else None


def fetch_etf_spot_rows(db: Database | None = None) -> list[dict]:
    timeout = _timeout()
    return provider_call(
        "eastmoney",
        "etf_spot_snapshot",
        lambda: fetch_eastmoney_etf_spot_rows(timeout=timeout),
        db=db,
    )


def _doc_from_row(row: dict, *, date_key: str, trade_date: str, snapshot_at) -> dict | None:
    code = normalize_etf_code(row.get("f12") or row.get("code"))
    if not code:
        return None
    latest = _number(row.get("f2") if "f2" in row else row.get("price"))
    prev_close = _number(row.get("f18") if "f18" in row else row.get("prev_close"))
    source_vol = _number(row.get("f5") if "f5" in row else row.get("source_vol"), 0.0)
    vol, source_volume_unit = normalize_stock_volume(source_vol, source_unit="hands")
    market_id = row.get("f13") if "f13" in row else row.get("market_id")
    return {
        "_id": f"{date_key}:{code}",
        "date_key": date_key,
        "trade_date": trade_date,
        "snapshot_at": snapshot_at,
        "source": "eastmoney_etf_spot",
        "asset_class": "etf",
        "security_type": "etf",
        "code": code,
        "symbol": etf_symbol_for_code(code, market_id),
        "name": str(row.get("f14") or row.get("name") or ""),
        "market_id": market_id,
        "latest": latest,
        "price": latest,
        "change_pct": _computed_change_pct(latest, prev_close, row.get("f3")),
        "change": _computed_change(latest, prev_close),
        "vol": vol,
        "volume_unit": CANONICAL_STOCK_VOLUME_UNIT,
        "source_vol": source_vol,
        "source_volume_unit": source_volume_unit,
        "amount": _number(row.get("f6") if "f6" in row else row.get("amount"), 0.0),
        "amplitude_pct": _number(row.get("f7") if "f7" in row else row.get("amplitude_pct")),
        "turnover_pct": _number(row.get("f8") if "f8" in row else row.get("turnover_pct")),
        "high": _number(row.get("f15") if "f15" in row else row.get("high")),
        "low": _number(row.get("f16") if "f16" in row else row.get("low")),
        "open": _number(row.get("f17") if "f17" in row else row.get("open")),
        "prev_close": prev_close,
        "market_cap": _number(row.get("f20") if "f20" in row else row.get("market_cap")),
        "float_market_cap": _number(row.get("f21") if "f21" in row else row.get("float_market_cap")),
        "raw": {key: row.get(key) for key in ("f2", "f3", "f5", "f6", "f7", "f8", "f12", "f13", "f14", "f15", "f16", "f17", "f18", "f20", "f21")},
        "expires_at": snapshot_at + timedelta(days=10),
    }


def _fullmarket_doc(doc: dict) -> dict:
    mirror = dict(doc)
    mirror["source"] = "eastmoney_etf_spot"
    mirror["source_collection"] = "etf_spot_snapshots"
    mirror["asset_class"] = "etf"
    mirror["security_type"] = "etf"
    return mirror


def _write_data_freshness(
    db: Database,
    *,
    count: int,
    mirrored: int,
    date_key: str,
    trade_date: str,
    elapsed: float,
    error: str = "",
) -> None:
    now = naive_market_now("A")
    lane = str(get_task_env("SIGNALS_CURRENT_SYNC_LANE", "") or "")
    mode = "realtime" if get_task_env("SIGNALS_CURRENT_SYNC_MARKET") or lane else "postmarket"
    status = "fresh" if count > 0 else "empty"
    db["data_freshness"].update_one(
        {"domain": "etf_spot", "market": "A", "mode": mode, "collection": "etf_spot_snapshots"},
        {"$set": {
            "domain": "etf_spot",
            "market": "A",
            "mode": mode,
            "lane": lane,
            "collection": "etf_spot_snapshots",
            "freshness": status,
            "latest_dt": trade_date,
            "as_of": trade_date,
            "date_key": date_key,
            "updated_at": now,
            "stale_reason": "" if count > 0 else (error or "etf_spot_empty"),
            "count": count,
            "mirrored": mirrored,
            "elapsed_seconds": round(elapsed, 3),
        }},
        upsert=True,
    )


def sync_etf_spot_snapshot(db: Database, proxy_url: str = None) -> dict:
    del proxy_url
    started = time.monotonic()
    now = naive_market_now("A")
    trade_date = trading_day_key("A", now=now, open_time=QUOTE_TRADING_DAY_OPEN)
    date_key = trading_day_key("A", now=now, compact=True, open_time=QUOTE_TRADING_DAY_OPEN)
    try:
        rows = fetch_etf_spot_rows(db)
    except Exception as exc:
        elapsed = time.monotonic() - started
        error = f"{exc.__class__.__name__}: {exc}"
        _write_data_freshness(db, count=0, mirrored=0, date_key=date_key, trade_date=trade_date, elapsed=elapsed, error=error)
        observe_eastmoney(
            db,
            endpoint="etf_spot_snapshot",
            domain="market_data",
            request_count=0,
            returned_count=0,
            elapsed_ms=elapsed * 1000,
            error=error,
        )
        logger.warning("ETF spot snapshot failed: %s", error)
        return {
            "module": "etf_spot_snapshot",
            "status": "degraded",
            "count": 0,
            "mirrored": 0,
            "inserted": 0,
            "modified": 0,
            "target_collection": "etf_spot_snapshots",
            "date_key": date_key,
            "elapsed": elapsed,
            "reason": error,
        }

    docs = [
        doc
        for row in rows
        for doc in [_doc_from_row(row, date_key=date_key, trade_date=trade_date, snapshot_at=now)]
        if doc is not None
    ]
    valid_quote_count = sum(
        1
        for doc in docs
        if all((doc.get(key) or 0) > 0 for key in ("price", "open", "high", "low"))
    )

    inserted = 0
    modified = 0
    mirrored_inserted = 0
    mirrored_modified = 0
    if docs:
        result = db["etf_spot_snapshots"].bulk_write(
            [UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True) for doc in docs],
            ordered=False,
        )
        inserted = int(result.upserted_count)
        modified = int(result.modified_count)

        mirror_docs = [_fullmarket_doc(doc) for doc in docs]
        mirror_result = db["fullmarket_spot_snapshots"].bulk_write(
            [UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True) for doc in mirror_docs],
            ordered=False,
        )
        mirrored_inserted = int(mirror_result.upserted_count)
        mirrored_modified = int(mirror_result.modified_count)

    elapsed = time.monotonic() - started
    _write_data_freshness(db, count=len(docs), mirrored=len(docs), date_key=date_key, trade_date=trade_date, elapsed=elapsed)
    request_count = max(1, math.ceil(len(rows) / ETF_SPOT_PAGE_SIZE)) if rows else 0
    observe_eastmoney(
        db,
        endpoint="etf_spot_snapshot",
        domain="market_data",
        request_count=request_count,
        returned_count=len(rows),
        elapsed_ms=elapsed * 1000,
        http_status=200 if rows else None,
        rc=0 if rows else None,
        batch_size=ETF_SPOT_PAGE_SIZE,
        extra={"date_key": date_key},
    )
    logger.info(
        "ETF spot snapshot: %d/%d rows, inserted=%d modified=%d mirrored_inserted=%d mirrored_modified=%d",
        len(docs),
        len(rows),
        inserted,
        modified,
        mirrored_inserted,
        mirrored_modified,
    )
    return {
        "module": "etf_spot_snapshot",
        "status": "ok" if docs else "degraded",
        "count": len(docs),
        "valid_quote_count": valid_quote_count,
        "raw_count": len(rows),
        "mirrored": len(docs),
        "inserted": inserted,
        "modified": modified,
        "mirrored_inserted": mirrored_inserted,
        "mirrored_modified": mirrored_modified,
        "target_collection": "etf_spot_snapshots",
        "mirror_collection": "fullmarket_spot_snapshots",
        "date_key": date_key,
        "trade_date": trade_date,
        "elapsed": elapsed,
    }
