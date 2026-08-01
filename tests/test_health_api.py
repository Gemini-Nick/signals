# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.web.api import health


class _Collection:
    def __init__(self, *, docs=None, count=0, reject_latest=False):
        self.docs = list(docs or [])
        self.count = count
        self.reject_latest = reject_latest

    def estimated_document_count(self):
        return self.count

    def find_one(self, query, projection=None, sort=None):
        if self.reject_latest and any(key in query for key in ("dt", "latest_dt", "signal_date", "snapshot_at", "updated_at")):
            raise AssertionError("cache health must not scan collection data for latest timestamps")
        collection = query.get("collection")
        if collection:
            return next((doc for doc in self.docs if doc.get("collection") == collection), None)
        return self.docs[0] if self.docs else None

    def distinct(self, key, query, **kwargs):
        assert kwargs.get("maxTimeMS") == 500
        return list({
            doc.get(key)
            for doc in self.docs
            if doc.get(key) is not None
        })


class _DB:
    def __init__(self):
        self.collections = {
            "bars": _Collection(count=28_000_000, reject_latest=True),
            "data_freshness": _Collection(
                docs=[{
                    "collection": "bars",
                    "count": 28_000_000,
                    "latest_dt": "2026-07-31",
                    "updated_at": "2026-07-31T16:20:00",
                    "freshness": "fresh",
                }]
            ),
        }

    def __getitem__(self, name):
        return self.collections.setdefault(name, _Collection())


def test_cache_health_uses_preaggregated_freshness_without_scanning_bars(monkeypatch):
    db = _DB()
    monkeypatch.setattr("signals.data.mongo_fallback.get_db", lambda: db)

    payload = health.cache_health()

    bars = next(item for item in payload["items"] if item["collection"] == "bars")
    assert bars["count"] == 28_000_000
    assert bars["latest_dt"] == "2026-07-31"
    assert bars["freshness"] == "fresh"


def test_active_pool_coverage_batches_symbol_queries():
    db = _DB()
    db.collections["market_pools"] = _Collection(docs=[{
        "_id": "active:2026-07-31",
        "pool": "active",
        "symbols": ["SH.600001", "SZ.000001", "HK.00117"],
        "dt": "2026-07-31",
    }])
    db.collections["bars"] = _Collection(docs=[
        {"meta.symbol": "SH.600001"},
    ])
    db.collections["sync_log"] = _Collection(docs=[
        {"_id": "stock_daily:600001"},
        {"_id": "hk_stock_daily:HK.00117"},
    ])
    db.collections["quote_snapshots"] = _Collection(docs=[
        {"symbol": "SZ.000001"},
    ])

    coverage = health._active_pool_coverage(db)

    assert coverage["coverage_status"] == "ok"
    assert coverage["count"] == 3
    assert coverage["bars_covered"] == 2
    assert coverage["quote_covered"] == 1
    assert coverage["bars_missing"] == ["SZ.000001"]
    assert coverage["quote_missing"] == ["SH.600001", "HK.00117"]
