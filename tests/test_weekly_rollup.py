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
    assert weekly[0]["open"] == 9
    assert weekly[0]["close"] == 14
    assert weekly[0]["vol"] == 500
