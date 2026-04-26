# -*- coding: utf-8 -*-
"""Populate the persistent signal pool from local trading-system records."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from statistics import mean

from pymongo import UpdateOne
from pymongo.database import Database

logger = logging.getLogger("signals.sync.signal_pool")


def _read_signal_records(db_path: str) -> list[dict]:
    path = Path(db_path)
    if not path.exists():
        return []
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM signal_records").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _pool_status(signal_type: str) -> str:
    text = str(signal_type or "")
    if any(token in text for token in ("卖", "顶", "风险", "死叉", "减仓")):
        return "warning"
    return "candidate"


def _dedupe_key(record: dict) -> str:
    return "|".join([
        str(record.get("symbol") or ""),
        str(record.get("signal_date") or ""),
        str(record.get("signal_type") or ""),
        str(record.get("freq") or ""),
    ])


def _symbol_candidates(symbol: object) -> list[str]:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return []
    pure = raw.split(".", 1)[-1] if "." in raw else raw
    candidates = [raw, pure]
    if pure.isdigit() and len(pure) == 6:
        candidates.extend([
            f"SH.{pure}" if pure.startswith(("5", "6", "9")) else f"SZ.{pure}",
            f"sh{pure}" if pure.startswith(("5", "6", "9")) else f"sz{pure}",
        ])
    return list(dict.fromkeys(candidates))


def _to_doc(record: dict) -> dict:
    doc = {k: v for k, v in record.items() if k != "id"}
    doc["sqlite_id"] = record.get("id")
    doc["dedupe_key"] = _dedupe_key(record)
    doc["pool_status"] = _pool_status(str(record.get("signal_type") or ""))
    doc["source"] = "sqlite.backtest.signal_records"
    doc["updated_at"] = datetime.now()
    details = doc.get("details")
    if isinstance(details, str) and details:
        try:
            doc["details_json"] = json.loads(details)
        except Exception:
            pass
    return doc


def _latest_pool_symbols(db: Database) -> list[str]:
    doc = db["market_pools"].find_one(
        {"pool": "active"},
        {"symbols": 1},
        sort=[("dt", -1), ("updated_at", -1)],
    ) or {}
    if doc.get("symbols"):
        return [str(item) for item in doc["symbols"] if item]

    symbols = []
    for item in db["signals"].find({}, {"symbol": 1}).sort("signal_date", -1).limit(50):
        if item.get("symbol"):
            symbols.append(str(item["symbol"]))
    return list(dict.fromkeys(symbols))


def _latest_daily_bars(db: Database, symbol: str, limit: int = 25) -> list[dict]:
    docs = list(db["bars"].find(
        {
            "meta.symbol": {"$in": _symbol_candidates(symbol)},
            "meta.freq": {"$in": ["daily", "日线", "D", "1d"]},
        },
        {"_id": 0},
    ).sort("dt", -1).limit(limit))
    return list(reversed(docs))


def _dt_str(value: object) -> str:
    return str(value.date()) if hasattr(value, "date") else str(value)[:10]


def _float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _generated_signal_docs(db: Database) -> list[dict]:
    docs: list[dict] = []
    now = datetime.now()
    for symbol in _latest_pool_symbols(db):
        bars = _latest_daily_bars(db, symbol)
        if len(bars) < 5:
            continue

        latest = bars[-1]
        prev = bars[-2] if len(bars) >= 2 else None
        close = _float(latest.get("close"))
        prev_close = _float(prev.get("close")) if prev else None
        if close is None or prev_close in (None, 0):
            continue

        closes = [_float(item.get("close")) for item in bars]
        closes = [item for item in closes if item is not None]
        if len(closes) < 5:
            continue

        change_pct = (close - prev_close) / prev_close * 100
        five_day_base = closes[-5]
        five_day_pct = (close - five_day_base) / five_day_base * 100 if five_day_base else 0.0
        ma5 = mean(closes[-5:])
        ma20 = mean(closes[-20:]) if len(closes) >= 20 else None
        prev_ma20 = mean(closes[-21:-1]) if len(closes) >= 21 else None

        signal_type = ""
        pool_status = ""
        reasons: list[str] = []
        score = 50.0

        if change_pct <= -4.0:
            signal_type = "日线预警: 单日跌幅扩大"
            pool_status = "warning"
            reasons.append("daily_drop")
            score = max(0.0, 50.0 + change_pct * 5)
        elif ma20 is not None and prev_ma20 is not None and close < ma20 <= prev_close:
            signal_type = "日线预警: 跌破二十日均线"
            pool_status = "warning"
            reasons.append("ma20_breakdown")
            score = 35.0
        elif close >= ma5 and (ma20 is None or close >= ma20) and five_day_pct >= 3.0:
            signal_type = "日线候选: 活跃池趋势增强"
            pool_status = "candidate"
            reasons.append("trend_strength")
            score = min(95.0, 60.0 + five_day_pct * 2 + max(change_pct, 0))
        else:
            continue

        signal_date = _dt_str(latest.get("dt"))
        doc = {
            "symbol": symbol,
            "signal_date": signal_date,
            "signal_type": signal_type,
            "freq": "日线",
            "pool_status": pool_status,
            "score": round(score, 2),
            "confidence": 0.55,
            "source": "sync.signal_pool.generated",
            "dedupe_key": "|".join([symbol, signal_date, signal_type, "日线"]),
            "details_json": {
                "close": close,
                "prev_close": prev_close,
                "change_pct": round(change_pct, 4),
                "five_day_pct": round(five_day_pct, 4),
                "ma5": round(ma5, 4),
                "ma20": round(ma20, 4) if ma20 is not None else None,
                "reasons": reasons,
            },
            "updated_at": now,
        }
        docs.append(doc)
    return docs


def _write_data_freshness(db: Database, count: int, latest_dt: str | None) -> None:
    now = datetime.now()
    db["data_freshness"].update_one(
        {"domain": "signal", "market": "A", "mode": "historical", "collection": "signals"},
        {"$set": {
            "domain": "signal",
            "market": "A",
            "mode": "historical",
            "collection": "signals",
            "freshness": "fresh" if count else "empty",
            "latest_dt": latest_dt,
            "as_of": latest_dt,
            "updated_at": now,
            "stale_reason": "" if count else "signal_pool_empty",
        }},
        upsert=True,
    )


def _latest_signal_date(db: Database) -> str | None:
    doc = db["signals"].find_one(
        {"signal_date": {"$exists": True}},
        {"signal_date": 1, "updated_at": 1},
        sort=[("signal_date", -1), ("updated_at", -1)],
    ) or {}
    value = doc.get("signal_date") or doc.get("updated_at")
    return _dt_str(value) if value else None


def sync_signal_pool(db: Database, proxy_url: str = None) -> dict:
    """Populate Mongo `signals` from migrated records and generated pool signals."""
    del proxy_url
    import config

    records = _read_signal_records(config.BACKTEST_DB_PATH)
    ops = []
    latest_dt = None
    for record in records:
        doc = _to_doc(record)
        signal_date = str(doc.get("signal_date") or "")[:10]
        if signal_date:
            latest_dt = max(latest_dt or signal_date, signal_date)
        ops.append(UpdateOne(
            {"dedupe_key": doc["dedupe_key"]},
            {"$set": doc, "$setOnInsert": {"created_from_sync_at": datetime.now()}},
            upsert=True,
        ))

    generated_docs = _generated_signal_docs(db)
    for doc in generated_docs:
        signal_date = str(doc.get("signal_date") or "")[:10]
        if signal_date:
            latest_dt = max(latest_dt or signal_date, signal_date)
        ops.append(UpdateOne(
            {"dedupe_key": doc["dedupe_key"]},
            {"$set": doc, "$setOnInsert": {"created_from_sync_at": datetime.now()}},
            upsert=True,
        ))

    if not ops:
        existing = db["signals"].count_documents({})
        _write_data_freshness(db, existing, _latest_signal_date(db))
        return {"inserted": 0, "modified": 0, "matched": 0, "generated": 0, "count": existing, "target_collection": "signals"}

    result = db["signals"].bulk_write(ops, ordered=False)
    existing = db["signals"].count_documents({})
    _write_data_freshness(db, existing, latest_dt)
    inserted = int(result.upserted_count)
    modified = int(result.modified_count)
    logger.info(
        "signal pool populated: %d inserted, %d modified, %d matched, %d generated",
        inserted,
        modified,
        result.matched_count,
        len(generated_docs),
    )
    return {
        "inserted": inserted,
        "modified": modified,
        "matched": int(result.matched_count),
        "generated": len(generated_docs),
        "migrated": len(records),
        "count": existing,
        "target_collection": "signals",
    }
