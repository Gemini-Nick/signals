# -*- coding: utf-8 -*-
"""Repair legacy Mongo OHLCV volume units.

Canonical A-share stock bars store ``vol`` in shares. Older daily rows were
written before unit metadata existed, and some of those rows kept provider
volume in hands. This module repairs only rows with missing unit metadata and
keeps ``source_vol`` / repair metadata for auditability.
"""
from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Any, Iterable

from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.sync.volume_units import CANONICAL_STOCK_VOLUME_UNIT, normalize_volume_unit

DAILY_FREQ = "日线"
REPAIR_VERSION = "daily_volume_unit_v1"

_HAND_SOURCES = {
    "eastmoney",
    "eastmoney_spot_clist_batch",
    "eastmoney_push2delay",
    "eastmoney_push2delay_clist",
    "eastmoney_push2delay_ulist",
    "tencent",
}
_SHARE_SOURCES = {
    "sina",
    "baostock",
    "bars_latest",
    "fullmarket_spot_snapshot",
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        import pandas as pd

        parsed = pd.to_numeric(value, errors="coerce")
        if pd.isna(parsed):
            return default
        return float(parsed)
    except Exception:
        try:
            return float(value)
        except Exception:
            return default


def _source_unit_hint(source: Any) -> str:
    raw = str(source or "").strip().lower()
    if raw in _HAND_SOURCES:
        return "hands"
    if raw in _SHARE_SOURCES:
        return "shares"
    return ""


def _amount_unit_hint(doc: dict[str, Any]) -> tuple[str, str]:
    vol = _number(doc.get("vol"))
    amount = _number(doc.get("amount"))
    close = _number(doc.get("close"))
    high = _number(doc.get("high"), close)
    low = _number(doc.get("low"), close)
    if vol <= 0 or amount <= 0 or close <= 0:
        return "", ""
    low_bound = min(low, close) * 0.65
    high_bound = max(high, close) * 1.35
    if low_bound <= 0 or high_bound <= 0:
        return "", ""
    share_price = amount / vol
    hand_price = amount / (vol * 100)
    share_ok = low_bound <= share_price <= high_bound
    hand_ok = low_bound <= hand_price <= high_bound
    if hand_ok and not share_ok:
        return "hands", "amount_price_hands"
    if share_ok and not hand_ok:
        return "shares", "amount_price_shares"
    if hand_ok and share_ok:
        share_error = abs(share_price / close - 1)
        hand_error = abs(hand_price / close - 1)
        return ("hands", "amount_price_hands") if hand_error < share_error else ("shares", "amount_price_shares")
    return "", ""


def _reference_unit_hint(vol: float, reference_shares_volume: float | None) -> tuple[str, str]:
    if vol <= 0 or not reference_shares_volume or reference_shares_volume <= 0:
        return "", ""
    if 35 <= reference_shares_volume / vol <= 165:
        return "hands", "neighbor_scale_hands"
    if 0.2 <= vol / reference_shares_volume <= 5:
        return "shares", "neighbor_scale_shares"
    return "", ""


def infer_legacy_daily_volume_unit(
    doc: dict[str, Any],
    *,
    reference_shares_volume: float | None = None,
) -> tuple[str, str]:
    """Infer source unit for a legacy daily bar with missing unit metadata."""
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    explicit = normalize_volume_unit(meta.get("source_volume_unit") or meta.get("volume_unit"))
    if explicit:
        return explicit, "explicit_meta"

    amount_hint, amount_reason = _amount_unit_hint(doc)
    if amount_hint:
        return amount_hint, amount_reason

    source_hint = _source_unit_hint(meta.get("source") or doc.get("source"))
    if source_hint:
        return source_hint, f"source_{source_hint}"

    reference_hint, reference_reason = _reference_unit_hint(_number(doc.get("vol")), reference_shares_volume)
    if reference_hint:
        return reference_hint, reference_reason

    return "shares", "default_shares"


def canonical_stock_volume(raw_vol: Any, source_unit: str) -> int:
    vol = _number(raw_vol)
    if normalize_volume_unit(source_unit) == "hands":
        return int(round(vol * 100))
    return int(round(vol))


def _symbols_for_daily_repair(db: Database, symbols: Iterable[str] | None = None) -> list[str]:
    if symbols:
        return [str(symbol).strip().upper().split(".")[-1] for symbol in symbols if str(symbol).strip()]
    return [
        str(symbol)
        for symbol in db["bars"].distinct(
            "meta.symbol",
            {
                "meta.freq": DAILY_FREQ,
                "meta.symbol": {"$regex": r"^\d{6}$"},
                "$or": [
                    {"meta.volume_unit": {"$exists": False}},
                    {"meta.volume_unit": None},
                    {"meta.volume_unit": ""},
                ],
            },
        )
        if symbol
    ]


def _reference_volume(docs: list[dict[str, Any]]) -> float | None:
    values: list[float] = []
    for doc in docs:
        meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
        if normalize_volume_unit(meta.get("volume_unit")) == CANONICAL_STOCK_VOLUME_UNIT:
            vol = _number(doc.get("vol"))
            if vol > 0:
                values.append(vol)
            continue
        unit, reason = _amount_unit_hint(doc)
        if unit:
            repaired = canonical_stock_volume(doc.get("vol"), unit)
            if repaired > 0:
                values.append(float(repaired))
    if not values:
        return None
    return float(median(values[-30:]))


def repair_daily_volume_units(
    db: Database,
    *,
    symbols: Iterable[str] | None = None,
    dry_run: bool = True,
    batch_size: int = 1000,
) -> dict[str, Any]:
    """Repair legacy A-share daily bars whose volume unit metadata is missing."""
    symbol_list = _symbols_for_daily_repair(db, symbols)
    now = naive_market_now("A")
    stats: dict[str, Any] = {
        "symbols": len(symbol_list),
        "scanned": 0,
        "updates": 0,
        "multiplied": 0,
        "annotated_only": 0,
        "rewritten_symbols": 0,
        "reasons": {},
        "dry_run": dry_run,
    }

    for symbol in symbol_list:
        docs = list(db["bars"].find(
            {"meta.symbol": symbol, "meta.freq": DAILY_FREQ},
        ).sort("dt", 1))
        reference = _reference_volume(docs)
        symbol_updates = 0
        rewritten_docs: list[dict[str, Any]] = []
        for doc in docs:
            rewritten_doc = dict(doc)
            rewritten_doc.pop("_id", None)
            rewritten_doc["meta"] = dict(doc.get("meta") or {})
            meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
            if normalize_volume_unit(meta.get("volume_unit")):
                rewritten_docs.append(rewritten_doc)
                continue
            stats["scanned"] += 1
            unit, reason = infer_legacy_daily_volume_unit(doc, reference_shares_volume=reference)
            raw_vol = _number(doc.get("vol"))
            repaired_vol = canonical_stock_volume(raw_vol, unit)
            if repaired_vol <= 0:
                rewritten_docs.append(rewritten_doc)
                continue
            rewritten_doc["meta"].update({
                "volume_unit": CANONICAL_STOCK_VOLUME_UNIT,
                "source_volume_unit": unit,
                "source_vol": raw_vol,
                "volume_unit_repaired_at": now,
                "volume_unit_repair": REPAIR_VERSION,
                "volume_unit_repair_reason": reason,
            })
            if int(round(raw_vol)) != repaired_vol:
                rewritten_doc["vol"] = repaired_vol
                stats["multiplied"] += 1
            else:
                stats["annotated_only"] += 1
            stats["updates"] += 1
            symbol_updates += 1
            stats["reasons"][reason] = int(stats["reasons"].get(reason, 0)) + 1
            rewritten_docs.append(rewritten_doc)
        if symbol_updates and not dry_run:
            # ``bars`` is a time-series collection in the local runtime. MongoDB
            # only allows multi updates on meta fields there, so fixing ``vol``
            # must rewrite the symbol's daily series as a whole.
            db["bars"].delete_many({"meta.symbol": symbol, "meta.freq": DAILY_FREQ})
            if rewritten_docs:
                for start in range(0, len(rewritten_docs), batch_size):
                    db["bars"].insert_many(rewritten_docs[start:start + batch_size], ordered=False)
            stats["rewritten_symbols"] += 1
    return stats
