# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.sync.modules import index_minute, minute_readiness
from signals.core.macro_universe import (
    canonical_macro_industry_etf_symbol,
    macro_a_index_codes,
    macro_industry_etf_name,
    macro_industry_etf_pure_codes,
    macro_industry_etf_symbol_by_code,
    macro_watchlist,
    supports_a_index_minute_cache,
)


class _BulkResult:
    def __init__(self, *, matched_count: int = 0, modified_count: int = 0, upserted_ids: dict | None = None):
        self.matched_count = matched_count
        self.modified_count = modified_count
        self.upserted_ids = upserted_ids or {}


class _FakeCollection:
    def __init__(self, result: _BulkResult):
        self.result = result
        self.ops = []
        self.ordered = None

    def bulk_write(self, ops, ordered=False):
        self.ops.extend(ops)
        self.ordered = ordered
        return self.result


class _DeleteResult:
    def __init__(self, deleted_count: int):
        self.deleted_count = deleted_count


class _InsertManyResult:
    def __init__(self, inserted_ids: list[int]):
        self.inserted_ids = inserted_ids


class _FakeRefreshCollection:
    def __init__(self, deleted_count: int = 0):
        self.deleted_count = deleted_count
        self.deleted_query = None
        self.inserted_docs = []
        self.ordered = None

    def delete_many(self, query):
        self.deleted_query = query
        return _DeleteResult(self.deleted_count)

    def insert_many(self, docs, ordered=False):
        self.inserted_docs.extend(docs)
        self.ordered = ordered
        return _InsertManyResult(list(range(len(docs))))


def test_index_minute_worker_count_is_constrained(monkeypatch):
    monkeypatch.setenv("INDEX_MINUTE_WORKERS", "99")
    assert index_minute._worker_count() == 6

    monkeypatch.setenv("INDEX_MINUTE_WORKERS", "0")
    assert index_minute._worker_count() == 1


def test_index_minute_tail_count_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("INDEX_MINUTE_TAIL_COUNT", raising=False)
    monkeypatch.delenv("INDEX_MINUTE_TAIL_COUNT_5", raising=False)
    monkeypatch.delenv("INDEX_MINUTE_TAIL_COUNT_15", raising=False)
    monkeypatch.delenv("INDEX_MINUTE_TAIL_COUNT_30", raising=False)

    assert index_minute._tail_count_for_freq("5分钟") == 240
    assert index_minute._tail_count_for_freq("15分钟") == 160
    assert index_minute._tail_count_for_freq("30分钟") == 120

    monkeypatch.setenv("INDEX_MINUTE_TAIL_COUNT", "80")
    monkeypatch.setenv("INDEX_MINUTE_TAIL_COUNT_30", "90")
    assert index_minute._tail_count_for_freq("5分钟") == 80
    assert index_minute._tail_count_for_freq("30分钟") == 90


def test_index_minute_public_provider_defaults_to_sina_only(monkeypatch):
    monkeypatch.delenv("INDEX_MINUTE_PROVIDERS", raising=False)
    monkeypatch.delenv("INDEX_MINUTE_TENCENT_FALLBACK", raising=False)

    assert index_minute._minute_providers() == ("sina",)


def test_index_minute_public_provider_can_opt_into_tencent(monkeypatch):
    monkeypatch.delenv("INDEX_MINUTE_PROVIDERS", raising=False)
    monkeypatch.setenv("INDEX_MINUTE_TENCENT_FALLBACK", "true")

    assert index_minute._minute_providers() == ("sina", "tencent")


def test_index_minute_public_provider_honors_explicit_order(monkeypatch):
    monkeypatch.setenv("INDEX_MINUTE_PROVIDERS", "tencent, sina")

    assert index_minute._minute_providers() == ("tencent", "sina")


def test_index_minute_upserts_tail_docs_to_refresh_live_bars():
    dt = datetime(2026, 5, 6, 11, 30)
    doc = {
        "dt": dt,
        "meta": {"symbol": "sh000688", "freq": "30分钟", "source": "sina"},
        "open": 1699.0,
        "high": 1702.0,
        "low": 1698.0,
        "close": 1700.0,
        "vol": 103525700,
        "amount": 9517179000,
    }
    col = _FakeCollection(_BulkResult(matched_count=1, modified_count=1))

    result = index_minute._upsert_tail_docs(col, "sh000688", "30分钟", [doc])

    assert result == {
        "written": 1,
        "inserted": 0,
        "modified": 1,
        "matched": 1,
        "upserted": 0,
        "skipped_existing": 0,
    }
    assert col.ordered is False
    assert len(col.ops) == 1
    op = col.ops[0]
    assert op._filter == {"meta.symbol": "sh000688", "meta.freq": "30分钟", "dt": dt}
    assert op._doc == {"$set": doc}
    assert op._upsert is True


def test_index_minute_replaces_tail_docs_for_timeseries_compat_collection():
    dt = datetime(2026, 5, 6, 11, 30)
    earlier = {
        "dt": dt,
        "meta": {"symbol": "sh000688", "freq": "30分钟", "source": "tencent"},
        "open": 1699.0,
        "high": 1700.0,
        "low": 1698.0,
        "close": 1699.0,
        "vol": 189909,
        "amount": 9517179,
    }
    latest = {**earlier, "meta": {**earlier["meta"], "source": "sina"}, "close": 1700.0, "vol": 103525700}
    col = _FakeRefreshCollection(deleted_count=1)

    result = index_minute._replace_tail_docs(col, "sh000688", "30分钟", [earlier, latest])

    assert result == {"written": 1, "inserted": 1, "deleted": 1, "skipped_existing": 0}
    assert col.deleted_query == {"meta.symbol": "sh000688", "meta.freq": "30分钟", "dt": {"$in": [dt]}}
    assert col.inserted_docs == [latest]
    assert col.ordered is False


def test_index_minute_universe_includes_macro_watchlist_a_indices_only():
    codes = macro_a_index_codes()
    watchlist_names = {item["name"] for item in macro_watchlist()}

    assert codes["科创综指"] == "sh000680"
    assert codes["中证银行"] == "sz399986"
    assert codes["国证2000"] == "sz399303"
    assert "恒生科技ETF" not in codes
    assert "30年国债ETF" not in codes
    assert "中国石油" not in codes
    assert "中国石油" not in watchlist_names
    assert "半导体ETF" in watchlist_names
    assert "半导体设备ETF" in watchlist_names
    assert "通信ETF" in watchlist_names
    assert "纳指100ETF" in watchlist_names
    assert "机器人ETF" in watchlist_names
    assert "恒生医药ETF" in watchlist_names
    assert {
        "511090",
        "513130",
        "512480",
        "562590",
        "515880",
        "513100",
        "159770",
        "159506",
    } <= set(macro_industry_etf_pure_codes())
    assert macro_industry_etf_symbol_by_code()["562590"] == "SH.562590"
    assert canonical_macro_industry_etf_symbol("SZ.562590") == "SH.562590"
    assert macro_industry_etf_name("562590") == "半导体设备ETF"


def test_minute_readiness_probe_uses_macro_index_universe():
    symbols = minute_readiness._index_symbols()

    assert "sh000680" in symbols
    assert "sz399986" in symbols
    assert "sz399303" in symbols


def test_custom_csi_indices_are_explicitly_daily_only_for_minute_cache():
    assert supports_a_index_minute_cache("sh932038") is False
    assert supports_a_index_minute_cache("SH.931837") is False
    assert supports_a_index_minute_cache("sh000688") is True
