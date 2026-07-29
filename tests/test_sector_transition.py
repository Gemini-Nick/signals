from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from signals.notify.intraday_sector_alerts import process_sector_transition_events
from signals.sync.modules.sector_transition import (
    _eligible_sector_rows,
    _flow_state,
    _stable_turn_blockers,
    evaluate_transition,
)
from signals.sync.modules.stock_minute import _sector_transition_sentinel_symbols


def _features(**overrides):
    values = {
        "change_pct": 0.1,
        "previous_change_pct": -0.7,
        "breadth_ratio": 0.55,
        "previous_breadth_ratio": 0.35,
        "limit_down_exits": 0,
        "freshness_blockers": [],
        "sentinel_ma": {
            "closed_bar_count": 2,
            "above_ma10_ratio": 0.75,
            "above_ma20_ratio": 0.25,
        },
    }
    values.update(overrides)
    return values


def test_closed_5m_is_a_hard_promotion_gate():
    state, blockers = evaluate_transition(
        _features(sentinel_ma={"closed_bar_count": 0}),
        "pressure",
    )
    assert state == "pressure"
    assert "closed_5m_missing" in blockers


def test_state_machine_promotes_one_stage_at_a_time():
    state, _ = evaluate_transition(_features(), "pressure")
    assert state == "panic_release"

    state, _ = evaluate_transition(
        _features(previous_change_pct=0.0, breadth_ratio=0.56, previous_breadth_ratio=0.52),
        "panic_release",
    )
    assert state == "repairing"

    state, _ = evaluate_transition(
        _features(
            change_pct=0.4,
            previous_change_pct=0.1,
            breadth_ratio=0.68,
            previous_breadth_ratio=0.55,
            sentinel_ma={
                "closed_bar_count": 3,
                "above_ma10_ratio": 0.8,
                "above_ma20_ratio": 0.75,
            },
        ),
        "repairing",
    )
    assert state == "confirmed_intraday"


def test_flow_state_caps_at_f2_without_explicit_donor_decline():
    features = {
        "change_delta_15m": 0.8,
        "breadth_delta_15m": 0.2,
        "breadth_ratio": 0.7,
    }
    assert _flow_state(features, "confirmed_intraday") == "F1"
    features["amount_share"] = 0.02
    assert _flow_state(features, "confirmed_intraday") == "F2"
    assert _flow_state({**features, "donor_technology_decline": True}, "confirmed_intraday") == "F3"


def test_stable_turn_requires_three_sessions_and_daily_confirmation():
    one_day = {
        "event_low_hold_sessions": 1,
        "above_ma5_ratio": 1.0,
        "above_ma10_ratio": 1.0,
        "volume_confirm_ratio": 1.0,
        "retest_hold_ratio": 1.0,
    }
    assert _stable_turn_blockers(one_day) == ["stable_requires_event_low_hold_3_sessions"]
    assert _stable_turn_blockers({**one_day, "event_low_hold_sessions": 3}) == []


def test_concept_guard_filters_thin_names_and_jaccard_aliases():
    rows = [
        {"kind": "industry", "name": "保险"},
        {"kind": "concept", "name": "存储A"},
        {"kind": "concept", "name": "存储别名"},
        {"kind": "concept", "name": "小概念"},
    ]
    constituents = {
        ("industry", "保险"): ["000001"],
        ("concept", "存储A"): ["000001", "000002", "000003", "000004", "000005"],
        ("concept", "存储别名"): ["000001", "000002", "000003", "000004", "000005"],
        ("concept", "小概念"): ["000001", "000002", "000003", "000004"],
    }
    spots = {
        code: {"amount": amount}
        for code, amount in zip(
            ["000001", "000002", "000003", "000004", "000005"],
            [50, 40, 30, 20, 10],
        )
    }
    eligible = _eligible_sector_rows(rows, constituents, spots)
    assert [(row["kind"], row["name"]) for row in eligible] == [
        ("industry", "保险"),
        ("concept", "存储A"),
    ]


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, count):
        self.rows = self.rows[:count]
        return self

    def __iter__(self):
        return iter(self.rows)


class _StateCollection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, *_args, **_kwargs):
        return _Cursor(self.rows)


class _FreshnessCollection:
    def __init__(self, row):
        self.row = row

    def find_one(self, *_args, **_kwargs):
        return deepcopy(self.row)


class _AlertCollection:
    def __init__(self):
        self.docs = {}

    def find_one(self, query, *_args, **_kwargs):
        return deepcopy(self.docs.get(query["_id"]))

    def update_one(self, query, update, upsert=False):
        assert upsert
        key = query["_id"]
        doc = self.docs.setdefault(key, {})
        if not doc:
            doc.update(deepcopy(update.get("$setOnInsert") or {}))
        doc.update(deepcopy(update.get("$set") or {}))
        for field, value in (update.get("$inc") or {}).items():
            doc[field] = int(doc.get(field) or 0) + int(value)


class _DB:
    def __init__(self, collections):
        self.collections = collections

    def __getitem__(self, name):
        return self.collections[name]


def test_sector_sentinel_quota_is_independent_and_deduped(monkeypatch):
    from signals.sync.modules import stock_minute

    now = datetime(2026, 7, 29, 13, 10)
    monkeypatch.setenv("SECTOR_TRANSITION_ENABLED", "true")
    monkeypatch.setenv("STOCK_MINUTE_SECTOR_SENTINEL_MAX_CODES", "4")
    monkeypatch.setenv("STOCK_MINUTE_SECTOR_SENTINEL_PER_SECTOR", "2")
    monkeypatch.setattr(stock_minute, "naive_market_now", lambda market: now)
    db = _DB(
        {
            "data_freshness": _FreshnessCollection(
                {"updated_at": now, "freshness": "fresh", "stale_reason": ""}
            ),
            "sector_transition_states": _StateCollection(
                [
                    {
                        "trade_date": "2026-07-29",
                        "episode_id": "one",
                        "blockers": [],
                        "sentinel_symbols": ["SH.603986", "SZ.001309", "SZ.000001"],
                    },
                    {
                        "trade_date": "2026-07-29",
                        "episode_id": "two",
                        "blockers": [],
                        "sentinels": ["SZ.001309", "SH.601318", "SH.600036"],
                    },
                ]
            )
        }
    )
    assert _sector_transition_sentinel_symbols(db) == ["603986", "001309", "601318", "600036"]

    monkeypatch.setenv("SECTOR_TRANSITION_ENABLED", "false")
    assert _sector_transition_sentinel_symbols(db) == []


def test_alerts_default_off_and_shadow_dedupes_semantic_transition(monkeypatch):
    collection = _AlertCollection()
    db = _DB({"notification_events": collection})
    event = {
        "_id": "watermark-1",
        "episode_id": "episode-1",
        "from_state": "pressure",
        "to_state": "panic_release",
        "rule_version": "sector-transition-v1",
        "sector_id": "industry:保险",
        "sector_name": "保险",
    }
    monkeypatch.delenv("SECTOR_TRANSITION_ENABLED", raising=False)
    assert process_sector_transition_events(db, [event])["status"] == "disabled"
    assert collection.docs == {}

    monkeypatch.setenv("SECTOR_TRANSITION_ENABLED", "true")
    monkeypatch.setenv("SECTOR_TRANSITION_NOTIFY_MODE", "shadow")
    result = process_sector_transition_events(db, [event, {**event, "_id": "watermark-2"}])
    assert result["recorded"] == 1
    assert len(collection.docs) == 1
    assert next(iter(collection.docs.values()))["delivery_status"] == "shadow_recorded"


def test_live_delivery_failure_is_retryable_and_does_not_raise(monkeypatch):
    collection = _AlertCollection()
    db = _DB({"notification_events": collection})
    event = {
        "_id": "event-1",
        "episode_id": "episode-2",
        "from_state": "repairing",
        "to_state": "confirmed_intraday",
        "rule_version": "sector-transition-v1",
    }
    monkeypatch.setenv("SECTOR_TRANSITION_ENABLED", "true")
    monkeypatch.setenv("SECTOR_TRANSITION_NOTIFY_MODE", "live")

    def fail(_message):
        raise RuntimeError("temporary")

    first = process_sector_transition_events(db, [event], notify_func=fail, gate_status="NOTIFY")
    second = process_sector_transition_events(
        db,
        [event],
        notify_func=lambda _message: None,
        gate_status="NOTIFY",
    )
    assert first["failed"] == 1
    assert second["sent"] == 1
    assert next(iter(collection.docs.values()))["delivery_status"] == "sent"


def test_live_cycle_merges_distinct_events_into_one_delivery(monkeypatch):
    collection = _AlertCollection()
    db = _DB({"notification_events": collection})
    base = {
        "from_state": "pressure",
        "to_state": "panic_release",
        "rule_version": "sector-transition-v1",
    }
    events = [
        {**base, "_id": "event-a", "episode_id": "episode-a", "sector_name": "保险"},
        {**base, "_id": "event-b", "episode_id": "episode-b", "sector_name": "食品饮料"},
    ]
    monkeypatch.setenv("SECTOR_TRANSITION_ENABLED", "true")
    monkeypatch.setenv("SECTOR_TRANSITION_NOTIFY_MODE", "live")
    messages = []
    result = process_sector_transition_events(
        db,
        events,
        notify_func=messages.append,
        gate_status="NOTIFY",
    )
    assert result["sent"] == 2
    assert len(messages) == 1
    assert "保险" in messages[0] and "食品饮料" in messages[0]


def test_live_mode_without_approved_gate_never_calls_external_sender(monkeypatch):
    collection = _AlertCollection()
    db = _DB({"notification_events": collection})
    event = {
        "_id": "event-gated",
        "episode_id": "episode-gated",
        "from_state": "pressure",
        "to_state": "panic_release",
        "rule_version": "sector-transition-v1",
    }
    monkeypatch.setenv("SECTOR_TRANSITION_ENABLED", "true")
    monkeypatch.setenv("SECTOR_TRANSITION_NOTIFY_MODE", "live")
    calls = []
    result = process_sector_transition_events(
        db,
        [event],
        notify_func=calls.append,
        gate_status="DONT_NOTIFY",
    )
    assert result["status"] == "blocked"
    assert result["sent"] == 0
    assert calls == []
    assert next(iter(collection.docs.values()))["delivery_status"] == "gate_blocked"
