# -*- coding: utf-8 -*-
"""Full-market Eastmoney spot snapshot used to de-duplicate postmarket quote calls."""
from __future__ import annotations

import logging
import math
import os
import time
from datetime import timedelta

import requests
from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.core.trading_dates import trading_day_key
from signals.sync.eastmoney_observer import observe_eastmoney
from signals.sync.provider_limits import provider_call

logger = logging.getLogger("signals.sync.fullmarket_spot_snapshot")

_EM_CLIST_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
_EM_FIELDS = "f2,f3,f4,f5,f6,f7,f8,f12,f13,f14,f15,f16,f17,f18,f20,f21"
_EM_FS = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Connection": "close",
}


def _page_size() -> int:
    try:
        # Eastmoney currently caps clist responses at 100 rows even when pz is larger.
        return max(50, min(100, int(os.getenv("FULLMARKET_SPOT_PAGE_SIZE", "100"))))
    except (TypeError, ValueError):
        return 100


def _timeout() -> float:
    try:
        return max(3.0, float(os.getenv("FULLMARKET_SPOT_TIMEOUT", "10")))
    except (TypeError, ValueError):
        return 10.0


def _params(page: int, page_size: int) -> dict[str, str]:
    return {
        "pn": str(page),
        "pz": str(page_size),
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": _EM_FS,
        "fields": _EM_FIELDS,
    }


def _number(value, default: float | None = None) -> float | None:
    if value in (None, "", "-"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _symbol_for_code(code: str) -> str:
    raw = str(code or "").strip()
    if raw.startswith(("5", "6", "9")):
        return f"SH.{raw}"
    if raw.startswith(("4", "8")):
        return f"BJ.{raw}"
    return f"SZ.{raw}"


def fetch_eastmoney_spot_rows(db: Database | None = None) -> list[dict]:
    page_size = _page_size()
    timeout = _timeout()

    def _fetch_pages() -> list[dict]:
        rows: list[dict] = []
        with requests.Session() as session:
            session.trust_env = False

            def _request(page: int) -> dict:
                response = session.get(
                    _EM_CLIST_URL,
                    params=_params(page, page_size),
                    headers=_HEADERS,
                    timeout=timeout,
                )
                response.raise_for_status()
                return response.json()

            first_payload = _request(1)
            first_data = first_payload.get("data") or {}
            total = int(first_data.get("total") or 0)
            rows.extend(first_data.get("diff") or [])
            page_count = max(1, math.ceil(total / page_size))
            for page in range(2, page_count + 1):
                payload = _request(page)
                rows.extend((payload.get("data") or {}).get("diff") or [])
        return rows

    return provider_call("eastmoney", "fullmarket_spot_snapshot", _fetch_pages, db=db)


def _doc_from_row(row: dict, *, date_key: str, trade_date: str, snapshot_at) -> dict | None:
    code = str(row.get("f12") or "").strip()
    if not code:
        return None
    latest = _number(row.get("f2"))
    return {
        "_id": f"{date_key}:{code}",
        "date_key": date_key,
        "trade_date": trade_date,
        "snapshot_at": snapshot_at,
        "source": "eastmoney_push2delay_clist",
        "code": code,
        "symbol": _symbol_for_code(code),
        "name": str(row.get("f14") or ""),
        "market_id": row.get("f13"),
        "latest": latest,
        "price": latest,
        "change_pct": _number(row.get("f3")),
        "change": _number(row.get("f4")),
        "vol": _number(row.get("f5"), 0.0),
        "amount": _number(row.get("f6"), 0.0),
        "amplitude_pct": _number(row.get("f7")),
        "turnover_pct": _number(row.get("f8")),
        "high": _number(row.get("f15")),
        "low": _number(row.get("f16")),
        "open": _number(row.get("f17")),
        "prev_close": _number(row.get("f18")),
        "market_cap": _number(row.get("f20")),
        "float_market_cap": _number(row.get("f21")),
        "raw": {key: row.get(key) for key in _EM_FIELDS.split(",")},
        "expires_at": snapshot_at + timedelta(days=10),
    }


def _write_data_freshness(
    db: Database,
    *,
    count: int,
    date_key: str,
    trade_date: str,
    elapsed: float,
    error: str = "",
) -> None:
    now = naive_market_now("A")
    lane = os.getenv("SIGNALS_CURRENT_SYNC_LANE", "")
    mode = "realtime" if os.getenv("SIGNALS_CURRENT_SYNC_MARKET") or lane else "postmarket"
    status = "fresh" if count > 0 else "empty"
    db["data_freshness"].update_one(
        {"domain": "spot", "market": "A", "mode": mode, "collection": "fullmarket_spot_snapshots"},
        {"$set": {
            "domain": "spot",
            "market": "A",
            "mode": mode,
            "lane": lane,
            "collection": "fullmarket_spot_snapshots",
            "freshness": status,
            "latest_dt": trade_date,
            "as_of": trade_date,
            "date_key": date_key,
            "updated_at": now,
            "stale_reason": "" if count > 0 else (error or "fullmarket_spot_empty"),
            "count": count,
            "elapsed_seconds": round(elapsed, 3),
        }},
        upsert=True,
    )


def sync_fullmarket_spot_snapshot(db: Database, proxy_url: str = None) -> dict:
    del proxy_url
    started = time.monotonic()
    now = naive_market_now("A")
    trade_date = trading_day_key("A", now=now)
    date_key = trading_day_key("A", now=now, compact=True)
    try:
        rows = fetch_eastmoney_spot_rows(db)
    except Exception as exc:
        elapsed = time.monotonic() - started
        error = f"{exc.__class__.__name__}: {exc}"
        _write_data_freshness(db, count=0, date_key=date_key, trade_date=trade_date, elapsed=elapsed, error=error)
        observe_eastmoney(
            db,
            endpoint="fullmarket_spot_snapshot",
            domain="market_data",
            request_count=0,
            returned_count=0,
            elapsed_ms=elapsed * 1000,
            error=error,
        )
        logger.warning("fullmarket spot snapshot failed: %s", error)
        return {
            "module": "fullmarket_spot_snapshot",
            "status": "degraded",
            "count": 0,
            "inserted": 0,
            "modified": 0,
            "target_collection": "fullmarket_spot_snapshots",
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

    inserted = 0
    modified = 0
    if docs:
        ops = [UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True) for doc in docs]
        result = db["fullmarket_spot_snapshots"].bulk_write(ops, ordered=False)
        inserted = int(result.upserted_count)
        modified = int(result.modified_count)

    elapsed = time.monotonic() - started
    _write_data_freshness(db, count=len(docs), date_key=date_key, trade_date=trade_date, elapsed=elapsed)
    page_size = _page_size()
    request_count = max(1, math.ceil(len(rows) / page_size)) if rows else 0
    observe_eastmoney(
        db,
        endpoint="fullmarket_spot_snapshot",
        domain="market_data",
        request_count=request_count,
        returned_count=len(rows),
        elapsed_ms=elapsed * 1000,
        http_status=200 if rows else None,
        rc=0 if rows else None,
        batch_size=page_size,
        extra={"date_key": date_key},
    )
    logger.info("fullmarket spot snapshot: %d rows, inserted=%d modified=%d", len(docs), inserted, modified)
    return {
        "module": "fullmarket_spot_snapshot",
        "status": "ok" if docs else "degraded",
        "count": len(docs),
        "inserted": inserted,
        "modified": modified,
        "target_collection": "fullmarket_spot_snapshots",
        "date_key": date_key,
        "trade_date": trade_date,
        "elapsed": elapsed,
    }
