# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.sync.modules.quote_snapshots import _quote_doc_from_em, _secid_for_symbol


def test_eastmoney_secid_for_prefixed_symbols():
    assert _secid_for_symbol("SH.601958") == "1.601958"
    assert _secid_for_symbol("SZ.000001") == "0.000001"
    assert _secid_for_symbol("SH.000300") == "1.000300"
    assert _secid_for_symbol("SZ.399001") == "0.399001"


def test_quote_doc_from_eastmoney_payload_scales_fields():
    payload = {
        "rc": 0,
        "data": {
            "f43": 1967,
            "f44": 1986,
            "f45": 1898,
            "f46": 1933,
            "f47": 304987,
            "f48": 591786626.0,
            "f57": "601958",
            "f58": "金钼股份",
            "f60": 1943,
            "f168": 95,
            "f169": 24,
            "f170": 124,
            "f171": 453,
        },
    }

    doc = _quote_doc_from_em("SH.601958", payload, datetime(2026, 4, 25, 10, 0), "2026-04-24")

    assert doc is not None
    assert doc["source"] == "eastmoney_push2delay"
    assert doc["freshness"] == "fresh"
    assert doc["price"] == 19.67
    assert doc["prev_close"] == 19.43
    assert doc["change_pct"] == 1.24
    assert doc["vol"] == 30498700
