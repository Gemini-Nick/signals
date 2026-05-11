# -*- coding: utf-8 -*-
"""Volume-unit helpers for market-data sync modules.

Canonical stock OHLCV bars in Mongo use shares for ``vol``. Some public
A-share providers return volume in hands, so sync modules normalize at write
time and keep the raw source unit in metadata for auditability.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

CANONICAL_STOCK_VOLUME_UNIT = "shares"

_HAND_UNITS = {"hand", "hands", "lot", "lots", "手"}
_SHARE_UNITS = {"share", "shares", "股"}

_HAND_VOLUME_SOURCES = {
    "eastmoney",
    "eastmoney_spot_clist_batch",
    "eastmoney_push2delay",
    "eastmoney_push2delay_clist",
    "eastmoney_push2delay_ulist",
    "tencent",
}
_SHARE_VOLUME_SOURCES = {
    "sina",
    "baostock",
    "bars_latest",
    "fullmarket_spot_snapshot",
}
_TENCENT_DAILY_SHARE_PREFIXES = ("688", "689")


def _number(value: Any, default: float = 0.0) -> float:
    parsed = pd.to_numeric(value, errors="coerce")
    return default if pd.isna(parsed) else float(parsed)


def normalize_volume_unit(unit: Any) -> str:
    raw = str(unit or "").strip().lower()
    if raw in _HAND_UNITS:
        return "hands"
    if raw in _SHARE_UNITS:
        return "shares"
    return ""


def stock_source_volume_unit(source: Any, *, default: str = "shares") -> str:
    raw = str(source or "").strip().lower()
    if raw in _HAND_VOLUME_SOURCES:
        return "hands"
    if raw in _SHARE_VOLUME_SOURCES:
        return "shares"
    return normalize_volume_unit(default) or "shares"


def pure_stock_code(symbol: Any) -> str:
    raw = str(symbol or "").strip().upper()
    if "." in raw:
        raw = raw.split(".", 1)[-1]
    if len(raw) >= 8 and raw[:2] in {"SH", "SZ", "BJ"}:
        raw = raw[2:]
    return raw if raw.isdigit() and len(raw) == 6 else ""


def tencent_daily_volume_unit(symbol: Any) -> str:
    """Tencent qfq daily returns STAR-market volume in shares, others in hands."""
    code = pure_stock_code(symbol)
    if code.startswith(_TENCENT_DAILY_SHARE_PREFIXES):
        return "shares"
    return "hands"


def normalize_stock_volume(
    value: Any,
    *,
    source: Any = "",
    source_unit: Any = "",
    default_source_unit: str = "shares",
) -> tuple[int, str]:
    """Return ``(canonical_shares, detected_source_unit)`` for A-share stocks."""
    unit = normalize_volume_unit(source_unit) or stock_source_volume_unit(source, default=default_source_unit)
    raw = _number(value, 0.0)
    if unit == "hands":
        return int(round(raw * 100)), unit
    return int(round(raw)), "shares"
