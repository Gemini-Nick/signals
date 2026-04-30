# -*- coding: utf-8 -*-
"""Scan cached bars and publish explainable hard-technical signals."""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

import pandas as pd
from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.market_time import naive_market_now, to_market_naive

logger = logging.getLogger("signals.sync.technical_signal_scan")

DAILY_FREQS = ["日线", "daily", "D", "1d"]
WEEKLY_FREQS = ["周线", "weekly", "W", "1w"]
MINUTE_FREQS = {
    "30分钟": ["30分钟", "30min", "30m", "F30"],
    "15分钟": ["15分钟", "15min", "15m", "F15"],
    "5分钟": ["5分钟", "5min", "5m", "F5"],
}
REQUIRED_FULL_FREQS = ("日线", "周线", "30分钟")
OPTIONAL_ON_DEMAND_FREQS = ("15分钟", "5分钟")
FREQ_ORDER = {
    "周线": 0,
    "weekly": 0,
    "1w": 0,
    "w": 0,
    "日线": 1,
    "daily": 1,
    "1d": 1,
    "d": 1,
    "30分钟": 2,
    "30min": 2,
    "30m": 2,
    "f30": 2,
    "15分钟": 3,
    "15min": 3,
    "15m": 3,
    "f15": 3,
    "5分钟": 4,
    "5min": 4,
    "5m": 4,
    "f5": 4,
}


def _pure_a_code(symbol: Any) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    pure = raw.split(".", 1)[-1] if "." in raw else raw
    pure = pure.replace("SH", "").replace("SZ", "").replace("BJ", "")
    return pure if pure.isdigit() and len(pure) == 6 else ""


def _prefixed_symbol(symbol: Any) -> str:
    code = _pure_a_code(symbol)
    if not code:
        return str(symbol or "").strip()
    if code.startswith(("6", "9")):
        return f"SH.{code}"
    if code.startswith(("4", "8")):
        return f"BJ.{code}"
    return f"SZ.{code}"


def _symbols_with_daily(db: Database) -> list[str]:
    symbols = db["bars"].distinct("meta.symbol", {"meta.freq": {"$in": DAILY_FREQS}})
    clean = sorted({_pure_a_code(symbol) for symbol in symbols if _pure_a_code(symbol)})
    max_symbols = int(os.getenv("TECHNICAL_SIGNAL_SCAN_MAX_SYMBOLS", "0"))
    if max_symbols > 0:
        return clean[:max_symbols]
    return clean


def _freq_aliases(label: str) -> list[str]:
    if label == "日线":
        return DAILY_FREQS
    if label == "周线":
        return WEEKLY_FREQS
    return MINUTE_FREQS.get(label, [label])


def _coverage_by_freq(db: Database, symbols: list[str]) -> dict[str, dict[str, Any]]:
    symbol_set = {_pure_a_code(symbol) for symbol in symbols if _pure_a_code(symbol)}
    total = len(symbol_set)
    coverage: dict[str, dict[str, Any]] = {}
    for label in (*REQUIRED_FULL_FREQS, *OPTIONAL_ON_DEMAND_FREQS):
        aliases = _freq_aliases(label)
        try:
            raw_symbols = db["bars"].distinct("meta.symbol", {"meta.freq": {"$in": aliases}})
            covered = {
                _pure_a_code(symbol)
                for symbol in raw_symbols
                if _pure_a_code(symbol) in symbol_set
            }
            latest = db["bars"].find_one(
                {"meta.freq": {"$in": aliases}},
                {"dt": 1},
                sort=[("dt", -1)],
            ) or {}
        except Exception:
            covered = set()
            latest = {}
        missing_count = max(0, total - len(covered))
        required = label in REQUIRED_FULL_FREQS
        coverage[label] = {
            "freq": label,
            "required": required,
            "mode": "full_market_required" if required else "on_demand",
            "symbol_count": len(covered),
            "total_symbols": total,
            "missing_count": missing_count,
            "coverage_pct": round((len(covered) / total * 100), 2) if total else 0.0,
            "latest_dt": latest.get("dt"),
            "status": "complete" if missing_count == 0 else ("coverage_incomplete" if required else "on_demand_missing"),
        }
    return coverage


def _rawbar_dt(value: Any, *, symbol: str = "", source: str = "") -> pd.Timestamp:
    # czsc.RawBar shifts naive Python datetime through the host timezone; keep
    # cached market labels as pandas Timestamp to avoid local-machine drift.
    normalized = to_market_naive(value, market="A", symbol=symbol, source=source)
    if normalized is None:
        return pd.to_datetime(value)
    return pd.Timestamp(normalized)


def _doc_to_rawbar(doc: dict[str, Any], symbol: str, freq, idx: int) -> Any:
    from czsc import RawBar

    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    return RawBar(
        symbol=symbol,
        dt=_rawbar_dt(doc.get("dt"), symbol=symbol, source=str(meta.get("source") or "")),
        id=idx,
        freq=freq,
        open=float(doc.get("open") or 0),
        high=float(doc.get("high") or 0),
        low=float(doc.get("low") or 0),
        close=float(doc.get("close") or 0),
        vol=int(float(doc.get("vol") or 0)),
        amount=int(float(doc.get("amount") or 0)),
    )


def _load_bars(db: Database, symbol: str, freq_values: list[str], freq, *, limit: int) -> list[Any]:
    docs = list(db["bars"].find(
        {"meta.symbol": symbol, "meta.freq": {"$in": freq_values}},
        {"_id": 0, "dt": 1, "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1},
    ).sort("dt", -1).limit(limit))
    docs.reverse()
    out = []
    output_symbol = _prefixed_symbol(symbol)
    for idx, doc in enumerate(docs):
        try:
            out.append(_doc_to_rawbar(doc, output_symbol, freq, idx))
        except Exception:
            continue
    return out


def _signal_side(signal_type: str, score: float) -> str:
    if "卖" in signal_type or "顶" in signal_type or "风险" in signal_type or score < 0:
        return "sell"
    return "buy"


def _freq_sort_key(freq: Any) -> tuple[int, str]:
    text = str(freq or "").strip()
    return FREQ_ORDER.get(text.lower(), FREQ_ORDER.get(text, 99)), text


def _event_dt_value(event: Any) -> datetime | None:
    value = getattr(event, "dt", None)
    if not value:
        return None
    try:
        parsed = pd.to_datetime(value).to_pydatetime()
    except Exception:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _event_side(event: Any, weights: dict[str, Any]) -> str:
    signal_type = str(getattr(event, "signal_type", "") or "")
    score = float(weights.get(signal_type, 0) or 0) * float(getattr(event, "confidence", 0) or 0)
    return _signal_side(signal_type, score)


def _resonance_grade(aligned_freqs: list[str], conflict_freqs: list[str]) -> str:
    if conflict_freqs:
        return "conflict"
    if len(aligned_freqs) >= 3:
        return "strong_resonance"
    if len(aligned_freqs) >= 2:
        return "multi_period"
    return "single_period"


def _resonance_context(events: list[Any], *, side: str, primary_freq: str, direction: str, weights: dict[str, Any]) -> dict[str, Any]:
    aligned_freqs = sorted(
        {str(getattr(event, "freq", "") or "") for event in events if _event_side(event, weights) == side},
        key=_freq_sort_key,
    )
    conflict_freqs = sorted(
        {str(getattr(event, "freq", "") or "") for event in events if _event_side(event, weights) != side},
        key=_freq_sort_key,
    )
    latest_values = [value for value in (_event_dt_value(event) for event in events) if value]
    latest_dt = max(latest_values).isoformat() if latest_values else ""
    grade = _resonance_grade(aligned_freqs, conflict_freqs)
    tags: list[str] = []
    if grade == "conflict":
        tags.append("周期冲突")
    if grade in {"multi_period", "strong_resonance"}:
        tags.append("多周期共振")
    if grade == "strong_resonance":
        tags.append("强共振")
    if "周线" in aligned_freqs and "日线" in aligned_freqs:
        tags.append("日周同向")
    if any(freq in {"5分钟", "5min", "5m", "F5"} for freq in aligned_freqs):
        tags.append("5m确认")
    if not tags:
        tags.append("硬技术")
    side_text = "买点" if side == "buy" else "风险"
    if grade == "conflict":
        summary = f"{side_text}信号存在周期冲突：同向 {','.join(aligned_freqs) or primary_freq}；冲突 {','.join(conflict_freqs)}"
    else:
        summary = f"{side_text}信号获得 {','.join(aligned_freqs) or primary_freq} 确认"
    return {
        "direction": direction or side,
        "primary_freq": primary_freq,
        "aligned_freqs": aligned_freqs,
        "conflict_freqs": conflict_freqs,
        "grade": grade,
        "tags": tags[:5],
        "summary": summary[:240],
        "latest_dt": latest_dt,
    }


def _details_dict(details: str) -> dict[str, Any]:
    if not details:
        return {}
    return {"summary": details[:800]}


def _scan_symbol(db: Database, symbol: str) -> list[dict[str, Any]]:
    from czsc import Freq
    from signals.core.analyzer import SymbolAnalyzer
    from signals.core.detectors import detect_all_signals
    from signals.core.scorer import SIGNAL_WEIGHTS, score_signals

    bars_by_freq: list[tuple[str, Any, list[Any], int]] = [
        ("日线", Freq.D, _load_bars(db, symbol, DAILY_FREQS, Freq.D, limit=360), 200),
        ("周线", Freq.W, _load_bars(db, symbol, WEEKLY_FREQS, Freq.W, limit=180), 100),
        ("30分钟", Freq.F30, _load_bars(db, symbol, MINUTE_FREQS["30分钟"], Freq.F30, limit=260), 80),
        ("15分钟", Freq.F15, _load_bars(db, symbol, MINUTE_FREQS["15分钟"], Freq.F15, limit=260), 80),
        ("5分钟", Freq.F5, _load_bars(db, symbol, MINUTE_FREQS["5分钟"], Freq.F5, limit=260), 80),
    ]
    events = []
    bi_counts: dict[str, int] = {}
    for label, freq, bars, max_bi in bars_by_freq:
        if len(bars) < 20:
            continue
        try:
            analyzer = SymbolAnalyzer(_prefixed_symbol(symbol), freq, bars, max_bi_num=max_bi)
            detected = detect_all_signals(analyzer.czsc, _prefixed_symbol(symbol))
            events.extend(detected)
            bi_counts[label] = len(analyzer.finished_bis)
        except Exception as exc:
            logger.debug("technical scan failed %s/%s: %s", symbol, label, exc)
    if not events:
        return []

    scored = score_signals(_prefixed_symbol(symbol), events)
    now = naive_market_now("A")
    buy_freqs = sorted({event.freq for event in events if _event_side(event, SIGNAL_WEIGHTS) == "buy"}, key=_freq_sort_key)
    sell_freqs = sorted({event.freq for event in events if _event_side(event, SIGNAL_WEIGHTS) == "sell"}, key=_freq_sort_key)
    docs: list[dict[str, Any]] = []
    for event in events:
        base_score = float(SIGNAL_WEIGHTS.get(event.signal_type, 0)) * float(event.confidence or 0)
        side = _signal_side(event.signal_type, base_score)
        dt_value = event.dt.replace(tzinfo=None) if getattr(event.dt, "tzinfo", None) else event.dt
        dedupe_key = f"{_prefixed_symbol(symbol)}|{event.freq}|{event.signal_type}|{dt_value.isoformat()}"
        resonance_context = _resonance_context(
            events,
            side=side,
            primary_freq=str(event.freq or ""),
            direction=scored.direction,
            weights=SIGNAL_WEIGHTS,
        )
        docs.append({
            "dedupe_key": dedupe_key,
            "symbol": _prefixed_symbol(symbol),
            "raw_code": _pure_a_code(symbol),
            "market": "A",
            "freq": event.freq,
            "dt": dt_value,
            "as_of": now.date().isoformat(),
            "updated_at": now,
            "signal_type": event.signal_type,
            "signal_side": side,
            "signal_family": "hard_technical",
            "price": float(event.price or 0),
            "score": round(base_score, 3),
            "total_score": float(scored.total_score or 0),
            "direction": scored.direction,
            "confidence": float(event.confidence or 0),
            "resonance_freqs": buy_freqs if side == "buy" else sell_freqs,
            "resonance_context": resonance_context,
            "technical_evidence": {
                "signal_type": event.signal_type,
                "freq": event.freq,
                "details": event.details,
                "score_details": scored.details[:1600],
                "bi_counts": bi_counts,
                "direction": scored.direction,
                "resonance_context": resonance_context,
            },
            "invalidates_when": "跌破信号触发价或上级周期转弱" if side == "buy" else "重新站回风险触发周期并出现买点确认",
            "source": "sync.technical_signal_scan",
        })
    return docs


def sync_technical_signal_scan(db: Database, proxy_url: str = None) -> dict:
    """Scan cached A-share hard-technical bars and publish explainable signals."""
    now = naive_market_now("A")
    try:
        symbols = _symbols_with_daily(db)
    except Exception as exc:
        return {"status": "error", "error_msg": f"symbol_universe_failed: {exc}"}
    coverage_by_freq = _coverage_by_freq(db, symbols)
    skipped_by_freq = {
        freq: {
            "missing_symbols": int(meta.get("missing_count") or 0),
            "status": str(meta.get("status") or ""),
            "mode": str(meta.get("mode") or ""),
        }
        for freq, meta in coverage_by_freq.items()
        if int(meta.get("missing_count") or 0) > 0
    }
    is_full_market_complete = all(
        coverage_by_freq.get(freq, {}).get("status") == "complete"
        for freq in REQUIRED_FULL_FREQS
    )
    workers = max(1, int(os.getenv("TECHNICAL_SIGNAL_SCAN_WORKERS", "4")))
    operations: list[UpdateOne] = []
    scanned = 0
    failed = 0
    signal_count = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="technical-scan") as executor:
        futures = {executor.submit(_scan_symbol, db, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            scanned += 1
            symbol = futures[future]
            try:
                docs = future.result()
            except Exception as exc:
                failed += 1
                logger.debug("technical scan symbol failed %s: %s", symbol, exc)
                continue
            signal_count += len(docs)
            for doc in docs:
                operations.append(UpdateOne({"dedupe_key": doc["dedupe_key"]}, {"$set": doc}, upsert=True))
            if len(operations) >= 500:
                db["terminal_technical_signals"].bulk_write(operations, ordered=False)
                operations.clear()
    if operations:
        db["terminal_technical_signals"].bulk_write(operations, ordered=False)

    db["data_freshness"].update_one(
        {"domain": "technical_signal", "market": "A", "mode": "postmarket", "collection": "terminal_technical_signals"},
        {"$set": {
            "domain": "technical_signal",
            "market": "A",
            "mode": "postmarket",
            "lane": "postmarket",
            "collection": "terminal_technical_signals",
            "freshness": "fresh",
            "latest_dt": now.date().isoformat(),
            "as_of": now.date().isoformat(),
            "updated_at": now,
            "stale_reason": "" if signal_count or not symbols else "no_technical_signal_detected",
            "count": signal_count,
            "scanned_symbols": scanned,
            "failed_symbols": failed,
            "required_freqs": list(REQUIRED_FULL_FREQS),
            "optional_freqs": list(OPTIONAL_ON_DEMAND_FREQS),
            "coverage_by_freq": coverage_by_freq,
            "skipped_by_freq": skipped_by_freq,
            "is_full_market_complete": is_full_market_complete,
            "coverage_status": "full_market_complete" if is_full_market_complete else "coverage_incomplete",
        }},
        upsert=True,
    )
    logger.info("technical signal scan: symbols=%d signals=%d failed=%d", scanned, signal_count, failed)
    return {
        "status": "ok",
        "inserted": signal_count,
        "symbols": scanned,
        "signals": signal_count,
        "failed": failed,
        "required_freqs": list(REQUIRED_FULL_FREQS),
        "optional_freqs": list(OPTIONAL_ON_DEMAND_FREQS),
        "coverage_by_freq": coverage_by_freq,
        "skipped_by_freq": skipped_by_freq,
        "is_full_market_complete": is_full_market_complete,
        "coverage_status": "full_market_complete" if is_full_market_complete else "coverage_incomplete",
    }
