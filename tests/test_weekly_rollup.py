# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.sync.modules import weekly_rollup
from signals.sync.task_context import task_env


class _Cursor(list):
    def sort(self, keys, *args, **kwargs):
        if not isinstance(keys, list):
            keys = [(keys, args[0] if args else 1)]

        def value(row, key):
            cur = row
            for part in str(key).split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            return cur or ""

        for key, direction in reversed(keys):
            super().sort(key=lambda row: value(row, key), reverse=direction < 0)
        return self


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.find_calls = 0
        self.find_queries = []
        self.delete_queries = []
        self.inserted = []

    def find(self, query=None, projection=None):
        self.find_calls += 1
        self.find_queries.append(query or {})
        freqs = set((query or {}).get("meta.freq", {}).get("$in", []))
        symbols = set((query or {}).get("meta.symbol", {}).get("$in", []))
        rows = []
        for doc in self.docs:
            if (query or {}).get("_id") is not None and doc.get("_id") != (query or {}).get("_id"):
                continue
            meta = doc.get("meta") or {}
            if freqs and meta.get("freq") not in freqs:
                continue
            if symbols and meta.get("symbol") not in symbols:
                continue
            rows.append({key: value for key, value in doc.items() if key != "_id"})
        return _Cursor(rows)

    def find_one(self, query=None, projection=None, sort=None):
        rows = list(self.find(query, projection))
        return rows[0] if rows else None

    def delete_many(self, query):
        self.delete_queries.append(query)

    def insert_many(self, docs, ordered=False):
        self.inserted.extend(docs)

    def update_one(self, query, update, upsert=False):
        return None


class _Db(dict):
    def __missing__(self, key):
        self[key] = _Collection()
        return self[key]


def _daily(symbol, day, close):
    return {
        "dt": datetime.fromisoformat(day),
        "meta": {"symbol": symbol, "freq": "日线", "market": "A"},
        "open": close - 1,
        "high": close + 1,
        "low": close - 2,
        "close": close,
        "vol": 100,
        "amount": 1000,
    }


def test_weekly_rollup_batches_daily_docs_by_collection():
    bars = _Collection([
        _daily("SZ.000001", "2026-06-15", 10),
        _daily("SZ.000001", "2026-06-19", 12),
        _daily("SH.600000", "2026-06-16", 20),
        _daily("SH.600000", "2026-06-19", 21),
    ])
    index_bars = _Collection([_daily("sh000001", "2026-06-19", 3000)])
    db = _Db({"bars": bars, "index_bars": index_bars, "data_freshness": _Collection()})

    result = weekly_rollup.sync_weekly_rollup(db)

    assert bars.find_calls == 1
    assert index_bars.find_calls == 1
    assert len(bars.delete_queries) == 1
    assert set(bars.delete_queries[0]["meta.symbol"]["$in"]) == {"SZ.000001", "SH.600000"}
    assert result["stock_symbols"] == 2
    assert result["index_symbols"] == 1
    assert result["stock_inserted"] == 2
    assert result["index_inserted"] == 1


def test_weekly_rollup_postmarket_candidates_uses_task_env_bounded_symbol_query():
    bars = _Collection([
        _daily("SZ.000001", "2026-06-19", 12),
        _daily("SH.600000", "2026-06-19", 21),
        _daily("SZ.300001", "2026-06-19", 31),
    ])
    db = _Db({
        "bars": bars,
        "index_bars": _Collection(),
        "data_freshness": _Collection(),
        "sync_log": _Collection([{
            "_id": "stock_minute:selection:_meta",
            "pinned_symbols": ["000001", "600000", "300001"],
        }]),
        "terminal_stock_pool": _Collection(),
    })

    with task_env({
        "SIGNALS_POSTMARKET_RUN_ID": "postmarket:2026-06-22",
        "SIGNALS_POSTMARKET_TRADE_DATE": "2026-06-22",
        "WEEKLY_ROLLUP_SCOPE": "postmarket_candidates",
        "WEEKLY_ROLLUP_MAX_SYMBOLS": "2",
    }):
        result = weekly_rollup.sync_weekly_rollup(db)

    assert result["stock_scope"] == "postmarket_candidates"
    assert result["stock_symbols"] == 2
    symbol_query = bars.find_queries[0]["meta.symbol"]["$in"]
    assert "SZ.000001" in symbol_query
    assert "SH.600000" in symbol_query
    assert "SZ.300001" not in symbol_query
