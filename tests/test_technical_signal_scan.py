# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from czsc import Freq

from signals.sync.modules.technical_signal_scan import _coverage_by_freq, _doc_to_rawbar, _resonance_context


@dataclass
class _Event:
    freq: str
    signal_type: str
    confidence: float = 1.0
    dt: datetime = datetime(2026, 4, 28, 15, 0, 0)


WEIGHTS = {
    "三买": 100,
    "趋势买": 80,
    "背驰买": 70,
    "一卖": -100,
}


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


class _Bars:
    def __init__(self, docs):
        self.docs = docs

    def distinct(self, field, query=None):
        freqs = set(((query or {}).get("meta.freq") or {}).get("$in") or [])
        values = []
        for doc in self.docs:
            if freqs and doc.get("meta", {}).get("freq") not in freqs:
                continue
            value = doc.get("meta", {}).get("symbol")
            if value not in values:
                values.append(value)
        return values

    def find_one(self, query=None, projection=None, sort=None):
        freqs = set(((query or {}).get("meta.freq") or {}).get("$in") or [])
        rows = [doc for doc in self.docs if not freqs or doc.get("meta", {}).get("freq") in freqs]
        rows.sort(key=lambda item: item.get("dt"), reverse=True)
        return rows[0] if rows else None


class _Db(dict):
    def __getitem__(self, key):
        return super().__getitem__(key)


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
