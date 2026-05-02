# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.sync.volume_repair import (
    canonical_stock_volume,
    infer_legacy_daily_volume_unit,
)


def test_infer_hands_from_amount_price():
    doc = {
        "vol": 1553982,
        "amount": 8_000_000_000,
        "high": 53,
        "low": 50,
        "close": 52,
        "meta": {"symbol": "002709", "freq": "日线"},
    }

    unit, reason = infer_legacy_daily_volume_unit(doc)

    assert unit == "hands"
    assert reason == "amount_price_hands"
    assert canonical_stock_volume(doc["vol"], unit) == 155398200


def test_infer_shares_from_amount_price():
    doc = {
        "vol": 147046184,
        "amount": 7_906_351_813,
        "high": 55,
        "low": 52,
        "close": 54,
        "meta": {"symbol": "002709", "freq": "日线", "source": "sina"},
    }

    unit, reason = infer_legacy_daily_volume_unit(doc)

    assert unit == "shares"
    assert reason == "amount_price_shares"
    assert canonical_stock_volume(doc["vol"], unit) == 147046184


def test_infer_hands_from_neighbor_scale_when_amount_missing():
    doc = {
        "vol": 1553982,
        "amount": 0,
        "high": 53,
        "low": 50,
        "close": 52,
        "meta": {"symbol": "002709", "freq": "日线"},
    }

    unit, reason = infer_legacy_daily_volume_unit(doc, reference_shares_volume=147_046_184)

    assert unit == "hands"
    assert reason == "neighbor_scale_hands"
