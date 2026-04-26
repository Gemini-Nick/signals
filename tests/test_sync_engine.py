# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timedelta

from signals.sync.engine import SyncEngine


def test_schedule_due_after_scheduled_weekday():
    now = datetime(2026, 4, 24, 16, 45)  # Friday
    assert SyncEngine._schedule_due("16:30 weekday", now) is True


def test_schedule_not_due_before_scheduled_weekday():
    now = datetime(2026, 4, 24, 16, 15)  # Friday
    assert SyncEngine._schedule_due("16:30 weekday", now) is False


def test_sunday_schedule_only_on_sunday():
    sunday = datetime(2026, 4, 26, 10, 1)
    friday = datetime(2026, 4, 24, 10, 1)
    assert SyncEngine._schedule_due("Sunday 10:00", sunday) is True
    assert SyncEngine._schedule_due("Sunday 10:00", friday) is False


class _FakeCollection:
    def __init__(self, count=0, doc=None):
        self.count = count
        self.doc = doc or {}

    def estimated_document_count(self):
        return self.count

    def find_one(self, *args, **kwargs):
        return self.doc

    def update_one(self, *args, **kwargs):
        return None


class _FakeDb(dict):
    def __missing__(self, key):
        self[key] = _FakeCollection()
        return self[key]


def test_zero_insert_empty_target_is_degraded():
    engine = object.__new__(SyncEngine)
    engine.db = _FakeDb({"quote_snapshots": _FakeCollection(count=0)})

    status, error = engine._classify_result("quote_snapshots", {"inserted": 0})

    assert status == "degraded"
    assert error == "target_empty_after_zero_insert"


def test_stale_running_module_can_rerun():
    engine = object.__new__(SyncEngine)
    engine.db = _FakeDb({
        "sync_log": _FakeCollection(doc={
            "status": "running",
            "last_run": datetime.now() - timedelta(hours=3),
        })
    })

    assert engine._has_run_today("stock_daily", datetime.now().strftime("%Y-%m-%d")) is False
