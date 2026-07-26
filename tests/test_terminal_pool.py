# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.sync.modules.terminal_pool import (
    _attach_security_identities,
    _add_stock,
    _display_badges_for_pool,
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
