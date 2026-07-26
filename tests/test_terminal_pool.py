# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.sync.modules.terminal_pool import _add_stock, _display_badges_for_pool, _slim_reason_for_pool


def test_terminal_pool_does_not_add_index_code_as_stock():
    stocks: list[str] = []

    _add_stock(stocks, "000300", index_codes={"000300"})
    _add_stock(stocks, "688802", index_codes={"000300"})

    assert stocks == ["688802"]


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

    assert [item["kind"] for item in badges] == ["buy_point", "new_high", "ma_climb"]
    assert [item["label"] for item in badges] == ["周一买", "200日新高", "日MA5攀爬"]
    assert all({"kind", "timeframe", "priority"} <= set(item) for item in badges)
    assert len(badges) == 3


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
