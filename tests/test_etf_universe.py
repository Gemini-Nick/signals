# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime


class _Cursor(list):
    def sort(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self


class _Collection:
    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, query=None, projection=None):
        return _Cursor(self.docs)

    def find_one(self, query=None, projection=None, sort=None):
        docs = list(self.docs)
        if sort:
            for key, direction in reversed(sort):
                docs.sort(key=lambda item: _nested_get(item, key) or datetime.min, reverse=direction < 0)
        symbol = _nested_get(query or {}, "meta.symbol")
        for doc in docs:
            if symbol and _nested_get(doc, "meta.symbol") != symbol:
                continue
            return doc
        return None


class _DB(dict):
    def __getitem__(self, name):
        if name not in self:
            self[name] = _Collection()
        return dict.__getitem__(self, name)


def _nested_get(value, key):
    current = value
    for part in str(key).split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def test_all_market_etf_universe_merges_live_stock_names_bars_and_static(monkeypatch):
    from signals.core import etf_universe

    monkeypatch.setattr(etf_universe, "fetch_eastmoney_etf_spot_rows", lambda timeout=8.0: [
        {
            "f12": "589990",
            "f13": 1,
            "f14": "科创综指ETF华泰柏瑞",
            "f2": 1.61,
            "f3": -3.94,
            "f5": 45546,
            "f6": 7379606.0,
        },
    ])
    db = _DB({
        "stock_names": _Collection([
            {"name": "半导体ETF国联安", "code": "512480", "futu_symbol": "SH.512480", "symbol": "512480"},
            {"name": "普通股票", "code": "600000", "futu_symbol": "SH.600000", "symbol": "600000"},
        ]),
        "bars": _Collection([
            {
                "dt": datetime(2026, 5, 29),
                "meta": {"symbol": "512480", "freq": "日线", "source": "sina_etf"},
                "close": 2.16,
                "vol": 1140539402,
            },
        ]),
    })

    result = etf_universe.all_market_etf_universe(db=db, include_live=True, attach_daily_bars=True)
    by_code = {row["code"]: row for row in result["rows"]}

    assert by_code["589990"]["symbol"] == "SH.589990"
    assert by_code["589990"]["change_pct"] == -3.94
    assert by_code["512480"]["close"] == 2.16
    assert by_code["512480"]["latest_dt"] == "2026-05-29"
    assert "562590" in by_code
    assert "600000" not in by_code
    assert result["total"] >= 3


def test_all_market_etf_universe_reads_cached_spot_before_live(monkeypatch):
    from signals.core import etf_universe

    monkeypatch.delenv("SIGNALS_ETF_UNIVERSE_LIVE", raising=False)
    monkeypatch.setattr(
        etf_universe,
        "fetch_eastmoney_etf_spot_rows",
        lambda timeout=8.0: (_ for _ in ()).throw(AssertionError("live ETF fetch should not run when cache exists")),
    )
    db = _DB({
        "etf_spot_snapshots": _Collection([
            {
                "date_key": "20260702",
                "code": "562590",
                "symbol": "SH.562590",
                "name": "半导体设备ETF",
                "price": 3.82,
                "change_pct": -10.0,
                "amount": 240000000,
                "vol": 63130000,
                "source": "eastmoney_etf_spot",
            },
        ]),
    })

    result = etf_universe.all_market_etf_universe(db=db)
    by_code = {row["code"]: row for row in result["rows"]}

    assert by_code["562590"]["price"] == 3.82
    assert "etf_spot_snapshots" in by_code["562590"]["sources"]
    assert result["source_counts"]["etf_spot_snapshots"] >= 1


def test_etf_strategy_analysis_projects_review_lists(monkeypatch):
    from signals.core import etf_universe

    monkeypatch.setattr(etf_universe, "all_market_etf_universe", lambda **kwargs: {
        "type": "all_etf",
        "as_of": "2026-06-08",
        "source": "test",
        "source_counts": {"test": 2},
        "total": 2,
        "warnings": [],
        "rows": [
            {"code": "512480", "symbol": "SH.512480", "name": "半导体ETF", "asset_class": "theme_equity", "change_pct": 2.5, "amount": 200},
            {"code": "511090", "symbol": "SH.511090", "name": "30年国债ETF", "asset_class": "bond", "change_pct": -0.3, "amount": 100},
        ],
    })

    analysis = etf_universe.build_etf_strategy_analysis(db=None)

    assert analysis["universe"]["total"] == 2
    assert analysis["asset_class_counts"] == {"theme_equity": 1, "bond": 1}
    assert analysis["top_gainers"][0]["code"] == "512480"
    assert analysis["top_losers"][0]["code"] == "511090"
