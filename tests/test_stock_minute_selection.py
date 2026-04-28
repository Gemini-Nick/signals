# -*- coding: utf-8 -*-
from __future__ import annotations

import config
from signals.sync.modules import stock_minute


class _Cursor(list):
    def sort(self, *args, **kwargs):
        return self

    def limit(self, n):
        return _Cursor(self[:n])


class _Collection:
    def __init__(self, doc=None, docs=None):
        self.doc = doc or {}
        self.docs = docs or []

    def find_one(self, *args, **kwargs):
        return self.doc

    def find(self, *args, **kwargs):
        return _Cursor(self.docs)


class _Db(dict):
    def __missing__(self, key):
        self[key] = _Collection()
        return self[key]


def test_priority_selection_respects_signal_lane_cap():
    selected, skipped = stock_minute._select_symbols_with_priority(
        ["688802", "300575", "600000"],
        {"688802", "300575"},
        max_symbols=1,
    )

    assert selected == ["688802"]
    assert skipped == [
        {"symbol": "300575", "reason": "rotation_pending_priority", "next_due_hint": "stale-first signal_lane rotation"},
        {"symbol": "600000", "reason": "rotation_pending", "next_due_hint": "stale-first signal_lane rotation"},
    ]


def test_selection_rotates_stale_priority_after_pinned():
    selected, skipped = stock_minute._select_symbols_with_priority(
        ["688802", "300575", "688396", "600000"],
        {"688802", "300575", "688396"},
        max_symbols=3,
        pinned={"688802", "300575"},
        last_runs={"688802": "2026-04-28 11:30:00", "300575": "2026-04-28 11:30:00"},
    )

    assert selected == ["688802", "300575", "688396"]
    assert skipped == [
        {"symbol": "600000", "reason": "rotation_pending", "next_due_hint": "stale-first signal_lane rotation"}
    ]


def test_shanghai_composite_is_treated_as_index_not_stock():
    assert config.INDEX_AK_CODES["上证指数"] == "sh000001"
    assert "000001" in stock_minute._index_codes()


def test_stock_minute_worker_count_is_constrained(monkeypatch):
    monkeypatch.setenv("STOCK_MINUTE_WORKERS", "99")
    assert stock_minute._worker_count() == 6

    monkeypatch.setenv("STOCK_MINUTE_WORKERS", "0")
    assert stock_minute._worker_count() == 1


def test_stock_minute_tail_count_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("STOCK_MINUTE_TAIL_COUNT", raising=False)
    monkeypatch.delenv("STOCK_MINUTE_TAIL_COUNT_5", raising=False)
    monkeypatch.delenv("STOCK_MINUTE_TAIL_COUNT_15", raising=False)
    monkeypatch.delenv("STOCK_MINUTE_TAIL_COUNT_30", raising=False)

    assert stock_minute._tail_count_for_freq("5分钟") == 240
    assert stock_minute._tail_count_for_freq("15分钟") == 160
    assert stock_minute._tail_count_for_freq("30分钟") == 120

    monkeypatch.setenv("STOCK_MINUTE_TAIL_COUNT", "80")
    monkeypatch.setenv("STOCK_MINUTE_TAIL_COUNT_30", "90")
    assert stock_minute._tail_count_for_freq("5分钟") == 80
    assert stock_minute._tail_count_for_freq("30分钟") == 90


def test_stock_minute_cap_splits_intraday_and_close(monkeypatch):
    monkeypatch.setenv("SIGNALS_CURRENT_SYNC_LANE", "signal_lane")
    monkeypatch.setenv("SIGNALS_CURRENT_SYNC_MARKET", "A")
    monkeypatch.setenv("STOCK_MINUTE_SIGNAL_MAX_CODES", "24")
    monkeypatch.setenv("STOCK_MINUTE_CLOSE_MAX_CODES", "96")
    assert stock_minute._selection_cap() == 24

    monkeypatch.delenv("SIGNALS_CURRENT_SYNC_MARKET", raising=False)
    assert stock_minute._selection_cap() == 96


def test_postmarket_minute_scope_uses_expanded_candidate_cap(monkeypatch):
    monkeypatch.setenv("STOCK_MINUTE_SCOPE", "postmarket_candidates")
    monkeypatch.setenv("STOCK_MINUTE_POSTMARKET_MAX_CODES", "360")
    monkeypatch.setenv("SIGNALS_CURRENT_SYNC_LANE", "signal_lane")
    monkeypatch.setenv("STOCK_MINUTE_CLOSE_MAX_CODES", "72")

    assert stock_minute._selection_cap() == 360


def test_postmarket_minute_selection_consumes_pending_universe_before_cached():
    selected, skipped = stock_minute._select_postmarket_minute_symbols(
        ["300001", "300002", "300003", "300004"],
        {"300001", "300003"},
        max_symbols=2,
        pinned=set(),
        last_runs={},
        universe_states={
            "300001": {"status": "cached"},
            "300002": {"status": "pending"},
            "300003": {"status": "error"},
            "300004": {"status": "cached"},
        },
    )

    assert selected == ["300003", "300002"]
    assert skipped == [
        {
            "symbol": "300001",
            "reason": "postmarket_universe_pending",
            "next_due_hint": "next postmarket minute preheat rotation",
        },
        {
            "symbol": "300004",
            "reason": "postmarket_universe_pending",
            "next_due_hint": "next postmarket minute preheat rotation",
        },
    ]


def test_postmarket_minute_selection_merges_terminal_skipped_and_signal_sources(monkeypatch):
    monkeypatch.setenv("STOCK_MINUTE_SCOPE", "postmarket_candidates")
    monkeypatch.setenv("STOCK_MINUTE_POSTMARKET_MAX_CODES", "10")

    db = _Db({
        "terminal_stock_pool": _Collection(doc={
            "candidate_count": 6,
            "stocks": [
                {
                    "raw_code": "300001",
                    "inclusion_reasons": [{"reason_type": "technical_signal"}],
                }
            ],
            "skipped_stocks": [
                {
                    "raw_code": "300002",
                    "signal_origin": "technical_signal",
                    "inclusion_reasons": [{"reason_type": "technical_signal"}],
                }
            ],
        }),
        "terminal_technical_signals": _Collection(docs=[{"raw_code": "300003"}]),
        "knowledge_market_views": _Collection(docs=[{"raw_code": "300004"}]),
        "chain_heat_snapshots": _Collection(
            doc={"trade_minute": "2026-04-28 15:00"},
            docs=[{
                "representatives": [{"symbol": "300005"}],
                "integrated_domains": [{"leader_symbol": "300006"}],
            }],
        ),
        "sync_log": _Collection(),
    })

    selected, meta = stock_minute._get_active_symbols_with_meta(db)

    assert selected == ["300001", "300002", "300003", "300004", "300005", "300006"]
    assert meta["minute_scope"] == "postmarket_candidates"
    assert meta["rotation_policy"] == "postmarket_expanded_candidate_preheat"
    assert meta["source_counts"]["terminal_stock_pool_skipped"] == 1
    assert meta["source_counts"]["terminal_technical_signals"] == 1
    assert meta["source_counts"]["knowledge_market_views"] == 1
    assert meta["source_counts"]["chain_representatives"] == 1
    assert meta["source_counts"]["chain_domain_leaders"] == 1
