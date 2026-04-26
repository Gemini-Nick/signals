# -*- coding: utf-8 -*-
"""Backtest service facade shared by web1 and web2 compatibility routes."""
from __future__ import annotations

from typing import Any


async def backtest_run(**kwargs: Any) -> Any:
    """Run historical backtest through the current implementation."""
    from signals.web2.api.backtest import backtest_run as _impl

    return await _impl(**kwargs)


async def backtest_analyze(**kwargs: Any) -> Any:
    """Run full historical signal analysis through the current implementation."""
    from signals.web2.api.backtest import backtest_analyze as _impl

    return await _impl(**kwargs)


async def backtest_scan(**kwargs: Any) -> Any:
    """Run parameter scan through the current implementation."""
    from signals.web2.api.backtest import backtest_scan as _impl

    return await _impl(**kwargs)
