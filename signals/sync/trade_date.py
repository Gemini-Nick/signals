# -*- coding: utf-8 -*-
"""Resolve the persisted postmarket trade date for task-scoped writes."""
from __future__ import annotations

from datetime import datetime

from signals.core.trading_dates import a_share_realtime_day_key
from .task_context import get_task_env


def a_share_task_trade_date(*, now: datetime | None = None) -> str:
    """Use the postmarket run's immutable date when a task provides one."""
    explicit = str(get_task_env("SIGNALS_POSTMARKET_TRADE_DATE", "") or "").strip()
    if len(explicit) == 8 and explicit.isdigit():
        explicit = f"{explicit[:4]}-{explicit[4:6]}-{explicit[6:8]}"
    if len(explicit) >= 10 and explicit[4] == "-" and explicit[7] == "-":
        try:
            datetime.fromisoformat(explicit[:10])
            return explicit[:10]
        except ValueError:
            pass
    return a_share_realtime_day_key(now=now)
