# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.sync.modules.terminal_pool import _add_reason, _add_signal_rows, _reason_type_for_signal, _selected_rows


class _Cursor(list):
    def sort(self, *args, **kwargs):
        return self

    def limit(self, n):
        return _Cursor(self[:n])


class _Collection:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query=None, projection=None):
        return _Cursor(dict(item) for item in self.docs)


class _Db(dict):
    def __getitem__(self, key):
        return super().__getitem__(key)


def test_terminal_stock_pool_merges_reasons_and_keeps_user_pinned_first():
    rows = {}
    _add_reason(rows, "688802", {
        "reason_type": "custom_signal",
        "source_collection": "signals",
        "source_doc_id": "sig-1",
        "signal_type": "背驰买",
        "signal_side": "buy",
        "signal_family": "chan_style",
        "freq": "30分钟",
        "score": 88,
    }, index_codes=set(), name="沐曦股份")
    _add_reason(rows, "688802", {
        "reason_type": "user_pinned",
        "source_collection": "config",
        "source_doc_id": "priority",
        "signal_type": "用户重点观察",
        "signal_side": "buy",
    }, index_codes=set(), name="沐曦股份")

    selected, skipped = _selected_rows(rows, 1)

    assert skipped == []
    assert selected[0]["raw_code"] == "688802"
    assert selected[0]["signal_origin"] == "user_pinned"
    assert [item["reason_type"] for item in selected[0]["inclusion_reasons"]] == ["user_pinned", "custom_signal"]


def test_terminal_stock_pool_signal_origin_classification_is_explicit():
    assert _reason_type_for_signal({
        "source": "sqlite.backtest.signal_records",
        "signal_type": "背驰买",
    }) == "custom_signal"
    assert _reason_type_for_signal({
        "source": "czsc.engine",
        "signal_type": "三买",
    }) == "chan_signal"
    assert _reason_type_for_signal({
        "source": "sync.signal_pool.generated",
        "signal_type": "日线预警: 跌破二十日均线",
        "pool_status": "warning",
    }) == "generated_risk_signal"


def test_terminal_stock_pool_knowledge_conflict_downgrades_technical_candidate():
    rows = {}
    _add_reason(rows, "300575", {
        "reason_type": "technical_signal",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "tech-1",
        "signal_type": "三买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 120,
        "confidence": 0.8,
        "evidence": {"detail": "hard signal"},
    }, index_codes=set(), name="中旗新材")
    _add_reason(rows, "300575", {
        "reason_type": "knowledge_conflict",
        "source_collection": "knowledge_market_views",
        "source_doc_id": "view-1",
        "signal_type": "知识库看空",
        "signal_side": "buy",
        "sentiment": "看空",
        "knowledge_status": "conflict",
    }, index_codes=set(), name="中旗新材")

    selected, _ = _selected_rows(rows, 1)

    assert selected[0]["action_status"] == "knowledge_conflict"
    assert selected[0]["technical_evidence"]["signal_type"] == "三买"
    assert selected[0]["knowledge_confirmation"]["status"] == "conflict"
    assert "knowledge_conflict" in selected[0]["source_tags"]


def test_terminal_stock_pool_preserves_resonance_context_from_technical_reason():
    rows = {}
    resonance = {
        "direction": "buy",
        "primary_freq": "30分钟",
        "aligned_freqs": ["30分钟", "15分钟"],
        "conflict_freqs": [],
        "grade": "multi_period",
        "tags": ["多周期共振"],
        "summary": "买点信号获得 30分钟,15分钟 确认",
        "latest_dt": "2026-04-28",
    }

    _add_reason(rows, "300575", {
        "reason_type": "technical_signal",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "tech-1",
        "signal_type": "三买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 120,
        "confidence": 0.8,
        "resonance_context": resonance,
        "evidence": {"detail": "hard signal"},
    }, index_codes=set(), name="中旗新材")

    selected, _ = _selected_rows(rows, 1)

    assert selected[0]["resonance_context"]["grade"] == "multi_period"
    assert selected[0]["technical_evidence"]["resonance_context"]["tags"] == ["多周期共振"]


def test_terminal_stock_pool_uses_latest_screen_signals_before_generated_daily_candidates():
    db = _Db({
        "signals": _Collection([
            {
                "symbol": "SH.600487",
                "signal_type": "日线候选: 活跃池趋势增强",
                "freq": "日线",
                "pool_status": "candidate",
                "score": 95,
                "source": "sync.signal_pool.generated",
                "signal_date": "2026-04-28",
            },
            {
                "symbol": "SZ.002029",
                "signal_type": "趋势买",
                "freq": "30分钟",
                "pool_status": "candidate",
                "score": 80,
                "source": "sqlite.backtest.signal_records",
                "signal_date": "2026-04-28",
            },
            {
                "symbol": "SZ.002029",
                "signal_type": "背驰买",
                "freq": "15分钟",
                "pool_status": "candidate",
                "score": 70,
                "source": "sqlite.backtest.signal_records",
                "signal_date": "2026-04-28",
            },
        ]),
    })
    rows = {}

    _add_signal_rows(rows, db, index_codes=set())
    selected, _ = _selected_rows(rows, 10)

    assert [item["raw_code"] for item in selected] == ["002029"]
    assert selected[0]["signal_origin"] == "technical_signal"
    assert selected[0]["resonance_context"]["grade"] == "multi_period"
    assert selected[0]["technical_evidence"]["source_collection"] == "signals"
