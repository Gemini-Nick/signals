# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.sync.modules import stock_daily


class _InsertResult:
    def __init__(self, count: int):
        self.inserted_ids = list(range(count))


class _Collection:
    def __init__(self):
        self.docs = {}
        self.inserted = []

    def find(self, query=None, projection=None):
        return []

    def update_one(self, query, update, upsert=False):
        key = query.get("_id")
        doc = dict(self.docs.get(key, {"_id": key}))
        doc.update(update.get("$set", {}))
        for inc_key, inc_value in (update.get("$inc") or {}).items():
            doc[inc_key] = int(doc.get(inc_key) or 0) + int(inc_value)
        self.docs[key] = doc

    def insert_many(self, docs, ordered=False):
        self.inserted.extend(docs)
        return _InsertResult(len(docs))


class _DB(dict):
    def __getitem__(self, name):
        if name not in self:
            self[name] = _Collection()
        return dict.__getitem__(self, name)


def test_stock_daily_writes_progress_cursor(monkeypatch):
    db = _DB()
    monkeypatch.setattr(stock_daily, "_get_stock_codes", lambda _db: (["600001", "600002"], "all"))
    monkeypatch.setattr(stock_daily, "_progress_interval", lambda: 1)
    monkeypatch.setattr(stock_daily, "_BATCH_WORKERS", 1)
    monkeypatch.setattr(stock_daily, "_CALL_INTERVAL", 0)
    monkeypatch.setattr(stock_daily, "_stock_daily_providers_all_cooling", lambda _db: False)

    def fake_sync_one(code, start, end, proxy_url=None):
        return [{
            "dt": datetime(2026, 4, 28),
            "meta": {"symbol": code, "freq": "日线"},
            "open": 1,
            "high": 1,
            "low": 1,
            "close": 1,
            "vol": 1,
            "amount": 1,
        }]

    monkeypatch.setattr(stock_daily, "_sync_one_stock", fake_sync_one)

    result = stock_daily.sync_stock_daily(db)
    progress = db["sync_log"].docs["stock_daily:progress:_meta"]

    assert result["processed"] == 2
    assert result["progress_pct"] == 100
    assert progress["status"] == "ok"
    assert progress["processed"] == 2
    assert progress["total"] == 2
    assert progress["remaining"] == 0
    assert progress["latest_symbol"] in {"600001", "600002"}
    assert progress["inserted_per_min"] >= 0


def test_stock_daily_uses_bars_latest_to_skip_network(monkeypatch):
    db = _DB()
    calls = []
    monkeypatch.setattr(stock_daily, "naive_market_now", lambda _market: datetime(2026, 4, 28, 18, 0, 0))
    monkeypatch.setattr(stock_daily, "_get_stock_codes", lambda _db: (["600001"], "all"))
    monkeypatch.setattr(stock_daily, "_latest_daily_dates_by_symbol", lambda _db, _codes: {"600001": datetime(2026, 4, 28)})
    monkeypatch.setattr(stock_daily, "_progress_interval", lambda: 1)
    monkeypatch.setattr(stock_daily, "_CALL_INTERVAL", 0)
    monkeypatch.setattr(stock_daily, "_stock_daily_providers_all_cooling", lambda _db: False)

    def fake_sync_one(*args, **kwargs):
        calls.append(args)
        return []

    monkeypatch.setattr(stock_daily, "_sync_one_stock", fake_sync_one)

    result = stock_daily.sync_stock_daily(db)

    assert calls == []
    assert result["skipped"] == 1
    assert result["errors"] == 0


def test_stock_daily_defers_shard_when_all_providers_cooling(monkeypatch):
    db = _DB()
    monkeypatch.setattr(stock_daily, "_get_stock_codes", lambda _db: (["600001", "600002"], "all"))
    monkeypatch.setattr(stock_daily, "_latest_daily_dates_by_symbol", lambda _db, _codes: {})
    monkeypatch.setattr(stock_daily, "_stock_daily_providers_all_cooling", lambda _db: True)
    monkeypatch.setattr(stock_daily, "_progress_interval", lambda: 1)
    monkeypatch.setattr(stock_daily, "_BATCH_WORKERS", 1)
    monkeypatch.setattr(stock_daily, "_CALL_INTERVAL", 0)

    result = stock_daily.sync_stock_daily(db)
    progress = db["sync_log"].docs["stock_daily:progress:_meta"]

    assert result["status"] == "partial"
    assert result["errors"] == 0
    assert result["deferred"] == 2
    assert progress["deferred_symbols"] == 2
    assert "all_stock_daily_providers" in result["sample_deferred"][0][1]
