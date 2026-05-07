# -*- coding: utf-8 -*-
from __future__ import annotations

import pandas as pd

from signals.sync.modules import minute_sources


def test_stock_to_market_symbol_maps_bj_920_codes():
    assert minute_sources.stock_to_market_symbol("920118") == "bj920118"
    assert minute_sources.stock_to_market_symbol("900901") == "sh900901"


def test_fetch_public_minute_uses_sina_only_for_bj_symbols(monkeypatch):
    calls = []

    def fake_sina(symbol, period, *, timeout, datalen):
        calls.append(("sina", symbol, period, datalen))
        return pd.DataFrame([{"时间": "2026-05-06 15:00", "收盘": 10}])

    def fake_tencent(symbol, period, *, timeout, count):
        calls.append(("tencent", symbol, period, count))
        return pd.DataFrame([{"时间": "2026-05-06 15:00", "收盘": 10}])

    monkeypatch.setattr(minute_sources, "fetch_sina_minute", fake_sina)
    monkeypatch.setattr(minute_sources, "fetch_tencent_minute", fake_tencent)

    df, source = minute_sources.fetch_public_minute("bj920118", "30", datalen=88, count=77)

    assert source == "sina"
    assert not df.empty
    assert calls == [("sina", "bj920118", "30", 88)]


def test_fetch_public_minute_passes_tail_limits_to_provider(monkeypatch):
    calls = []

    def fake_sina(symbol, period, *, timeout, datalen):
        calls.append(("sina", symbol, period, timeout, datalen))
        return pd.DataFrame([{"时间": "2026-04-27 15:00", "收盘": 10}])

    monkeypatch.setattr(minute_sources, "fetch_sina_minute", fake_sina)

    df, source = minute_sources.fetch_public_minute("sh688802", "30", timeout=3, datalen=88, count=77)

    assert source == "sina"
    assert not df.empty
    assert calls == [("sina", "sh688802", "30", 3, 88)]


def test_fetch_public_minute_passes_tail_limits_to_tencent_after_empty_sina(monkeypatch):
    calls = []

    def fake_sina(symbol, period, *, timeout, datalen):
        calls.append(("sina", datalen))
        return pd.DataFrame()

    def fake_tencent(symbol, period, *, timeout, count):
        calls.append(("tencent", count))
        return pd.DataFrame([{"时间": "2026-04-27 15:00", "收盘": 10}])

    monkeypatch.setattr(minute_sources, "fetch_sina_minute", fake_sina)
    monkeypatch.setattr(minute_sources, "fetch_tencent_minute", fake_tencent)

    df, source = minute_sources.fetch_public_minute("sh688802", "30", datalen=88, count=77)

    assert source == "tencent"
    assert not df.empty
    assert calls == [("sina", 88), ("tencent", 77)]


def test_fetch_public_minute_keeps_stock_and_index_cooldown_endpoints_separate(monkeypatch):
    calls = []

    def fake_cooldown(db, provider, endpoint, *, domain):
        calls.append(("cooldown", provider, endpoint, domain))
        return 0

    def fake_sina(symbol, period, *, timeout, datalen, db=None, endpoint="stock_minute"):
        calls.append(("sina", endpoint, db is not None))
        return pd.DataFrame([{"时间": "2026-04-27 15:00", "收盘": 10}])

    monkeypatch.setattr(minute_sources, "provider_cooldown_remaining", fake_cooldown)
    monkeypatch.setattr(minute_sources, "fetch_sina_minute", fake_sina)

    df, source = minute_sources.fetch_public_minute(
        "sh000680",
        "30",
        db=object(),
        endpoint="index_minute",
    )

    assert source == "sina"
    assert not df.empty
    assert calls == [
        ("cooldown", "sina", "index_minute", "minute"),
        ("sina", "index_minute", True),
    ]
