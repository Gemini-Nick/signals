# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.sync.modules.terminal_pool import _add_reason, _reason_type_for_signal, _selected_rows


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
