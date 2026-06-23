# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd
from czsc import Freq

from signals.sync.modules.technical_signal_scan import (
    INTRADAY_SCAN_SCOPE,
    POSTMARKET_CANDIDATE_SCAN_SCOPE,
    POSTMARKET_SCAN_SCOPE,
    _coverage_by_freq,
    _doc_to_rawbar,
    _entry_factor_docs,
    _entry_factor_score,
    _append_quote_snapshot_daily_bar,
    _load_bars,
    _ma_alignment_from_daily_bars,
    _prefixed_symbol,
    _refusal_pullback_factor,
    _resampled_5m_docs,
    _resonance_context,
    _symbols_for_scope,
    _use_intraday_daily_acceptance,
)
from signals.core.entry_factors import detect_200d_new_high_entries


@dataclass
class _Event:
    freq: str
    signal_type: str
    confidence: float = 1.0
    dt: datetime = datetime(2026, 4, 28, 15, 0, 0)


@dataclass
class _Bar:
    close: float


@dataclass
class _OhlcvBar:
    dt: datetime
    open: float
    high: float
    low: float
    close: float
    vol: int = 1000
    amount: int = 10000


WEIGHTS = {
    "三买": 100,
    "趋势买": 80,
    "背驰买": 70,
    "一卖": -100,
}


def test_technical_scan_preserves_macro_etf_exchange_prefix():
    assert _prefixed_symbol("562590") == "SH.562590"
    assert _prefixed_symbol("SZ.562590") == "SH.562590"
    assert _prefixed_symbol("511090") == "SH.511090"


def test_detect_200d_new_high_entries_reports_breakout_metrics():
    rows = []
    dates = pd.date_range("2025-07-01", periods=205, freq="B")
    for idx, dt in enumerate(dates):
        close = 10 + idx * 0.01
        rows.append({
            "dt": dt,
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "vol": 1000,
        })
    rows[-1].update({"open": 13.8, "high": 15.8, "low": 13.7, "close": 15.5, "vol": 3000})
    df = pd.DataFrame(rows).set_index("dt")

    signals = detect_200d_new_high_entries(df, lookback=1)

    assert len(signals) == 1
    signal = signals[0]
    assert signal["group"] == "200d_new_high_breakout"
    assert signal["type"] == "200日新高突破"
    assert signal["lookback_days"] == 200
    assert signal["breakout_pct"] > 0
    assert signal["five_day_gain_pct"] > 0
    assert signal["volume_ratio"] > 1


def test_entry_factor_docs_publish_200d_new_high_as_terminal_signal():
    bars = []
    dates = pd.date_range("2025-07-01", periods=205, freq="B")
    for idx, dt in enumerate(dates):
        close = 10 + idx * 0.01
        bars.append(_OhlcvBar(
            dt=dt.to_pydatetime(),
            open=close - 0.1,
            high=close + 0.2,
            low=close - 0.2,
            close=close,
            vol=1000,
        ))
    bars[-1] = _OhlcvBar(
        dt=dates[-1].to_pydatetime(),
        open=13.8,
        high=15.8,
        low=13.7,
        close=15.5,
        vol=3000,
    )

    docs = _entry_factor_docs(
        "300001",
        bars,
        ma_alignment={"score": 12, "above_ma20": True},
        now=datetime(2026, 5, 8, 16, 0),
        scan_scope=POSTMARKET_SCAN_SCOPE,
    )

    assert len(docs) == 1
    doc = docs[0]
    assert doc["signal_type"] == "200日新高突破"
    assert doc["signal_family"] == "entry_factor"
    assert doc["signal_side"] == "buy"
    assert doc["freq"] == "日线"
    assert doc["technical_evidence"]["entry_factor"]["group"] == "200d_new_high_breakout"
    assert _entry_factor_score(doc["technical_evidence"]["entry_factor"]) == doc["total_score"]


def test_refusal_pullback_factor_detects_tight_high_level_consolidation():
    dates = pd.date_range("2026-03-01", periods=35, freq="B")
    bars = []
    for idx, dt in enumerate(dates[:-3]):
        close = 10 + idx * 0.1
        bars.append(_OhlcvBar(
            dt=dt.to_pydatetime(),
            open=close - 0.05,
            high=close + 0.1,
            low=close - 0.15,
            close=close,
            vol=1000,
        ))
    for dt, close, low in zip(dates[-3:], [13.35, 13.42, 13.5], [13.18, 13.25, 13.36]):
        bars.append(_OhlcvBar(
            dt=dt.to_pydatetime(),
            open=close - 0.08,
            high=close + 0.03,
            low=low,
            close=close,
            vol=900,
        ))
    ma_alignment = _ma_alignment_from_daily_bars(bars)

    factor = _refusal_pullback_factor(bars, ma_alignment)
    docs = _entry_factor_docs(
        "300001",
        bars,
        ma_alignment=ma_alignment,
        now=datetime(2026, 5, 11, 16, 0),
        scan_scope=POSTMARKET_SCAN_SCOPE,
    )

    assert factor["group"] == "relative_resilience_refusal_pullback"
    assert factor["type"] == "拒绝回调相对强度"
    assert factor["max_drawdown_pct"] < 2
    assert factor["strong_close_days"] == 3
    doc = next(item for item in docs if item["signal_type"] == "拒绝回调相对强度")
    assert doc["signal_family"] == "entry_factor"
    assert doc["signal_side"] == "buy"
    assert doc["technical_evidence"]["entry_factor"]["group"] == "relative_resilience_refusal_pullback"
    assert "拒绝回调" in doc["resonance_context"]["tags"]


def test_resonance_context_marks_single_period_signal():
    context = _resonance_context(
        [_Event("30分钟", "三买")],
        side="buy",
        primary_freq="30分钟",
        direction="buy",
        weights=WEIGHTS,
    )

    assert context["grade"] == "single_period"
    assert context["aligned_freqs"] == ["30分钟"]
    assert context["conflict_freqs"] == []
    assert context["tags"] == ["硬技术"]


def test_resonance_context_marks_multi_period_alignment():
    context = _resonance_context(
        [_Event("日线", "趋势买"), _Event("周线", "背驰买"), _Event("5分钟", "三买")],
        side="buy",
        primary_freq="日线",
        direction="buy",
        weights=WEIGHTS,
    )

    assert context["grade"] == "strong_resonance"
    assert context["aligned_freqs"] == ["周线", "日线", "5分钟"]
    assert "多周期共振" in context["tags"]
    assert "日周同向" in context["tags"]
    assert "5m确认" in context["tags"]


def test_resonance_context_marks_period_conflict():
    context = _resonance_context(
        [_Event("30分钟", "三买"), _Event("日线", "一卖")],
        side="buy",
        primary_freq="30分钟",
        direction="buy",
        weights=WEIGHTS,
    )

    assert context["grade"] == "conflict"
    assert context["aligned_freqs"] == ["30分钟"]
    assert context["conflict_freqs"] == ["日线"]
    assert "周期冲突" in context["tags"]


def test_ma_alignment_marks_reclaim_and_above_key_daily_averages():
    closes = [10, 10.2, 10.1, 10.3, 10.2, 10.4, 10.5, 10.6, 10.7, 10.8]
    closes += [11, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8]
    closes += [10.5, 12.1]

    alignment = _ma_alignment_from_daily_bars([_Bar(close=value) for value in closes])

    assert alignment["above_ma5"] is True
    assert alignment["above_ma10"] is True
    assert alignment["above_ma20"] is True
    assert alignment["above_ma21"] is True
    assert alignment["reclaim_ma20"] is True
    assert alignment["above_count"] == 3
    assert [item["period"] for item in alignment["fib_ma_array"]] == [5, 8, 10, 13, 20, 21]
    assert alignment["score"] > 0
    assert "站上20日线" in alignment["tags"]


def test_ma_alignment_marks_fibonacci_ma_pullback_acceptance_array():
    bars = []
    dates = pd.date_range("2026-04-01", periods=14, freq="B")
    for dt in dates[:-1]:
        bars.append(_OhlcvBar(
            dt=dt.to_pydatetime(),
            open=10.0,
            high=10.2,
            low=9.8,
            close=10.0,
            vol=1000,
        ))
    bars.append(_OhlcvBar(
        dt=dates[-1].to_pydatetime(),
        open=10.1,
        high=10.6,
        low=9.95,
        close=10.5,
        vol=1200,
    ))

    alignment = _ma_alignment_from_daily_bars(bars)
    ma13 = next(item for item in alignment["fib_ma_array"] if item["period"] == 13)

    assert ma13["pullback_touch"] is True
    assert ma13["pullback_acceptance"] is True
    assert 13 in alignment["fib_accept_periods"]
    assert alignment["fib_accept_count"] >= 1
    assert alignment["fib_support_score"] > 0
    assert "关键均线回踩承接" in alignment["tags"]


def test_ma_alignment_marks_touched_close_below_fibonacci_ma_as_breakdown():
    bars = []
    dates = pd.date_range("2026-04-01", periods=22, freq="B")
    for dt in dates[:-1]:
        bars.append(_OhlcvBar(
            dt=dt.to_pydatetime(),
            open=10.0,
            high=10.1,
            low=9.95,
            close=10.0,
            vol=1000,
        ))
    bars.append(_OhlcvBar(
        dt=dates[-1].to_pydatetime(),
        open=10.08,
        high=10.2,
        low=9.9,
        close=9.95,
        vol=1200,
    ))

    alignment = _ma_alignment_from_daily_bars(bars)
    ma21 = next(item for item in alignment["fib_ma_array"] if item["period"] == 21)

    assert ma21["pullback_touch"] is True
    assert ma21["pullback_acceptance"] is False
    assert ma21["pullback_breakdown"] is True
    assert 21 in alignment["fib_breakdown_periods"]
    assert "MA21跌破待修复" in alignment["fib_array_summary"]
    assert "MA21触碰待确认" not in alignment["fib_array_summary"]


def test_ma_alignment_marks_touched_close_above_but_weak_as_reclaim():
    bars = []
    dates = pd.date_range("2026-04-01", periods=22, freq="B")
    for dt in dates[:-1]:
        bars.append(_OhlcvBar(
            dt=dt.to_pydatetime(),
            open=10.0,
            high=10.1,
            low=9.95,
            close=10.0,
            vol=1000,
        ))
    bars.append(_OhlcvBar(
        dt=dates[-1].to_pydatetime(),
        open=10.5,
        high=10.7,
        low=9.95,
        close=10.03,
        vol=1200,
    ))

    alignment = _ma_alignment_from_daily_bars(bars)
    ma20 = next(item for item in alignment["fib_ma_array"] if item["period"] == 20)

    assert ma20["pullback_touch"] is True
    assert ma20["pullback_acceptance"] is False
    assert ma20["pullback_breakdown"] is False
    assert ma20["touch_reclaim"] is True
    assert ma20["interaction"] == "touch_reclaim"
    assert 20 in alignment["fib_touch_reclaim_periods"]
    assert "触线收回" in alignment["fib_array_summary"]
    assert "回踩承接" not in alignment["fib_array_summary"]


def test_doc_to_rawbar_preserves_market_naive_datetime():
    raw_dt = datetime(2026, 4, 30, 13, 30)

    bar = _doc_to_rawbar(
        {
            "dt": raw_dt,
            "meta": {"symbol": "688381", "freq": "5分钟", "source": "sina"},
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "vol": 1000,
            "amount": 10000,
        },
        "SH.688381",
        Freq.F5,
        1,
    )

    assert bar.dt.to_pydatetime() == raw_dt


def test_load_bars_prefers_canonical_freq_on_duplicate_dt():
    db = _Db({
        "bars": _Collection(docs=[
            {
                "dt": datetime(2026, 4, 23),
                "meta": {"symbol": "002759", "freq": "daily"},
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "vol": 100,
                "amount": 1000,
            },
            {
                "dt": datetime(2026, 4, 23),
                "meta": {"symbol": "002759", "freq": "日线"},
                "open": 3,
                "high": 4,
                "low": 3,
                "close": 4,
                "vol": 200,
                "amount": 2000,
            },
        ]),
    })

    bars = _load_bars(db, "002759", ["日线", "daily"], Freq.D, limit=10, label="日线")

    assert len(bars) == 1
    assert bars[0].close == 4
    assert bars[0].vol == 200


def test_intraday_daily_acceptance_uses_quote_only_inside_a_share_session():
    assert _use_intraday_daily_acceptance(INTRADAY_SCAN_SCOPE, "A", datetime(2026, 5, 12, 14, 30)) is True
    assert _use_intraday_daily_acceptance(INTRADAY_SCAN_SCOPE, "A", datetime(2026, 5, 12, 15, 1)) is False
    assert _use_intraday_daily_acceptance(POSTMARKET_SCAN_SCOPE, "A", datetime(2026, 5, 12, 14, 30)) is False


def test_quote_snapshot_daily_bar_is_intraday_only_overlay():
    daily = [_OhlcvBar(
        dt=datetime(2026, 5, 11),
        open=58.2,
        high=60.43,
        low=57.61,
        close=59.56,
        vol=127793600,
    )]
    db = _Db({
        "quote_snapshots": _Collection(doc={
            "dt": "2026-05-12",
            "symbol": "SZ.002709",
            "code": "002709",
            "open": 59.24,
            "high": 59.8,
            "low": 56.05,
            "close": 58.58,
            "vol": 137992900,
            "amount": 7943891489,
        }),
    })

    assert _append_quote_snapshot_daily_bar(db, "002709", daily, Freq.D, enabled=False) == daily

    with_overlay = _append_quote_snapshot_daily_bar(db, "002709", daily, Freq.D, enabled=True)

    assert len(with_overlay) == 2
    assert with_overlay[-1].dt.to_pydatetime() == datetime(2026, 5, 12)
    assert with_overlay[-1].low == 56.05
    assert with_overlay[-1].close == 58.58


class _Bars:
    def __init__(self, docs):
        self.docs = docs

    def distinct(self, field, query=None):
        freqs = set(((query or {}).get("meta.freq") or {}).get("$in") or [])
        symbols = set(((query or {}).get("meta.symbol") or {}).get("$in") or [])
        values = []
        for doc in self.docs:
            if freqs and doc.get("meta", {}).get("freq") not in freqs:
                continue
            if symbols and doc.get("meta", {}).get("symbol") not in symbols:
                continue
            value = doc.get("meta", {}).get("symbol")
            if value not in values:
                values.append(value)
        return values

    def find_one(self, query=None, projection=None, sort=None):
        freqs = set(((query or {}).get("meta.freq") or {}).get("$in") or [])
        symbols = set(((query or {}).get("meta.symbol") or {}).get("$in") or [])
        rows = [
            doc for doc in self.docs
            if (not freqs or doc.get("meta", {}).get("freq") in freqs)
            and (not symbols or doc.get("meta", {}).get("symbol") in symbols)
        ]
        rows.sort(key=lambda item: item.get("dt"), reverse=True)
        return rows[0] if rows else None


class _Db(dict):
    def __getitem__(self, key):
        return super().__getitem__(key)


class _Cursor(list):
    def sort(self, key, direction=None):
        if isinstance(key, list):
            field, order = key[0]
            reverse = order < 0
        else:
            field, reverse = key, direction == -1
        return _Cursor(sorted(self, key=lambda item: item.get(field), reverse=reverse))

    def limit(self, n):
        return _Cursor(self[:n])


class _Collection:
    def __init__(self, doc=None, docs=None):
        self.doc = doc or {}
        self.docs = docs or []

    def find_one(self, query=None, projection=None, sort=None):
        return self.doc

    def find(self, query=None, projection=None):
        return _Cursor(self.docs)


def test_coverage_by_freq_marks_30m_incomplete_and_15m_on_demand():
    db = _Db({
        "bars": _Bars([
            {"meta": {"symbol": "000001", "freq": "日线"}, "dt": datetime(2026, 4, 28)},
            {"meta": {"symbol": "000002", "freq": "日线"}, "dt": datetime(2026, 4, 28)},
            {"meta": {"symbol": "000001", "freq": "周线"}, "dt": datetime(2026, 4, 28)},
            {"meta": {"symbol": "000002", "freq": "周线"}, "dt": datetime(2026, 4, 28)},
            {"meta": {"symbol": "000001", "freq": "30分钟"}, "dt": datetime(2026, 4, 28, 15, 0)},
        ]),
    })

    coverage = _coverage_by_freq(db, ["000001", "000002"])

    assert coverage["日线"]["status"] == "complete"
    assert coverage["周线"]["status"] == "complete"
    assert coverage["30分钟"]["status"] == "coverage_incomplete"
    assert coverage["30分钟"]["missing_count"] == 1
    assert coverage["15分钟"]["status"] == "on_demand_missing"


def test_postmarket_scope_defaults_to_a_hk_daily_symbols():
    db = _Db({
        "bars": _Bars([
            {"meta": {"symbol": "000001", "freq": "日线", "market": "A"}, "dt": datetime(2026, 4, 28)},
            {"meta": {"symbol": "HK.00700", "freq": "日线", "market": "HK"}, "dt": datetime(2026, 4, 28)},
            {"meta": {"symbol": "US.AAPL", "freq": "日线", "market": "US"}, "dt": datetime(2026, 4, 28)},
        ]),
    })

    symbols, source = _symbols_for_scope(db, POSTMARKET_SCAN_SCOPE)

    assert symbols == ["000001", "HK.00700"]
    assert source == "daily_bars:A+HK"


def test_coverage_by_freq_supports_daily_weekly_a_hk_scan():
    db = _Db({
        "bars": _Bars([
            {"meta": {"symbol": "000001", "freq": "日线", "market": "A"}, "dt": datetime(2026, 4, 28)},
            {"meta": {"symbol": "HK.00700", "freq": "日线", "market": "HK"}, "dt": datetime(2026, 4, 28)},
            {"meta": {"symbol": "HK.00700", "freq": "周线", "market": "HK"}, "dt": datetime(2026, 4, 24)},
        ]),
    })

    coverage = _coverage_by_freq(
        db,
        ["000001", "HK.00700"],
        required_freqs=("日线", "周线"),
        optional_freqs=(),
    )

    assert coverage["日线"]["status"] == "complete"
    assert coverage["周线"]["status"] == "coverage_incomplete"
    assert coverage["周线"]["missing_count"] == 1


def test_intraday_scope_uses_stock_minute_selection_then_terminal_pool():
    db = _Db({
        "sync_log": _Collection(doc={
            "selected_symbols": ["300001", "300002"],
            "priority_symbols": ["300002"],
        }),
        "terminal_stock_pool": _Collection(doc={
            "focus_stocks": [{"raw_code": "300003"}],
            "watch_stocks": [{"symbol": "SZ.300004"}],
            "clue_stocks": [{"code": "300002"}],
        }),
    })

    symbols, source = _symbols_for_scope(db, INTRADAY_SCAN_SCOPE)

    assert symbols == ["300002", "300001", "300003", "300004"]
    assert source == "stock_minute_selection+terminal_stock_pool"


def test_postmarket_candidate_scope_uses_active_terminal_universe(monkeypatch):
    monkeypatch.setenv("TECHNICAL_SIGNAL_POSTMARKET_MAX_SYMBOLS", "3")
    db = _Db({
        "sync_log": _Collection(doc={
            "selected_symbols": ["300001", "300002"],
            "priority_symbols": ["300002"],
        }),
        "terminal_stock_pool": _Collection(doc={
            "focus_stocks": [{"raw_code": "300003"}],
            "watch_stocks": [{"symbol": "SZ.300004"}],
        }),
    })

    symbols, source = _symbols_for_scope(db, POSTMARKET_CANDIDATE_SCAN_SCOPE)

    assert symbols == ["300002", "300001", "300003"]
    assert source == "stock_minute_selection+terminal_stock_pool"


def test_resampled_5m_docs_builds_intraday_30m_bars():
    docs = []
    for idx, minute in enumerate(range(30, 150, 5), start=1):
        hour = 9 + minute // 60
        minute_value = minute % 60
        docs.append({
            "dt": datetime(2026, 5, 6, hour, minute_value),
            "open": idx,
            "high": idx + 1,
            "low": idx - 1,
            "close": idx + 0.5,
            "vol": 100,
            "amount": 1000,
        })
    db = _Db({"bars": _Collection(docs=docs)})

    resampled = _resampled_5m_docs(db, "300001", "30分钟", limit=20)

    assert resampled
    assert resampled[-1]["meta"]["freq"] == "30分钟"
    assert resampled[-1]["meta"]["source"] == "5min_resampled_intraday"
    assert resampled[-1]["dt"] == datetime(2026, 5, 6, 11, 30)
