# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

import config
import pytest
from signals.sync.modules import stock_minute
from signals.sync.task_context import task_env


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
    assert "000680" in stock_minute._index_codes()
    assert "513130" not in stock_minute._index_codes()


def test_stock_minute_worker_count_is_constrained(monkeypatch):
    monkeypatch.setenv("STOCK_MINUTE_WORKERS", "99")
    assert stock_minute._worker_count() == 6

    monkeypatch.setenv("STOCK_MINUTE_WORKERS", "0")
    assert stock_minute._worker_count() == 1


def test_stock_minute_runtime_timing_reads_task_env(monkeypatch):
    monkeypatch.setenv("STOCK_MINUTE_CALL_INTERVAL", "0.5")
    monkeypatch.setenv("STOCK_MINUTE_TIMEOUT", "5")

    with task_env({"STOCK_MINUTE_CALL_INTERVAL": "0.15", "STOCK_MINUTE_TIMEOUT": "3"}):
        assert stock_minute._call_interval() == 0.15
        assert stock_minute._public_timeout() == 3.0


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


def test_stock_minute_strict_public_errors_reraises(monkeypatch):
    def fake_fetch_public_minute(*args, **kwargs):
        raise RuntimeError("sina: provider_cooling_down")

    monkeypatch.setattr(stock_minute, "fetch_public_minute", fake_fetch_public_minute)

    with task_env({"STOCK_MINUTE_STRICT_PUBLIC_ERRORS": "true"}):
        with pytest.raises(RuntimeError, match="provider_cooling_down"):
            stock_minute._sync_one_minute("920118", "30分钟", db=None)


def test_stock_minute_cap_splits_intraday_and_close(monkeypatch):
    monkeypatch.setattr(stock_minute, "naive_market_now", lambda _market: datetime(2026, 4, 29, 10, 0))
    monkeypatch.setenv("SIGNALS_CURRENT_SYNC_LANE", "signal_lane")
    monkeypatch.setenv("SIGNALS_CURRENT_SYNC_MARKET", "A")
    monkeypatch.setenv("STOCK_MINUTE_SIGNAL_MAX_CODES", "24")
    monkeypatch.setenv("STOCK_MINUTE_CLOSE_MAX_CODES", "96")
    assert stock_minute._selection_cap() == 24

    monkeypatch.delenv("SIGNALS_CURRENT_SYNC_MARKET", raising=False)
    assert stock_minute._selection_cap() == 96


def test_stock_minute_opening_phase_caps_symbols_and_freqs(monkeypatch):
    monkeypatch.setattr(stock_minute, "naive_market_now", lambda _market: datetime(2026, 4, 29, 9, 35))
    monkeypatch.setenv("SIGNALS_CURRENT_SYNC_LANE", "signal_lane")
    monkeypatch.setenv("SIGNALS_CURRENT_SYNC_MARKET", "A")
    monkeypatch.setenv("STOCK_MINUTE_SIGNAL_MAX_CODES", "72")
    monkeypatch.setenv("STOCK_MINUTE_OPENING_MAX_CODES", "24")
    monkeypatch.delenv("STOCK_MINUTE_FREQS", raising=False)
    monkeypatch.delenv("STOCK_MINUTE_OPENING_ALL_FREQS", raising=False)

    assert stock_minute._selection_cap() == 24
    assert stock_minute._active_minute_freqs() == ["5分钟", "15分钟", "30分钟"]

    monkeypatch.setenv("STOCK_MINUTE_OPENING_ALL_FREQS", "true")
    assert stock_minute._active_minute_freqs() == ["5分钟", "15分钟", "30分钟"]


def test_stock_minute_opening_phase_defaults_to_signal_cap(monkeypatch):
    monkeypatch.setattr(stock_minute, "naive_market_now", lambda _market: datetime(2026, 4, 29, 9, 15))
    monkeypatch.setenv("SIGNALS_CURRENT_SYNC_LANE", "signal_lane")
    monkeypatch.setenv("SIGNALS_CURRENT_SYNC_MARKET", "A")
    monkeypatch.setenv("STOCK_MINUTE_SIGNAL_MAX_CODES", "72")
    monkeypatch.delenv("STOCK_MINUTE_OPENING_MAX_CODES", raising=False)

    assert stock_minute._selection_cap() == 72


def test_stock_minute_signal_lane_defaults_to_all_realtime_freqs(monkeypatch):
    monkeypatch.setattr(stock_minute, "naive_market_now", lambda _market: datetime(2026, 4, 29, 10, 5))
    monkeypatch.setenv("SIGNALS_CURRENT_SYNC_LANE", "signal_lane")
    monkeypatch.setenv("SIGNALS_CURRENT_SYNC_MARKET", "A")
    monkeypatch.setenv("STOCK_MINUTE_FREQS", "5min,15min,30min")
    monkeypatch.delenv("STOCK_MINUTE_SIGNAL_FREQS", raising=False)
    monkeypatch.delenv("STOCK_MINUTE_SIGNAL_TAIL_COUNT_5", raising=False)
    monkeypatch.delenv("STOCK_MINUTE_SIGNAL_ALL_FREQS", raising=False)

    assert stock_minute._active_minute_freqs() == ["5分钟", "15分钟", "30分钟"]
    assert stock_minute._tail_count_for_freq("5分钟") == 80

    monkeypatch.setenv("STOCK_MINUTE_SIGNAL_FREQS", "5min,15min")
    monkeypatch.setenv("STOCK_MINUTE_SIGNAL_TAIL_COUNT_5", "120")
    assert stock_minute._active_minute_freqs() == ["5分钟", "15分钟"]
    assert stock_minute._tail_count_for_freq("5分钟") == 120

    monkeypatch.setenv("STOCK_MINUTE_SIGNAL_ALL_FREQS", "true")
    assert stock_minute._active_minute_freqs() == ["5分钟", "15分钟"]


def test_postmarket_minute_scope_uses_expanded_candidate_cap(monkeypatch):
    monkeypatch.setenv("STOCK_MINUTE_SCOPE", "postmarket_candidates")
    monkeypatch.setenv("STOCK_MINUTE_POSTMARKET_MAX_CODES", "360")
    monkeypatch.setenv("SIGNALS_CURRENT_SYNC_LANE", "signal_lane")
    monkeypatch.setenv("STOCK_MINUTE_CLOSE_MAX_CODES", "72")

    assert stock_minute._selection_cap() == 360


def test_postmarket_minute_freqs_read_task_env(monkeypatch):
    monkeypatch.delenv("STOCK_MINUTE_FREQS", raising=False)
    monkeypatch.delenv("SIGNALS_CURRENT_SYNC_LANE", raising=False)
    monkeypatch.delenv("SIGNALS_CURRENT_SYNC_MARKET", raising=False)

    with task_env({"STOCK_MINUTE_SCOPE": "postmarket_candidates", "STOCK_MINUTE_FREQS": "5min,15min"}):
        assert stock_minute._active_minute_freqs() == ["5分钟", "15分钟"]


def test_postmarket_minute_cap_reads_task_env(monkeypatch):
    monkeypatch.delenv("STOCK_MINUTE_POSTMARKET_MAX_CODES", raising=False)

    with task_env({"STOCK_MINUTE_SCOPE": "postmarket_candidates", "STOCK_MINUTE_POSTMARKET_MAX_CODES": "120"}):
        assert stock_minute._selection_cap() == 120


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


def test_postmarket_minute_selection_consumes_pending_before_cached_pinned():
    selected, skipped = stock_minute._select_postmarket_minute_symbols(
        ["300001", "300002", "300003"],
        {"300001", "300002", "300003"},
        max_symbols=2,
        pinned={"300001", "300003"},
        last_runs={"300001": "2026-05-08T15:00:00"},
        universe_states={
            "300001": {"status": "cached"},
            "300002": {"status": "pending"},
            "300003": {"status": "pending"},
        },
    )

    assert selected == ["300003", "300002"]
    assert skipped == [
        {
            "symbol": "300001",
            "reason": "postmarket_universe_pending",
            "next_due_hint": "next postmarket minute preheat rotation",
        }
    ]


def test_postmarket_minute_selection_merges_terminal_skipped_and_signal_sources(monkeypatch):
    monkeypatch.setenv("STOCK_MINUTE_SCOPE", "postmarket_candidates")
    monkeypatch.setenv("STOCK_MINUTE_POSTMARKET_MAX_CODES", "10")
    monkeypatch.setattr(stock_minute, "_iter_static_chain_representative_symbols", lambda: [])

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

    assert selected == ["300005", "300006", "300001", "300002", "300003", "300004"]
    assert meta["minute_scope"] == "postmarket_candidates"
    assert meta["rotation_policy"] == "postmarket_expanded_candidate_preheat"
    assert meta["source_counts"]["terminal_stock_pool_skipped"] == 1
    assert meta["source_counts"]["terminal_technical_signals"] == 1
    assert meta["source_counts"]["knowledge_market_views"] == 1
    assert meta["source_counts"]["chain_representatives"] == 1
    assert meta["source_counts"]["chain_domain_leaders"] == 1


def test_postmarket_minute_selection_pins_visible_chain_representatives(monkeypatch):
    monkeypatch.setenv("STOCK_MINUTE_SCOPE", "postmarket_candidates")
    monkeypatch.setenv("STOCK_MINUTE_POSTMARKET_MAX_CODES", "3")
    monkeypatch.setattr(stock_minute, "_iter_static_chain_representative_symbols", lambda: [])

    db = _Db({
        "terminal_stock_pool": _Collection(doc={"stocks": []}),
        "terminal_technical_signals": _Collection(docs=[
            {"raw_code": "300001"},
            {"raw_code": "300002"},
            {"raw_code": "300003"},
            {"raw_code": "300004"},
        ]),
        "knowledge_market_views": _Collection(docs=[
            {"raw_code": "300005"},
            {"raw_code": "300006"},
        ]),
        "chain_heat_snapshots": _Collection(
            doc={"trade_minute": "2026-05-06 14:59"},
            docs=[{
                "representatives": [{"symbol": "SH.601899"}],
                "integrated_domains": [{"leader_symbol": "SZ.002466"}],
            }],
        ),
        "chain_node_security_rollups": _Collection(
            doc={"trade_date": "2026-05-06"},
            docs=[{
                "top_securities": [
                    {"symbol": "SH.600941", "raw_code": "600941", "name": "中国移动"},
                ],
            }],
        ),
        "sync_log": _Collection(),
    })

    selected, meta = stock_minute._get_active_symbols_with_meta(db)

    assert {"601899", "002466", "600941"} <= set(selected)
    assert set(meta["pinned_symbols"]) == {"601899", "002466", "600941"}
    assert meta["source_counts"]["chain_rebuild_rollups"] == 1
    assert meta["source_counts"]["chain_representatives"] == 1
    assert meta["source_counts"]["chain_domain_leaders"] == 1


def test_postmarket_minute_selection_pins_static_chain_representatives(monkeypatch):
    monkeypatch.setenv("STOCK_MINUTE_SCOPE", "postmarket_candidates")
    monkeypatch.setenv("STOCK_MINUTE_POSTMARKET_MAX_CODES", "3")
    monkeypatch.setattr(
        stock_minute,
        "_iter_static_chain_representative_symbols",
        lambda: ["SH.600941", "SH.600050", "SH.603019"],
    )

    db = _Db({
        "terminal_stock_pool": _Collection(doc={"stocks": []}),
        "chain_heat_snapshots": _Collection(),
        "chain_node_security_rollups": _Collection(),
        "sync_log": _Collection(),
    })

    selected, meta = stock_minute._get_active_symbols_with_meta(db)

    assert selected == ["600941", "600050", "603019"]
    assert set(meta["pinned_symbols"]) == {"600941", "600050", "603019"}
    assert meta["source_counts"]["semantic_industry_chain_representatives"] == 3


def test_pinned_candidate_source_promotes_existing_terminal_symbol(monkeypatch):
    monkeypatch.setenv("STOCK_MINUTE_SCOPE", "postmarket_candidates")
    monkeypatch.setenv("STOCK_MINUTE_POSTMARKET_MAX_CODES", "2")
    monkeypatch.setattr(
        stock_minute,
        "_iter_static_chain_representative_symbols",
        lambda: ["SH.600941", "SH.600050"],
    )

    db = _Db({
        "terminal_stock_pool": _Collection(doc={
            "stocks": [
                {"raw_code": "300001"},
                {"raw_code": "600941"},
                {"raw_code": "300002"},
            ],
        }),
        "chain_heat_snapshots": _Collection(),
        "chain_node_security_rollups": _Collection(),
        "sync_log": _Collection(),
    })

    selected, meta = stock_minute._get_active_symbols_with_meta(db)

    assert selected == ["600941", "600050"]
    assert set(meta["pinned_symbols"]) == {"600941", "600050"}


def test_split_current_minute_tasks_skips_already_closed_bars():
    tasks = [("300001", "5分钟"), ("300001", "15分钟"), ("300002", "5分钟")]
    expected = {
        "5分钟": datetime(2026, 4, 29, 15, 0),
        "15分钟": datetime(2026, 4, 29, 15, 0),
    }
    latest = {
        ("300001", "5分钟"): datetime(2026, 4, 29, 15, 0),
        ("300001", "15分钟"): datetime(2026, 4, 29, 14, 45),
        ("300002", "5分钟"): datetime(2026, 4, 29, 15, 5),
    }

    pending, current = stock_minute._split_current_minute_tasks(tasks, latest, expected)

    assert pending == [("300001", "15分钟")]
    assert current == [
        ("300001", "5分钟", datetime(2026, 4, 29, 15, 0)),
        ("300002", "5分钟", datetime(2026, 4, 29, 15, 5)),
    ]
