# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

import pandas as pd

from signals.sync.modules import ma_climb_scan as climb


def _rising_frame(*, step: float = 0.1, spread: float = 0.8, count: int = 90) -> pd.DataFrame:
    rows = []
    for index in range(count):
        close = 10.0 + step * index
        rows.append({
            "dt": pd.Timestamp("2026-01-05") + pd.offsets.BDay(index),
            "open": close - 0.1,
            "high": close + spread / 2,
            "low": close - spread / 2,
            "close": close,
        })
    return pd.DataFrame(rows)


def test_ma_climb_accepts_smooth_close_above_effective_ma():
    result = climb._evaluate_ma_climb(_rising_frame(), 5)

    assert result is not None
    assert result["running"] is True
    assert result["hold_count"] == 5
    assert result["climb_score"] >= climb.BUY_REVIEW_SCORE
    assert result["climb_grade"] == "buy_review"


def test_ma_climb_rejects_any_recent_close_below_effective_ma():
    frame = _rising_frame()
    ma5 = frame["close"].rolling(5).mean()
    frame.loc[len(frame) - 1, "close"] = float(ma5.iloc[-1] - 0.5)
    frame.loc[len(frame) - 1, "low"] = float(ma5.iloc[-1] - 0.6)

    result = climb._evaluate_ma_climb(frame, 5)

    assert result is not None
    assert result["running"] is False
    assert result["hold_count"] == 4
    assert result["climb_grade"] == "invalid"


def test_ma_climb_selects_only_one_effective_ma_per_timeframe():
    frame = _rising_frame()

    best, alternates = climb._best_climb(frame)

    assert best is not None
    assert best["period"] in {5, 10}
    assert len(alternates) == 1
    assert alternates[0]["period"] != best["period"]


def test_completed_weekly_frame_excludes_open_week(monkeypatch):
    frame = _rising_frame(count=39)
    frame.loc[len(frame) - 1, "dt"] = pd.Timestamp("2026-02-26")
    frame = frame.sort_values("dt").drop_duplicates("dt", keep="last")
    monkeypatch.setattr(climb, "is_trading_day", lambda market, day: day.weekday() < 5)

    weekly = climb._completed_weekly_frame(frame)

    assert weekly["dt"].max().date().isoformat() < "2026-02-27"


def test_signal_doc_exposes_explicit_climb_contract():
    frame = _rising_frame()

    docs = climb._scan_symbol("300001", frame, datetime(2026, 5, 8, 20, 30), as_of="2026-05-08")
    daily = next(doc for doc in docs if doc["freq"] == "日线")
    evidence = daily["technical_evidence"]["ma_climb"]

    assert daily["signal_family"] == "ma_climb"
    assert daily["producer"] == "ma_climb_scan"
    assert daily["active"] is True
    assert evidence["running"] is True
    assert evidence["effective_ma"] in {"MA5", "MA10"}
    assert "收盘跌破有效" in daily["invalidates_when"]


def test_established_climb_continues_when_slope_flattens_above_ma():
    frame = _rising_frame()
    flat_close = float(frame["close"].iloc[-6])
    for index in frame.tail(5).index:
        frame.loc[index, ["open", "high", "low", "close"]] = [
            flat_close,
            flat_close + 0.4,
            flat_close,
            flat_close,
        ]
    previous = {
        "period": 5,
        "climb_score": 85,
        "climb_grade": "buy_review",
    }

    result = climb._continue_climb(frame, previous)

    assert result is not None
    assert result["running"] is True
    assert result["continued"] is True
    assert result["climb_score"] == 85
    assert result["climb_grade"] == "buy_review"
