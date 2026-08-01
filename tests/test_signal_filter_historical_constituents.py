from unittest.mock import patch

from signals.core.signal_filter import _get_index_constituents


class _Collection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, *_args, **_kwargs):
        return list(self.rows)


class _DB:
    def __init__(self, rows):
        self.collection = _Collection(rows)

    def __getitem__(self, name):
        assert name == "index_constituents"
        return self.collection


def test_historical_index_lookup_requires_exactly_one_hashed_version():
    with patch("signals.sync.db.get_db", return_value=_DB([
        {"stocks": ["600000"], "payload_hash": "hash-1"},
    ])):
        assert _get_index_constituents("沪深300", trade_date="2026-07-31") == ["SH.600000"]

    with patch("signals.sync.db.get_db", return_value=_DB([
        {"stocks": ["600000"], "payload_hash": "hash-1"},
        {"stocks": ["600001"], "payload_hash": "hash-2"},
    ])):
        assert _get_index_constituents("沪深300", trade_date="2026-07-31") == []
