# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

import pandas as pd

from signals.sync.modules import index_daily
from signals.data.bar_quality import validate_ohlcv_bar


class _DeleteResult:
    def __init__(self, deleted_count: int):
        self.deleted_count = deleted_count


class _InsertManyResult:
    def __init__(self, inserted_ids: list[int]):
        self.inserted_ids = inserted_ids


class _FakeCollection:
    def __init__(self):
        self.deleted_queries = []
        self.inserted_docs = []
        self.ordered = None

    def delete_many(self, query):
        self.deleted_queries.append(query)
        return _DeleteResult(1)

    def insert_many(self, docs, ordered=False):
        self.inserted_docs.extend(docs)
        self.ordered = ordered
        return _InsertManyResult(list(range(len(docs))))


def test_index_daily_replaces_exact_docs_without_timeseries_update():
    dt = datetime(2026, 5, 6)
    stale = {
        "_id": "old-id",
        "dt": dt,
        "meta": {"symbol": "sh000001", "freq": "日线", "source": "akshare"},
        "close": 3330.0,
    }
    latest = {
        "_id": "new-id",
        "dt": dt,
        "meta": {"symbol": "sh000001", "freq": "日线", "source": "index_minute_rollup"},
        "close": 3342.0,
    }
    other = {
        "dt": dt,
        "meta": {"symbol": "sz399006", "freq": "日线", "source": "index_minute_rollup"},
        "close": 2142.0,
    }
    col = _FakeCollection()

    written = index_daily._replace_exact_bar_docs(col, [stale, latest, other])

    assert written == 2
    assert col.deleted_queries == [
        {"meta.symbol": "sh000001", "meta.freq": "日线", "dt": {"$in": [dt]}},
        {"meta.symbol": "sz399006", "meta.freq": "日线", "dt": {"$in": [dt]}},
    ]
    assert col.inserted_docs == [
        {k: v for k, v in latest.items() if k != "_id"},
        other,
    ]
    assert col.ordered is False


def test_index_daily_builds_provisional_close_from_quote_snapshot():
    quote = {
        "symbol": "SH.000001",
        "code": "000001",
        "dt": "2026-05-06",
        "snapshot_at": datetime(2026, 5, 6, 15, 37, 37),
        "source": "eastmoney_push2delay_ulist",
        "freshness": "fresh",
        "is_stale": False,
        "open": 4135.45,
        "high": 4166.15,
        "low": 4129.91,
        "close": 4160.17,
        "prev_close": 4112.16,
        "change": 48.01,
        "change_pct": 1.1675,
        "vol": 70117748000,
        "amount": 1465903193400.0,
    }

    doc = index_daily._daily_doc_from_quote_snapshot("sh000001", "2026-05-06", quote)

    assert doc is not None
    assert doc["dt"] == datetime(2026, 5, 6)
    assert doc["close"] == 4160.17
    assert doc["change_pct"] == 1.1675
    assert doc["pct_chg"] == 1.1675
    assert doc["meta"]["source"] == "eastmoney_push2delay_ulist"
    assert doc["meta"]["source_type"] == "direct_quote_ohlcv"
    assert doc["meta"]["quality"] == "provisional_close"
    assert doc["meta"]["quote_symbol"] == "SH.000001"


def test_index_daily_rejects_stale_or_wrong_day_quote_snapshot():
    stale = {
        "symbol": "SH.000001",
        "dt": "2026-05-06",
        "freshness": "stale",
        "open": 1,
        "high": 1,
        "low": 1,
        "close": 1,
    }
    wrong_day = {
        "symbol": "SH.000001",
        "dt": "2026-04-30",
        "freshness": "fresh",
        "open": 1,
        "high": 1,
        "low": 1,
        "close": 1,
    }

    assert index_daily._daily_doc_from_quote_snapshot("sh000001", "2026-05-06", stale) is None
    assert index_daily._daily_doc_from_quote_snapshot("sh000001", "2026-05-06", wrong_day) is None


def test_index_daily_quote_candidates_do_not_match_stock_code_collision():
    candidates = index_daily._quote_candidates_for_index("sh000001")

    assert "SH.000001" in candidates
    assert "sh000001" in candidates
    assert "000001" not in candidates


def test_index_daily_normalizes_official_csindex_fallback():
    frame = pd.DataFrame([
        {
            "日期": "2026-07-31",
            "指数代码": "932038",
            "开盘": "1689.46",
            "最高": "1705.02",
            "最低": "1656.00",
            "收盘": "1657.83",
            "成交量": "1893255356",
            "成交金额": "960.11",
        }
    ])

    normalized = index_daily._normalize_csindex_frame(frame)

    assert normalized.iloc[0]["date"] == pd.Timestamp("2026-07-31")
    assert normalized.iloc[0]["open"] == 1689.46
    assert normalized.iloc[0]["close"] == 1657.83
    assert normalized.iloc[0]["volume"] == 1893255356


def test_index_daily_uses_csindex_after_public_providers_fail(monkeypatch):
    calls = []

    def fail_em(**kwargs):
        calls.append("em")
        raise RuntimeError("em unavailable")

    def fail_sina(**kwargs):
        calls.append("sina")
        raise RuntimeError("sina unavailable")

    def csindex(**kwargs):
        calls.append(("csindex", kwargs["symbol"]))
        return pd.DataFrame([
            {"日期": "2026-07-31", "开盘": 1, "最高": 2, "最低": 0.5, "收盘": 1.5, "成交量": 10, "成交金额": 20}
        ])

    monkeypatch.setattr(index_daily.ak, "stock_zh_index_daily_em", fail_em)
    monkeypatch.setattr(index_daily.ak, "stock_zh_index_daily", fail_sina)
    monkeypatch.setattr(index_daily.ak, "stock_zh_index_hist_csindex", csindex)

    frame, source = index_daily._fetch_a_index_daily_frame("sh932038", "2026-07-01", "2026-08-02")

    assert source == "akshare_csindex"
    assert frame.iloc[0]["close"] == 1.5
    assert calls == ["em", "sina", ("csindex", "932038")]


def test_us_index_nan_regression_is_rejected_before_persistence():
    bad = {
        "dt": datetime(2026, 7, 24),
        "open": 620.0,
        "high": 625.0,
        "low": 615.0,
        "close": float("nan"),
        "vol": 100,
        "amount": 0,
    }

    accepted, reason = validate_ohlcv_bar(bad)

    assert accepted is False
    assert reason == "non_finite_price"

    col = _FakeCollection()
    written = index_daily._replace_exact_bar_docs(
        col,
        [
            {
                **bad,
                "meta": {"symbol": "US.DIA", "freq": "日线", "market": "US"},
            }
        ],
    )
    assert written == 0
    assert col.inserted_docs == []
