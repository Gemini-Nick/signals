from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from signals.sync.modules.us_optical_research import (
    _market_naive_index,
    _normalize_download_frame,
    _weekly_from_daily,
    optical_study_universe,
)


def test_optical_study_universe_separates_direct_and_context_names():
    rows = {row["ticker"]: row for row in optical_study_universe()}

    assert {"AXTI", "COHR", "LITE", "FN", "AAOI", "CIEN"} <= rows.keys()
    assert {"GLW", "AVGO", "MRVL"} <= rows.keys()
    assert rows["AXTI"]["basket_role"] == "direct_optical"
    assert rows["AVGO"]["basket_role"] == "context_only"


def test_yfinance_utc_minutes_are_converted_to_new_york_wall_time():
    index = pd.DatetimeIndex([datetime(2026, 7, 31, 13, 30, tzinfo=timezone.utc)])

    normalized = _market_naive_index(index)

    assert normalized[0] == pd.Timestamp("2026-07-31 09:30:00")
    assert normalized.tz is None


def test_download_normalization_and_weekly_rollup_preserve_ohlcv():
    frame = pd.DataFrame(
        {
            "Open": [10.0, 11.0],
            "High": [12.0, 13.0],
            "Low": [9.0, 10.0],
            "Close": [11.0, 12.0],
            "Volume": [100, 200],
        },
        index=pd.to_datetime(["2026-07-30", "2026-07-31"]),
    )

    normalized = _normalize_download_frame(frame)
    weekly = _weekly_from_daily(normalized)

    assert normalized.iloc[-1]["amount"] == 2400
    assert weekly.iloc[0]["open"] == 10
    assert weekly.iloc[0]["high"] == 13
    assert weekly.iloc[0]["low"] == 9
    assert weekly.iloc[0]["close"] == 12
    assert weekly.iloc[0]["vol"] == 300
