# -*- coding: utf-8 -*-
from __future__ import annotations

import pandas as pd

from signals.core.macd_detector import detect_macd_signals
from signals.web2.api.backtest import _compute_macd_data, _position_for_index_label, _records_to_df


def test_detect_macd_signals_handles_duplicate_datetime_index():
    dates = pd.date_range("2026-01-01", periods=45, freq="D").tolist()
    duplicate_dates = dates[:30] + dates[25:40] + dates[40:]
    rows = []
    for i, _ in enumerate(duplicate_dates):
        close = 10 + i * 0.12
        rows.append({
            "open": close - 0.05,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "vol": 1000 + i,
        })
    df = pd.DataFrame(rows, index=pd.DatetimeIndex(duplicate_dates))

    signals = detect_macd_signals(df, "SZ.002759", "日线", lookback=30)

    assert isinstance(signals, list)


def test_position_for_index_label_uses_position_when_dates_repeat():
    index = pd.DatetimeIndex([
        pd.Timestamp("2026-01-01"),
        pd.Timestamp("2026-01-02"),
        pd.Timestamp("2026-01-02"),
    ])

    assert _position_for_index_label(index, pd.Timestamp("2026-01-02")) == 2


def test_compute_macd_data_handles_duplicate_datetime_index():
    dates = pd.date_range("2026-01-01", periods=30, freq="D").tolist()
    duplicate_dates = dates[:20] + dates[18:28] + dates[28:]
    df = pd.DataFrame(
        {
            "close": [10 + i * 0.1 for i in range(len(duplicate_dates))],
        },
        index=pd.DatetimeIndex(duplicate_dates),
    )

    result = _compute_macd_data(df)

    assert result


def test_records_to_df_deduplicates_datetime_index():
    records = [
        {"dt": "2026-01-01", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "vol": 100},
        {"dt": "2026-01-01", "open": 2, "high": 3, "low": 1.5, "close": 2.5, "vol": 200},
    ]

    df = _records_to_df(records)

    assert len(df) == 1
    assert df.iloc[0]["close"] == 2.5
