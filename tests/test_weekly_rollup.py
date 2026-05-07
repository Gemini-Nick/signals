# -*- coding: utf-8 -*-
from __future__ import annotations

import pandas as pd

from signals.sync.modules.weekly_rollup import _weekly_docs


def test_weekly_docs_are_generated_from_daily_cache():
    docs = []
    for dt, close in zip(pd.date_range("2026-04-20", periods=5, freq="D"), [10, 11, 12, 13, 14]):
        docs.append({
            "dt": dt,
            "meta": {"symbol": "600000", "freq": "日线", "source": "test"},
            "open": close - 1,
            "high": close + 1,
            "low": close - 2,
            "close": close,
            "vol": 100,
            "amount": 1000,
        })

    weekly = _weekly_docs("600000", docs, collection="bars")

    assert len(weekly) == 1
    assert weekly[0]["meta"]["freq"] == "周线"
    assert weekly[0]["meta"]["source"] == "daily_rollup"
    assert weekly[0]["meta"]["volume_unit"] == "shares"
    assert weekly[0]["meta"]["source_volume_unit"] == "daily_shares_rollup"
    assert weekly[0]["open"] == 9
    assert weekly[0]["close"] == 14
    assert weekly[0]["vol"] == 500


def test_weekly_docs_label_unfinished_week_by_data_as_of():
    docs = []
    for dt, close in zip(pd.date_range("2026-04-27", periods=3, freq="D"), [10, 11, 12]):
        docs.append({
            "dt": dt,
            "meta": {"symbol": "600000", "freq": "日线", "source": "test"},
            "open": close - 1,
            "high": close + 1,
            "low": close - 2,
            "close": close,
            "vol": 100,
            "amount": 1000,
        })

    weekly = _weekly_docs("600000", docs, collection="bars")

    assert len(weekly) == 1
    assert pd.to_datetime(weekly[0]["dt"]).date().isoformat() == "2026-04-29"
    assert weekly[0]["meta"]["period_end"] == "2026-05-01"
    assert weekly[0]["meta"]["data_as_of"] == "2026-04-29"
    assert weekly[0]["meta"]["is_partial_period"] is True


def test_weekly_docs_preserve_hk_market_metadata():
    docs = []
    for dt, close in zip(pd.date_range("2026-04-20", periods=5, freq="D"), [300, 302, 304, 306, 308]):
        docs.append({
            "dt": dt,
            "meta": {"symbol": "HK.00700", "freq": "日线", "market": "HK", "source": "test"},
            "open": close - 1,
            "high": close + 1,
            "low": close - 2,
            "close": close,
            "vol": 100,
            "amount": 1000,
        })

    weekly = _weekly_docs("HK.00700", docs, collection="bars")

    assert weekly[0]["meta"]["market"] == "HK"
    assert weekly[0]["meta"]["symbol"] == "HK.00700"
