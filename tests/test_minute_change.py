# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.sync.modules.minute_change import recalculate_minute_change_pct


class _Cursor(list):
    def sort(self, key, direction=1):
        reverse = direction < 0
        return _Cursor(sorted(self, key=lambda item: item.get(key), reverse=reverse))


class _Collection:
    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, query, projection=None):
        symbols = set(query.get("meta.symbol", {}).get("$in", []))
        freqs = set(query.get("meta.freq", {}).get("$in", []))
        asset_type = query.get("meta.asset_type")
        out = []
        for doc in self.docs:
            meta = doc.get("meta", {})
            if symbols and meta.get("symbol") not in symbols:
                continue
            if freqs and meta.get("freq") not in freqs:
                continue
            if asset_type is not None and meta.get("asset_type") != asset_type:
                continue
            out.append(doc)
        return _Cursor(out)


class _Db(dict):
    def __missing__(self, key):
        self[key] = _Collection()
        return self[key]


def test_recalculate_minute_change_uses_same_day_daily_prev_close_then_prior_close():
    db = _Db({
        "bars": _Collection([
            {"dt": datetime(2026, 4, 29), "close": 10.0, "meta": {"symbol": "000001", "freq": "日线"}},
            {
                "dt": datetime(2026, 4, 30),
                "close": 11.0,
                "meta": {"symbol": "000001", "freq": "日线", "prev_close": 10.0},
            },
        ])
    })
    docs = [
        {"dt": datetime(2026, 4, 30, 9, 35), "close": 10.5, "meta": {"symbol": "000001", "freq": "5分钟"}},
        {"dt": datetime(2026, 5, 6, 9, 35), "close": 12.1, "meta": {"symbol": "000001", "freq": "5分钟"}},
    ]

    out = recalculate_minute_change_pct(db, "000001", docs, asset_type="stock")

    assert out[0]["prev_close"] == 10.0
    assert out[0]["change_pct"] == 5.0
    assert out[0]["pct_chg"] == 5.0
    assert out[0]["meta"]["change_pct_source"] == "daily_prev_close"
    assert out[1]["prev_close"] == 11.0
    assert out[1]["change_pct"] == 10.0
    assert out[1]["meta"]["change_pct_base_date"] == "2026-04-30"


def test_index_change_uses_index_bars_not_same_numeric_stock_daily():
    db = _Db({
        "bars": _Collection([
            {"dt": datetime(2026, 4, 30), "close": 51.48, "meta": {"symbol": "000688", "freq": "日线"}},
        ]),
        "index_bars": _Collection([
            {"dt": datetime(2026, 4, 30), "close": 1571.065, "meta": {"symbol": "sh000688", "freq": "日线", "asset_type": "index"}},
        ]),
    })
    docs = [
        {"dt": datetime(2026, 5, 6, 11, 30), "close": 1688.92, "meta": {"symbol": "sh000688", "freq": "5分钟", "asset_type": "index"}},
    ]

    out = recalculate_minute_change_pct(db, "sh000688", docs, asset_type="index")

    assert out[0]["prev_close"] == 1571.065
    assert out[0]["change_pct"] == round((1688.92 - 1571.065) / 1571.065 * 100, 4)


def test_legacy_pure_399_index_symbol_maps_to_index_daily_bars():
    db = _Db({
        "index_bars": _Collection([
            {"dt": datetime(2026, 4, 30), "close": 2100.0, "meta": {"symbol": "sz399006", "freq": "日线", "asset_type": "index"}},
        ]),
    })
    docs = [
        {"dt": datetime(2026, 5, 6, 11, 30), "close": 2142.0, "meta": {"symbol": "399006", "freq": "30分钟"}},
    ]

    out = recalculate_minute_change_pct(db, "399006", docs, asset_type="index")

    assert out[0]["prev_close"] == 2100.0
    assert out[0]["change_pct"] == 2.0
