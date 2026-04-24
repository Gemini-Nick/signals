# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

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
