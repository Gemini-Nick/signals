# -*- coding: utf-8 -*-
from __future__ import annotations

import pandas as pd

from signals.sync.modules import stock_daily


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
