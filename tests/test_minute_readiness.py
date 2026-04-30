# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.sync.modules import minute_readiness


class _Cursor(list):
    def sort(self, *args, **kwargs):
        return self

    def limit(self, n):
        return _Cursor(self[:n])


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def _match(self, doc, query):
        for key, value in (query or {}).items():
            actual = doc
            for part in key.split("."):
                actual = actual.get(part) if isinstance(actual, dict) else None
            if isinstance(value, dict) and "$in" in value:
                if actual not in value["$in"]:
                    return False
                continue
            if actual != value:
                return False
        return True

    def count_documents(self, query=None):
        return len([doc for doc in self.docs if self._match(doc, query)])

    def find_one(self, query=None, projection=None, sort=None):
        rows = [doc for doc in self.docs if self._match(doc, query)]
        if sort:
            for key, direction in reversed(sort):
                rows.sort(key=lambda item: item.get(key), reverse=direction < 0)
        return dict(rows[0]) if rows else None

    def find(self, query=None, projection=None):
        return _Cursor([dict(doc) for doc in self.docs if self._match(doc, query)])

    def delete_many(self, query=None):
        self.docs = [doc for doc in self.docs if not self._match(doc, query)]

    def bulk_write(self, ops, ordered=False):
        for op in ops:
            doc = dict(getattr(op, "_filter", {}))
            doc.update((getattr(op, "_doc", {}) or {}).get("$set", {}))
            self.docs.append(doc)

    def update_one(self, query=None, update=None, upsert=False):
        doc = dict(query or {})
        doc.update((update or {}).get("$set", {}))
        self.docs.append(doc)


class _Db(dict):
    def __missing__(self, key):
        self[key] = _Collection()
        return self[key]


def test_minute_readiness_uses_current_stock_minute_freq_scope(monkeypatch):
    now = datetime(2026, 4, 30, 10, 45)
    monkeypatch.setattr(minute_readiness, "naive_market_now", lambda _market: now)
    monkeypatch.setattr(minute_readiness, "_index_symbols", lambda: [])
    monkeypatch.setattr(minute_readiness, "_heat_names", lambda _db, _kind: [])
    db = _Db({
        "sync_log": _Collection([
            {
                "_id": "stock_minute:selection:_meta",
                "selected_symbols": ["688379"],
                "minute_freqs": ["5分钟"],
                "last_run": now,
            }
        ]),
        "terminal_stock_pool": _Collection(),
        "bars": _Collection([
            {"meta": {"symbol": "688379", "freq": "5分钟", "source": "eastmoney"}, "dt": now}
        ]),
        "minute_readiness": _Collection(),
        "data_freshness": _Collection(),
    })

    result = minute_readiness.sync_minute_readiness_probe(db)

    readiness_rows = [doc for doc in db["minute_readiness"].docs if doc.get("domain") == "stock"]
    assert result["not_ready"] == 0
    assert [row["freq"] for row in readiness_rows] == ["5分钟"]
