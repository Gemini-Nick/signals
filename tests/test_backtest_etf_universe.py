# -*- coding: utf-8 -*-
from __future__ import annotations


def test_backtest_batch_resolves_all_etf_universe(monkeypatch):
    from signals.core import etf_universe
    from signals.data import mongo_fallback
    from signals.web2.api import backtest

    monkeypatch.setattr(mongo_fallback, "get_db", lambda: None)
    monkeypatch.setattr(etf_universe, "all_market_etf_universe", lambda **kwargs: {
        "codes": ["512480", "159516"],
        "total": 1494,
        "limit": kwargs.get("limit", 0),
        "source": "eastmoney_etf_spot",
        "source_counts": {"eastmoney_etf_spot": 1494},
        "as_of": "2026-06-08",
        "warnings": [],
    })

    codes, meta = backtest._resolve_batch_codes({"codes": ["all_etf"], "universe_limit": 2})

    assert codes == ["512480", "159516"]
    assert meta["type"] == "all_etf"
    assert meta["total"] == 1494
    assert meta["selected"] == 2
    assert meta["source"] == "eastmoney_etf_spot"


def test_backtest_batch_keeps_manual_codes_without_universe():
    from signals.web2.api import backtest

    codes, meta = backtest._resolve_batch_codes({"codes": "002759, 600519"})

    assert codes == ["002759", "600519"]
    assert meta == {}
