# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from signals.sync.modules import ma_climb_scan as climb


class _Cursor(list):
    def sort(self, *args, **kwargs):
        return self


class _Collection:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.update_many_calls = []
        self.update_one_calls = []

    def find(self, query=None, projection=None):
        return _Cursor(self.docs)

    def update_many(self, query, update):
        self.update_many_calls.append((query, update))
        return SimpleNamespace(modified_count=1)

    def update_one(self, query, update, upsert=False):
        self.update_one_calls.append((query, update, upsert))
        return SimpleNamespace(modified_count=1)


class _DB(dict):
    def __getitem__(self, name):
        if name not in self:
            self[name] = _Collection()
        return dict.__getitem__(self, name)


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


def _staircase_frame(*, count: int = 140) -> pd.DataFrame:
    wave = (0.8, 0.55, 0.3, 0.05, 0.2, 0.45, 0.7, 0.95)
    rows = []
    for index in range(count):
        close = 10.0 + 0.1 * index + 0.6 * wave[index % len(wave)]
        rows.append({
            "dt": pd.Timestamp("2025-01-06") + pd.offsets.BDay(index),
            "open": close - 0.05,
            "high": close + 0.25,
            "low": close - 0.30,
            "close": close,
        })
    return pd.DataFrame(rows)


def test_ma_climb_rejects_smooth_trend_without_pullback_rebound_cycles():
    result = climb._evaluate_ma_climb(_rising_frame(), 5)

    assert result is not None
    assert result["running"] is False
    assert result["pullback_cycle_count"] < climb.MIN_PULLBACK_CYCLES


def test_ma_climb_accepts_staircase_pullbacks_with_higher_lows():
    result = climb._evaluate_ma_climb(_staircase_frame(), 10)

    assert result is not None
    assert result["running"] is True
    assert result["hold_count"] == climb.CLIMB_WINDOW
    assert result["pullback_cycle_count"] >= climb.MIN_PULLBACK_CYCLES
    assert result["higher_pullback_low"] is True
    assert result["ma5_rising"] is True
    assert result["ma10_rising"] is True


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
    frame = _staircase_frame()

    best, alternates = climb._best_climb(frame)

    assert best is not None
    assert best["period"] in {5, 10}
    assert len(alternates) <= 1
    assert all(item["period"] != best["period"] for item in alternates)


def test_completed_weekly_frame_excludes_open_week(monkeypatch):
    frame = _rising_frame(count=39)
    frame.loc[len(frame) - 1, "dt"] = pd.Timestamp("2026-02-26")
    frame = frame.sort_values("dt").drop_duplicates("dt", keep="last")
    monkeypatch.setattr(climb, "is_trading_day", lambda market, day: day.weekday() < 5)

    weekly = climb._completed_weekly_frame(frame)

    assert weekly["dt"].max().date().isoformat() < "2026-02-27"


def test_signal_doc_exposes_explicit_climb_contract():
    frame = _staircase_frame()

    docs = climb._scan_symbol("300001", frame, datetime(2026, 5, 8, 20, 30), as_of="2026-05-08")
    daily_docs = [doc for doc in docs if doc["freq"] == "日线"]
    assert len(daily_docs) == 1
    daily = daily_docs[0]
    evidence = daily["technical_evidence"]["ma_climb"]

    assert daily["signal_family"] == "ma_climb"
    assert daily["signal_type"] == "日线攀爬"
    assert daily["producer"] == "ma_climb_scan"
    assert daily["active"] is True
    assert evidence["running"] is True
    assert evidence["effective_ma"] in {"MA5", "MA10"}
    assert evidence["pullback_cycle_count"] >= 2
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


def test_load_daily_frames_excludes_etfs_from_scan_universe():
    bars = [
        {
            "dt": datetime(2026, 7, 27),
            "meta": {"market": "A", "freq": "日线", "symbol": code},
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
        }
        for code in ("600031", "526070", "159825")
    ]
    db = _DB({
        "bars": _Collection(bars),
        "etf_spot_snapshots": _Collection([
            {
                "code": "526070",
                "symbol": "SH.526070",
                "name": "恒指港股通ETF博时",
                "security_type": "etf",
                "source": "eastmoney_etf_spot",
            },
        ]),
    })

    frames, latest_dt, excluded_etf_codes = climb._load_daily_frames(
        db,
        datetime(2026, 7, 27, 18, 0),
    )

    assert set(frames) == {"600031"}
    assert latest_dt == "2026-07-27"
    assert excluded_etf_codes == {"159825", "526070"}


def test_sync_invalidates_existing_etf_climb_signals(monkeypatch):
    technical_signals = _Collection()
    data_freshness = _Collection()
    db = _DB({
        "terminal_technical_signals": technical_signals,
        "data_freshness": data_freshness,
    })
    monkeypatch.setenv("MA_CLIMB_MIN_FULLMARKET_SYMBOLS", "1")
    monkeypatch.setattr(
        climb,
        "_load_daily_frames",
        lambda db, now: ({"600031": _staircase_frame()}, "2026-07-27", {"526070"}),
    )
    monkeypatch.setattr(climb, "_active_climb_states", lambda db: {})
    monkeypatch.setattr(climb, "_scan_symbol", lambda *args, **kwargs: [])

    result = climb.sync_ma_climb_scan(db)

    etf_query, etf_update = technical_signals.update_many_calls[0]
    assert etf_query["raw_code"] == {"$in": ["526070"]}
    assert etf_update["$set"]["active"] is False
    assert etf_update["$set"]["invalidated_reason"] == "asset_scope_excluded_etf"
    assert result["excluded_etfs"] == 1
    assert result["excluded_etf_signals"] == 1
    assert result["asset_scope"] == "a_share_stocks_only"
