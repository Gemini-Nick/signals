# -*- coding: utf-8 -*-
from __future__ import annotations

from bson import BSON

from signals.sync.modules.terminal_pool import _add_fallback_watch_rows, _add_reason, _add_signal_rows, _add_user_pinned, _backfill_watch_from_clue_candidates, _default_opportunity_candidate_rows, _entry_age_limit, _fib_ma_support_score_from_alignment, _reason_is_current_for_entry, _reason_type_for_signal, _selected_rows, _split_pool_rows
from signals.sync.modules.terminal_pool import _slim_pool_row_for_storage


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


def _ma_alignment(*, above5=True, above10=True, above20=True, near10=False, near20=False, score=44, fib=False):
    return {
        "above_ma5": above5,
        "above_ma10": above10,
        "above_ma20": above20,
        "near_ma10": near10,
        "near_ma20": near20,
        "reclaim_ma5": False,
        "reclaim_ma10": False,
        "reclaim_ma20": False,
        **({
            "ma8": 10.1,
            "ma13": 10.0,
            "ma21": 10.0,
            "above_ma8": True,
            "above_ma13": True,
            "above_ma21": True,
            "near_ma8": False,
            "near_ma13": True,
            "near_ma21": True,
            "reclaim_ma8": False,
            "reclaim_ma13": False,
            "reclaim_ma21": False,
            "fib_ma_array": [
                {
                    "period": 13,
                    "name": "MA13",
                    "value": 10.0,
                    "pullback_touch": True,
                    "pullback_acceptance": True,
                    "acceptance_score": 2.5,
                },
                {
                    "period": 21,
                    "name": "MA21",
                    "value": 10.0,
                    "pullback_touch": True,
                    "pullback_acceptance": True,
                    "acceptance_score": 3.5,
                },
                {
                    "period": 8,
                    "name": "MA8",
                    "value": 10.1,
                    "pullback_touch": False,
                    "pullback_acceptance": False,
                    "acceptance_score": 0.0,
                },
            ],
            "fib_accept_count": 2,
            "fib_accept_periods": [13, 21],
            "fib_touch_count": 2,
            "fib_touch_periods": [13, 21],
            "fib_above_count": 3,
            "fib_reclaim_count": 0,
            "fib_support_score": 6.0,
        } if fib else {}),
        "ma_stack": "bullish" if above5 and above10 and above20 else "mixed",
        "ma20_direction": "向上",
        "above_count": sum(1 for value in (above5, above10, above20) if value),
        "score": score,
        "summary": "站上20日线 / 站上10日线 / 站上5日线",
        "tags": ["站上20日线", "站上10日线", "站上5日线"],
    }


def test_terminal_stock_pool_uses_macro_etf_symbol_and_name():
    rows = {}
    _add_reason(rows, "562590", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "etf-30m",
        "signal_type": "二买",
        "signal_side": "buy",
        "freq": "30分钟",
    }, index_codes=set())

    row = rows["562590"]

    assert row["symbol"] == "SH.562590"
    assert row["code"] == "SH.562590"
    assert row["target_label"] == "SH.562590"
    assert row["name"] == "半导体设备ETF"


def _add_chain_context(
    rows,
    code,
    *,
    chain_name,
    node_name,
    phase="warming",
    rank=6,
    reason_type="chain_core_rep",
    name="产业链标的",
):
    _add_reason(rows, code, {
        "reason_type": reason_type,
        "source_collection": "security_chain_memberships",
        "source_doc_id": f"chain-{code}",
        "chain_id": chain_name,
        "chain_name": chain_name,
        "node_id": node_name,
        "node_name": node_name,
        "board_or_concept": node_name,
        "signal_type": "产业链主线",
        "signal_side": "buy",
        "evidence": {
            "chain_name": chain_name,
            "node_name": node_name,
            "board_or_concept": node_name,
            "phase": phase,
            "rank": rank,
            "heat_score": 88,
        },
    }, index_codes=set(), name=name)


def _set_broad_market(rows, *, falling=True, change_pct=-0.8, index_setup_side=None, volume_state="normal", volume_ratio=1.0):
    setup_side = index_setup_side or ("left_sell" if falling else "left_buy")
    setup_label = {
        "right_buy": "指数右侧买",
        "left_buy": "指数左侧买",
        "left_sell": "指数左侧卖",
        "right_sell": "指数右侧卖",
        "unknown": "指数未知",
    }.get(setup_side, "指数未知")
    context = {
        "is_falling": falling,
        "label": setup_label,
        "index_setup_side": setup_side,
        "index_setup_label": setup_label,
        "volume_state": volume_state,
        "volume_label": {"expanding": "放量", "shrinking": "缩量", "normal": "量能正常", "unknown": "量能未知"}.get(volume_state, "量能未知"),
        "volume_ratio_5d": volume_ratio,
        "average_change_pct": change_pct,
        "indexes": [
            {"name": "上证指数", "change_pct": change_pct},
            {"name": "沪深300", "change_pct": change_pct},
            {"name": "创业板指", "change_pct": change_pct},
        ],
    }
    for row in rows.values():
        row["broad_market_context"] = context


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


def test_terminal_stock_pool_skips_b_share_codes():
    rows = {}

    _add_reason(rows, "SH.900939", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "b-share-buy",
        "signal_type": "趋势买",
        "signal_side": "buy",
        "freq": "5分钟",
        "score": 99,
    }, index_codes=set(), name="汇丽B")

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


def test_trade_role_does_not_guess_from_industry_keywords():
    from signals.sync.modules.terminal_pool import _trade_role_from_context

    row = {
        "name": "测试股票",
        "reason": "电池 煤炭 CPO 都只是文本噪音",
        "setup_explanation": "等确认",
        "missing_condition": "等30m买点",
        "trader_action": "盯盘观察",
    }

    role = _trade_role_from_context(
        row,
        chain_position={"chain": "来源板块", "node": "映射节点", "phase": "mapped"},
        trade_stage="watch_pool",
        pool_type="watch",
        blocked_by=[],
    )

    assert role == "chain_watch"


def test_trade_role_uses_phase_and_gate_state():
    from signals.sync.modules.terminal_pool import _trade_role_from_context

    assert _trade_role_from_context(
        {"chain_phase": "consensus_climax"},
        chain_position={"phase": "consensus_climax"},
        trade_stage="watch_pool",
        pool_type="watch",
        blocked_by=[],
    ) == "climax_risk"
    assert _trade_role_from_context(
        {"chain_phase": "cooling"},
        chain_position={"phase": "cooling"},
        trade_stage="watch_pool",
        pool_type="watch",
        blocked_by=[],
    ) == "second_wave"
    assert _trade_role_from_context(
        {"chain_phase": "warming"},
        chain_position={"phase": "warming"},
        trade_stage="watch_pool",
        pool_type="watch",
        blocked_by=[],
    ) == "mainline_attack"


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
        "source_doc_id": "buy-ready-daily",
        "signal_type": "趋势买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 105,
        "confidence": 0.8,
    }, index_codes=set(), name="中旗新材")
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
            "aligned_freqs": ["日线", "30分钟", "15分钟"],
            "conflict_freqs": [],
            "grade": "multi_period",
        },
    }, index_codes=set(), name="中旗新材")
    _add_reason(rows, "300575", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "buy-ready-15m",
        "signal_type": "MACD绿柱扩大_零上",
        "signal_side": "buy",
        "freq": "15分钟",
        "score": 90,
        "confidence": 0.8,
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
    assert split["focus"][0]["rank"] == 1
    assert split["focus"][0]["rank_reason"]
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
    assert split["watch"][0]["entry_gate_status"] == "entry_waiting_upper_context"


def test_terminal_stock_pool_daily_30m_without_right_side_waits_for_confirmation():
    rows = {}
    _add_reason(rows, "300575", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "daily-anchor",
        "signal_type": "趋势买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 105,
        "confidence": 0.8,
    }, index_codes=set(), name="中旗新材")
    _add_reason(rows, "300575", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "daily-30m",
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

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["focus"] == []
    assert split["watch"][0]["entry_gate_status"] == "entry_waiting_right_side_confirm"
    assert split["watch"][0]["trade_stage"] == "watch_pool"
    assert split["watch"][0]["stage_label"] == "盯盘池"
    assert split["watch"][0]["trade_intent"] == "wait_execution"
    assert split["watch"][0]["trade_intent_label"] == "等下单周期"
    assert split["watch"][0]["trader_action"] == "等下单周期确认"


def test_terminal_stock_pool_30m_resonance_without_direct_upper_waits_for_daily_weekly_buy():
    rows = {}
    _add_reason(rows, "300575", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "resonance-30m",
        "signal_type": "三买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 120,
        "confidence": 0.8,
        "resonance_context": {
            "direction": "buy",
            "aligned_freqs": ["日线", "30分钟", "15分钟"],
            "conflict_freqs": [],
            "grade": "multi_period",
        },
    }, index_codes=set(), name="共振未确认股")
    _add_reason(rows, "300575", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "resonance-15m",
        "signal_type": "MACD绿柱扩大_零上",
        "signal_side": "buy",
        "freq": "15分钟",
        "score": 90,
        "confidence": 0.8,
    }, index_codes=set(), name="共振未确认股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["focus"] == []
    row = split["watch"][0]
    assert row["entry_gate_status"] == "entry_waiting_upper_context"
    assert row["trade_intent"] == "wait_big_cycle"
    assert row["trade_intent_label"] == "等日/周买点"
    assert row["primary_timeframe_signal"] == "缺日/周买点"
    assert row["latest_signal"] == "短周期异动，缺日/周买点"


def test_terminal_stock_pool_mainline_30m_right_review_stays_watch_without_upper_buy():
    rows = {}
    _add_reason(rows, "300106", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "mainline-30m-right",
        "signal_type": "趋势买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 96,
        "confidence": 0.85,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="主线右侧复核股")
    _add_chain_context(
        rows,
        "300106",
        chain_name="AI算力产业链",
        node_name="AI硬件/CPO",
        phase="warming",
        rank=2,
        name="主线右侧复核股",
    )
    _add_reason(rows, "300107", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "neutral-30m-right",
        "signal_type": "趋势买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 96,
        "confidence": 0.85,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="中性右侧复核股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["focus"] == []
    assert [row["raw_code"] for row in split["watch"][:2]] == ["300106", "300107"]
    row = split["watch"][0]
    assert row["entry_gate_status"] == "entry_waiting_upper_context"
    assert row["market_setup_bias"] == "watch_only"
    assert row["setup_rank_tier"] == 0
    assert row["mainline_rank_tier"] == 240
    assert row["queue_lane"] == "watch_preheat"
    assert row["trader_action"] == "盯盘等日/周买点"
    assert row["can_trade_now"] is False


def test_terminal_stock_pool_attack_entry_does_not_wait_for_30m():
    rows = {}
    _add_reason(rows, "002050", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "weekly-right",
        "signal_type": "趋势买",
        "signal_side": "buy",
        "freq": "周线",
        "score": 95,
        "confidence": 0.8,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="三花智控")
    _add_reason(rows, "002050", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "f15-right",
        "signal_type": "MACD绿柱扩大_零上",
        "signal_side": "buy",
        "freq": "15分钟",
        "score": 92,
        "confidence": 0.8,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="三花智控")
    _add_reason(rows, "002050", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "f5-right",
        "signal_type": "趋势买",
        "signal_side": "buy",
        "freq": "5分钟",
        "score": 90,
        "confidence": 0.8,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="三花智控")
    _add_chain_context(
        rows,
        "002050",
        chain_name="机器人产业链",
        node_name="人形机器人/执行器",
        phase="warming",
        rank=2,
        name="三花智控",
    )

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["focus"][0]["raw_code"] == "002050"
    assert split["focus"][0]["entry_gate_status"] == "entry_attack_confirmed"
    assert split["focus"][0]["stage_label"] == "进攻买点"
    assert split["focus"][0]["decision_stage"] == "entry_waiting_confirm"
    assert split["focus"][0]["queue_lane"] == "entry_waiting_confirm"
    assert split["focus"][0]["trader_action"] == "进攻买点复核"
    assert "30m未补齐" in split["focus"][0]["missing_condition"]


def test_terminal_stock_pool_buy_shape_with_resistance_text_stays_right_attack():
    rows = {}
    _add_reason(rows, "600007", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "daily-right",
        "signal_type": "MACD绿柱扩大_零上",
        "signal_side": "buy",
        "freq": "日线",
        "score": 62,
        "confidence": 0.8,
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="中国国贸")
    _add_reason(rows, "600007", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "f15-right",
        "signal_type": "MACD绿柱扩大_零上",
        "signal_side": "buy",
        "freq": "15分钟",
        "score": 62,
        "confidence": 0.8,
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="中国国贸")
    _add_reason(rows, "600007", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "f5-shape",
        "signal_type": "形态:上升三角",
        "signal_side": "buy",
        "freq": "5分钟",
        "score": 62,
        "confidence": 0.6,
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "evidence": {"details": "阻力位形成平顶，低点递升"},
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="中国国贸")
    _add_chain_context(
        rows,
        "600007",
        chain_name="AI算力产业链",
        node_name="AI硬件/CPO",
        phase="warming",
        rank=2,
        name="中国国贸",
    )

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["focus"][0]["raw_code"] == "600007"
    assert split["focus"][0]["entry_gate_status"] == "entry_attack_confirmed"
    assert split["focus"][0]["market_setup_bias"] == "right_attack"
    assert split["focus"][0]["can_trade_now"] is True


def test_terminal_stock_pool_neutral_attack_entry_stays_watch_without_mainline():
    rows = {}
    for freq, signal_type in (("日线", "MACD绿柱扩大_零上"), ("15分钟", "MACD绿柱扩大_零上"), ("5分钟", "趋势买")):
        _add_reason(rows, "600008", {
            "reason_type": "technical_trigger",
            "source_collection": "terminal_technical_signals",
            "source_doc_id": f"neutral-attack-{freq}",
            "signal_type": signal_type,
            "signal_side": "buy",
            "freq": freq,
            "score": 90,
            "confidence": 0.8,
            "ma_alignment": _ma_alignment(),
        }, index_codes=set(), name="中性右侧进攻")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["focus"] == []
    assert split["watch"][0]["raw_code"] == "600008"
    assert split["watch"][0]["entry_gate_status"] == "entry_attack_confirmed"
    assert split["watch"][0]["market_setup_bias"] == "right_attack"
    assert split["watch"][0]["can_trade_now"] is False


def test_terminal_stock_pool_focus_sorts_right_setups_above_left_review():
    rows = {}
    for freq, signal_type in (("日线", "趋势买"), ("30分钟", "三买"), ("15分钟", "趋势买")):
        _add_reason(rows, "300901", {
            "reason_type": "technical_trigger",
            "source_collection": "terminal_technical_signals",
            "source_doc_id": f"right-exec-{freq}",
            "signal_type": signal_type,
            "signal_side": "buy",
            "freq": freq,
            "score": 40,
            "confidence": 0.5,
            "ma_alignment": _ma_alignment(score=20),
        }, index_codes=set(), name="低分右侧确认")
    for freq, signal_type in (("日线", "趋势买"), ("15分钟", "趋势买"), ("5分钟", "趋势买")):
        _add_reason(rows, "300902", {
            "reason_type": "technical_trigger",
            "source_collection": "terminal_technical_signals",
            "source_doc_id": f"right-attack-{freq}",
            "signal_type": signal_type,
            "signal_side": "buy",
            "freq": freq,
            "score": 45,
            "confidence": 0.5,
            "ma_alignment": _ma_alignment(score=20),
        }, index_codes=set(), name="低分进攻买点")
    _add_chain_context(
        rows,
        "300902",
        chain_name="AI算力产业链",
        node_name="AI硬件/CPO",
        phase="warming",
        rank=2,
        name="低分进攻买点",
    )
    _add_reason(rows, "300903", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "left-high-score",
        "signal_type": "一买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 300,
        "confidence": 0.95,
        "ma_alignment": _ma_alignment(score=44),
    }, index_codes=set(), name="高分主线低吸")
    _add_chain_context(
        rows,
        "300903",
        chain_name="AI算力产业链",
        node_name="AI硬件/CPO",
        phase="warming",
        rank=1,
        name="高分主线低吸",
    )

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert [row["raw_code"] for row in split["focus"][:3]] == ["300901", "300902", "300903"]
    assert [row["market_setup_bias"] for row in split["focus"][:3]] == ["right_executable", "right_attack", "left_review"]
    assert [row["setup_rank_tier"] for row in split["focus"][:3]] == [300, 200, 100]
    assert [row["can_trade_now"] for row in split["focus"][:3]] == [True, True, False]


def test_terminal_stock_pool_one_buy_ma_confirmed_waits_for_daily_weekly_buy_by_default():
    rows = {}
    _add_reason(rows, "300001", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "one-buy-ma",
        "signal_type": "一买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 92,
        "confidence": 0.8,
        "ma_alignment": _ma_alignment(above5=False, above10=True, above20=True, score=36),
    }, index_codes=set(), name="左侧进攻股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["focus"] == []
    row = split["watch"][0]
    assert row["raw_code"] == "300001"
    assert row["entry_gate_status"] == "entry_waiting_upper_context"
    assert row["setup_mode"] == "watch"
    assert row["stage_label"] == "盯盘池"
    assert row["queue_lane"] == "watch_preheat"
    assert row["market_setup_bias"] == "watch_only"
    assert row["setup_rank_tier"] == 0
    assert row["trade_intent"] == "wait_big_cycle"
    assert row["trade_intent_label"] == "等日/周买点"
    assert row["trader_action"] == "盯盘等日/周买点"
    assert row["latest_signal"] == "短周期异动，缺日/周买点"
    assert "买点质量" in row["rank_reason"]
    assert "均线确认" in row["rank_reason"]


def test_terminal_stock_pool_one_buy_without_key_ma_stays_watch_above_plain_clue():
    rows = {}
    _add_reason(rows, "300002", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "one-buy-no-ma",
        "signal_type": "一买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 92,
        "confidence": 0.8,
    }, index_codes=set(), name="左侧观察股")
    _add_reason(rows, "300003", {
        "reason_type": "fallback_watch",
        "source_collection": "strategy_snapshots",
        "source_doc_id": "plain-clue",
        "signal_type": "普通线索",
        "signal_side": "buy",
        "source_role": "fallback",
        "decision_effect": "fallback_watch",
        "actionability": "observe_only",
        "queue_lane": "fallback_watch",
    }, index_codes=set(), name="普通线索股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["focus"] == []
    assert [row["raw_code"] for row in split["watch"][:2]] == ["300002", "300003"]
    assert split["watch"][0]["setup_mode"] == "watch"
    assert split["watch"][0]["score_components"]["buy_point_quality"] > 0


def test_terminal_stock_pool_30m_two_buy_with_ma_ranks_above_plain_macd_right_signal():
    rows = {}
    _add_reason(rows, "300004", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "two-buy-daily",
        "signal_type": "趋势买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 105,
        "confidence": 0.8,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="二买进攻股")
    _add_reason(rows, "300004", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "two-buy-30m",
        "signal_type": "2买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 110,
        "confidence": 0.85,
        "ma_alignment": _ma_alignment(),
        "resonance_context": {
            "direction": "buy",
            "aligned_freqs": ["日线", "30分钟", "15分钟"],
            "conflict_freqs": [],
            "grade": "multi_period",
        },
    }, index_codes=set(), name="二买进攻股")
    _add_reason(rows, "300004", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "two-buy-15m",
        "signal_type": "二买",
        "signal_side": "buy",
        "freq": "15分钟",
        "score": 100,
        "confidence": 0.8,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="二买进攻股")
    _add_reason(rows, "300005", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "macd-daily",
        "signal_type": "MACD绿柱扩大_零上",
        "signal_side": "buy",
        "freq": "日线",
        "score": 95,
        "confidence": 0.8,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="MACD进攻股")
    _add_reason(rows, "300005", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "macd-30m",
        "signal_type": "MACD绿柱扩大_零上",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 110,
        "confidence": 0.85,
        "ma_alignment": _ma_alignment(),
        "resonance_context": {
            "direction": "buy",
            "aligned_freqs": ["日线", "30分钟", "15分钟"],
            "conflict_freqs": [],
            "grade": "multi_period",
        },
    }, index_codes=set(), name="MACD进攻股")
    _add_reason(rows, "300005", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "macd-15m",
        "signal_type": "MACD绿柱扩大_零上",
        "signal_side": "buy",
        "freq": "15分钟",
        "score": 100,
        "confidence": 0.8,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="MACD进攻股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert [row["raw_code"] for row in split["focus"][:2]] == ["300004", "300005"]
    assert split["focus"][0]["top_buy_reason"]["signal_type"] == "二买"
    assert split["focus"][0]["setup_mode"] == "right_attack"
    assert "30分钟 二买" in split["focus"][0]["right_signal_reasons"]
    assert split["focus"][0]["score_components"]["buy_point_quality"] > split["focus"][1]["score_components"]["buy_point_quality"]


def test_terminal_stock_pool_daily_two_buy_is_upper_context_not_right_attack_signal():
    rows = {}
    _add_reason(rows, "300006", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "daily-two-buy",
        "signal_type": "2买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 100,
        "confidence": 0.8,
        "ma_alignment": _ma_alignment(above5=False, above10=True, above20=True, score=36),
    }, index_codes=set(), name="日线二买股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)
    assert split["focus"] == []
    row = split["watch"][0]

    assert row["top_buy_reason"]["signal_type"] == "二买"
    assert row["setup_mode"] == "left_attack"
    assert row["trade_timeframe_side"] == "none"
    assert row["right_signal_reasons"] == []


def test_terminal_stock_pool_daily_buy_with_short_sell_waits_in_watch_not_risk():
    rows = {}
    _add_reason(rows, "300007", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "daily-buy",
        "signal_type": "一买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 96,
        "confidence": 0.86,
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
    }, index_codes=set(), name="日线优先股")
    _add_reason(rows, "300007", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "short-sell",
        "signal_type": "一卖",
        "signal_side": "sell",
        "freq": "5分钟",
        "score": -80,
        "confidence": 0.8,
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
    }, index_codes=set(), name="日线优先股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["focus"] == []
    assert split["risk"] == []
    row = split["watch"][0]
    assert row["raw_code"] == "300007"
    assert row["entry_gate_status"] == "entry_waiting_30m_confirm"
    assert row["blocked_by"] == ["30m_missing"]
    assert row["top_risk_reason"]["signal_type"] == "一卖"
    assert row["risk_marked"] is True


def test_terminal_stock_pool_mainline_tech_gets_lenient_attack_focus_without_30m():
    rows = {}
    _add_reason(rows, "300101", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "ai-upper",
        "signal_type": "一买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 92,
        "confidence": 0.8,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="AI硬件主线股")
    _add_reason(rows, "300101", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "ai-15m",
        "signal_type": "二买",
        "signal_side": "buy",
        "freq": "15分钟",
        "score": 96,
        "confidence": 0.85,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="AI硬件主线股")
    _add_chain_context(
        rows,
        "300101",
        chain_name="AI算力/数据中心产业链",
        node_name="AI硬件/CPO/光连接",
        phase="warming",
        rank=3,
        name="AI硬件主线股",
    )
    _add_reason(rows, "300102", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "neutral-upper",
        "signal_type": "一买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 92,
        "confidence": 0.8,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="中性同形态股")
    _add_reason(rows, "300102", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "neutral-15m",
        "signal_type": "二买",
        "signal_side": "buy",
        "freq": "15分钟",
        "score": 96,
        "confidence": 0.85,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="中性同形态股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert [row["raw_code"] for row in split["focus"]] == ["300101"]
    assert split["watch"][0]["raw_code"] == "300102"
    assert split["focus"][0]["entry_gate_status"] == "left_attack_confirmed"
    assert split["focus"][0]["sector_policy"] == "mainline_lenient"
    assert split["focus"][0]["score_components"]["mainline_lenient_policy"] == 18.0
    assert split["focus"][0]["left_allowed_reason"] == "mainline_lenient"
    assert split["focus"][0]["can_trade_now"] is False
    assert split["watch"][0]["sector_policy"] == "neutral"


def test_terminal_stock_pool_defensive_sector_requires_full_confirmation():
    rows = {}
    _add_reason(rows, "300103", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "consumer-upper",
        "signal_type": "一买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 92,
        "confidence": 0.8,
        "ma_alignment": _ma_alignment(above5=True, above10=False, above20=False, score=18),
    }, index_codes=set(), name="防守消费股")
    _add_reason(rows, "300103", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "consumer-15m",
        "signal_type": "趋势买",
        "signal_side": "buy",
        "freq": "15分钟",
        "score": 96,
        "confidence": 0.85,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="防守消费股")
    _add_chain_context(
        rows,
        "300103",
        chain_name="消费品产业链",
        node_name="白酒/食品饮料",
        phase="warming",
        rank=2,
        name="防守消费股",
    )

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["focus"] == []
    row = split["watch"][0]
    assert row["raw_code"] == "300103"
    assert row["sector_policy"] == "defensive_strict"
    assert row["entry_gate_status"] == "entry_waiting_defensive_confirmation"
    assert row["trader_action"] == "防守板块等完整确认"
    assert row["score_components"]["hot_sector"] == 0


def test_terminal_stock_pool_defensive_sector_relaxes_when_broad_market_falls():
    rows = {}
    _add_reason(rows, "600036", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "bank-upper",
        "signal_type": "一买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 92,
        "confidence": 0.8,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="招商银行")
    _add_reason(rows, "600036", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "bank-15m",
        "signal_type": "二买",
        "signal_side": "buy",
        "freq": "15分钟",
        "score": 96,
        "confidence": 0.85,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="招商银行")
    _add_chain_context(
        rows,
        "600036",
        chain_name="银行消费防守产业链",
        node_name="银行",
        phase="warming",
        rank=4,
        name="招商银行",
    )
    _set_broad_market(rows, falling=True, change_pct=-0.9)

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert [row["raw_code"] for row in split["focus"]] == ["600036"]
    row = split["focus"][0]
    assert row["entry_gate_status"] == "left_attack_confirmed"
    assert row["sector_policy"] == "defensive_lenient"
    assert row["broad_market_label"] == "指数左侧卖"
    assert row["score_components"]["defensive_lenient_policy"] == 12.0
    assert row["left_allowed_reason"] == "defensive_lenient_broad_market_falling"
    assert row["queue_lane"] == "left_review"
    assert row["can_trade_now"] is False


def test_terminal_stock_pool_mainline_concept_overrides_defensive_industry():
    rows = {}
    _add_reason(rows, "300918", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "nanshan-30m",
        "signal_type": "二买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 96,
        "confidence": 0.85,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="南山智尚")
    _add_reason(rows, "300918", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "nanshan-15m",
        "signal_type": "二买",
        "signal_side": "buy",
        "freq": "15分钟",
        "score": 96,
        "confidence": 0.85,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="南山智尚")
    _add_reason(rows, "300918", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "nanshan-daily",
        "signal_type": "一买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 92,
        "confidence": 0.8,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="南山智尚")
    _add_reason(rows, "300918", {
        "reason_type": "chain_membership",
        "source_collection": "security_chain_memberships",
        "source_doc_id": "textile-industry",
        "signal_type": "产业链归属",
        "signal_side": "neutral",
        "source_role": "context",
        "decision_effect": "context_only",
        "chain_id": "textile_light",
        "chain_name": "纺织轻工产业链",
        "node_id": "textile_light_core",
        "node_name": "纺织/轻工",
        "board_or_concept": "纺织/轻工",
        "evidence": {"phase": "warming", "rank": 5, "heat_score": 78},
    }, index_codes=set(), name="南山智尚")
    _add_reason(rows, "300918", {
        "reason_type": "constituent_hot",
        "source_collection": "concept_constituents",
        "source_doc_id": "机器人概念",
        "signal_type": "产业链升温",
        "signal_side": "neutral",
        "source_role": "context",
        "decision_effect": "confirm",
        "chain_id": "robotics",
        "chain_name": "机器人/自动化产业链",
        "node_id": "robotics",
        "node_name": "机器人",
        "board_or_concept": "机器人概念",
        "evidence": {"phase": "warming", "rank": 2, "heat_score": 91},
    }, index_codes=set(), name="南山智尚")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    row = split["focus"][0]
    assert row["raw_code"] == "300918"
    assert row["sector_policy"] == "mainline_lenient"
    assert row["sector_policy_matched_token"] == "机器人"
    assert row["sector_policy_source"] == "概念/题材"
    assert "覆盖防守行业" in row["sector_policy_reason"]
    assert "defensive_strict_policy" not in row["score_components"]


def test_terminal_stock_pool_watch_rank_uses_signal_ma_and_hot_sector_not_intent_priority():
    rows = {}
    _add_reason(rows, "300201", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "hot-daily",
        "signal_type": "趋势买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 115,
        "confidence": 0.9,
        "ma_alignment": _ma_alignment(above5=True, above10=False, above20=False, score=18),
    }, index_codes=set(), name="热门技术强股")
    _add_chain_context(
        rows,
        "300201",
        chain_name="AI算力产业链",
        node_name="AI硬件/CPO",
        phase="warming",
        rank=2,
        name="热门技术强股",
    )
    _add_reason(rows, "300202", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "plain-15m",
        "signal_type": "二买",
        "signal_side": "buy",
        "freq": "15分钟",
        "score": 75,
        "confidence": 0.55,
        "ma_alignment": _ma_alignment(above5=True, above10=False, above20=False, score=18),
    }, index_codes=set(), name="普通右侧动量股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert [row["raw_code"] for row in split["watch"][:2]] == ["300201", "300202"]
    assert set(split["watch"][0]["score_components"]) >= {"buy_point_quality", "ma_alignment", "hot_sector"}
    assert split["watch"][0]["rank_score"] > split["watch"][1]["rank_score"]
    assert split["watch"][0]["watch_sort_priority"] > split["watch"][1]["watch_sort_priority"]


def test_terminal_stock_pool_freshness_limits_match_watch_horizon():
    assert _entry_age_limit("5分钟") == 0
    assert _entry_age_limit("15分钟") == 0
    assert _entry_age_limit("30分钟") == 1
    assert _entry_age_limit("日线") == 2
    assert _entry_age_limit("周线") == 5
    assert _reason_is_current_for_entry({"freq": "15分钟", "event_dt": "2026-05-08", "as_of": "2026-05-08"})
    assert not _reason_is_current_for_entry({"freq": "15分钟", "event_dt": "2026-05-07", "as_of": "2026-05-08"})
    assert _reason_is_current_for_entry({"freq": "30分钟", "event_dt": "2026-05-07", "as_of": "2026-05-08"})
    assert not _reason_is_current_for_entry({"freq": "30分钟", "event_dt": "2026-05-06", "as_of": "2026-05-08"})
    assert _reason_is_current_for_entry({"freq": "日线", "event_dt": "2026-05-06", "as_of": "2026-05-08"})
    assert not _reason_is_current_for_entry({"freq": "日线", "event_dt": "2026-05-05", "as_of": "2026-05-08"})
    assert _reason_is_current_for_entry({"freq": "周线", "event_dt": "2026-05-01", "as_of": "2026-05-08"})
    assert not _reason_is_current_for_entry({"freq": "周线", "event_dt": "2026-04-30", "as_of": "2026-05-08"})


def test_terminal_stock_pool_default_candidates_require_fresh_30m_daily_or_weekly_anchor():
    rows = {}
    _add_reason(rows, "300501", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "only-5m",
        "signal_type": "趋势买",
        "signal_side": "buy",
        "freq": "5分钟",
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="只有五分钟")
    _add_reason(rows, "300502", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "stale-daily",
        "signal_type": "一买",
        "signal_side": "buy",
        "freq": "日线",
        "event_dt": "2026-05-05",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="过期日线")
    _add_reason(rows, "300503", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "fresh-daily",
        "signal_type": "一买",
        "signal_side": "buy",
        "freq": "日线",
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="日线锚点")
    _add_reason(rows, "300503", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "fresh-5m",
        "signal_type": "趋势买",
        "signal_side": "buy",
        "freq": "5分钟",
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="日线锚点")
    _add_reason(rows, "300504", {
        "reason_type": "review_sector_bullish",
        "source_collection": "review_sector_clues",
        "source_doc_id": "clue",
        "signal_type": "复盘线索",
        "signal_side": "buy",
        "as_of": "2026-05-08",
    }, index_codes=set(), name="纯线索")
    _add_reason(rows, "300506", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "weak-30m",
        "signal_type": "MACD绿柱扩大_零上",
        "signal_side": "buy",
        "freq": "30分钟",
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="三十分钟MACD")
    _add_reason(rows, "300507", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "strong-30m",
        "signal_type": "趋势买",
        "signal_side": "buy",
        "freq": "30分钟",
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="三十分钟强买点")
    _add_reason(rows, "300508", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "weak-daily-refusal",
        "signal_type": "拒绝回调相对强度",
        "signal_side": "buy",
        "freq": "日线",
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="日线宽泛因子")
    _add_reason(rows, "300508", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "weak-daily-15m",
        "signal_type": "MACD绿柱扩大_零上",
        "signal_side": "buy",
        "freq": "15分钟",
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="日线宽泛因子")
    _add_reason(rows, "300509", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "weak-weekly-new-high",
        "signal_type": "200日新高突破",
        "signal_side": "buy",
        "freq": "周线",
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="周线宽泛因子")
    _add_reason(rows, "300505", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "fresh-st",
        "signal_type": "一买",
        "signal_side": "buy",
        "freq": "日线",
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="*ST测试")

    filtered = _default_opportunity_candidate_rows(rows)

    assert set(filtered) == {"300503", "300507"}
    assert {reason["freq"] for reason in filtered["300503"]["inclusion_reasons"]} == {"日线", "5分钟"}


def test_terminal_stock_pool_watch_rank_prefers_daily_then_30m_weekly_execution():
    rows = {}
    for code, freq, name in (
        ("300401", "日线", "日线优先股"),
        ("300402", "30分钟", "三十分钟股"),
        ("300403", "周线", "周线股"),
        ("300404", "15分钟", "执行周期股"),
    ):
        _add_reason(rows, code, {
            "reason_type": "technical_trigger",
            "source_collection": "terminal_technical_signals",
            "source_doc_id": f"{code}-{freq}",
            "signal_type": "二买",
            "signal_side": "buy",
            "freq": freq,
            "score": 80,
            "confidence": 0.7,
            "event_dt": "2026-05-08",
            "as_of": "2026-05-08",
            "ma_alignment": _ma_alignment(above5=True, above10=False, above20=False, score=18),
        }, index_codes=set(), name=name)

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert [row["raw_code"] for row in split["watch"][:4]] == ["300401", "300403", "300402", "300404"]
    assert [row["score_components"]["timeframe_priority"] for row in split["watch"][:4]] == [36.0, 34.0, 22.0, 12.0]


def test_terminal_stock_pool_watch_rank_prioritizes_near_upper_buy_point():
    rows = {}
    near_upper_ma = _ma_alignment(above5=False, above10=True, above20=True, near20=True, score=32)
    near_upper_ma.update({
        "distance_ma20_pct": 0.7,
        "low_distance_ma20_pct": 0.25,
    })
    _add_reason(rows, "300441", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "near-daily-buy",
        "signal_type": "一买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 78,
        "confidence": 0.72,
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": near_upper_ma,
    }, index_codes=set(), name="日线近买点股")
    _add_reason(rows, "300442", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "plain-30m-buy",
        "signal_type": "三买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 100,
        "confidence": 0.86,
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="短线强但缺日周股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert [row["raw_code"] for row in split["watch"][:2]] == ["300441", "300442"]
    assert split["watch"][0]["score_components"]["upper_buy_proximity"] > 0
    assert split["watch"][1]["score_components"]["upper_buy_proximity"] == 0
    assert "日/周近买点" in split["watch"][0]["rank_reason"]


def test_terminal_stock_pool_scores_fibonacci_ma_acceptance_separately():
    assert _fib_ma_support_score_from_alignment(_ma_alignment(fib=True)) > 0

    rows = {}
    _add_reason(rows, "300421", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "fib-daily",
        "signal_type": "一买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 80,
        "confidence": 0.7,
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(above5=False, above10=False, above20=False, score=10, fib=True),
    }, index_codes=set(), name="Fib支撑股")
    _add_reason(rows, "300422", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "plain-daily",
        "signal_type": "一买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 80,
        "confidence": 0.7,
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(above5=False, above10=True, above20=True, score=10),
    }, index_codes=set(), name="普通支撑股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    by_code = {row["raw_code"]: row for row in split["watch"]}
    assert by_code["300421"]["score_components"]["fib_ma_acceptance"] > 0
    assert by_code["300422"]["score_components"]["fib_ma_acceptance"] == 0
    assert "关键均线承接" in by_code["300421"]["rank_reason"]


def test_terminal_stock_pool_watch_rank_rewards_multi_period_and_indicator_breadth():
    rows = {}
    _add_reason(rows, "300411", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "broad-daily",
        "signal_type": "二买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 80,
        "confidence": 0.7,
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(above5=True, above10=False, above20=False, score=18),
    }, index_codes=set(), name="多指标股")
    _add_reason(rows, "300411", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "broad-30m",
        "signal_type": "趋势买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 80,
        "confidence": 0.7,
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(above5=True, above10=False, above20=False, score=18),
    }, index_codes=set(), name="多指标股")
    _add_reason(rows, "300412", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "single-daily",
        "signal_type": "二买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 80,
        "confidence": 0.7,
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(above5=True, above10=False, above20=False, score=18),
    }, index_codes=set(), name="单指标股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert [row["raw_code"] for row in split["watch"][:2]] == ["300411", "300412"]
    assert split["watch"][0]["score_components"]["multi_period_bonus"] == 8.0
    assert split["watch"][0]["score_components"]["indicator_breadth"] == 8.0
    assert split["watch"][1]["score_components"]["multi_period_bonus"] == 0.0
    assert split["watch"][1]["score_components"]["indicator_breadth"] == 4.0


def test_terminal_stock_pool_watch_rank_rewards_200d_new_high_breakout():
    rows = {}
    _add_reason(rows, "300501", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "new-high-breakout",
        "signal_type": "200日新高突破",
        "signal_side": "buy",
        "signal_family": "entry_factor",
        "freq": "日线",
        "score": 88,
        "confidence": 0.9,
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
        "evidence": {
            "details": "200日新高, 突破5.0%, 5日涨幅20.0%, 量比2.0",
            "entry_factor": {
                "group": "200d_new_high_breakout",
                "breakout_pct": 5.0,
                "five_day_gain_pct": 20.0,
                "volume_ratio": 2.0,
            },
        },
    }, index_codes=set(), name="新高突破股")
    _add_reason(rows, "300502", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "daily-two-buy",
        "signal_type": "二买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 60,
        "confidence": 0.7,
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="日线二买股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["focus"] == []
    assert [row["raw_code"] for row in split["watch"][:2]] == ["300502", "300501"]
    row = next(item for item in split["watch"] if item["raw_code"] == "300501")
    assert row["raw_code"] == "300501"
    assert row["latest_signal"] == "日线 200日新高突破"
    assert row["score_components"]["breakout_momentum"] > 0
    assert row["score_components"]["buy_point_quality"] > 0
    assert row["score_components"]["breakout_momentum"] < split["watch"][0]["score_components"]["buy_point_quality"]
    assert "新高动量" in row["rank_reason"]


def test_terminal_stock_pool_watch_rank_rewards_refusal_pullback_factor():
    rows = {}
    _add_reason(rows, "300601", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "refusal-pullback",
        "signal_type": "拒绝回调相对强度",
        "signal_side": "buy",
        "signal_family": "entry_factor",
        "freq": "日线",
        "score": 82,
        "confidence": 0.84,
        "event_dt": "2026-05-11",
        "as_of": "2026-05-11",
        "ma_alignment": _ma_alignment(),
        "evidence": {
            "details": "近3日拒绝回调，最大回撤0.8%，强收盘3/3日",
            "entry_factor": {
                "group": "relative_resilience_refusal_pullback",
                "max_drawdown_pct": 0.8,
                "max_close_drawdown_pct": 0.0,
                "three_day_change_pct": 2.5,
                "twenty_day_gain_pct": 16.0,
                "high_proximity_pct": 101.2,
                "strong_close_days": 3,
                "recent_volume_ratio": 0.9,
            },
        },
    }, index_codes=set(), name="拒绝回调股")
    _add_reason(rows, "300602", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "daily-trend-buy",
        "signal_type": "趋势买",
        "signal_side": "buy",
        "signal_family": "hard_technical",
        "freq": "日线",
        "score": 82,
        "confidence": 0.84,
        "event_dt": "2026-05-11",
        "as_of": "2026-05-11",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="普通趋势股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["focus"] == []
    assert [row["raw_code"] for row in split["watch"][:2]] == ["300601", "300602"]
    row = split["watch"][0]
    assert row["score_components"]["relative_resilience"] > 0
    assert "拒绝回调" in row["rank_reason"]


def test_terminal_stock_pool_market_right_buy_rewards_confirmed_stock_right_buy():
    rows = {}
    for freq, signal_type in (("日线", "趋势买"), ("30分钟", "三买"), ("15分钟", "趋势买")):
        _add_reason(rows, "300611", {
            "reason_type": "technical_trigger",
            "source_collection": "terminal_technical_signals",
            "source_doc_id": f"aligned-right-{freq}",
            "signal_type": signal_type,
            "signal_side": "buy",
            "freq": freq,
            "score": 86,
            "confidence": 0.86,
            "event_dt": "2026-05-11",
            "as_of": "2026-05-11",
            "ma_alignment": _ma_alignment(),
        }, index_codes=set(), name="市场右买共振股")
    _set_broad_market(rows, falling=False, change_pct=0.9, index_setup_side="right_buy", volume_state="expanding", volume_ratio=1.2)

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert [row["raw_code"] for row in split["focus"]] == ["300611"]
    row = split["focus"][0]
    assert row["index_setup_side"] == "right_buy"
    assert row["stock_setup_side"] == "right_buy"
    assert row["alignment_policy"] == "allow_focus"
    assert row["score_components"]["market_alignment"] == 20.0
    assert "市场共振" in row["rank_reason"]


def test_terminal_stock_pool_market_right_sell_marks_stock_right_buy_without_filtering():
    rows = {}
    for freq, signal_type in (("日线", "趋势买"), ("30分钟", "三买"), ("15分钟", "趋势买")):
        _add_reason(rows, "300612", {
            "reason_type": "technical_trigger",
            "source_collection": "terminal_technical_signals",
            "source_doc_id": f"blocked-right-{freq}",
            "signal_type": signal_type,
            "signal_side": "buy",
            "freq": freq,
            "score": 86,
            "confidence": 0.86,
            "event_dt": "2026-05-11",
            "as_of": "2026-05-11",
            "ma_alignment": _ma_alignment(),
        }, index_codes=set(), name="指数卖侧逆势股")
    _set_broad_market(rows, falling=True, change_pct=-0.9, index_setup_side="right_sell", volume_state="expanding", volume_ratio=1.2)

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert [row["raw_code"] for row in split["focus"]] == ["300612"]
    row = split["focus"][0]
    assert row["raw_code"] == "300612"
    assert row["entry_gate_status"] == "entry_confirmed"
    assert row["index_setup_side"] == "right_sell"
    assert row["stock_setup_side"] == "right_buy"
    assert row["alignment_policy"] == "mark_index_risk"
    assert row["score_components"]["market_alignment"] == -28.0


def test_terminal_stock_pool_upper_buy_with_current_sell_risk_stays_opportunity_first():
    rows = {}
    for freq, signal_type in (("日线", "趋势买"), ("30分钟", "三买"), ("15分钟", "趋势买")):
        _add_reason(rows, "300701", {
            "reason_type": "technical_trigger",
            "source_collection": "terminal_technical_signals",
            "source_doc_id": f"buy-risk-{freq}",
            "signal_type": signal_type,
            "signal_side": "buy",
            "freq": freq,
            "score": 82,
            "confidence": 0.82,
            "event_dt": "2026-05-08",
            "as_of": "2026-05-08",
            "ma_alignment": _ma_alignment(),
        }, index_codes=set(), name="买点带风险股")
    _add_reason(rows, "300701", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "buy-risk-sell",
        "signal_type": "日线卖点风险",
        "signal_side": "sell",
        "freq": "日线",
        "score": -92,
        "confidence": 0.8,
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
    }, index_codes=set(), name="买点带风险股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["risk"] == []
    row = split["focus"][0]
    assert row["raw_code"] == "300701"
    assert row["entry_gate_status"] == "entry_confirmed"
    assert row["blocked_by"] == []
    assert row["pool_type"] == "focus"
    assert row["trade_stage"] == "confirmed_entry"
    assert row["setup_mode"] == "right_attack"
    assert row["top_risk_reason"]["signal_type"] == "日线卖点风险"
    assert row["risk_marked"] is True
    assert row["risk_marker"] == "日线卖点风险"
    assert row["risk_level"] == "high"
    assert "日线卖点风险" in row["risk_signal_reasons"]


def test_terminal_stock_pool_upper_buy_intraday_big_drop_stays_opportunity_first():
    rows = {}
    for freq, signal_type in (("日线", "趋势买"), ("30分钟", "三买"), ("15分钟", "趋势买")):
        _add_reason(rows, "300704", {
            "reason_type": "technical_trigger",
            "source_collection": "terminal_technical_signals",
            "source_doc_id": f"buy-drop-{freq}",
            "signal_type": signal_type,
            "signal_side": "buy",
            "freq": freq,
            "score": 86,
            "confidence": 0.86,
            "event_dt": "2026-05-08",
            "as_of": "2026-05-08",
            "ma_alignment": _ma_alignment(),
        }, index_codes=set(), name="当日大跌买点股")
    rows["300704"]["day_change_pct"] = -6.2
    rows["300704"]["day_change_as_of"] = "2026-05-08"
    rows["300704"]["day_change_source"] = "fullmarket_spot_snapshots"

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["risk"] == []
    row = split["focus"][0]
    assert row["raw_code"] == "300704"
    assert row["entry_gate_status"] == "entry_confirmed"
    assert row["blocked_by"] == []
    assert row["top_risk_reason"]["reason_type"] == "intraday_day_drop"
    assert row["risk_marker"] == "当天跌幅-6.20%"


def test_terminal_stock_pool_upper_buy_bypasses_red_trend_validation():
    rows = {}
    for freq, signal_type in (("日线", "趋势买"), ("30分钟", "三买"), ("15分钟", "趋势买")):
        _add_reason(rows, "300705", {
            "reason_type": "technical_trigger",
            "source_collection": "terminal_technical_signals",
            "source_doc_id": f"buy-not-red-{freq}",
            "signal_type": signal_type,
            "signal_side": "buy",
            "freq": freq,
            "score": 86,
            "confidence": 0.86,
            "event_dt": "2026-05-08",
            "as_of": "2026-05-08",
            "ma_alignment": _ma_alignment(),
        }, index_codes=set(), name="非红趋势买点股")
    rows["300705"]["day_change_pct"] = -0.2
    rows["300705"]["day_change_as_of"] = "2026-05-08"
    rows["300705"]["day_change_source"] = "fullmarket_spot_snapshots"

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["risk"] == []
    assert split["watch"] == []
    row = split["focus"][0]
    assert row["raw_code"] == "300705"
    assert row["entry_gate_status"] == "entry_confirmed"
    assert row["blocked_by"] == []
    assert row["risk_marked"] is False


def test_terminal_stock_pool_watch_validation_requires_red_trend_after_entry():
    rows = {}
    _add_reason(rows, "300706", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "watch-not-red-30m",
        "signal_type": "三买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 90,
        "confidence": 0.8,
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="非红趋势盯盘股")
    rows["300706"]["day_change_pct"] = 0.0
    rows["300706"]["day_change_as_of"] = "2026-05-08"
    rows["300706"]["day_change_source"] = "fullmarket_spot_snapshots"

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["focus"] == []
    assert split["watch"] == []
    row = split["risk"][0]
    assert row["raw_code"] == "300706"
    assert row["blocked_by"] == ["pool_trend_not_red"]
    assert row["risk_marker"] == "趋势未红0.00%"


def test_terminal_stock_pool_period_conflict_marks_risk_without_blocking_buy():
    rows = {}
    for freq, signal_type in (("日线", "趋势买"), ("30分钟", "三买"), ("15分钟", "趋势买")):
        _add_reason(rows, "300703", {
            "reason_type": "technical_trigger",
            "source_collection": "terminal_technical_signals",
            "source_doc_id": f"buy-conflict-{freq}",
            "signal_type": signal_type,
            "signal_side": "buy",
            "freq": freq,
            "score": 82,
            "confidence": 0.82,
            "event_dt": "2026-05-08",
            "as_of": "2026-05-08",
            "ma_alignment": _ma_alignment(),
            "resonance_context": {
                "direction": "buy",
                "aligned_freqs": ["日线", "30分钟", "15分钟"],
                "conflict_freqs": ["5分钟"] if freq == "30分钟" else [],
                "grade": "conflict" if freq == "30分钟" else "multi_period",
            },
        }, index_codes=set(), name="买点冲突标记股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["risk"] == []
    row = split["focus"][0]
    assert row["raw_code"] == "300703"
    assert row["entry_gate_status"] == "entry_confirmed"
    assert row["trade_stage"] == "confirmed_entry"
    assert row["risk_marked"] is True
    assert row["risk_marker"] == "周期冲突"
    assert row["risk_level"] == "medium"
    assert "周期冲突" in row["risk_signal_reasons"]


def test_terminal_stock_pool_pure_risk_still_enters_risk_pool():
    rows = {}
    _add_reason(rows, "300702", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "pure-risk-sell",
        "signal_type": "日线卖点风险",
        "signal_side": "sell",
        "freq": "日线",
        "score": -90,
        "confidence": 0.8,
        "event_dt": "2026-05-08",
        "as_of": "2026-05-08",
    }, index_codes=set(), name="纯风险股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert split["focus"] == []
    assert split["watch"] == []
    row = split["risk"][0]
    assert row["raw_code"] == "300702"
    assert row["entry_gate_status"] == "blocked_by_risk"
    assert row["top_buy_reason"] == {}
    assert row["top_risk_reason"]["signal_type"] == "日线卖点风险"
    assert row["risk_marked"] is True
    assert row["pool_type"] == "risk"


def test_terminal_stock_pool_stale_resonance_context_does_not_count_as_fresh_periods():
    rows = {}
    stale_resonance = {
        "direction": "buy",
        "aligned_freqs": ["周线", "日线", "5分钟"],
        "conflict_freqs": [],
        "grade": "strong_resonance",
        "latest_dt": "2026-05-08T14:40:00",
        "primary_freq": "5分钟",
    }
    _add_reason(rows, "600391", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "fresh-5m",
        "signal_type": "MACD绿柱扩大_零上",
        "signal_side": "buy",
        "freq": "5分钟",
        "score": 120,
        "confidence": 0.9,
        "event_dt": "2026-05-08T14:40:00",
        "as_of": "2026-05-08",
        "resonance_context": stale_resonance,
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="航发科技")
    _add_reason(rows, "600391", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "stale-daily",
        "signal_type": "一买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 120,
        "confidence": 0.7,
        "event_dt": "2026-04-29T10:00:00",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="航发科技")
    _add_reason(rows, "600391", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "stale-weekly",
        "signal_type": "MACD绿柱扩大_零上",
        "signal_side": "buy",
        "freq": "周线",
        "score": 120,
        "confidence": 0.8,
        "event_dt": "2026-04-30T10:00:00",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="航发科技")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    row = split["watch"][0]
    assert row["raw_code"] == "600391"
    assert row["score_components"]["timeframe_priority"] == 12.0
    assert row["score_components"]["multi_period_bonus"] == 0.0
    assert row["score_components"]["indicator_breadth"] == 4.0
    assert row["upper_timeframe_side"] == "none"
    assert row["right_signal_reasons"] == ["5分钟 MACD绿柱扩大_零上"]
    assert row["stale_signal_count"] == 2


def test_terminal_stock_pool_stale_intraday_signal_does_not_score_as_buy_point_or_ma():
    rows = {}
    _add_reason(rows, "300301", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "stale-15m",
        "signal_type": "二买",
        "signal_side": "buy",
        "freq": "15分钟",
        "score": 120,
        "confidence": 0.95,
        "event_dt": "2026-05-07",
        "as_of": "2026-05-08",
        "ma_alignment": _ma_alignment(),
    }, index_codes=set(), name="过期分钟股")
    _add_chain_context(
        rows,
        "300301",
        chain_name="机器人产业链",
        node_name="人形机器人",
        phase="warming",
        rank=2,
        name="过期分钟股",
    )

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    row = split["watch"][0]
    assert row["raw_code"] == "300301"
    assert row["score_components"]["buy_point_quality"] == 0.0
    assert row["score_components"]["ma_alignment"] == 0.0
    assert row["score_components"]["hot_sector"] > 0.0
    assert row["stale_signal_count"] == 1


def test_terminal_stock_pool_defensive_full_confirmation_ranks_behind_mainline():
    rows = {}
    for code, chain_name, node_name, name in (
        ("300104", "电新/锂电池产业链", "储能/锂资源", "电新主线股"),
        ("300105", "纺织轻工产业链", "纺织/轻工", "纺织防守股"),
    ):
        _add_reason(rows, code, {
            "reason_type": "technical_trigger",
            "source_collection": "terminal_technical_signals",
            "source_doc_id": f"{code}-30m",
            "signal_type": "二买",
            "signal_side": "buy",
            "freq": "30分钟",
            "score": 100,
            "confidence": 0.85,
            "ma_alignment": _ma_alignment(),
        }, index_codes=set(), name=name)
        _add_reason(rows, code, {
            "reason_type": "technical_trigger",
            "source_collection": "terminal_technical_signals",
            "source_doc_id": f"{code}-15m",
            "signal_type": "二买",
            "signal_side": "buy",
            "freq": "15分钟",
            "score": 100,
            "confidence": 0.85,
            "ma_alignment": _ma_alignment(),
        }, index_codes=set(), name=name)
        _add_reason(rows, code, {
            "reason_type": "technical_trigger",
            "source_collection": "terminal_technical_signals",
            "source_doc_id": f"{code}-daily",
            "signal_type": "一买",
            "signal_side": "buy",
            "freq": "日线",
            "score": 100,
            "confidence": 0.85,
            "ma_alignment": _ma_alignment(),
        }, index_codes=set(), name=name)
        _add_chain_context(rows, code, chain_name=chain_name, node_name=node_name, phase="warming", rank=3, name=name)

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert [row["raw_code"] for row in split["focus"][:2]] == ["300104", "300105"]
    assert split["focus"][0]["sector_policy"] == "mainline_lenient"
    assert split["focus"][1]["sector_policy"] == "defensive_strict"
    assert split["focus"][0]["rank_score"] > split["focus"][1]["rank_score"]


def test_terminal_stock_pool_entry_ready_rank_uses_timeframe_and_score_components():
    rows = {}
    _add_reason(rows, "300575", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "daily-ready-anchor",
        "signal_type": "趋势买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 105,
        "confidence": 0.8,
    }, index_codes=set(), name="日线确认股")
    _add_reason(rows, "300575", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "daily-ready",
        "signal_type": "三买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 100,
        "confidence": 0.7,
        "resonance_context": {
            "direction": "buy",
            "aligned_freqs": ["日线", "30分钟", "15分钟"],
            "conflict_freqs": [],
            "grade": "multi_period",
        },
    }, index_codes=set(), name="日线确认股")
    _add_reason(rows, "300575", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "daily-ready-15m",
        "signal_type": "MACD绿柱扩大_零上",
        "signal_side": "buy",
        "freq": "15分钟",
        "score": 90,
        "confidence": 0.8,
    }, index_codes=set(), name="日线确认股")
    _add_reason(rows, "688484", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "weekly-ready-anchor",
        "signal_type": "趋势买",
        "signal_side": "buy",
        "freq": "周线",
        "score": 105,
        "confidence": 0.8,
    }, index_codes=set(), name="周线确认股")
    _add_reason(rows, "688484", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "weekly-ready",
        "signal_type": "三买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 100,
        "confidence": 0.7,
        "resonance_context": {
            "direction": "buy",
            "aligned_freqs": ["周线", "30分钟", "15分钟"],
            "conflict_freqs": [],
            "grade": "multi_period",
        },
    }, index_codes=set(), name="周线确认股")
    _add_reason(rows, "688484", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "weekly-ready-15m",
        "signal_type": "MACD绿柱扩大_零上",
        "signal_side": "buy",
        "freq": "15分钟",
        "score": 90,
        "confidence": 0.8,
    }, index_codes=set(), name="周线确认股")

    split = _split_pool_rows(rows, focus_limit=72, risk_limit=72, watch_limit=72)

    assert [row["raw_code"] for row in split["focus"]] == ["300575", "688484"]
    assert [row["rank"] for row in split["focus"]] == [1, 2]
    assert "周期优先级" in split["focus"][0]["rank_reason"]
    assert "周/日线" in split["focus"][0]["rank_reason"]


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


def test_review_sector_bullish_creates_candidate_with_correct_weight():
    from signals.sync.modules.terminal_pool import _add_reason, _has_clue_source, _clue_quality_score, REVIEW_CLUE_REASON_TYPES

    rows = {}
    _add_reason(rows, "600036", {
        "reason_type": "review_sector_bullish",
        "source_collection": "review_sector_clues",
        "source_doc_id": "道长2026-04-27复盘",
        "signal_type": "道长看好银行",
        "signal_side": "buy",
        "source_role": "review_clue",
        "decision_effect": "context_only",
        "can_create_candidate": True,
        "board_or_concept": "银行",
        "evidence": {"author": "daozhang", "review_date": "2026-04-27", "snippet": "银行防御反击"},
    }, index_codes=set(), name="招商银行")

    assert "600036" in rows
    row = rows["600036"]
    assert row["raw_code"] == "600036"
    assert row["signal_origin"] == "review_sector_bullish"
    assert _has_clue_source(row)
    assert row["inclusion_reasons"][0]["reason_type"] == "review_sector_bullish"
    assert row["inclusion_reasons"][0]["source_role"] == "review_clue"
    assert row["inclusion_reasons"][0]["board_or_concept"] == "银行"


def test_add_review_clue_rows_uses_recent_review_window(monkeypatch):
    from datetime import date, datetime
    from signals.sync.modules import terminal_pool

    captured = {}

    def fake_iter_review_notes(db, since=None):
        captured["since"] = since
        return []

    monkeypatch.setenv("TERMINAL_REVIEW_CLUE_LOOKBACK_DAYS", "14")
    monkeypatch.setattr(terminal_pool, "naive_market_now", lambda market: datetime(2026, 5, 24, 10, 0))
    monkeypatch.setattr(terminal_pool, "_iter_review_notes", fake_iter_review_notes)

    terminal_pool._add_review_clue_rows({}, _Db({}), set())

    assert captured["since"] == date(2026, 5, 10)


def test_iter_review_notes_filters_stale_frontmatter(tmp_path, monkeypatch):
    from datetime import date
    from signals.sync.modules import knowledge_market_views, terminal_pool

    inbox = tmp_path / "10 Inbox" / "WeChat"
    inbox.mkdir(parents=True)
    (inbox / "old.md").write_text(
        "---\n"
        "title: 旧银行复盘\n"
        "author_focus: daozhang\n"
        "created_at: 2026-04-28T09:00:00\n"
        "---\n"
        "银行继续防御反击，可以关注。",
        encoding="utf-8",
    )
    (inbox / "new.md").write_text(
        "---\n"
        "title: 新半导体复盘\n"
        "author_focus: pangge\n"
        "created_at: 2026-05-17T09:00:00\n"
        "---\n"
        "半导体中期仍有机会，可以关注。",
        encoding="utf-8",
    )
    monkeypatch.setattr(knowledge_market_views, "_vault_dir", lambda: tmp_path)

    notes = terminal_pool._iter_review_notes(_Db({}), since=date(2026, 5, 10))

    assert [note["title"] for note in notes] == ["新半导体复盘"]
    assert notes[0]["sectors"][0]["keyword"] == "半导体"


def test_review_sector_bearish_has_zero_weight():
    from signals.sync.modules.terminal_pool import _add_reason

    rows = {}
    _add_reason(rows, "600036", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "tech-buy",
        "signal_type": "三买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 120,
    }, index_codes=set(), name="招商银行")
    _add_reason(rows, "600036", {
        "reason_type": "review_sector_bearish",
        "source_collection": "review_sector_clues",
        "source_doc_id": "胖哥看空恒科",
        "signal_type": "胖哥回避银行",
        "signal_side": "neutral",
        "source_role": "review_clue",
        "decision_effect": "exit_priority",
        "can_create_candidate": False,
        "board_or_concept": "银行",
        "evidence": {"author": "pangge", "review_date": "2026-04-28"},
    }, index_codes=set(), name="招商银行")

    bear_reason = [r for r in rows["600036"]["inclusion_reasons"] if r["reason_type"] == "review_sector_bearish"][0]
    assert bear_reason["weight"] == 0.0


def test_risk_reasons_includes_review_sector_bearish():
    from signals.sync.modules.terminal_pool import _add_reason, _risk_reasons

    rows = {}
    _add_reason(rows, "688981", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "tech-buy",
        "signal_type": "三买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 120,
    }, index_codes=set(), name="中芯国际")
    _add_reason(rows, "688981", {
        "reason_type": "review_sector_bearish",
        "source_collection": "review_sector_clues",
        "source_doc_id": "胖哥看空恒科",
        "signal_type": "胖哥回避科技",
        "signal_side": "sell",
        "source_role": "review_clue",
        "decision_effect": "exit_priority",
        "can_create_candidate": False,
        "board_or_concept": "科技",
        "evidence": {"author": "pangge"},
    }, index_codes=set(), name="中芯国际")

    risks = _risk_reasons(rows["688981"])
    assert any(r["reason_type"] == "review_sector_bearish" for r in risks)


def test_clue_quality_score_ranks_higher_with_review_source():
    from signals.sync.modules.terminal_pool import _add_reason, _clue_quality_score

    rows = {}
    _add_reason(rows, "600036", {
        "reason_type": "review_sector_bullish",
        "source_collection": "review_sector_clues",
        "source_doc_id": "道长复盘",
        "signal_type": "道长看好银行",
        "signal_side": "buy",
        "source_role": "review_clue",
        "decision_effect": "context_only",
        "can_create_candidate": True,
        "board_or_concept": "银行",
        "evidence": {"author": "daozhang"},
    }, index_codes=set(), name="招商银行")

    rows2 = {}
    _add_reason(rows2, "000858", {
        "reason_type": "user_pinned",
        "source_collection": "config",
        "source_doc_id": "priority",
        "signal_type": "用户重点观察",
        "signal_side": "buy",
    }, index_codes=set(), name="五粮液")

    assert _clue_quality_score(rows["600036"]) > _clue_quality_score(rows2["000858"])


def test_clue_quality_score_boosts_with_technical_proximity():
    from signals.sync.modules.terminal_pool import _add_reason, _clue_quality_score

    rows = {}
    _add_reason(rows, "600036", {
        "reason_type": "review_sector_bullish",
        "source_collection": "review_sector_clues",
        "source_doc_id": "道长复盘",
        "signal_type": "道长看好银行",
        "signal_side": "buy",
        "source_role": "review_clue",
        "decision_effect": "context_only",
        "can_create_candidate": True,
        "board_or_concept": "银行",
        "evidence": {"author": "daozhang"},
    }, index_codes=set(), name="招商银行")

    score_no_tech = _clue_quality_score(rows["600036"])

    _add_reason(rows, "600036", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "tech-daily",
        "signal_type": "背驰买",
        "signal_side": "buy",
        "freq": "日线",
        "score": 88,
        "confidence": 0.7,
    }, index_codes=set(), name="招商银行")

    score_with_tech = _clue_quality_score(rows["600036"])
    assert score_with_tech > score_no_tech


def test_has_clue_source_detects_review_and_legacy_sources():
    from signals.sync.modules.terminal_pool import _add_reason, _has_clue_source

    rows_review = {}
    _add_reason(rows_review, "600036", {
        "reason_type": "review_sector_bullish",
        "source_collection": "review_sector_clues",
        "source_doc_id": "道长复盘",
        "signal_type": "道长看好银行",
        "signal_side": "buy",
        "source_role": "review_clue",
        "decision_effect": "context_only",
        "can_create_candidate": True,
        "board_or_concept": "银行",
        "evidence": {"author": "daozhang"},
    }, index_codes=set(), name="招商银行")
    assert _has_clue_source(rows_review["600036"])

    rows_pin = {}
    _add_reason(rows_pin, "000858", {
        "reason_type": "user_pinned",
        "source_collection": "config",
        "source_doc_id": "priority",
        "signal_type": "用户重点观察",
        "signal_side": "buy",
    }, index_codes=set(), name="五粮液")
    assert _has_clue_source(rows_pin["000858"])

    rows_tech = {}
    _add_reason(rows_tech, "300575", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "tech-1",
        "signal_type": "三买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 120,
    }, index_codes=set(), name="中旗新材")
    assert not _has_clue_source(rows_tech["300575"])


def test_terminal_stock_pool_clue_overflow_backfills_watch_pool():
    rows = {}
    for index in range(6):
        code = f"30060{index}"
        _add_reason(rows, code, {
            "reason_type": "review_sector_bullish",
            "source_collection": "review_sector_clues",
            "source_doc_id": f"clue-{index}",
            "signal_type": "复盘线索",
            "signal_side": "buy",
            "source_role": "review_clue",
            "decision_effect": "context_only",
            "can_create_candidate": True,
            "board_or_concept": "机器人",
            "evidence": {"author": "daozhang"},
        }, index_codes=set(), name=f"线索股{index}")

    clue_candidates = list(rows.values())
    clue_symbols = {row["symbol"] for row in clue_candidates[:2]}
    watch_stocks = []

    added = _backfill_watch_from_clue_candidates(
        watch_stocks,
        clue_candidates,
        clue_symbols=clue_symbols,
        watch_limit=3,
    )

    assert added == 3
    assert [row["raw_code"] for row in watch_stocks] == ["300602", "300603", "300604"]
    assert all(row["pool_type"] == "watch" for row in watch_stocks)
    assert all(row["watch_backfill_source"] == "clue_overflow" for row in watch_stocks)
    assert all(row["entry_gate_status"] == "watch_only_not_hard_buy" for row in watch_stocks)
    assert all(row["market_setup_bias"] == "watch_only" for row in watch_stocks)
    assert all(row["can_trade_now"] is False for row in watch_stocks)


def test_terminal_stock_pool_storage_row_compacts_repeated_reason_analysis():
    ma_alignment = {
        "ma13": 10.0,
        "above_ma13": True,
        "fib_array_summary": "MA13回踩承接",
        "fib_ma_array": [
            {
                "period": period,
                "name": f"MA{period}",
                "value": 10.0 + period / 100,
                "pullback_touch": True,
                "pullback_acceptance": True,
                "acceptance_score": 3.5,
                "notes": "x" * 4000,
            }
            for period in (5, 8, 10, 13, 20, 21)
        ],
        "tags": ["MA13承接", "多周期共振"],
    }
    reason = {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "large-reason",
        "signal_type": "三买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 120,
        "confidence": 0.9,
        "as_of": "2026-05-13",
        "event_dt": "2026-05-13 15:00:00",
        "evidence": {
            "details": "y" * 8000,
            "entry_factor": {
                "group": "200d_new_high_breakout",
                "breakout_pct": 3.2,
                "volume_ratio": 2.1,
            },
            "ma_alignment": ma_alignment,
        },
        "resonance_context": {
            "grade": "multi_period",
            "aligned_freqs": ["日线", "30分钟", "15分钟"],
            "tags": ["多周期共振"],
        },
        "ma_alignment": ma_alignment,
    }
    row = {
        "symbol": "SZ.300575",
        "code": "300575",
        "raw_code": "300575",
        "name": "中旗新材",
        "pool_type": "focus",
        "rank": 1,
        "rank_score": 300,
        "latest_signal": "三买",
        "inclusion_reasons": [dict(reason, source_doc_id=f"large-reason-{idx}") for idx in range(12)],
        "top_buy_reason": reason,
        "technical_evidence": reason,
        "ma_alignment": ma_alignment,
        "broad_market_context": {"summary": "z" * 10000},
    }

    raw_size = len(BSON.encode(row))
    slim = _slim_pool_row_for_storage(row)
    slim_size = len(BSON.encode(slim))

    assert raw_size > 500_000
    assert slim_size < 120_000
    assert len(slim["inclusion_reasons"]) == 6
    assert all("ma_alignment" not in item for item in slim["inclusion_reasons"])
    assert slim["technical_evidence"]["ma_alignment"]["fib_array_summary"] == "MA13回踩承接"
    assert slim["top_buy_reason"]["evidence"]["entry_factor"]["group"] == "200d_new_high_breakout"


def test_gate_progress_returns_zero_for_no_technical_reasons():
    from signals.sync.modules.terminal_pool import _add_reason, _gate_progress

    rows = {}
    _add_reason(rows, "600036", {
        "reason_type": "review_sector_bullish",
        "source_collection": "review_sector_clues",
        "source_doc_id": "道长复盘",
        "signal_type": "道长看好银行",
        "signal_side": "buy",
        "source_role": "review_clue",
        "decision_effect": "context_only",
        "can_create_candidate": True,
        "board_or_concept": "银行",
        "evidence": {"author": "daozhang"},
    }, index_codes=set(), name="招商银行")

    assert _gate_progress(rows["600036"]) == 0


def test_gate_progress_scores_multi_freq_entry():
    from signals.sync.modules.terminal_pool import _add_reason, _gate_progress

    rows = {}
    _add_reason(rows, "300575", {
        "reason_type": "technical_trigger",
        "source_collection": "terminal_technical_signals",
        "source_doc_id": "daily-30m-15m",
        "signal_type": "三买",
        "signal_side": "buy",
        "freq": "30分钟",
        "score": 120,
        "confidence": 0.8,
        "resonance_context": {
            "direction": "buy",
            "aligned_freqs": ["日线", "30分钟", "15分钟"],
            "conflict_freqs": [],
            "grade": "multi_period",
        },
    }, index_codes=set(), name="中旗新材")

    score = _gate_progress(rows["300575"])
    assert score >= 5, f"Expected >=5 got {score}"
