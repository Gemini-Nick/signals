from __future__ import annotations

import pandas as pd

from signals.core.trendlines import analyze_multitimeframe_trendlines, analyze_trendlines


def _frame(*, latest_close: float = 68.0) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=60, freq="B")
    rows = []
    for index, dt in enumerate(dates):
        close = 90.0
        rows.append({
            "dt": dt,
            "open": close,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "vol": 1000,
        })
    for index, high in ((10, 140.0), (20, 125.0), (30, 110.0)):
        rows[index]["high"] = high
        rows[index]["close"] = high - 3
    for index in (15, 25, 35):
        rows[index]["low"] = 80.0
        rows[index]["close"] = 82.0
    rows[-1]["close"] = latest_close
    rows[-1]["open"] = latest_close
    rows[-1]["high"] = latest_close + 1
    rows[-1]["low"] = latest_close - 1
    return pd.DataFrame(rows).set_index("dt")


def test_detects_descending_resistance_and_breakout_signal():
    result = analyze_trendlines(_frame(), freq="daily", source="futu")

    line = next(item for item in result["trendlines"] if item["direction"] == "descending")
    assert line["kind"] == "resistance"
    assert line["anchor_count"] >= 2
    assert line["projected_price"] > 0
    assert any(item["type"] == "突破下降趋势线" for item in result["signals"])
    assert all(item["source"] == "futu" for item in result["signals"])


def test_detects_horizontal_support_retest():
    frame = _frame(latest_close=81.0)
    result = analyze_trendlines(frame, freq="daily", source="yfinance")

    support = next(item for item in result["trendlines"] if item["direction"] == "horizontal" and item["kind"] == "support")
    assert support["anchor_count"] >= 2
    assert support["projected_price"] == 80.0
    assert any(item["type"] == "水平支撑回踩" for item in result["signals"])


def test_empty_or_short_input_is_safe():
    result = analyze_trendlines(pd.DataFrame({"close": [1, 2, 3]}))

    assert result == {"trendlines": [], "signals": []}


def test_multitimeframe_keeps_context_separate_from_primary_triggers():
    frame = _frame()
    weekly = frame.resample("W-FRI").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "vol": "sum",
    }).dropna()

    result = analyze_multitimeframe_trendlines(
        {"weekly": weekly, "daily": frame},
        primary_freq="daily",
        source="yfinance",
    )

    assert result["primary_freq"] == "daily"
    assert set(result["timeframes"]) == {"weekly", "daily"}
    assert result["context"]["timeframe_states"]["daily"]["line_count"] >= 1
    assert all(item["timeframe"] in {"weekly", "daily"} for item in result["trendlines"])
    assert all(item["timeframe"] in {"weekly", "daily"} for item in result["signals"])


def test_signal_exposes_candle_and_volume_confirmation_state():
    result = analyze_trendlines(_frame(), freq="daily", source="yfinance")

    signal = next(item for item in result["signals"] if item["type"] == "突破下降趋势线")
    assert signal["confirmation"] in {"confirmed", "watch"}
    assert 0 <= signal["body_ratio"] <= 1
    assert signal["volume_ratio"] >= 0
    assert "K线锚点" in signal["details"]
