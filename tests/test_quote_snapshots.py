# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from signals.sync.modules.quote_snapshots import (
    _a_quote_symbols,
    _hot_quote_symbols,
    _realtime_quote_symbols,
    _quote_doc_from_em,
    _quote_doc_from_ulist_row,
    _read_current_quote_snapshot_docs,
    _read_fullmarket_no_price_symbols,
    _read_fullmarket_spot_quotes,
    _secid_for_symbol,
)


def test_eastmoney_secid_for_prefixed_symbols():
    assert _secid_for_symbol("SH.601958") == "1.601958"
    assert _secid_for_symbol("SZ.000001") == "0.000001"
    assert _secid_for_symbol("SH.000300") == "1.000300"
    assert _secid_for_symbol("SZ.399001") == "0.399001"
    assert _secid_for_symbol("SZ.588170") == "1.588170"
    assert _secid_for_symbol("920118") == "0.920118"


def test_a_quote_symbols_filters_non_a_symbols_before_eastmoney():
    symbols = ["SH.600000", "600001", "BJ.920118", "HK.00050", "US.AAPL", "SZ.399001"]

    assert _a_quote_symbols(symbols) == ["SH.600000", "SH.600001", "BJ.920118", "SZ.399001"]


def test_a_quote_symbols_canonicalizes_wrong_exchange_prefixes():
    symbols = ["SZ.588170", "SH.588170", "SH.000300", "SZ.399001"]

    assert _a_quote_symbols(symbols) == ["SH.588170", "SH.000300", "SZ.399001"]


def test_realtime_quote_symbols_leave_index_codes_to_index_lane(monkeypatch):
    from signals.sync.modules import quote_snapshots

    monkeypatch.setattr(
        quote_snapshots,
        "_hot_quote_symbols",
        lambda _db: ["SH.600000", "SH.000300", "SH.932038", "SZ.399001", "SZ.159915"],
    )

    assert _realtime_quote_symbols(object()) == ["SH.600000", "SZ.159915"]


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
    assert doc["trade_date"] == "2026-04-24"
    assert doc["price"] == 19.67
    assert doc["prev_close"] == 19.43
    assert doc["change"] == 0.24
    assert doc["change_pct"] == 1.2352
    assert doc["vol"] == 30498700
    assert doc["volume_unit"] == "shares"
    assert doc["source_vol"] == 304987
    assert doc["source_volume_unit"] == "hands"


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
        "f124": int(datetime(2026, 4, 30, 15, 0, 5, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()),
    }

    doc = _quote_doc_from_ulist_row("SH.601958", row, datetime(2026, 4, 30, 10, 0), "2026-04-30")

    assert doc is not None
    assert doc["source"] == "eastmoney_push2delay_ulist"
    assert doc["trade_date"] == "2026-04-30"
    assert doc["price"] == 19.67
    assert doc["change"] == 0.24
    assert doc["change_pct"] == 1.2352
    assert doc["vol"] == 30498700
    assert doc["volume_unit"] == "shares"
    assert doc["source_updated_at"] == datetime(2026, 4, 30, 15, 0, 5)


def test_quote_doc_from_ulist_row_tolerates_preopen_dash_fields():
    row = {
        "f2": 12.05,
        "f3": 0.0,
        "f4": 0.0,
        "f5": "-",
        "f6": "-",
        "f7": 0.0,
        "f8": 0.0,
        "f12": "601857",
        "f13": 1,
        "f14": "中国石油",
        "f15": "-",
        "f16": "-",
        "f17": "-",
        "f18": 12.05,
        "f20": "-",
        "f21": "-",
    }

    doc = _quote_doc_from_ulist_row("SH.601857", row, datetime(2026, 5, 6, 9, 16), "2026-05-06")

    assert doc is not None
    assert doc["price"] == 12.05
    assert doc["source_vol"] == 0.0
    assert doc["amount"] == 0.0
    assert doc["open"] == 0.0
    assert doc["vol"] == 0


def test_quote_doc_recomputes_stale_provider_change_fields():
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
            "f169": 999,
            "f170": 999,
        },
    }
    row = {
        "f2": 19.67,
        "f3": 99.9,
        "f4": 9.99,
        "f5": 304987,
        "f6": 591786626.0,
        "f12": "601958",
        "f14": "金钼股份",
        "f18": 19.43,
    }

    em_doc = _quote_doc_from_em("SH.601958", payload, datetime(2026, 4, 30, 10, 0), "2026-04-30")
    ulist_doc = _quote_doc_from_ulist_row("SH.601958", row, datetime(2026, 4, 30, 10, 0), "2026-04-30")

    assert em_doc["change"] == 0.24
    assert em_doc["change_pct"] == 1.2352
    assert ulist_doc["change"] == 0.24
    assert ulist_doc["change_pct"] == 1.2352


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


def test_hot_quote_symbols_include_terminal_clue_stocks():
    class _Cursor(list):
        def sort(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

    class _SimpleCollection:
        def __init__(self, doc=None, rows=None):
            self.doc = doc
            self.rows = rows or []

        def find_one(self, query=None, projection=None, sort=None):
            return self.doc

        def find(self, query=None, projection=None):
            return _Cursor(self.rows)

        def aggregate(self, pipeline):
            return _Cursor([])

    db = _Db({
        "terminal_stock_pool": _SimpleCollection({
            "clue_stocks": [{"symbol": "SZ.301363"}],
        }),
        "market_pools": _SimpleCollection({"symbols": []}),
        "signals": _SimpleCollection(rows=[]),
        "bars": _SimpleCollection(rows=[]),
    })

    symbols = _hot_quote_symbols(db)

    assert "SZ.301363" in symbols


def test_hot_quote_symbols_include_chain_heat_representatives(monkeypatch):
    from signals.sync.modules import quote_snapshots

    class _Cursor(list):
        def sort(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

    class _SimpleCollection:
        def __init__(self, doc=None, rows=None):
            self.doc = doc
            self.rows = rows or []

        def find_one(self, query=None, projection=None, sort=None):
            return self.doc

        def find(self, query=None, projection=None):
            return _Cursor(self.rows)

        def aggregate(self, pipeline):
            return _Cursor([])

    db = _Db({
        "terminal_stock_pool": _SimpleCollection({}),
        "terminal_manual_clues": _SimpleCollection(rows=[]),
        "market_pools": _SimpleCollection({"symbols": []}),
        "signals": _SimpleCollection(rows=[]),
        "bars": _SimpleCollection(rows=[]),
        "chain_heat_snapshots": _SimpleCollection(
            {"trade_minute": datetime(2026, 5, 8, 10, 30)},
            rows=[{
                "representatives": [
                    {"symbol": "SZ.002281", "representative_type": "elastic"},
                    {"symbol": "SH.688498", "representative_type": "elastic"},
                ]
            }],
        ),
    })
    monkeypatch.setattr(quote_snapshots, "_iter_strategy_snapshot_symbols", lambda: [])

    symbols = quote_snapshots._hot_quote_symbols(db)

    assert "SZ.002281" in symbols
    assert "SH.688498" in symbols


def test_hot_quote_symbols_prioritize_strategy_snapshot_before_limit(monkeypatch):
    from signals.sync.modules import quote_snapshots

    class _Cursor(list):
        def sort(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

    class _SimpleCollection:
        def __init__(self, doc=None, rows=None):
            self.doc = doc
            self.rows = rows or []

        def find_one(self, query=None, projection=None, sort=None):
            return self.doc

        def find(self, query=None, projection=None):
            return _Cursor(self.rows)

        def aggregate(self, pipeline):
            return _Cursor([])

    terminal_rows = [{"symbol": f"SZ.{idx:06d}"} for idx in range(1, 20)]
    db = _Db({
        "terminal_stock_pool": _SimpleCollection({"watch_stocks": terminal_rows}),
        "terminal_manual_clues": _SimpleCollection(rows=[]),
        "market_pools": _SimpleCollection({"symbols": []}),
        "signals": _SimpleCollection(rows=[]),
        "bars": _SimpleCollection(rows=[]),
    })
    monkeypatch.setenv("EASTMONEY_ULIST_MAX_SYMBOLS", "6")
    monkeypatch.setattr(quote_snapshots, "_iter_strategy_snapshot_symbols", lambda: ["SZ.002759"])

    symbols = quote_snapshots._hot_quote_symbols(db)

    assert "SZ.002759" in symbols


def test_hot_quote_symbols_prioritize_manual_clues_before_limit(monkeypatch):
    from signals.sync.modules import quote_snapshots

    class _Cursor(list):
        def sort(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

    class _SimpleCollection:
        def __init__(self, doc=None, rows=None):
            self.doc = doc
            self.rows = rows or []

        def find_one(self, query=None, projection=None, sort=None):
            return self.doc

        def find(self, query=None, projection=None):
            return _Cursor(self.rows)

        def aggregate(self, pipeline):
            return _Cursor([])

    terminal_rows = [{"symbol": f"SZ.{idx:06d}"} for idx in range(1, 20)]
    db = _Db({
        "terminal_stock_pool": _SimpleCollection({"watch_stocks": terminal_rows}),
        "terminal_manual_clues": _SimpleCollection(rows=[{"symbol": "SZ.002759"}]),
        "market_pools": _SimpleCollection({"symbols": []}),
        "signals": _SimpleCollection(rows=[]),
        "bars": _SimpleCollection(rows=[]),
    })
    monkeypatch.setenv("EASTMONEY_ULIST_MAX_SYMBOLS", "6")
    monkeypatch.setattr(quote_snapshots, "_iter_strategy_snapshot_symbols", lambda: [])

    symbols = quote_snapshots._hot_quote_symbols(db)

    assert "SZ.002759" in symbols


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
    assert doc["trade_date"] == "2026-04-29"
    assert doc["price"] == 19.67
    assert doc["vol"] == 30498700


def test_quote_snapshots_do_not_rescale_normalized_fullmarket_volume():
    db = _Db({
        "fullmarket_spot_snapshots": _Collection([
            {
                "date_key": "20260429",
                "trade_date": "2026-04-29",
                "code": "601958",
                "symbol": "SH.601958",
                "price": 19.67,
                "vol": 30498700,
                "volume_unit": "shares",
                "source_vol": 304987,
                "source_volume_unit": "hands",
            }
        ])
    })

    docs = _read_fullmarket_spot_quotes(db, ["SH.601958"], "2026-04-29", datetime(2026, 4, 29, 16, 0))

    doc = docs["SH.601958"]
    assert doc["vol"] == 30498700
    assert doc["source_vol"] == 304987
    assert doc["source_volume_unit"] == "hands"


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


def test_quote_snapshots_classifies_current_no_price_rows():
    db = _Db({
        "fullmarket_spot_snapshots": _Collection([
            {
                "date_key": "20260430",
                "trade_date": "2026-04-30",
                "code": "600193",
                "symbol": "SH.600193",
                "price": None,
                "latest": None,
            },
            {
                "date_key": "20260430",
                "trade_date": "2026-04-30",
                "code": "600421",
                "symbol": "SH.600421",
                "price": "-",
            },
            {
                "date_key": "20260430",
                "trade_date": "2026-04-30",
                "code": "601958",
                "symbol": "SH.601958",
                "price": 19.67,
            },
        ])
    })

    no_price = _read_fullmarket_no_price_symbols(
        db,
        ["SH.600193", "SH.600421", "SH.601958", "SH.600999"],
        "2026-04-30",
    )

    assert no_price == {"SH.600193", "SH.600421"}


def test_quote_snapshots_reuses_current_day_snapshot_when_market_paused():
    class _QuoteCollection:
        def find_one(self, query=None, projection=None, sort=None):
            if query == {"_id": "SH.588170:latest"}:
                return {
                    "symbol": "SH.588170",
                    "code": "588170",
                    "dt": "2026-06-22",
                    "trade_date": "2026-06-22",
                    "price": 3.214,
                    "freshness": "stale",
                    "stale_reason": "eastmoney_current_quote_missing",
                }
            return None

    db = _Db({"quote_snapshots": _QuoteCollection()})

    docs = _read_current_quote_snapshot_docs(
        db,
        ["SH.588170"],
        "2026-06-22",
        datetime(2026, 6, 23, 9, 3),
    )

    assert docs["SH.588170"]["freshness"] == "fresh"
    assert docs["SH.588170"]["stale_reason"] == ""
    assert docs["SH.588170"]["price"] == 3.214


def test_quote_snapshots_does_not_reuse_current_day_snapshot_during_continuous_trading():
    class _QuoteCollection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "symbol": "SH.588170",
                "dt": "2026-06-22",
                "trade_date": "2026-06-22",
                "price": 3.214,
            }

    db = _Db({"quote_snapshots": _QuoteCollection()})

    docs = _read_current_quote_snapshot_docs(
        db,
        ["SH.588170"],
        "2026-06-22",
        datetime(2026, 6, 23, 10, 3),
    )

    assert docs == {}
