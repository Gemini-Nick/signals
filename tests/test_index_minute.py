# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.sync.modules import index_minute, minute_readiness
from signals.core.macro_universe import macro_a_index_codes


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


def test_index_minute_universe_includes_macro_watchlist_a_indices_only():
    codes = macro_a_index_codes()

    assert codes["科创综指"] == "sh000680"
    assert codes["中证银行"] == "sz399986"
    assert codes["国证2000"] == "sz399303"
    assert "恒生科技ETF" not in codes
    assert "30年国债ETF" not in codes
    assert "中国石油" not in codes


def test_minute_readiness_probe_uses_macro_index_universe():
    symbols = minute_readiness._index_symbols()

    assert "sh000680" in symbols
    assert "sz399986" in symbols
    assert "sz399303" in symbols
