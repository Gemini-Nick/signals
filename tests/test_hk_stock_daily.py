# -*- coding: utf-8 -*-
from __future__ import annotations

import pandas as pd

from signals.sync.modules.hk_stock_daily import (
    _docs_from_hk_daily_df,
    _extract_hk_codes_from_hkex_frame,
    _extract_hk_codes_from_spot,
    _fetch_one_hk_daily,
    _hk_history_sources,
    _hk_universe_sources,
    _pure_hk_code,
    _write_daily_docs_batch,
)
import signals.sync.modules.hk_stock_daily as hk_stock_daily_module


def test_pure_hk_code_normalizes_prefixed_and_short_codes():
    assert _pure_hk_code("HK.700") == "00700"
    assert _pure_hk_code("00700") == "00700"


def test_extract_hk_codes_from_spot_filters_delisted_names():
    df = pd.DataFrame([
        {"代码": "00700", "名称": "腾讯控股"},
        {"代码": "700", "名称": "腾讯控股"},
        {"代码": "01234", "名称": "退市样本"},
    ])

    assert _extract_hk_codes_from_spot(df) == ["00700"]


def test_extract_hk_codes_from_hkex_frame_keeps_equity_codes_only():
    df = pd.DataFrame([
        {"Stock Code": "00700", "Name of Securities": "TENCENT", "Category": "Equity"},
        {"Stock Code": "03033", "Name of Securities": "ETF SAMPLE", "Category": "ETF"},
        {"Stock Code": "5", "Name of Securities": "HSBC HOLDINGS", "Category": "Equity"},
        {"Stock Code": "00700", "Name of Securities": "DUP", "Category": "Equity"},
    ])

    assert _extract_hk_codes_from_hkex_frame(df) == ["00700", "00005"]


def test_hk_universe_sources_defaults_to_hkex_first(monkeypatch):
    monkeypatch.delenv("HK_STOCK_DAILY_UNIVERSE_SOURCE", raising=False)
    monkeypatch.delenv("HK_STOCK_DAILY_UNIVERSE_SOURCES", raising=False)

    assert _hk_universe_sources() == ["hkex", "akshare", "cache"]


def test_hk_universe_sources_allows_akshare_alias(monkeypatch):
    monkeypatch.setenv("HK_STOCK_DAILY_UNIVERSE_SOURCE", "eastmoney")

    assert _hk_universe_sources() == ["akshare"]


def test_hk_history_sources_defaults_to_daily_then_tencent(monkeypatch):
    monkeypatch.delenv("HK_STOCK_DAILY_HISTORY_SOURCE", raising=False)
    monkeypatch.delenv("HK_STOCK_DAILY_HISTORY_SOURCES", raising=False)

    assert _hk_history_sources() == ["daily", "tencent", "hist"]


def test_docs_from_hk_daily_df_writes_canonical_bar_docs():
    df = pd.DataFrame([
        {
            "日期": "2026-04-20",
            "开盘": 300.0,
            "最高": 310.0,
            "最低": 298.0,
            "收盘": 308.0,
            "成交量": 123400,
            "成交额": 45670000,
        }
    ])

    docs = _docs_from_hk_daily_df("700", df, "test_source", end_date="20260420")

    assert len(docs) == 1
    assert docs[0]["meta"]["symbol"] == "HK.00700"
    assert docs[0]["meta"]["raw_code"] == "00700"
    assert docs[0]["meta"]["market"] == "HK"
    assert docs[0]["meta"]["freq"] == "日线"
    assert docs[0]["vol"] == 123400


def test_write_daily_docs_batch_inserts_for_timeseries_collection():
    class InsertResult:
        def __init__(self, count):
            self.inserted_ids = list(range(count))

    class FakeBars:
        def __init__(self):
            self.inserted = []

        def find(self, query, projection):
            return []

        def insert_many(self, docs, ordered=False):
            self.inserted.extend(docs)
            return InsertResult(len(docs))

    class FakeSync:
        def __init__(self):
            self.updates = []

        def update_one(self, query, update, upsert=False):
            self.updates.append((query, update, upsert))

    docs = _docs_from_hk_daily_df(
        "700",
        pd.DataFrame([{
            "日期": "2026-04-20",
            "开盘": 300.0,
            "最高": 310.0,
            "最低": 298.0,
            "收盘": 308.0,
            "成交量": 123400,
            "成交额": 45670000,
        }]),
        "test_source",
        end_date="20260420",
    )
    bars = FakeBars()
    sync = FakeSync()

    result = _write_daily_docs_batch(bars, sync, {"00700": docs})

    assert result == {"00700": 1}
    assert len(bars.inserted) == 1
    assert sync.updates[0][1]["$set"]["written"] == 1


def test_write_daily_docs_batch_refreshes_changed_existing_doc():
    class WriteResult:
        def __init__(self, *, inserted_ids=None, deleted_count=0):
            self.inserted_ids = inserted_ids or []
            self.deleted_count = deleted_count

    class FakeBars:
        def __init__(self):
            self.docs = [{
                "_id": "old",
                "dt": pd.Timestamp("2026-05-28").to_pydatetime(),
                "meta": {"symbol": "HK.03750", "freq": "日线"},
                "open": 709.0,
                "high": 709.0,
                "low": 686.0,
                "close": 702.0,
                "vol": 2788087,
                "amount": 1937885920,
            }]

        def find(self, query, projection):
            return list(self.docs)

        def delete_many(self, query):
            ids = set(query["_id"]["$in"])
            before = len(self.docs)
            self.docs = [doc for doc in self.docs if doc["_id"] not in ids]
            return WriteResult(deleted_count=before - len(self.docs))

        def insert_many(self, docs, ordered=False):
            inserted = []
            for idx, doc in enumerate(docs):
                new_doc = dict(doc)
                new_doc["_id"] = f"new-{idx}"
                self.docs.append(new_doc)
                inserted.append(new_doc["_id"])
            return WriteResult(inserted_ids=inserted)

    class FakeSync:
        def update_one(self, *args, **kwargs):
            pass

    docs = _docs_from_hk_daily_df(
        "3750",
        pd.DataFrame([{
            "日期": "2026-05-28",
            "开盘": 709.0,
            "最高": 720.0,
            "最低": 686.0,
            "收盘": 717.0,
            "成交量": 4974563,
            "成交额": 3493710367,
        }]),
        "akshare_stock_hk_daily",
        end_date="20260528",
    )
    bars = FakeBars()

    result = _write_daily_docs_batch(bars, FakeSync(), {"03750": docs})

    assert result == {"03750": 1}
    assert len(bars.docs) == 1
    assert bars.docs[0]["_id"] == "new-0"
    assert bars.docs[0]["close"] == 717.0
    assert bars.docs[0]["high"] == 720.0


def test_fetch_one_hk_daily_falls_back_when_hist_raises(monkeypatch):
    monkeypatch.setenv("HK_STOCK_DAILY_HISTORY_SOURCES", "hist,daily")

    def raise_hist(**kwargs):
        raise ConnectionError("hist down")

    def fake_daily(symbol, adjust):
        assert symbol == "00700"
        assert adjust == "qfq"
        return pd.DataFrame([
            {
                "date": "2026-04-20",
                "open": 300.0,
                "high": 310.0,
                "low": 298.0,
                "close": 308.0,
                "volume": 123400,
                "amount": 45670000,
            }
        ])

    monkeypatch.setattr(hk_stock_daily_module.ak, "stock_hk_hist", raise_hist)
    monkeypatch.setattr(hk_stock_daily_module.ak, "stock_hk_daily", fake_daily)

    docs = _fetch_one_hk_daily("00700", "20260401", "20260420")

    assert len(docs) == 1
    assert docs[0]["meta"]["source"] == "akshare_stock_hk_daily"
