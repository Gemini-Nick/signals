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
        self.deleted = []

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

    def delete_many(self, query):
        self.deleted.append(query)


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


def test_stock_daily_replace_daily_docs_uses_meta_only_delete():
    bars = _Collection()
    sync = _Collection()
    docs = [{
        "dt": datetime(2026, 4, 28),
        "meta": {"symbol": "600001", "freq": "日线", "source": "tencent"},
        "open": 10,
        "high": 11,
        "low": 9,
        "close": 10.5,
        "vol": 100,
        "amount": 1000,
    }]

    written = stock_daily._replace_daily_docs_batch(
        bars,
        sync,
        {"600001": docs},
        source="unit_test_repair",
    )

    assert written == {"600001": 1}
    assert bars.deleted == [{"meta.symbol": "600001", "meta.freq": "日线"}]
    assert bars.inserted[0]["close"] == 10.5
    cursor = sync.docs["stock_daily:600001"]
    assert cursor["source"] == "unit_test_repair"
    assert cursor["repair_mode"] == "replace_daily_symbol"


def test_stock_daily_batch_today_candidates_skip_cn_labor_day_gap():
    candidates = stock_daily._batch_today_candidates(
        ["600001", "600002", "600003"],
        {
            "600001": datetime(2026, 4, 30),
            "600002": datetime(2026, 4, 29),
            "600003": datetime(2026, 5, 6),
        },
        "20260506",
    )

    assert candidates == ["600001"]


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


def test_stock_daily_empty_provider_docs_do_not_count_as_covered(monkeypatch):
    db = _DB()
    monkeypatch.setenv("STOCK_DAILY_BATCH_TODAY_ENABLED", "false")
    monkeypatch.setattr(stock_daily, "_get_stock_codes", lambda _db: (["920118"], "all"))
    monkeypatch.setattr(stock_daily, "_latest_daily_dates_by_symbol", lambda _db, _codes: {})
    monkeypatch.setattr(stock_daily, "_progress_interval", lambda: 1)
    monkeypatch.setattr(stock_daily, "_BATCH_WORKERS", 1)
    monkeypatch.setattr(stock_daily, "_CALL_INTERVAL", 0)
    monkeypatch.setattr(stock_daily, "_stock_daily_providers_all_cooling", lambda _db: False)
    monkeypatch.setattr(stock_daily, "_sync_one_stock", lambda *args, **kwargs: [])

    result = stock_daily.sync_stock_daily(db)

    assert result["status"] == "partial"
    assert result["errors"] == 1
    assert result["covered_codes"] == 0
    assert result["coverage_pct"] == 0
    assert result["sample_errors"] == [("920118", "empty_docs")]


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
    monkeypatch.setattr(stock_daily, "naive_market_now", lambda _market: datetime(2026, 5, 6, 18, 0, 0))
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
        {"600001": datetime(2026, 4, 30), "600002": datetime(2026, 4, 30)},
        "20260506",
    )

    assert set(docs) == {"600001"}
    assert docs["600001"][0]["source"] == "eastmoney_spot_clist_batch"
    assert docs["600001"][0]["meta"]["quality"] == "provisional_close"
    assert docs["600001"][0]["meta"]["source_type"] == "direct_quote_ohlcv"
    assert docs["600001"][0]["prev_close"] == 10.0
    assert docs["600001"][0]["change_pct"] == 5.0
    assert docs["600001"][0]["vol"] == 100000
    assert docs["600001"][0]["meta"]["volume_unit"] == "shares"
    assert docs["600001"][0]["meta"]["source_volume_unit"] == "hands"
    assert "fallback=1" in reason


def test_stock_daily_batch_today_refreshes_existing_provider_current_day(monkeypatch):
    db = _DB()
    current_doc = {
        "dt": datetime(2026, 5, 6),
        "meta": {"symbol": "600001", "freq": "日线", "source": "tencent"},
        "close": 9.8,
    }

    def fake_current_refresh(_db, codes, end_date):
        assert codes == ["600001"]
        assert end_date == "20260506"
        return ["600001"]

    monkeypatch.setattr(stock_daily, "naive_market_now", lambda _market: datetime(2026, 5, 6, 18, 0, 0))
    monkeypatch.setattr(stock_daily, "_current_daily_quote_refresh_candidates", fake_current_refresh)
    monkeypatch.setattr(stock_daily, "_previous_daily_close_by_symbol", lambda _db, _codes, _end_date: {"600001": 10.0})
    monkeypatch.setattr(
        stock_daily,
        "_fetch_eastmoney_spot_batch_df",
        lambda _db, _end_date: pd.DataFrame([{
            "代码": "600001",
            "_pure_code": "600001",
            "今开": 10.1,
            "最高": 10.8,
            "最低": 10.0,
            "最新价": 10.5,
            "成交量": 1000,
            "成交额": 100000,
            "昨收": 10.0,
        }]),
    )

    docs, reason = stock_daily._sync_today_from_spot_batch(
        db,
        ["600001"],
        {"600001": datetime(2026, 5, 6)},
        "20260506",
    )

    assert current_doc["close"] == 9.8
    assert set(docs) == {"600001"}
    assert docs["600001"][0]["source"] == "eastmoney_spot_clist_batch"
    assert docs["600001"][0]["close"] == 10.5
    assert "current_refresh=1" in reason


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


def test_stock_daily_keeps_tencent_star_daily_volume_as_shares():
    df = pd.DataFrame([
        {
            "日期": "2026-05-11",
            "开盘": 799,
            "最高": 803,
            "最低": 760.2,
            "收盘": 787.5,
            "成交量": 3326290,
            "成交额": 0,
        }
    ])
    docs = stock_daily._docs_from_daily_df(
        "688802",
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
        end_date="20260511",
    )

    assert docs[0]["vol"] == 3326290
    assert docs[0]["meta"]["volume_unit"] == "shares"
    assert docs[0]["meta"]["source_volume_unit"] == "shares"


def test_snapshot_daily_doc_preserves_raw_source_volume_unit():
    row = pd.Series({
        "今开": 333.33,
        "最高": 335.0,
        "最低": 318.1,
        "最新价": 331.92,
        "成交量": 46423200,
        "_volume_unit": "shares",
        "_source_vol": 464232,
        "_source_volume_unit": "hands",
        "成交额": 15270518541,
        "昨收": 326.3,
    })

    doc = stock_daily._snapshot_daily_doc("300394", row, "20260511")

    assert doc["vol"] == 46423200
    assert doc["meta"]["source_vol"] == 464232
    assert doc["meta"]["source_volume_unit"] == "hands"


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
