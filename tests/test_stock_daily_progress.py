# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

import pandas as pd

from signals.sync.modules import stock_daily


class _InsertResult:
    def __init__(self, count: int):
        self.inserted_ids = list(range(count))


class _Collection:
    def __init__(self):
        self.docs = {}
        self.inserted = []

    def find(self, query=None, projection=None):
        return []

    def update_one(self, query, update, upsert=False):
        key = query.get("_id")
        doc = dict(self.docs.get(key, {"_id": key}))
        doc.update(update.get("$set", {}))
        for inc_key, inc_value in (update.get("$inc") or {}).items():
            doc[inc_key] = int(doc.get(inc_key) or 0) + int(inc_value)
        self.docs[key] = doc

    def insert_many(self, docs, ordered=False):
        self.inserted.extend(docs)
        return _InsertResult(len(docs))


class _DB(dict):
    def __getitem__(self, name):
        if name not in self:
            self[name] = _Collection()
        return dict.__getitem__(self, name)


def test_stock_daily_end_date_skips_cn_labor_day_holiday():
    assert stock_daily._stock_daily_end_date_key(datetime(2026, 5, 1, 16, 30)) == "20260430"


def test_stock_daily_filters_holiday_rows_before_cursor_update():
    bars = _Collection()
    sync = _Collection()

    written = stock_daily._write_daily_docs_batch(
        bars,
        sync,
        {
            "600001": [{
                "dt": datetime(2026, 5, 1),
                "meta": {"symbol": "600001", "freq": "日线"},
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "vol": 1,
                "amount": 1,
            }]
        },
    )

    assert written == {}
    assert bars.inserted == []
    assert sync.docs == {}


def test_stock_daily_writes_progress_cursor(monkeypatch):
    db = _DB()
    monkeypatch.setattr(stock_daily, "_get_stock_codes", lambda _db: (["600001", "600002"], "all"))
    monkeypatch.setattr(stock_daily, "_progress_interval", lambda: 1)
    monkeypatch.setattr(stock_daily, "_BATCH_WORKERS", 1)
    monkeypatch.setattr(stock_daily, "_CALL_INTERVAL", 0)
    monkeypatch.setattr(stock_daily, "_stock_daily_providers_all_cooling", lambda _db: False)

    def fake_sync_one(code, start, end, proxy_url=None):
        return [{
            "dt": datetime(2026, 4, 28),
            "meta": {"symbol": code, "freq": "日线"},
            "open": 1,
            "high": 1,
            "low": 1,
            "close": 1,
            "vol": 1,
            "amount": 1,
        }]

    monkeypatch.setattr(stock_daily, "_sync_one_stock", fake_sync_one)

    result = stock_daily.sync_stock_daily(db)
    progress = db["sync_log"].docs["stock_daily:progress:_meta"]

    assert result["processed"] == 2
    assert result["progress_pct"] == 100
    assert progress["status"] == "ok"
    assert progress["processed"] == 2
    assert progress["total"] == 2
    assert progress["remaining"] == 0
    assert progress["latest_symbol"] in {"600001", "600002"}
    assert progress["inserted_per_min"] >= 0


def test_stock_daily_uses_bars_latest_to_skip_network(monkeypatch):
    db = _DB()
    calls = []
    monkeypatch.setattr(stock_daily, "naive_market_now", lambda _market: datetime(2026, 4, 28, 18, 0, 0))
    monkeypatch.setattr(stock_daily, "_get_stock_codes", lambda _db: (["600001"], "all"))
    monkeypatch.setattr(stock_daily, "_latest_daily_dates_by_symbol", lambda _db, _codes: {"600001": datetime(2026, 4, 28)})
    monkeypatch.setattr(stock_daily, "_progress_interval", lambda: 1)
    monkeypatch.setattr(stock_daily, "_CALL_INTERVAL", 0)
    monkeypatch.setattr(stock_daily, "_stock_daily_providers_all_cooling", lambda _db: False)

    def fake_sync_one(*args, **kwargs):
        calls.append(args)
        return []

    monkeypatch.setattr(stock_daily, "_sync_one_stock", fake_sync_one)

    result = stock_daily.sync_stock_daily(db)

    assert calls == []
    assert result["skipped"] == 1
    assert result["errors"] == 0


def test_stock_daily_batch_today_skips_single_symbol_fetch(monkeypatch):
    db = _DB()
    calls = []
    monkeypatch.setattr(stock_daily, "naive_market_now", lambda _market: datetime(2026, 4, 29, 18, 0, 0))
    monkeypatch.setattr(stock_daily, "_get_stock_codes", lambda _db: (["600001", "600002"], "all"))
    monkeypatch.setattr(stock_daily, "_latest_daily_dates_by_symbol", lambda _db, _codes: {"600001": datetime(2026, 4, 28)})
    monkeypatch.setattr(stock_daily, "_progress_interval", lambda: 1)
    monkeypatch.setattr(stock_daily, "_BATCH_WORKERS", 1)
    monkeypatch.setattr(stock_daily, "_CALL_INTERVAL", 0)
    monkeypatch.setattr(stock_daily, "_stock_daily_providers_all_cooling", lambda _db: False)

    def fake_batch(_db, _codes, _sync_docs, _end_date):
        return {
            "600001": [{
                "dt": datetime(2026, 4, 29),
                "meta": {"symbol": "600001", "freq": "日线"},
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "vol": 1,
                "amount": 1,
            }]
        }, "test_batch"

    def fake_sync_one(code, start, end, proxy_url=None, db=None):
        calls.append((code, start, end))
        return [{
            "dt": datetime(2026, 4, 29),
            "meta": {"symbol": code, "freq": "日线"},
            "open": 2,
            "high": 2,
            "low": 2,
            "close": 2,
            "vol": 2,
            "amount": 2,
        }]

    monkeypatch.setattr(stock_daily, "_sync_today_from_spot_batch", fake_batch)
    monkeypatch.setattr(stock_daily, "_sync_one_stock", fake_sync_one)

    result = stock_daily.sync_stock_daily(db)

    assert calls == [("600002", "20240429", "20260429")]
    assert result["processed"] == 2
    assert result["batch_today"] == 1
    assert result["batch_today_inserted"] == 1


def test_stock_daily_batch_today_uses_eastmoney_clist_snapshot(monkeypatch):
    db = _DB()
    monkeypatch.setattr(stock_daily, "naive_market_now", lambda _market: datetime(2026, 4, 29, 18, 0, 0))
    monkeypatch.setattr(
        stock_daily,
        "_fetch_eastmoney_spot_batch_df",
        lambda _db, _end_date: pd.DataFrame([
            {
                "代码": "600001",
                "_pure_code": "600001",
                "今开": 10.1,
                "最高": 10.8,
                "最低": 10.0,
                "最新价": 10.5,
                "成交量": 1000,
                "成交额": 100000,
                "昨收": 10.0,
            },
            {
                "代码": "600002",
                "_pure_code": "600002",
                "今开": 9.0,
                "最高": 9.1,
                "最低": 8.8,
                "最新价": 8.9,
                "成交量": 900,
                "成交额": 90000,
                "昨收": 10.0,
            },
        ]),
    )
    monkeypatch.setattr(
        stock_daily,
        "_previous_daily_close_by_symbol",
        lambda _db, _codes, _end_date: {"600001": 10.0, "600002": 8.0},
    )

    docs, reason = stock_daily._sync_today_from_spot_batch(
        db,
        ["600001", "600002"],
        {"600001": datetime(2026, 4, 28), "600002": datetime(2026, 4, 28)},
        "20260429",
    )

    assert set(docs) == {"600001"}
    assert docs["600001"][0]["source"] == "eastmoney_spot_clist_batch"
    assert docs["600001"][0]["vol"] == 100000
    assert docs["600001"][0]["meta"]["volume_unit"] == "shares"
    assert docs["600001"][0]["meta"]["source_volume_unit"] == "hands"
    assert "fallback=1" in reason


def test_stock_daily_batch_today_bootstraps_snapshot_only_codes(monkeypatch):
    db = _DB()
    monkeypatch.setattr(stock_daily, "naive_market_now", lambda _market: datetime(2026, 4, 30, 18, 0, 0))
    monkeypatch.setattr(
        stock_daily,
        "_fetch_eastmoney_spot_batch_df",
        lambda _db, _end_date: pd.DataFrame([
            {
                "代码": "920118",
                "_pure_code": "920118",
                "今开": 16.2,
                "最高": 16.8,
                "最低": 16.1,
                "最新价": 16.5,
                "成交量": 1200,
                "成交额": 198000,
                "昨收": 16.0,
            }
        ]),
    )
    monkeypatch.setattr(stock_daily, "_previous_daily_close_by_symbol", lambda _db, _codes, _end_date: {})

    docs, reason = stock_daily._sync_today_from_spot_batch(
        db,
        ["920118"],
        {},
        "20260430",
    )

    assert set(docs) == {"920118"}
    assert docs["920118"][0]["dt"] == pd.to_datetime("20260430")
    assert docs["920118"][0]["vol"] == 120000
    assert "snapshot_bootstrap=1" in reason


def test_stock_daily_normalizes_provider_daily_volume_units():
    df = pd.DataFrame([
        {
            "日期": "2026-04-29",
            "开盘": 10,
            "最高": 11,
            "最低": 9,
            "收盘": 10.5,
            "成交量": 1234,
            "成交额": 100000,
        }
    ])
    docs = stock_daily._docs_from_daily_df(
        "600001",
        df,
        {
            "dt": "日期",
            "open": "开盘",
            "high": "最高",
            "low": "最低",
            "close": "收盘",
            "vol": "成交量",
            "amount": "成交额",
        },
        "tencent",
        end_date="20260429",
    )

    assert docs[0]["vol"] == 123400
    assert docs[0]["meta"]["volume_unit"] == "shares"
    assert docs[0]["meta"]["source_volume_unit"] == "hands"


def test_stock_daily_defers_shard_when_all_providers_cooling(monkeypatch):
    db = _DB()
    monkeypatch.setattr(stock_daily, "_get_stock_codes", lambda _db: (["600001", "600002"], "all"))
    monkeypatch.setattr(stock_daily, "_latest_daily_dates_by_symbol", lambda _db, _codes: {})
    monkeypatch.setattr(stock_daily, "_stock_daily_providers_all_cooling", lambda _db: True)
    monkeypatch.setattr(stock_daily, "_progress_interval", lambda: 1)
    monkeypatch.setattr(stock_daily, "_BATCH_WORKERS", 1)
    monkeypatch.setattr(stock_daily, "_CALL_INTERVAL", 0)

    result = stock_daily.sync_stock_daily(db)
    progress = db["sync_log"].docs["stock_daily:progress:_meta"]

    assert result["status"] == "partial"
    assert result["errors"] == 0
    assert result["deferred"] == 2
    assert progress["deferred_symbols"] == 2
    assert "all_stock_daily_providers" in result["sample_deferred"][0][1]
