# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from signals.sync import provider_limits
from signals.sync.task_context import task_env


class _Collection:
    def __init__(self):
        self.docs = {}

    def find_one(self, query, projection=None):
        key = (query.get("provider"), query.get("endpoint"), query.get("domain"))
        return dict(self.docs.get(key) or {})

    def update_one(self, query, update, upsert=False):
        key = (query.get("provider"), query.get("endpoint"), query.get("domain"))
        doc = dict(self.docs.get(key) or {})
        doc.update(query)
        doc.update(update.get("$set", {}))
        for inc_key, inc_value in (update.get("$inc") or {}).items():
            doc[inc_key] = int(doc.get(inc_key) or 0) + int(inc_value)
        self.docs[key] = doc


class _Db(dict):
    def __missing__(self, key):
        self[key] = _Collection()
        return self[key]


def test_provider_cooldown_preserves_root_error(monkeypatch):
    provider_limits._STATES.clear()
    monkeypatch.setenv("SIGNALS_PROVIDER_COOLDOWN_SECONDS", "30,30")
    monkeypatch.setenv("SIGNALS_PROVIDER_JITTER_SECONDS", "0,0")
    db = _Db()

    def fail():
        raise RuntimeError("RemoteDisconnected: remote end closed connection")

    with pytest.raises(RuntimeError):
        provider_limits.provider_call("tencent", "stock_daily", fail, db=db)

    doc = db["provider_health"].find_one({"provider": "tencent", "endpoint": "stock_daily", "domain": "market_data"})
    assert doc["status"] == "cooldown"
    assert "RemoteDisconnected" in doc["last_error_type"]

    with pytest.raises(provider_limits.ProviderCoolingDown):
        provider_limits.provider_call("tencent", "stock_daily", lambda: "ok", db=db)

    doc = db["provider_health"].find_one({"provider": "tencent", "endpoint": "stock_daily", "domain": "market_data"})
    assert "RemoteDisconnected" in doc["last_error_type"]
    assert doc["cooldown_hit_type"].startswith("provider_cooling_down:tencent:")


def test_shared_cooldown_older_than_update_is_ignored(monkeypatch):
    provider_limits._STATES.clear()
    monkeypatch.setenv("SIGNALS_PROVIDER_JITTER_SECONDS", "0,0")
    db = _Db()
    now = provider_limits._now()
    db["provider_health"].docs[("sina", "stock_daily", "market_data")] = {
        "provider": "sina",
        "endpoint": "stock_daily",
        "domain": "market_data",
        "status": "cooldown",
        "last_error_type": "provider_cooling_down:sina:120s",
        "cooldown_until": now + timedelta(seconds=30),
        "updated_at": now + timedelta(seconds=60),
    }

    assert provider_limits.provider_call("sina", "stock_daily", lambda: "ok", db=db) == "ok"
    doc = db["provider_health"].find_one({"provider": "sina", "endpoint": "stock_daily", "domain": "market_data"})
    assert doc["status"] == "ok"


def test_expired_provider_cooldown_is_marked_expired(monkeypatch):
    provider_limits._STATES.clear()
    db = _Db()
    now = provider_limits._now()
    db["provider_health"].docs[("sina", "stock_daily", "market_data")] = {
        "provider": "sina",
        "endpoint": "stock_daily",
        "domain": "market_data",
        "status": "cooldown",
        "last_error_type": "429",
        "cooldown_until": now - timedelta(seconds=1),
        "updated_at": now - timedelta(seconds=30),
    }

    assert provider_limits.provider_cooldown_remaining(db, "sina", "stock_daily") == 0
    doc = db["provider_health"].find_one({"provider": "sina", "endpoint": "stock_daily", "domain": "market_data"})
    assert doc["status"] == "cooldown_expired"
    assert doc["cooldown_until"] is None


def test_provider_limits_are_endpoint_scoped(monkeypatch):
    provider_limits._STATES.clear()
    monkeypatch.setenv("SIGNALS_PROVIDER_COOLDOWN_SECONDS", "30,30")
    monkeypatch.setenv("SIGNALS_PROVIDER_JITTER_SECONDS", "0,0")
    db = _Db()

    def fail():
        raise RuntimeError("RemoteDisconnected: remote end closed connection")

    with pytest.raises(RuntimeError):
        provider_limits.provider_call("tencent", "stock_daily", fail, db=db)

    assert provider_limits.provider_call("tencent", "quote_snapshot", lambda: "ok", db=db) == "ok"
    with pytest.raises(provider_limits.ProviderCoolingDown):
        provider_limits.provider_call("tencent", "stock_daily", lambda: "ok", db=db)

    daily = db["provider_health"].find_one({"provider": "tencent", "endpoint": "stock_daily", "domain": "market_data"})
    quote = db["provider_health"].find_one({"provider": "tencent", "endpoint": "quote_snapshot", "domain": "market_data"})
    assert daily["cooldown_hit_count"] == 1
    assert quote["success_count"] == 1


def test_dotted_endpoint_uses_safe_env_name(monkeypatch):
    provider_limits._STATES.clear()
    monkeypatch.setenv("SIGNALS_PROVIDER_EASTMONEY_PUSH2DELAY_STOCK_GET_CONCURRENCY", "3")

    state = provider_limits._state("eastmoney", "push2delay.stock.get", "quote")

    assert state.capacity == 3


def test_provider_jitter_reads_task_env(monkeypatch):
    monkeypatch.delenv("SIGNALS_PROVIDER_JITTER_SECONDS", raising=False)

    with task_env({"SIGNALS_PROVIDER_JITTER_SECONDS": "0,0.15"}):
        assert provider_limits._jitter_seconds() == (0.0, 0.15)


def test_providers_all_cooling_down_uses_provider_health(monkeypatch):
    provider_limits._STATES.clear()
    db = _Db()
    now = provider_limits._now()
    for provider, endpoint in (("tencent", "stock_daily"), ("eastmoney", "stock_daily_hist"), ("sina", "stock_daily")):
        db["provider_health"].docs[(provider, endpoint, "market_data")] = {
            "provider": provider,
            "endpoint": endpoint,
            "domain": "market_data",
            "status": "cooldown",
            "last_error_type": "429",
            "cooldown_until": now + timedelta(seconds=30),
            "updated_at": now,
        }

    assert provider_limits.providers_all_cooling_down(
        db,
        (("tencent", "stock_daily"), ("eastmoney", "stock_daily_hist"), ("sina", "stock_daily")),
    )

    db["provider_health"].docs[("sina", "stock_daily", "market_data")]["cooldown_until"] = now - timedelta(seconds=1)
    assert not provider_limits.providers_all_cooling_down(
        db,
        (("tencent", "stock_daily"), ("eastmoney", "stock_daily_hist"), ("sina", "stock_daily")),
    )
