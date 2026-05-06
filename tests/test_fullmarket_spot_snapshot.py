# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.sync.modules import fullmarket_spot_snapshot
from signals.sync.modules import stock_daily


def test_fullmarket_spot_doc_maps_quote_and_daily_fields():
    row = {
        "f12": "600001",
        "f14": "测试股份",
        "f2": 10.5,
        "f3": 2.4,
        "f4": 0.25,
        "f5": 1000,
        "f6": 100000,
        "f7": 3.2,
        "f8": 1.1,
        "f15": 10.8,
        "f16": 10.0,
        "f17": 10.1,
        "f18": 10.25,
        "f20": 2000000,
        "f21": 1500000,
    }

    doc = fullmarket_spot_snapshot._doc_from_row(
        row,
        date_key="20260429",
        trade_date="2026-04-29",
        snapshot_at=datetime(2026, 4, 29, 15, 30),
    )

    assert doc["_id"] == "20260429:600001"
    assert doc["symbol"] == "SH.600001"
    assert doc["price"] == 10.5
    assert doc["open"] == 10.1
    assert doc["prev_close"] == 10.25
    assert doc["change"] == 0.25
    assert doc["change_pct"] == 2.439
    assert doc["turnover_pct"] == 1.1
    assert doc["vol"] == 100000
    assert doc["volume_unit"] == "shares"
    assert doc["source_vol"] == 1000
    assert doc["source_volume_unit"] == "hands"


def test_fullmarket_spot_doc_recomputes_stale_provider_change_fields():
    row = {
        "f12": "600001",
        "f14": "测试股份",
        "f2": 10.5,
        "f3": 99.9,
        "f4": 9.99,
        "f5": 1000,
        "f6": 100000,
        "f18": 10.25,
    }

    doc = fullmarket_spot_snapshot._doc_from_row(
        row,
        date_key="20260429",
        trade_date="2026-04-29",
        snapshot_at=datetime(2026, 4, 29, 15, 30),
    )

    assert doc["change"] == 0.25
    assert doc["change_pct"] == 2.439


class _Collection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query=None, projection=None):
        query = query or {}
        return [row for row in self.rows if row.get("date_key") == query.get("date_key")]


class _Db(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def test_stock_daily_reads_persisted_fullmarket_spot_snapshot(monkeypatch):
    monkeypatch.setenv("STOCK_DAILY_SPOT_SNAPSHOT_MIN_ROWS", "1")
    db = _Db({
        "fullmarket_spot_snapshots": _Collection([
            {
                "date_key": "20260429",
                "code": "600001",
                "price": 10.5,
                "vol": 1000,
                "amount": 100000,
                "high": 10.8,
                "low": 10.0,
                "open": 10.1,
                "prev_close": 10.25,
            }
        ])
    })

    df = stock_daily._read_persisted_spot_batch_df(db, "20260429")

    assert len(df) == 1
    assert df.iloc[0]["代码"] == "600001"
    assert df.iloc[0]["最新价"] == 10.5
    assert df.iloc[0]["昨收"] == 10.25
    assert df.iloc[0]["_volume_unit"] == "hands"
