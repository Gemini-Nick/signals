# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timedelta

import signals.sync.modules.terminal_pool as terminal_pool_module
from signals.sync.modules.terminal_pool import (
    _apply_sector_transition_context,
    _attach_security_identities,
    _add_stock,
    _display_badges_for_pool,
    _load_sector_transition_context,
    _prefixed_symbol,
    _retain_ma_climb_reasons,
    _slim_reason_for_pool,
)


class _IdentityCursor(list):
    def sort(self, *args, **kwargs):
        return self


class _IdentityCollection:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query=None, projection=None):
        return _IdentityCursor(dict(item) for item in self.docs)


class _IdentityDb(dict):
    def __getitem__(self, key):
        return super().get(key, _IdentityCollection([]))


class _ContextCollection:
    def __init__(self, docs=None, one=None):
        self.docs = list(docs or [])
        self.one = one
        self.find_queries = []

    def find_one(self, query, projection=None, sort=None):
        self.find_queries.append(query)
        return dict(self.one) if self.one else None

    def find(self, query=None, projection=None):
        self.find_queries.append(query or {})
        return _IdentityCursor(dict(item) for item in self.docs)


def test_transition_context_loader_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SECTOR_TRANSITION_ENABLED", raising=False)
    assert _load_sector_transition_context({}) == []


def test_transition_context_loader_requires_current_fresh_unblocked_episode(monkeypatch):
    now = datetime(2026, 7, 29, 15, 10)
    monkeypatch.setenv("SECTOR_TRANSITION_ENABLED", "true")
    monkeypatch.setattr(terminal_pool_module, "naive_market_now", lambda _market: now)
    freshness = _ContextCollection(
        one={
            "updated_at": now - timedelta(minutes=1),
            "freshness": "fresh",
            "stale_reason": "",
        }
    )
    states = _ContextCollection(
        docs=[
            {
                "_id": "industry:保险",
                "market": "A",
                "trade_date": "2026-07-29",
                "active": True,
                "episode_id": "ep-ok",
                "turn_state": "repairing",
                "blockers": [],
            },
            {
                "_id": "concept:旧线索",
                "market": "A",
                "trade_date": "2026-07-29",
                "active": True,
                "episode_id": "ep-blocked",
                "turn_state": "panic_release",
                "blockers": ["technical_stale"],
            },
        ]
    )
    events = _ContextCollection(
        docs=[
            {
                "_id": "event-ok",
                "episode_id": "ep-ok",
                "trade_date": "2026-07-29",
                "observed_at": now,
            }
        ]
    )
    db = _IdentityDb(
        {
            "data_freshness": freshness,
            "sector_transition_states": states,
            "sector_transition_events": events,
        }
    )
    loaded = _load_sector_transition_context(db)
    assert [row["episode_id"] for row in loaded] == ["ep-ok"]
    assert loaded[0]["sector_event_id"] == "event-ok"
    assert states.find_queries[0]["trade_date"] == "2026-07-29"
    assert events.find_queries[0]["episode_id"] == {"$in": ["ep-ok"]}

    freshness.one["updated_at"] = now - timedelta(hours=1)
    assert _load_sector_transition_context(db) == []


def test_sector_transition_context_never_promotes_or_changes_risk_priority():
    focus = [{"raw_code": "000001", "rank_score": 100.0}]
    watch = [
        {"raw_code": "000002", "rank_score": 50.0},
        {"raw_code": "000003", "rank_score": 40.0},
    ]
    risk = [{"raw_code": "000004", "rank_score": 999.0}]
    rows = {}
    transitions = [
        {
            "_id": "industry:银行",
            "turn_state": "stable_turn",
            "active": True,
            "episode_id": "ep-stable",
            "sector_event_id": "event-stable",
            "sentinel_symbols": ["SZ.000001", "SZ.000003"],
        },
        {
            "_id": "industry:保险",
            "turn_state": "repairing",
            "active": True,
            "episode_id": "ep-repair",
            "sentinel_symbols": ["SZ.000002", "SZ.000006"],
        },
        {
            "_id": "concept:金融科技",
            "turn_state": "confirmed_intraday",
            "active": True,
            "episode_id": "ep-confirm",
            "sentinel_symbols": ["SZ.000004"],
        },
        {
            "_id": "concept:消费",
            "turn_state": "panic_release",
            "active": True,
            "episode_id": "ep-panic",
            "sentinel_symbols": ["SZ.000005"],
        },
        {
            "_id": "concept:旧转折",
            "turn_state": "failed",
            "active": False,
            "episode_id": "ep-failed",
            "sentinel_symbols": ["SZ.000007"],
        },
    ]
    counts = _apply_sector_transition_context(
        rows,
        focus_stocks=focus,
        risk_stocks=risk,
        watch_stocks=watch,
        transitions=transitions,
        index_codes=set(),
    )
    assert counts == {"clue": 2, "watch_tag": 2, "focus_tag": 1}
    assert focus[0]["source"] == "sector_transition"
    assert focus[0]["turn_state"] == "stable_turn"
    assert focus[0]["sector_transition_pool"] == "focus"
    assert focus[0]["sector_transition_eligibility"] == "buy_review_eligible"
    assert focus[0]["sector_transition_promoted"] is False
    assert "个股已独立通过买点池" in focus[0]["sector_transition_annotation"]
    assert watch[0]["turn_state"] == "repairing"
    assert watch[0]["sector_transition_eligibility"] == "watch_eligible"
    assert watch[1]["turn_state"] == "stable_turn"
    assert watch[1]["sector_transition_pool"] == "watch"
    assert watch[1]["sector_transition_eligibility"] == "buy_review_pending_individual_gates"
    assert "source" not in risk[0]
    assert focus[0]["rank_score"] == 100.0
    assert risk[0]["rank_score"] == 999.0
    assert rows["000005"]["source"] == "sector_transition"
    assert rows["000005"]["sector_transition_eligibility"] == "clue_only"
    assert "不是盯盘或买点信号" in rows["000005"]["sector_transition_annotation"]
    assert rows["000005"]["inclusion_reasons"][0]["reason_type"] == "sector_transition"
    assert rows["000006"]["turn_state"] == "repairing"
    assert rows["000006"]["sector_transition_pool"] == "clue"
    assert rows["000006"]["sector_transition_eligibility"] == "watch_pending_individual_gates"
    assert rows["000006"]["sector_transition_promoted"] is False
    assert "000007" not in rows


def test_terminal_pool_does_not_add_index_code_as_stock():
    stocks: list[str] = []

    _add_stock(stocks, "000300", index_codes={"000300"})
    _add_stock(stocks, "688802", index_codes={"000300"})

    assert stocks == ["688802"]


def test_terminal_pool_canonicalizes_etf_exchange_and_batch_resolves_names():
    rows = {
        "512600": {"symbol": "SZ.512600", "raw_code": "512600", "name": ""},
        "159520": {"symbol": "SZ.159520", "raw_code": "159520", "name": ""},
    }
    db = _IdentityDb({
        "etf_spot_snapshots": _IdentityCollection([
            {"code": "159520", "symbol": "SZ.159520", "name": "消费龙头ETF工银", "security_type": "etf"},
        ]),
        "security_master": _IdentityCollection([
            {"raw_code": "512600", "symbol": "SH.512600", "name": "消费ETF嘉实", "asset_type": "stock"},
        ]),
    })

    _attach_security_identities(rows, db)

    assert _prefixed_symbol("512600") == "SH.512600"
    assert rows["512600"]["symbol"] == "SH.512600"
    assert rows["512600"]["name"] == "消费ETF嘉实"
    assert rows["159520"]["symbol"] == "SZ.159520"
    assert rows["159520"]["name"] == "消费龙头ETF工银"
    assert rows["159520"]["security_type"] == "etf"
    assert all(row["name_status"] == "resolved" for row in rows.values())


def test_terminal_pool_display_badges_keep_only_hard_signals_in_priority_order():
    row = {
        "inclusion_reasons": [
            {
                "reason_type": "technical_trigger",
                "signal_side": "buy",
                "signal_type": "周一买",
                "freq": "周线",
                "score": 70,
            },
            {
                "reason_type": "technical_trigger",
                "signal_side": "buy",
                "signal_type": "200d_new_high_breakout",
                "freq": "日线",
                "score": 80,
            },
            {
                "reason_type": "technical_trigger",
                "signal_side": "buy",
                "signal_type": "MA攀爬",
                "signal_family": "ma_climb",
                "freq": "日线",
                "evidence": {"ma_climb": {"running": True, "effective_ma_name": "MA5", "climb_score": 88}},
            },
            {
                "reason_type": "technical_trigger",
                "signal_side": "buy",
                "signal_type": "缺口买:持续",
                "freq": "30分钟",
                "score": 70,
                "evidence": {"entry_factor": {"volume_ratio": 2.1}},
            },
            {
                "reason_type": "technical_trigger",
                "signal_side": "buy",
                "signal_type": "vol_contraction",
                "freq": "日线",
                "score": 90,
            },
            {
                "reason_type": "chain_context",
                "signal_type": "主线机会",
                "freq": "日线",
                "score": 99,
            },
        ],
    }

    badges = _display_badges_for_pool(row)

    assert [item["kind"] for item in badges] == ["buy_point", "ma_climb", "new_high"]
    assert [item["label"] for item in badges] == ["周一买", "日线攀爬", "200日新高"]
    assert [item["tone"] for item in badges] == ["buy", "hot", "hot"]
    assert all({"kind", "timeframe", "priority"} <= set(item) for item in badges)
    assert len(badges) == 3


def test_terminal_pool_reserves_sell_buy_and_climb_slots():
    row = {
        "inclusion_reasons": [
            {"reason_type": "technical_trigger", "signal_side": "sell", "signal_type": "5分钟一卖", "freq": "5分钟", "score": 70},
            {"reason_type": "technical_trigger", "signal_side": "buy", "signal_type": "30分钟二买", "freq": "30分钟", "score": 72},
            {
                "reason_type": "technical_trigger",
                "signal_side": "buy",
                "signal_type": "MA攀爬",
                "signal_family": "ma_climb",
                "freq": "周线",
                "evidence": {"ma_climb": {"running": True, "period": 10, "climb_score": 82}},
            },
            {"reason_type": "technical_trigger", "signal_side": "buy", "signal_type": "200日新高突破", "freq": "日线", "score": 90},
        ],
    }

    badges = _display_badges_for_pool(row)

    assert [item["kind"] for item in badges] == ["sell_point", "buy_point", "ma_climb"]
    assert [item["label"] for item in badges] == ["5m一卖", "30m二买", "周线攀爬"]
    assert [item["tone"] for item in badges] == ["risk", "buy", "hot"]


def test_terminal_pool_retains_one_effective_climb_reason_per_timeframe():
    reasons = [
        {
            "reason_type": "technical_trigger",
            "signal_family": "ma_climb",
            "freq": freq,
            "weight": 1,
            "evidence": {"ma_climb": {"period": period, "climb_score": score}},
        }
        for freq, period, score in (
            ("日线", 5, 86),
            ("日线", 10, 82),
            ("周线", 5, 80),
            ("周线", 10, 78),
        )
    ]
    reasons.extend({"reason_type": "technical_trigger", "signal_type": f"其他{index}", "weight": 99 - index} for index in range(8))

    retained = _retain_ma_climb_reasons(reasons, 8)
    climbs = [item for item in retained if item.get("signal_family") == "ma_climb"]

    assert {(item["freq"], item["evidence"]["ma_climb"]["period"]) for item in climbs} == {
        ("日线", 5),
        ("周线", 5),
    }


def test_terminal_pool_slim_reason_keeps_slim_ma_climb_evidence():
    reason = {
        "reason_type": "technical_trigger",
        "signal_side": "buy",
        "signal_type": "MA攀爬",
        "signal_family": "ma_climb",
        "freq": "周线",
        "evidence": {
            "ma_climb": {
                "running": True,
                "period": 10,
                "effective_ma_name": "MA10",
                "effective_ma": 12.34,
                "climb_score": 86,
                "debug_path": ["drop"],
            },
        },
    }

    slim = _slim_reason_for_pool(reason)

    assert slim["evidence"]["ma_climb"]["effective_ma_name"] == "MA10"
    assert slim["evidence"]["ma_climb"]["climb_score"] == 86
    assert "debug_path" not in slim["evidence"]["ma_climb"]
