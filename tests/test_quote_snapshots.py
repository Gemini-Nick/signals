# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.sync.modules.quote_snapshots import (
    _quote_doc_from_em,
    _quote_doc_from_ulist_row,
    _read_fullmarket_spot_quotes,
    _secid_for_symbol,
)


def test_eastmoney_secid_for_prefixed_symbols():
    assert _secid_for_symbol("SH.601958") == "1.601958"
    assert _secid_for_symbol("SZ.000001") == "0.000001"
    assert _secid_for_symbol("SH.000300") == "1.000300"
    assert _secid_for_symbol("SZ.399001") == "0.399001"


def test_quote_doc_from_eastmoney_payload_scales_fields():
    payload = {
        "rc": 0,
        "data": {
            "f43": 1967,
            "f44": 1986,
            "f45": 1898,
            "f46": 1933,
            "f47": 304987,
            "f48": 591786626.0,
            "f57": "601958",
            "f58": "金钼股份",
            "f60": 1943,
            "f168": 95,
            "f169": 24,
            "f170": 124,
            "f171": 453,
        },
    }

    doc = _quote_doc_from_em("SH.601958", payload, datetime(2026, 4, 25, 10, 0), "2026-04-24")

    assert doc is not None
    assert doc["source"] == "eastmoney_push2delay"
    assert doc["freshness"] == "fresh"
    assert doc["price"] == 19.67
    assert doc["prev_close"] == 19.43
    assert doc["change_pct"] == 1.24
    assert doc["vol"] == 30498700


def test_quote_doc_from_ulist_row_uses_batch_fields():
    row = {
        "f2": 19.67,
        "f3": 1.24,
        "f4": 0.24,
        "f5": 304987,
        "f6": 591786626.0,
        "f7": 4.53,
        "f8": 0.95,
        "f12": "601958",
        "f13": 1,
        "f14": "金钼股份",
        "f15": 19.86,
        "f16": 18.98,
        "f17": 19.33,
        "f18": 19.43,
    }

    doc = _quote_doc_from_ulist_row("SH.601958", row, datetime(2026, 4, 30, 10, 0), "2026-04-30")

    assert doc is not None
    assert doc["source"] == "eastmoney_push2delay_ulist"
    assert doc["trade_date"] == "2026-04-30"
    assert doc["price"] == 19.67
    assert doc["change_pct"] == 1.24
    assert doc["vol"] == 30498700


class _Collection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query=None, projection=None):
        query = query or {}
        date_key = query.get("date_key")
        wanted_codes = set()
        wanted_symbols = set()
        for item in query.get("$or", []):
            if "code" in item:
                wanted_codes.update(item["code"].get("$in", []))
            if "symbol" in item:
                wanted_symbols.update(item["symbol"].get("$in", []))
        return [
            row for row in self.rows
            if row.get("date_key") == date_key
            and (row.get("code") in wanted_codes or row.get("symbol") in wanted_symbols)
        ]

    def find_one(self, query=None, projection=None, sort=None):
        if not self.rows:
            return None
        if sort:
            return self.rows[-1]
        return self.rows[0]


class _Db(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def test_quote_snapshots_reads_fullmarket_spot_snapshot():
    db = _Db({
        "fullmarket_spot_snapshots": _Collection([
            {
                "date_key": "20260429",
                "trade_date": "2026-04-29",
                "code": "601958",
                "symbol": "SH.601958",
                "name": "金钼股份",
                "price": 19.67,
                "prev_close": 19.43,
                "change_pct": 1.24,
                "vol": 304987,
                "amount": 591786626.0,
                "open": 19.33,
                "high": 19.86,
                "low": 18.98,
            }
        ])
    })

    docs = _read_fullmarket_spot_quotes(db, ["SH.601958"], "2026-04-29", datetime(2026, 4, 29, 16, 0))

    doc = docs["SH.601958"]
    assert doc["source"] == "fullmarket_spot_snapshot"
    assert doc["freshness"] == "fresh"
    assert doc["price"] == 19.67
    assert doc["vol"] == 30498700


def test_quote_snapshots_falls_back_to_latest_fullmarket_spot_snapshot():
    db = _Db({
        "fullmarket_spot_snapshots": _Collection([
            {
                "date_key": "20260429",
                "trade_date": "2026-04-29",
                "code": "601958",
                "symbol": "SH.601958",
                "price": 19.67,
            }
        ])
    })

    docs = _read_fullmarket_spot_quotes(db, ["SH.601958"], "2026-04-28", datetime(2026, 4, 29, 16, 0))

    assert docs["SH.601958"]["dt"] == "2026-04-29"


def test_quote_snapshots_can_disable_stale_fullmarket_spot_fallback():
    db = _Db({
        "fullmarket_spot_snapshots": _Collection([
            {
                "date_key": "20260429",
                "trade_date": "2026-04-29",
                "code": "601958",
                "symbol": "SH.601958",
                "price": 19.67,
            }
        ])
    })

    docs = _read_fullmarket_spot_quotes(
        db,
        ["SH.601958"],
        "2026-04-30",
        datetime(2026, 4, 30, 10, 30),
        allow_latest_fallback=False,
    )

    assert docs == {}
