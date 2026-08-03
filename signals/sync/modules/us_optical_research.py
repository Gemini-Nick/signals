# -*- coding: utf-8 -*-
"""Bounded US optical-chain bar hydration and custom-signal research scan.

This module is intentionally not registered in the production sync engine.  It
is an explicit research job for a small, named universe and is safe to rerun:
bar writes replace only exact symbol/frequency/timestamp measurements and
signals are upserted by their existing dedupe keys.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

import pandas as pd
from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.cross_market_chains import load_cross_market_chains
from signals.core.global_market_universe import load_global_market_universe
from signals.core.market_time import naive_market_now
from signals.data.bar_quality import validate_ohlcv_bar
from signals.sync.modules.global_market_foundation import seed_global_market_foundation
from signals.sync.modules.index_daily import _replace_exact_bar_docs
from signals.sync.modules.technical_signal_scan import _scan_symbol

RESEARCH_UNIVERSE = "us_optical_chain"
CONTEXT_TICKERS = ("GLW", "AVGO", "MRVL")
DOWNLOAD_SPECS = (
    ("1d", "5y", "日线"),
    ("60m", "60d", "60分钟"),
    ("30m", "60d", "30分钟"),
    ("15m", "60d", "15分钟"),
    ("5m", "60d", "5分钟"),
)


def _us_representatives() -> dict[str, dict[str, Any]]:
    chain = load_cross_market_chains().get("us_ai_hardware") or {}
    output: dict[str, dict[str, Any]] = {}
    for node in chain.get("nodes") or []:
        for rep in node.get("us_representatives") or []:
            ticker = str(rep.get("symbol") or "").strip().upper()
            if ticker:
                output.setdefault(ticker, {**rep, "node_id": node.get("node_id")})
    return output


def optical_study_universe(*, include_context: bool = True) -> list[dict[str, Any]]:
    """Return the direct optical basket plus explicitly separated context names."""
    chain = load_cross_market_chains().get("us_ai_hardware") or {}
    optical = next(
        (node for node in chain.get("nodes") or [] if node.get("node_id") == "optical_interconnect"),
        {},
    )
    rows: list[dict[str, Any]] = []
    for rep in optical.get("us_representatives") or []:
        ticker = str(rep.get("symbol") or "").strip().upper()
        if ticker:
            rows.append({
                "ticker": ticker,
                "symbol": f"US.{ticker}",
                "name": rep.get("name") or ticker,
                "role": rep.get("role") or "optical_direct",
                "evidence_type": rep.get("evidence_type") or "",
                "basket_role": "direct_optical",
            })
    if include_context:
        reps = _us_representatives()
        for ticker in CONTEXT_TICKERS:
            rep = reps.get(ticker) or {}
            rows.append({
                "ticker": ticker,
                "symbol": f"US.{ticker}",
                "name": rep.get("name") or ticker,
                "role": rep.get("role") or "chain_context",
                "evidence_type": rep.get("evidence_type") or "",
                "basket_role": "context_only",
            })
    return list({row["symbol"]: row for row in rows}.values())


def _ticker_frame(downloaded: pd.DataFrame, ticker: str, ticker_count: int) -> pd.DataFrame:
    if downloaded is None or downloaded.empty:
        return pd.DataFrame()
    if not isinstance(downloaded.columns, pd.MultiIndex):
        return downloaded.copy() if ticker_count == 1 else pd.DataFrame()
    level_zero = set(map(str, downloaded.columns.get_level_values(0)))
    level_one = set(map(str, downloaded.columns.get_level_values(1)))
    if ticker in level_zero:
        return downloaded[ticker].copy()
    if ticker in level_one:
        return downloaded.xs(ticker, axis=1, level=1).copy()
    return pd.DataFrame()


def _market_naive_index(index: Any) -> pd.DatetimeIndex:
    parsed = pd.DatetimeIndex(pd.to_datetime(index, errors="coerce"))
    if parsed.tz is not None:
        parsed = parsed.tz_convert("America/New_York").tz_localize(None)
    return parsed


def _normalize_download_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    renamed = frame.rename(columns={
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "vol",
    }).copy()
    required = {"open", "high", "low", "close", "vol"}
    if not required.issubset(renamed.columns):
        return pd.DataFrame()
    renamed.index = _market_naive_index(renamed.index)
    renamed.index.name = "dt"
    for column in required:
        renamed[column] = pd.to_numeric(renamed[column], errors="coerce")
    renamed = renamed.dropna(subset=["open", "high", "low", "close", "vol"])
    renamed = renamed[~renamed.index.isna()]
    renamed["amount"] = renamed["close"] * renamed["vol"]
    return renamed[["open", "high", "low", "close", "vol", "amount"]]


def _frame_to_docs(frame: pd.DataFrame, *, symbol: str, freq: str, source: str = "yfinance") -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for dt_value, row in frame.iterrows():
        doc = {
            "dt": dt_value.to_pydatetime() if hasattr(dt_value, "to_pydatetime") else dt_value,
            "meta": {
                "symbol": symbol,
                "freq": freq,
                "market": "US",
                "asset_type": "stock",
                "source": source,
                "feed": "yahoo_consolidated",
                "adjustment": "auto_adjust",
                "research_universe": RESEARCH_UNIVERSE,
            },
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "vol": int(float(row["vol"])),
            "amount": float(row["amount"]),
        }
        accepted, _ = validate_ohlcv_bar(doc, allow_zero_volume=False)
        if accepted:
            docs.append(doc)
    return docs


def _weekly_from_daily(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    weekly = frame.resample("W-FRI").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "vol": "sum",
        "amount": "sum",
    })
    return weekly.dropna(subset=["open", "high", "low", "close"])


def _single_ticker_fallback(ticker: str, *, period: str, interval: str) -> pd.DataFrame:
    """Retry a symbol outside the batch response when Yahoo drops one member."""
    import yfinance as yf

    raw = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    return _normalize_download_frame(raw)


def hydrate_us_optical_bars(
    db: Database,
    *,
    universe: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Batch-download five years of daily and 60 days of minute bars."""
    import yfinance as yf

    rows = list(universe or optical_study_universe())
    tickers = [row["ticker"] for row in rows]
    symbols = {row["ticker"]: row["symbol"] for row in rows}
    counts: dict[str, dict[str, int]] = {symbol: {} for symbol in symbols.values()}
    latest: dict[str, dict[str, str]] = {symbol: {} for symbol in symbols.values()}
    errors: list[str] = []
    daily_frames: dict[str, pd.DataFrame] = {}

    for interval, period, freq in DOWNLOAD_SPECS:
        try:
            downloaded = yf.download(
                tickers,
                period=period,
                interval=interval,
                auto_adjust=True,
                progress=False,
                threads=True,
                group_by="ticker",
            )
        except Exception as exc:
            errors.append(f"{interval}:download:{type(exc).__name__}:{exc}")
            continue
        for ticker in tickers:
            symbol = symbols[ticker]
            frame = _normalize_download_frame(_ticker_frame(downloaded, ticker, len(tickers)))
            if frame.empty:
                try:
                    frame = _single_ticker_fallback(ticker, period=period, interval=interval)
                except Exception as exc:
                    errors.append(f"{symbol}:{interval}:fallback:{type(exc).__name__}:{exc}")
                if frame.empty:
                    errors.append(f"{symbol}:{interval}:empty")
                    counts[symbol][freq] = 0
                    continue
            if interval == "1d":
                daily_frames[ticker] = frame
            docs = _frame_to_docs(frame, symbol=symbol, freq=freq)
            counts[symbol][freq] = _replace_exact_bar_docs(db["bars"], docs)
            latest[symbol][freq] = frame.index.max().isoformat()

    for ticker, frame in daily_frames.items():
        symbol = symbols[ticker]
        weekly = _weekly_from_daily(frame)
        docs = _frame_to_docs(weekly, symbol=symbol, freq="周线", source="yfinance_daily_rollup")
        counts[symbol]["周线"] = _replace_exact_bar_docs(db["bars"], docs)
        if not weekly.empty:
            latest[symbol]["周线"] = weekly.index.max().isoformat()

    return {
        "status": "ok" if not errors else "partial",
        "provider": "yfinance",
        "universe": rows,
        "counts": counts,
        "latest": latest,
        "errors": errors,
    }


def scan_us_optical_signals(
    db: Database,
    *,
    universe: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run every existing custom detector across weekly/daily/60/30/15/5m."""
    rows = list(universe or optical_study_universe())
    now = naive_market_now("US")
    run_id = f"{RESEARCH_UNIVERSE}:{now.strftime('%Y%m%dT%H%M%S')}"
    signals: list[dict[str, Any]] = []
    errors: list[str] = []
    scanned: list[str] = []

    for row in rows:
        symbol = row["symbol"]
        try:
            docs = _scan_symbol(db, symbol, scan_scope=RESEARCH_UNIVERSE, include_60m=True)
        except Exception as exc:
            errors.append(f"{symbol}:{type(exc).__name__}:{exc}")
            continue
        latest_bar = db["bars"].find_one(
            {"meta.symbol": symbol}, {"dt": 1}, sort=[("dt", -1)]
        ) or {}
        data_as_of = latest_bar.get("dt")
        for doc in docs:
            doc["research_run_id"] = run_id
            doc["research_universe"] = RESEARCH_UNIVERSE
            doc["basket_role"] = row.get("basket_role")
            doc["company_name"] = row.get("name")
            doc["data_as_of"] = data_as_of
        signals.extend(docs)
        scanned.append(symbol)

    if signals:
        db["terminal_technical_signals"].bulk_write(
            [UpdateOne({"dedupe_key": doc["dedupe_key"]}, {"$set": doc}, upsert=True) for doc in signals],
            ordered=False,
        )

    by_symbol: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = row["symbol"]
        symbol_signals = [doc for doc in signals if doc.get("symbol") == symbol]
        by_symbol[symbol] = {
            "name": row.get("name"),
            "basket_role": row.get("basket_role"),
            "signal_count": len(symbol_signals),
            "direction": Counter(str(doc.get("direction") or "") for doc in symbol_signals).most_common(1)[0][0] if symbol_signals else "无触发",
            "signals": [
                {
                    "freq": doc.get("freq"),
                    "signal_type": doc.get("signal_type"),
                    "side": doc.get("signal_side"),
                    "dt": doc.get("dt"),
                    "price": doc.get("price"),
                    "score": doc.get("score"),
                    "total_score": doc.get("total_score"),
                    "resonance_grade": (doc.get("resonance_context") or {}).get("grade"),
                }
                for doc in sorted(symbol_signals, key=lambda item: (str(item.get("freq")), str(item.get("signal_type"))))
            ],
        }

    detector_scope = {
        "structural": ["一买/一卖", "二买/二卖", "三买/三卖", "背驰买/背驰卖", "趋势买/趋势卖"],
        "patterns": ["双顶/双底", "头肩顶/头肩底", "上升/下降三角"],
        "auxiliary": ["MACD绿柱极值", "缺口突破/持续/衰竭"],
        "entry_factors": ["200日新高突破", "拒绝回调相对强度"],
        "frequencies": ["周线", "日线", "60分钟", "30分钟", "15分钟", "5分钟"],
    }
    snapshot = {
        "_id": run_id,
        "run_id": run_id,
        "research_universe": RESEARCH_UNIVERSE,
        "market": "US",
        "provider": "yfinance",
        "as_of": now,
        "symbols": scanned,
        "detector_scope": detector_scope,
        "signal_count": len(signals),
        "by_symbol": by_symbol,
        "errors": errors,
    }
    db["us_optical_signal_runs"].replace_one({"_id": run_id}, snapshot, upsert=True)
    return {
        "status": "ok" if not errors else "partial",
        "run_id": run_id,
        "symbols": scanned,
        "signal_count": len(signals),
        "by_symbol": by_symbol,
        "detector_scope": detector_scope,
        "errors": errors,
    }


def run_us_optical_research(db: Database, *, include_context: bool = True) -> dict[str, Any]:
    universe = optical_study_universe(include_context=include_context)
    now = naive_market_now("US")
    identity_counts = seed_global_market_foundation(
        db,
        as_of=now.date().isoformat(),
        now=now,
        markets=["US"],
    )
    hydration = hydrate_us_optical_bars(db, universe=universe)
    scan = scan_us_optical_signals(db, universe=universe)
    latest_values = [
        value
        for symbol_freqs in hydration.get("latest", {}).values()
        for value in symbol_freqs.values()
        if value
    ]
    db["data_freshness"].update_one(
        {"domain": RESEARCH_UNIVERSE, "market": "US", "mode": "research", "collection": "bars"},
        {"$set": {
            "domain": RESEARCH_UNIVERSE,
            "market": "US",
            "mode": "research",
            "collection": "bars",
            "freshness": "fresh" if hydration.get("status") == "ok" else "partial",
            "provider": hydration.get("provider"),
            "latest_dt": max(latest_values) if latest_values else None,
            "updated_at": now,
            "symbols": [row["symbol"] for row in universe],
            "bar_counts": hydration.get("counts"),
            "signal_run_id": scan.get("run_id"),
            "signal_count": scan.get("signal_count"),
            "errors": [*hydration.get("errors", []), *scan.get("errors", [])][:20],
            "universe_version": load_global_market_universe()["version"],
        }, "$inc": {"manifest_revision": 1}},
        upsert=True,
    )
    return {
        "status": "ok" if hydration.get("status") == "ok" and scan.get("status") == "ok" else "partial",
        "identity_counts": identity_counts,
        "hydration": hydration,
        "scan": scan,
    }
