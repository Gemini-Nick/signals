# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.sync.modules.terminal_pool import _add_fallback_watch_rows, _add_reason, _add_signal_rows, _add_user_pinned, _reason_type_for_signal, _selected_rows, _split_pool_rows


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

    def find_one(self, query=None, projection=None, sort=None):
        return dict(self.docs[0]) if self.docs else None


class _Db(dict):
    def __getitem__(self, key):
        return super().__getitem__(key)


def test_terminal_stock_pool_keeps_manual_focus_as_context_not_signal_leader():
    rows = {}
    _add_reason(rows, "688802", {
        "reason_type": "technical_trigger",
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
    assert selected[0]["signal_origin"] == "technical_trigger"
    assert [item["reason_type"] for item in selected[0]["inclusion_reasons"]] == ["technical_trigger", "user_pinned"]


def test_terminal_stock_pool_has_no_default_user_observation(monkeypatch):
    from datetime import datetime

    monkeypatch.delenv("TERMINAL_REALTIME_PRIORITY_CODES", raising=False)
    rows = {}
    _add_user_pinned(rows, index_codes=set(), now=datetime(2026, 4, 28))

    assert rows == {}


def test_terminal_stock_pool_signal_origin_classification_is_explicit():
    assert _reason_type_for_signal({
        "source": "sqlite.backtest.signal_records",
        "signal_type": "背驰买",
    }) == "historical_signal_record"
    assert _reason_type_for_signal({
        "source": "czsc.engine",
        "signal_type": "三买",
    }) == "technical_trigger"
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
        "resonance_context": {
            "direction": "buy",
            "aligned_freqs": ["30分钟", "15分钟"],
            "conflict_freqs": [],
            "grade": "multi_period",
        },
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
        "knowledge_effect": "block",
        "decision_effect": "block",
    }, index_codes=set(), name="中旗新材")

    selected, _ = _selected_rows(rows, 1)

    assert selected[0]["action_status"] == "knowledge_blocked"
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


def test_terminal_stock_pool_single_period_trigger_is_watch_only():
    rows = {}
    _add_reason(rows, "300575", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "tech-single",
        "signal_type": "三买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 120,
    }, index_codes=set(), name="中旗新材")

    selected, _ = _selected_rows(rows, 1)

    assert selected[0]["action_status"] == "technical_watch"
    assert selected[0]["queue_lane"] == "context_only"
    assert selected[0]["actionability"] == "observe_only"


def test_terminal_stock_pool_chain_context_cannot_create_focus_without_technical_trigger():
    chain_reason = {
        "reason_type": "chain_core_rep",
        "source_collection": "chain_heat_snapshots",
        "source_doc_id": "semiconductor:foundry:2026-04-28T10:30",
        "signal_type": "chain_consensus_climax",
        "signal_side": "neutral",
        "source_role": "context",
        "decision_effect": "exit_priority",
        "can_create_candidate": False,
        "chain_id": "semiconductor",
        "node_id": "foundry",
        "board_or_concept": "半导体",
        "evidence": {"phase": "consensus_climax", "heat_score": 88},
    }
    rows = {}
    _add_reason(rows, "688981", chain_reason, index_codes=set(), name="中芯国际")
    assert rows == {}

    _add_reason(rows, "688981", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "tech-ready",
        "signal_type": "三买",
        "signal_side": "buy",
        "freq": "15分钟",
        "score": 120,
        "resonance_context": {
            "direction": "buy",
            "aligned_freqs": ["30分钟", "15分钟"],
            "conflict_freqs": [],
            "grade": "multi_period",
        },
    }, index_codes=set(), name="中芯国际")
    _add_reason(rows, "688981", chain_reason, index_codes=set(), name="中芯国际")
    selected, _ = _selected_rows(rows, 1)

    assert selected[0]["chain_context"]["effect"] == "exit_priority"
    assert selected[0]["action_status"] == "chain_risk_review"
    assert selected[0]["queue_lane"] == "risk_exit_first"


def test_terminal_stock_pool_historical_records_do_not_create_candidates():
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

    assert selected == []
    assert rows == {}

    _add_reason(rows, "002029", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "tech-ready",
        "signal_type": "三买",
        "signal_side": "buy",
        "freq": "15分钟",
        "score": 120,
        "resonance_context": {
            "direction": "buy",
            "aligned_freqs": ["30分钟", "15分钟"],
            "conflict_freqs": [],
            "grade": "multi_period",
        },
    }, index_codes=set(), name="七匹狼")
    _add_signal_rows(rows, db, index_codes=set())
    selected, _ = _selected_rows(rows, 10)
    history_reason = next(item for item in selected[0]["inclusion_reasons"] if item["reason_type"] == "historical_signal_record")

    assert history_reason["score"] == 0
    assert history_reason["backtest_quality"]["score"] == 0
    assert history_reason["decision_effect"] == "history_pending"


def test_terminal_stock_pool_fallback_watch_is_observation_only():
    from datetime import datetime

    db = _Db({
        "strategy_snapshots": _Collection([
            {
                "_id": "strategy:2026-04-29",
                "as_of": "2026-04-29",
                "snapshot": {
                    "candidates": [
                        {
                            "symbol": "SZ.002812",
                            "name": "恩捷股份",
                            "score": 96.7,
                            "reason": "背驰买",
                            "metadata": {
                                "freq": "15分钟",
                                "source": "sqlite.backtest.signal_records",
                            },
                        },
                    ],
                },
            },
        ]),
        "market_pools": _Collection([]),
    })
    rows = {}

    added = _add_fallback_watch_rows(rows, db, index_codes=set(), limit=3, now=datetime(2026, 4, 29))
    selected, _ = _selected_rows(rows, 3)

    assert added == 1
    assert selected[0]["raw_code"] == "002812"
    assert selected[0]["signal_origin"] == "fallback_watch"
    assert selected[0]["action_status"] == "fallback_watch"
    assert selected[0]["queue_lane"] == "fallback_watch"
    assert selected[0]["actionability"] == "observe_only"


def test_terminal_stock_pool_splits_buy_entries_from_risk_controls():
    rows = {}
    _add_reason(rows, "300575", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "buy-ready",
        "signal_type": "三买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 120,
        "confidence": 0.8,
        "resonance_context": {
            "direction": "buy",
            "aligned_freqs": ["日线", "30分钟"],
            "conflict_freqs": [],
            "grade": "multi_period",
        },
    }, index_codes=set(), name="中旗新材")
    _add_reason(rows, "688484", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "risk-high",
        "signal_type": "一卖",
        "signal_side": "sell",
        "freq": "日线",
        "score": 200,
        "confidence": 0.9,
        "resonance_context": {
            "direction": "sell",
            "aligned_freqs": ["日线", "30分钟"],
            "conflict_freqs": [],
            "grade": "multi_period",
        },
    }, index_codes=set(), name="风险股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert [row["raw_code"] for row in split["focus"]] == ["300575"]
    assert [row["raw_code"] for row in split["risk"]] == ["688484"]
    assert split["focus"][0]["pool_type"] == "focus"
    assert split["focus"][0]["entry_gate_status"] == "entry_confirmed"
    assert split["risk"][0]["pool_type"] == "risk"


def test_terminal_stock_pool_single_period_buy_waits_in_watch_pool():
    rows = {}
    _add_reason(rows, "300575", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "single-30m",
        "signal_type": "三买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 120,
        "confidence": 0.8,
        "resonance_context": {
            "direction": "buy",
            "aligned_freqs": ["30分钟"],
            "conflict_freqs": [],
            "grade": "single_period",
        },
    }, index_codes=set(), name="中旗新材")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["focus"] == []
    assert split["risk"] == []
    assert split["watch"][0]["raw_code"] == "300575"
    assert split["watch"][0]["entry_gate_status"] == "entry_waiting_resonance_confirm"


def test_terminal_stock_pool_strategy_fallback_only_goes_to_watch_pool():
    from datetime import datetime

    db = _Db({
        "strategy_snapshots": _Collection([
            {
                "_id": "strategy:2026-04-29",
                "as_of": "2026-04-29",
                "snapshot": {
                    "candidates": [
                        {
                            "symbol": "SZ.002812",
                            "name": "恩捷股份",
                            "score": 96.7,
                            "reason": "背驰买",
                            "metadata": {"freq": "15分钟", "source": "sqlite.backtest.signal_records"},
                        },
                    ],
                },
            },
        ]),
        "market_pools": _Collection([]),
    })
    rows = {}

    _add_fallback_watch_rows(rows, db, index_codes=set(), limit=3, now=datetime(2026, 4, 29))
    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["focus"] == []
    assert split["risk"] == []
    assert split["watch"][0]["raw_code"] == "002812"
    assert split["watch"][0]["pool_type"] == "watch"
