# -*- coding: utf-8 -*-
"""Import existing local disk caches into Mongo collections.

This module does not fetch external providers. It makes the gateway usable
after install/restart by moving already-known local cache artifacts into the
canonical/snapshot collections the runtime reads.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.market_time import naive_market_now

logger = logging.getLogger("signals.sync.cache_preheat")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _freq_from_name(freq: str) -> str:
    return {
        "daily": "daily",
        "weekly": "weekly",
        "monthly": "monthly",
        "15m": "15m",
        "30m": "30m",
    }.get(freq, freq)


def _now() -> datetime:
    return naive_market_now("A")


def _today_str() -> str:
    return _now().strftime("%Y-%m-%d")


def _import_kline_files(db: Database, root: Path) -> int:
    docs = []
    for path in (root / ".data" / "cache").glob("kline_*.json"):
        stem = path.stem
        if not stem.startswith("kline_"):
            continue
        try:
            code, freq = stem[len("kline_"):].rsplit("_", 1)
        except ValueError:
            continue
        try:
            records = _load_json(path)
        except Exception as e:
            logger.warning("skip kline cache %s: %s", path, e)
            continue
        freq = _freq_from_name(freq)
        if db["bars"].count_documents({"meta.symbol": code, "meta.freq": freq}, limit=1):
            continue
        for record in records:
            dt = pd.to_datetime(record.get("dt"), errors="coerce")
            if pd.isna(dt):
                continue
            docs.append({
                "dt": dt.to_pydatetime(),
                "meta": {"symbol": code, "freq": freq},
                "open": float(record.get("open", 0) or 0),
                "high": float(record.get("high", 0) or 0),
                "low": float(record.get("low", 0) or 0),
                "close": float(record.get("close", 0) or 0),
                "vol": int(float(record.get("vol", 0) or 0)),
                "amount": int(float(record.get("amount", 0) or 0)),
                "source": "disk_kline_cache",
                "updated_at": _now(),
            })
    if docs:
        db["bars"].insert_many(docs, ordered=False)
    return len(docs)


def _import_stock_names(db: Database, root: Path) -> int:
    path = root / ".cache" / "name_to_code.json"
    if not path.exists():
        return 0
    data = _load_json(path)
    ops = []
    for name, code in data.items():
        doc = {
            "name": name,
            "code": code,
            "symbol": code,
            "dt": _today_str(),
            "updated_at": _now(),
            "source": "disk_name_to_code",
        }
        ops.append(UpdateOne({"name": name}, {"$set": doc}, upsert=True))
    if ops:
        db["stock_names"].bulk_write(ops, ordered=False)
    return len(ops)


def _import_constituents(db: Database, root: Path) -> int:
    board_ops = []
    concept_ops = []
    for path in (root / ".data" / "cache").glob("stocks_*.json"):
        name = path.stem[len("stocks_"):]
        try:
            symbols = _load_json(path)
        except Exception as e:
            logger.warning("skip constituents cache %s: %s", path, e)
            continue
        doc = {
            "board_name": name,
            "concept_name": name,
            "symbols": symbols,
            "stock_count": len(symbols),
            "dt": _today_str(),
            "updated_at": _now(),
            "source": "disk_stocks_cache",
        }
        board_ops.append(UpdateOne({"board_name": name}, {"$set": doc}, upsert=True))
        concept_ops.append(UpdateOne({"concept_name": name}, {"$set": doc}, upsert=True))
    for path in (root / ".data" / "cache").glob("social_concept_stocks_*.json"):
        name = path.stem[len("social_concept_stocks_"):]
        try:
            payload = _load_json(path)
        except Exception as e:
            logger.warning("skip social concept constituents cache %s: %s", path, e)
            continue
        stocks = payload.get("stocks") if isinstance(payload, dict) else []
        symbols = []
        stock_names = {}
        for item in stocks or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            symbols.append(code)
            stock_names[code] = str(item.get("name") or "").strip()
        if not symbols:
            continue
        doc = {
            "concept_name": name,
            "symbols": symbols,
            "stock_names": stock_names,
            "stock_count": len(symbols),
            "dt": _today_str(),
            "updated_at": _now(),
            "source": "disk_social_concept_stocks",
            "source_code": payload.get("code") if isinstance(payload, dict) else "",
        }
        concept_ops.append(UpdateOne({"concept_name": name}, {"$set": doc}, upsert=True))
    if board_ops:
        db["board_constituents"].bulk_write(board_ops, ordered=False)
    if concept_ops:
        db["concept_constituents"].bulk_write(concept_ops, ordered=False)
    return len(board_ops) + max(0, len(concept_ops) - len(board_ops))


def _import_social(db: Database, root: Path) -> int:
    count = 0
    weibo = root / ".data" / "cache" / "social_weibo_sentiment.json"
    if weibo.exists():
        data = _load_json(weibo)
        dt = datetime.fromtimestamp(data.get("ts", _now().timestamp()))
        db["social_weibo"].update_one(
            {"_id": "weibo_sentiment_latest"},
            {"$set": {"dt": dt, "updated_at": _now(), "source": "disk_social_weibo", **data}},
            upsert=True,
        )
        ops = []
        for item in data.get("stocks", []):
            name = item.get("name")
            if not name:
                continue
            doc = {
                "symbol": name,
                "name": name,
                "heat_score": item.get("rate", 0),
                "heat_grade": "cached",
                "tag": "weibo_sentiment",
                "dt": dt,
                "updated_at": _now(),
                "source": "disk_social_weibo",
            }
            ops.append(UpdateOne({"symbol": name}, {"$set": doc}, upsert=True))
        if ops:
            db["social_heat"].bulk_write(ops, ordered=False)
            count += len(ops)

    concepts = root / ".data" / "cache" / "social_concept_list.json"
    if concepts.exists():
        data = _load_json(concepts)
        dt = datetime.fromtimestamp(data.get("ts", _now().timestamp()))
        records = data.get("records", [])
        db["social_comment"].update_one(
            {"_id": "concept_list_latest"},
            {"$set": {
                "dt": dt,
                "updated_at": _now(),
                "source": "disk_social_concept_list",
                "records": records,
            }},
            upsert=True,
        )
        count += 1
    return count


def _import_history_docs(db: Database, root: Path) -> int:
    count = 0
    files = [
        (root / ".data" / "cache" / "rotation_history.json", "rotation_history"),
    ]
    for path, collection in files:
        if not path.exists():
            continue
        data = _load_json(path)
        db[collection].update_one(
            {"_id": path.stem},
            {"$set": {
                "dt": _today_str(),
                "updated_at": _now(),
                "source": "disk_cache",
                "data": data,
            }},
            upsert=True,
        )
        count += 1
    cluster_dir = root / ".data" / "cache" / "cluster_history"
    if cluster_dir.exists():
        for path in cluster_dir.glob("*.json"):
            db["cluster_history"].update_one(
                {"_id": path.stem},
                {"$set": {
                    "dt": path.stem,
                    "updated_at": _now(),
                    "source": "disk_cache",
                    "data": _load_json(path),
                }},
                upsert=True,
            )
            count += 1
    return count


def sync_cache_preheat(db: Database, proxy_url: str = None) -> dict:
    root = _repo_root()
    result = {
        "bars": _import_kline_files(db, root),
        "stock_names": _import_stock_names(db, root),
        "constituents": _import_constituents(db, root),
        "social": _import_social(db, root),
        "history": _import_history_docs(db, root),
    }
    db["data_freshness"].update_one(
        {"domain": "cache", "market": "A", "mode": "historical", "collection": "disk_preheat"},
        {"$set": {
            "domain": "cache",
            "market": "A",
            "mode": "historical",
            "collection": "disk_preheat",
            "latest_dt": _today_str(),
            "as_of": _today_str(),
            "stale_reason": "",
            "updated_at": _now(),
            "result": result,
        }},
        upsert=True,
    )
    logger.info("cache preheat imported: %s", result)
    return result
