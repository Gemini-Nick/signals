# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.sync.modules.market_pools import _collect_pool_symbols


class _Cursor(list):
    def sort(self, *args, **kwargs):
        return self

    def limit(self, count):
        return _Cursor(self[:count])


class _Collection:
    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, *args, **kwargs):
        return _Cursor(self.docs)

    def find_one(self, query=None, *args, **kwargs):
        wanted = (query or {}).get("_id")
        for doc in self.docs:
            if wanted is None or doc.get("_id") == wanted:
                return doc
        return None

    def aggregate(self, *args, **kwargs):
        raise AssertionError("market pool collection must not aggregate the full bars collection")


class _Db(dict):
    def __missing__(self, key):
        self[key] = _Collection()
        return self[key]


def test_collect_pool_symbols_reuses_selection_and_bounded_daily_bars(monkeypatch):
    monkeypatch.setattr("config.WHITELIST", [], raising=False)
    db = _Db({
        "sync_log": _Collection([
            {
                "_id": "stock_minute:selection:_meta",
                "status": "ok",
                "selected_symbols": ["600000", "000001"],
            },
        ]),
        "bars": _Collection([
            {"meta": {"symbol": "300001", "freq": "日线"}},
        ]),
    })

    pool = _collect_pool_symbols(db)

    assert "SH.600000" in pool
    assert "SZ.000001" in pool
    assert "SZ.300001" in pool
    assert "stock_minute_selection" in pool["SH.600000"]
