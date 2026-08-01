# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.sync.task_context import task_env
from signals.sync.trade_date import a_share_task_trade_date


def test_postmarket_task_trade_date_overrides_wall_clock():
    with task_env({"SIGNALS_POSTMARKET_TRADE_DATE": "2026-07-30"}):
        assert a_share_task_trade_date(now=datetime(2026, 7, 31, 9, 0)) == "2026-07-30"


def test_trade_date_context_accepts_compact_date():
    with task_env({"SIGNALS_POSTMARKET_TRADE_DATE": "20260730"}):
        assert a_share_task_trade_date(now=datetime(2026, 7, 31, 9, 0)) == "2026-07-30"
