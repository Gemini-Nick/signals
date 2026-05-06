# -*- coding: utf-8 -*-
from __future__ import annotations

import pandas as pd
import pytest

from signals.sync import provider_limits
from signals.sync.modules import stock_daily


class _Collection:
    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, query=None, projection=None):
        return list(self.docs)

    def find_one(self, query, projection=None, sort=None):
        for doc in self.docs:
            if all(doc.get(key) == value for key, value in query.items()):
                return doc
        return None

    def aggregate(self, pipeline):
        return list(self.docs)


class _DB(dict):
    def __getitem__(self, name):
        if name not in self:
            self[name] = _Collection()
        return dict.__getitem__(self, name)


@pytest.fixture(autouse=True)
def _clear_provider_state():
    provider_limits._STATES.clear()
    yield
    provider_limits._STATES.clear()


def test_stock_daily_uses_tencent_primary_without_akshare(monkeypatch):
    calls = []

    def fake_tencent_daily(code, *, start_date="", end_date="", count=800, timeout=8.0):
        calls.append((code, start_date, end_date, timeout))
        return pd.DataFrame([{
            "日期": "2026-04-27",
            "开盘": 10.0,
            "收盘": 10.5,
            "最高": 10.8,
            "最低": 9.9,
            "成交量": 12345,
            "成交额": 0,
        }])

    monkeypatch.setenv("STOCK_DAILY_PRIMARY_SOURCE", "tencent")
    monkeypatch.setattr(stock_daily, "fetch_tencent_daily", fake_tencent_daily)
    monkeypatch.setattr(
        stock_daily.ak,
        "stock_zh_a_hist",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("akshare eastmoney should not be called")),
    )

    docs = stock_daily._sync_one_stock("600019", "20260427", "20260427")

    assert calls == [("600019", "20260427", "20260427", 8.0)]
    assert len(docs) == 1
    assert docs[0]["meta"]["source"] == "tencent"
    assert docs[0]["close"] == 10.5


def test_stock_daily_tencent_empty_returns_without_akshare(monkeypatch):
    monkeypatch.setenv("STOCK_DAILY_PRIMARY_SOURCE", "tencent")
    monkeypatch.setattr(stock_daily, "fetch_tencent_daily", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        stock_daily.ak,
        "stock_zh_a_hist",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("akshare eastmoney should not be called")),
    )

    assert stock_daily._sync_one_stock("600423", "20260427", "20260427") == []


def test_stock_daily_provider_prefix_maps_bj_920_codes():
    assert stock_daily._daily_provider_prefix("920118") == "bj"
    assert stock_daily._daily_provider_prefix("900901") == "sh"


def test_get_all_stock_codes_falls_back_to_cached_universe(monkeypatch):
    db = _DB({
        "sync_log": _Collection([
            {"module": "stock_daily", "symbol": "600001"},
            {"module": "stock_daily", "symbol": "SZ.300001"},
        ]),
        "market_pools": _Collection([
            {
                "pool": "active",
                "symbols": ["SH.600001", "BJ.430001"],
                "items": [{"symbol": "SZ.000001"}],
            }
        ]),
        "bars": _Collection([
            {"_id": "SH.600002"},
        ]),
    })

    monkeypatch.setattr(
        stock_daily.ak,
        "stock_info_a_code_name",
        lambda: (_ for _ in ()).throw(ConnectionError("sse eof")),
    )
    monkeypatch.setattr(
        stock_daily.ak,
        "stock_zh_a_spot_em",
        lambda: (_ for _ in ()).throw(ConnectionError("push2 proxy")),
    )

    assert stock_daily._get_all_stock_codes(db) == [
        "600001",
        "300001",
        "430001",
        "000001",
        "600002",
    ]


def test_get_stock_codes_all_scope_uses_cached_universe_without_raising(monkeypatch):
    db = _DB({
        "sync_log": _Collection([
            {"module": "stock_daily", "symbol": "600001"},
        ]),
    })

    monkeypatch.setenv("STOCK_DAILY_SCOPE", "all")
    monkeypatch.setenv("SIGNALS_SYNC_FULL_STOCK_DAILY", "false")
    monkeypatch.setattr(
        stock_daily.ak,
        "stock_info_a_code_name",
        lambda: (_ for _ in ()).throw(ConnectionError("bse ssl eof")),
    )
    monkeypatch.setattr(
        stock_daily.ak,
        "stock_zh_a_spot_em",
        lambda: (_ for _ in ()).throw(ConnectionError("remote disconnected")),
    )

    assert stock_daily._get_stock_codes(db) == (["600001"], "all")
