# -*- coding: utf-8 -*-
"""Audit or repair A-share holiday natural-date pollution in Mongo.

Default mode is read-only audit. Use --apply to normalize affected live/cache
documents to the expected trading day.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from pymongo import MongoClient

import config


REALTIME_QUOTE_SOURCES = {
    "eastmoney_push2delay",
    "eastmoney_push2delay_ulist",
    "fullmarket_spot_snapshot",
}

RANKING_COLLECTIONS = (
    "board_ranking",
    "concept_ranking",
    "board_em",
    "board_ths",
    "board_sina",
    "concept_sina",
    "concept_em",
    "concept_ths",
)


def _date_expr(field: str) -> dict[str, Any]:
    return {"$substr": [{"$toString": {"$ifNull": [f"${field}", ""]}}, 0, 10]}


def _date_query(field: str, day: str) -> dict[str, Any]:
    return {"$expr": {"$eq": [_date_expr(field), day]}}


def _any_date_query(day: str, *fields: str) -> dict[str, Any]:
    return {"$or": [_date_query(field, day) for field in fields]}


def _day_start(day: str) -> datetime:
    return datetime.strptime(day, "%Y-%m-%d")


def _trade_minute(day: str) -> datetime:
    return _day_start(day).replace(hour=15, minute=0)


def _print_count(label: str, count: int) -> None:
    print(f"{label}: {count}")


def _repair_quote_snapshots(db, holiday: str, trade_day: str, apply: bool) -> dict[str, int]:
    holiday_query = _any_date_query(holiday, "dt", "trade_date")
    stale_trade_query = {
        "$and": [
            _any_date_query(trade_day, "dt", "trade_date"),
            {"source": {"$in": sorted(REALTIME_QUOTE_SOURCES)}},
            {"stale_reason": "non_current_quote_day"},
        ]
    }
    holiday_count = db["quote_snapshots"].count_documents(holiday_query)
    stale_trade_count = db["quote_snapshots"].count_documents(stale_trade_query)
    if apply:
        repaired_at = datetime.now()
        if holiday_count:
            db["quote_snapshots"].update_many(
                holiday_query,
                {"$set": {
                    "dt": trade_day,
                    "trade_date": trade_day,
                    "freshness": "fresh",
                    "is_stale": False,
                    "stale_reason": "",
                    "holiday_repaired_from": holiday,
                    "holiday_repaired_at": repaired_at,
                }},
            )
        if stale_trade_count:
            db["quote_snapshots"].update_many(
                stale_trade_query,
                {"$set": {
                    "freshness": "fresh",
                    "is_stale": False,
                    "stale_reason": "",
                    "holiday_repaired_at": repaired_at,
                }},
            )
    return {"holiday": holiday_count, "stale_trade_day": stale_trade_count}


def _repair_heat_collection(db, collection: str, holiday: str, trade_day: str, apply: bool) -> int:
    query = _any_date_query(holiday, "trade_date", "dt", "trade_minute")
    rows = list(db[collection].find(query, {"trade_minute": 1}))
    if apply and rows:
        trade_start = _day_start(trade_day)
        for row in rows:
            old_minute = row.get("trade_minute")
            if isinstance(old_minute, datetime):
                repaired_minute = trade_start.replace(hour=old_minute.hour, minute=old_minute.minute)
            else:
                repaired_minute = _trade_minute(trade_day)
            db[collection].update_one(
                {"_id": row["_id"]},
                {"$set": {
                    "dt": trade_start,
                    "trade_date": trade_day,
                    "trade_minute": repaired_minute,
                    "holiday_repaired_from": holiday,
                    "holiday_repaired_at": datetime.now(),
                }},
            )
    return len(rows)


def _repair_fullmarket_spot(db, holiday: str, trade_day: str, apply: bool) -> int:
    holiday_key = holiday.replace("-", "")
    trade_key = trade_day.replace("-", "")
    query = {"$or": [{"date_key": holiday_key}, {"trade_date": holiday}]}
    rows = list(db["fullmarket_spot_snapshots"].find(query))
    if apply:
        for row in rows:
            code = str(row.get("code") or "")
            if not code:
                continue
            repaired = deepcopy(row)
            repaired["_id"] = f"{trade_key}:{code}"
            repaired["date_key"] = trade_key
            repaired["trade_date"] = trade_day
            repaired["holiday_repaired_from"] = holiday
            repaired["holiday_repaired_at"] = datetime.now()
            db["fullmarket_spot_snapshots"].replace_one({"_id": repaired["_id"]}, repaired, upsert=True)
            db["fullmarket_spot_snapshots"].update_one(
                {"_id": row["_id"]},
                {"$set": {"holiday_quarantined": True, "holiday_repaired_to": repaired["_id"]}},
            )
    return len(rows)


def _repair_ranking_collections(db, holiday: str, trade_day: str, apply: bool) -> int:
    holiday_start = _day_start(holiday)
    trade_start = _day_start(trade_day)
    total = 0
    repaired_at = datetime.now()
    for collection in RANKING_COLLECTIONS:
        rows = list(db[collection].find({"dt": holiday_start}, {"_id": 1}))
        total += len(rows)
        if not apply or not rows:
            continue
        ids = [row["_id"] for row in rows]
        if db[collection].count_documents({"dt": trade_start}):
            db[collection].delete_many({"_id": {"$in": ids}})
        else:
            db[collection].update_many(
                {"_id": {"$in": ids}},
                {"$set": {
                    "dt": trade_start,
                    "trade_date": trade_day,
                    "holiday_repaired_from": holiday,
                    "holiday_repaired_at": repaired_at,
                }},
            )
    return total


def _replace_or_delete_doc(db, collection: str, old_id: Any, new_id: Any, update: dict[str, Any], apply: bool) -> int:
    row = db[collection].find_one({"_id": old_id})
    if not row:
        return 0
    if not apply:
        return 1
    if db[collection].find_one({"_id": new_id}):
        db[collection].delete_one({"_id": old_id})
        return 1
    repaired = deepcopy(row)
    repaired["_id"] = new_id
    repaired.update(update)
    repaired["holiday_repaired_from"] = update.get("holiday_repaired_from")
    repaired["holiday_repaired_at"] = update.get("holiday_repaired_at")
    db[collection].replace_one({"_id": new_id}, repaired, upsert=True)
    db[collection].delete_one({"_id": old_id})
    return 1


def _repair_derived_pool_docs(db, holiday: str, trade_day: str, apply: bool) -> int:
    holiday_start = _day_start(holiday)
    trade_start = _day_start(trade_day)
    repaired_at = datetime.now()
    total = 0
    for collection in ("terminal_stock_pool", "terminal_realtime_pool"):
        rows = list(db[collection].find({"dt": holiday_start}, {"_id": 1}))
        total += len(rows)
        if apply and rows:
            db[collection].update_many(
                {"_id": {"$in": [row["_id"] for row in rows]}},
                {"$set": {
                    "dt": trade_start,
                    "trade_date": trade_day,
                    "holiday_repaired_from": holiday,
                    "holiday_repaired_at": repaired_at,
                }},
            )
    total += _replace_or_delete_doc(
        db,
        "market_pools",
        f"active:{holiday}",
        f"active:{trade_day}",
        {
            "dt": trade_day,
            "trade_date": trade_day,
            "holiday_repaired_from": holiday,
            "holiday_repaired_at": repaired_at,
        },
        apply,
    )
    total += _replace_or_delete_doc(
        db,
        "strategy_snapshots",
        f"strategy:{holiday}",
        f"strategy:{trade_day}",
        {
            "as_of": trade_day,
            "holiday_repaired_from": holiday,
            "holiday_repaired_at": repaired_at,
        },
        apply,
    )
    return total


def _repair_freshness(db, holiday: str, trade_day: str, apply: bool) -> int:
    query = {
        "$or": [
            _date_query("as_of", holiday),
            _date_query("latest_dt", holiday),
            {"date_key": holiday.replace("-", "")},
        ]
    }
    rows = list(db["data_freshness"].find(query, {"domain": 1, "collection": 1}))
    if apply:
        for row in rows:
            domain = str(row.get("domain") or "")
            latest_dt = f"{trade_day}T15:00" if domain in {"board", "concept", "board_heat", "chain_heat"} else trade_day
            db["data_freshness"].update_one(
                {"_id": row["_id"]},
                {"$set": {
                    "as_of": trade_day,
                    "latest_dt": latest_dt,
                    "date_key": trade_day.replace("-", ""),
                    "holiday_repaired_from": holiday,
                    "holiday_repaired_at": datetime.now(),
                }},
            )
    return len(rows)


def _repair_stock_daily_cursors(db, trade_day: str, apply: bool) -> int:
    cutoff = _day_start(trade_day) + timedelta(days=1)
    bad_rows = list(db["sync_log"].find(
        {"module": "stock_daily", "symbol": {"$exists": True}, "last_dt": {"$gte": cutoff}},
        {"symbol": 1, "last_dt": 1},
    ))
    if not apply or not bad_rows:
        return len(bad_rows)
    symbols = [row["symbol"] for row in bad_rows if row.get("symbol")]
    latest_by_symbol: dict[str, datetime] = {}
    for row in db["bars"].aggregate([
        {"$match": {"meta.freq": "日线", "meta.symbol": {"$in": symbols}, "dt": {"$lt": cutoff}}},
        {"$group": {"_id": "$meta.symbol", "latest_dt": {"$max": "$dt"}}},
    ]):
        if row.get("_id") and isinstance(row.get("latest_dt"), datetime):
            latest_by_symbol[str(row["_id"])] = row["latest_dt"]
    for row in bad_rows:
        symbol = str(row.get("symbol") or "")
        latest_dt = latest_by_symbol.get(symbol)
        if not latest_dt:
            continue
        db["sync_log"].update_one(
            {"_id": row["_id"]},
            {"$set": {
                "last_dt": latest_dt,
                "holiday_cursor_repaired_from": row.get("last_dt"),
                "holiday_repaired_at": datetime.now(),
            }},
        )
    return len(bad_rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair A-share holiday natural-date pollution.")
    parser.add_argument("--holiday-date", default="2026-05-01")
    parser.add_argument("--trade-date", default="2026-04-30")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    client = MongoClient(config.MONGO_URL, serverSelectionTimeoutMS=3000)
    db = client[config.MONGO_DB_NAME]
    client.admin.command("ping")

    print(f"mode: {'apply' if args.apply else 'audit'}")
    print(f"holiday_date: {args.holiday_date}")
    print(f"trade_date: {args.trade_date}")

    quote = _repair_quote_snapshots(db, args.holiday_date, args.trade_date, args.apply)
    _print_count("quote_snapshots holiday docs", quote["holiday"])
    _print_count("quote_snapshots stale trade-day docs", quote["stale_trade_day"])
    _print_count("fullmarket_spot_snapshots holiday docs", _repair_fullmarket_spot(db, args.holiday_date, args.trade_date, args.apply))
    _print_count("ranking holiday docs", _repair_ranking_collections(db, args.holiday_date, args.trade_date, args.apply))
    _print_count("derived pool holiday docs", _repair_derived_pool_docs(db, args.holiday_date, args.trade_date, args.apply))
    _print_count("board_heat_ticks holiday docs", _repair_heat_collection(db, "board_heat_ticks", args.holiday_date, args.trade_date, args.apply))
    _print_count("chain_heat_snapshots holiday docs", _repair_heat_collection(db, "chain_heat_snapshots", args.holiday_date, args.trade_date, args.apply))
    _print_count("data_freshness holiday docs", _repair_freshness(db, args.holiday_date, args.trade_date, args.apply))
    _print_count("stock_daily cursors after trade date", _repair_stock_daily_cursors(db, args.trade_date, args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
