# -*- coding: utf-8 -*-
"""Shared acceptance checks for persisted OHLCV bars."""
from __future__ import annotations

import math
from typing import Any


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def validate_ohlcv_bar(bar: dict[str, Any], *, allow_zero_volume: bool = False) -> tuple[bool, str]:
    values = {field: _finite(bar.get(field)) for field in ("open", "high", "low", "close")}
    if any(value is None for value in values.values()):
        return False, "non_finite_price"
    if any(value <= 0 for value in values.values() if value is not None):
        return False, "non_positive_price"
    if values["low"] > values["high"]:
        return False, "low_above_high"
    if not values["low"] <= values["open"] <= values["high"]:
        return False, "open_outside_range"
    if not values["low"] <= values["close"] <= values["high"]:
        return False, "close_outside_range"
    volume = _finite(bar.get("vol", bar.get("volume")))
    if volume is None:
        return False, "non_finite_volume"
    if volume < 0 or (volume == 0 and not allow_zero_volume):
        return False, "invalid_volume"
    amount = bar.get("amount")
    if amount is not None:
        parsed_amount = _finite(amount)
        if parsed_amount is None or parsed_amount < 0:
            return False, "invalid_amount"
    if bar.get("dt") is None:
        return False, "missing_datetime"
    return True, ""
