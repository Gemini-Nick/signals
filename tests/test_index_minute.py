# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.sync.modules import index_minute


def test_index_minute_worker_count_is_constrained(monkeypatch):
    monkeypatch.setenv("INDEX_MINUTE_WORKERS", "99")
    assert index_minute._worker_count() == 6

    monkeypatch.setenv("INDEX_MINUTE_WORKERS", "0")
    assert index_minute._worker_count() == 1


def test_index_minute_tail_count_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("INDEX_MINUTE_TAIL_COUNT", raising=False)
    monkeypatch.delenv("INDEX_MINUTE_TAIL_COUNT_5", raising=False)
    monkeypatch.delenv("INDEX_MINUTE_TAIL_COUNT_15", raising=False)
    monkeypatch.delenv("INDEX_MINUTE_TAIL_COUNT_30", raising=False)

    assert index_minute._tail_count_for_freq("5分钟") == 240
    assert index_minute._tail_count_for_freq("15分钟") == 160
    assert index_minute._tail_count_for_freq("30分钟") == 120

    monkeypatch.setenv("INDEX_MINUTE_TAIL_COUNT", "80")
    monkeypatch.setenv("INDEX_MINUTE_TAIL_COUNT_30", "90")
    assert index_minute._tail_count_for_freq("5分钟") == 80
    assert index_minute._tail_count_for_freq("30分钟") == 90
