# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.sync.volume_repair import (
    canonical_stock_volume,
    infer_legacy_daily_volume_unit,
    repair_daily_volume_units,
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


class _Cursor(list):
    def sort(self, *args, **kwargs):
        return self


class _Result:
    def __init__(self, count: int):
        self.deleted_count = count
        self.inserted_ids = list(range(count))


class _Collection:
    def __init__(self, docs):
        self.docs = list(docs)

    def distinct(self, field, query):
        values = []
        for doc in self.docs:
            if _matches(doc, query):
                value = _get(doc, field)
                if value not in values:
                    values.append(value)
        return values

    def find(self, query, projection=None):
        return _Cursor([doc for doc in self.docs if _matches(doc, query)])

    def delete_many(self, query):
        kept = [doc for doc in self.docs if not _matches(doc, query)]
        deleted = len(self.docs) - len(kept)
        self.docs = kept
        return _Result(deleted)

    def insert_many(self, docs, ordered=False):
        self.docs.extend(docs)
        return _Result(len(docs))


class _Db(dict):
    def __getitem__(self, key):
        return super().__getitem__(key)


def _get(doc, dotted):
    value = doc
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _matches(doc, query):
    import re

    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(doc, item) for item in expected):
                return False
            continue
        actual = _get(doc, key)
        if isinstance(expected, dict):
            if "$regex" in expected and not re.search(expected["$regex"], str(actual or "")):
                return False
            if "$exists" in expected and ((_get(doc, key) is not None) != bool(expected["$exists"])):
                return False
            continue
        if actual != expected:
            return False
    return True


def test_repair_daily_volume_units_divides_legacy_tencent_star_rows():
    docs = [
        {
            "dt": "2026-05-08",
            "meta": {
                "symbol": "688802",
                "freq": "日线",
                "source": "tencent",
                "volume_unit": "shares",
                "source_volume_unit": "hands",
                "source_vol": 3313196,
            },
            "open": 825,
            "high": 825,
            "low": 783.47,
            "close": 784,
            "vol": 331319600,
            "amount": 0,
        }
    ]
    db = _Db({"bars": _Collection(docs)})

    stats = repair_daily_volume_units(db, dry_run=False)
    repaired = db["bars"].docs[0]

    assert stats["divided"] == 1
    assert repaired["vol"] == 3313196
    assert repaired["meta"]["source_volume_unit"] == "shares"
    assert repaired["meta"]["volume_unit_repair_reason"] == "tencent_star_daily_source_shares"
