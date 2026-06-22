# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from pymongo import ASCENDING, DESCENDING


class FakeCollection:
    def __init__(self, name: str):
        self.name = name
        self.created_indexes: list[tuple[tuple[tuple[str, int], ...], dict]] = []

    def create_index(self, keys, **kwargs):
        self.created_indexes.append((tuple(keys), dict(kwargs)))
        return "_".join(f"{key}_{direction}" for key, direction in keys)

    def index_information(self):
        return {}

    def drop_index(self, name):
        raise AssertionError(f"unexpected drop_index({name})")

    def delete_many(self, query):
        return None

    def update_one(self, query, update, upsert=False):
        return None


class FakeDB(dict):
    def __getitem__(self, name: str):
        if name not in self:
            self[name] = FakeCollection(name)
        return dict.__getitem__(self, name)


def test_storage_model_indexes_board_heat_tick_runtime_queries(monkeypatch):
    from signals.sync import storage

    db = FakeDB()
    monkeypatch.setattr(storage, "naive_market_now", lambda market: datetime(2026, 6, 8, 15, 0))
    monkeypatch.setattr(storage, "trading_day_key", lambda market, now=None: "2026-06-08")

    storage.ensure_storage_model(db)

    board_heat_indexes = {keys for keys, _kwargs in db["board_heat_ticks"].created_indexes}
    assert (
        ("kind", ASCENDING),
        ("trade_date", ASCENDING),
        ("trade_minute", DESCENDING),
        ("snapshot_at", DESCENDING),
    ) in board_heat_indexes
    assert (
        ("kind", ASCENDING),
        ("trade_minute", ASCENDING),
        ("change_pct", DESCENDING),
        ("rank_idx", ASCENDING),
    ) in board_heat_indexes


def test_storage_model_indexes_terminal_status_latest_queries(monkeypatch):
    from signals.sync import storage

    db = FakeDB()
    monkeypatch.setattr(storage, "naive_market_now", lambda market: datetime(2026, 6, 8, 15, 0))
    monkeypatch.setattr(storage, "trading_day_key", lambda market, now=None: "2026-06-08")

    storage.ensure_storage_model(db)

    for collection_name in (
        "terminal_stock_pool",
        "terminal_technical_signals",
        "knowledge_market_views",
        "chain_heat_snapshots",
    ):
        indexes = {keys for keys, _kwargs in db[collection_name].created_indexes}
        assert (("updated_at", DESCENDING),) in indexes
