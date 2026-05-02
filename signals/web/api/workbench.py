from __future__ import annotations

import json
import os
from difflib import SequenceMatcher
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import config
from signals.core.market_time import (
    infer_market,
    market_timezone_name,
    market_today,
    naive_market_now,
    timestamp_range_to_dates,
    to_unix_seconds,
)
from signals.core.concept_carriers import non_chain_reason
from signals.core.macro_universe import (
    macro_index_themes,
    macro_watchlist,
    supports_a_index_minute_cache,
)
from signals.core.stock_names import get_resolver
from signals.data.gateway import get_index_bars, get_kline
from signals.data.models import DataRequest
from signals.core.trade_log import get_trade_log
from signals.services import backtest as backtest_service
from signals.services import cluster as cluster_service
from signals.strategy.snapshot import get_strategy_snapshot

from ..services.engine import get_engine
from ..services.serializers import (
    serialize_index_report,
    serialize_market_context,
    serialize_scored_symbol,
    serialize_signal_change,
)
from .industry import get_industry_detail
from .plan import _serialize_plan
from .stock import analyze_stock

router = APIRouter(prefix="/api/workbench", tags=["workbench"])

UI_FREQS = ["30min", "15min", "5min", "daily", "weekly"]
DEFAULT_TERMINAL_FREQ = "30min"
MINUTE_FREQS = {"5min", "5m", "15min", "15m", "30min", "30m"}
BUY_FREQS = ["daily", "30min", "15min", "5min"]
CHART_FREQ_ORDER = {"weekly": 0, "daily": 1, "30min": 2, "15min": 3, "5min": 4}
SECOND_SCREEN_LANES = {
    "quote_lane": {
        "label": "实时观察",
        "cadence": "15-60s",
        "purpose": "关键指数、当前标的和关注池轻量 quote。",
    },
    "signal_lane": {
        "label": "信号确认",
        "cadence": "5m close",
        "purpose": "5m/15m/30m/日/周闭合结构确认。",
    },
    "workbench_lane": {
        "label": "工作台重算",
        "cadence": "10m",
        "purpose": "主观察列表、候选池、风险预警和策略快照。",
    },
    "board_lane": {
        "label": "板块异动",
        "cadence": "20-30m",
        "purpose": "行业/概念排行、leader、产业链承接。",
    },
}
FREQ_ALIASES = {
    "5m": "5min",
    "5min": "5min",
    "5分钟": "5min",
    "15m": "15min",
    "15min": "15min",
    "15分钟": "15min",
    "30m": "30min",
    "30min": "30min",
    "30分钟": "30min",
    "daily": "daily",
    "日线": "daily",
    "weekly": "weekly",
    "周线": "weekly",
    "monthly": "monthly",
    "月线": "monthly",
}
GATEWAY_FREQS = {
    "5min": "5m",
    "15min": "15m",
    "30min": "30m",
    "daily": "daily",
    "weekly": "weekly",
    "monthly": "monthly",
}
MINGDAO_INDEX_THEMES = macro_index_themes()
MINGDAO_MACRO_WATCHLIST = macro_watchlist()

INDEX_NAME_ALIASES = {
    "上证综指": ("上证指数", "sh000001"),
    "上证指数": ("上证指数", "sh000001"),
    "上证综合指数": ("上证指数", "sh000001"),
    "沪指": ("上证指数", "sh000001"),
    "sh000001": ("上证指数", "sh000001"),
    "SH.000001": ("上证指数", "sh000001"),
    "000001.SH": ("上证指数", "sh000001"),
    "深证综指": ("深证成指", "sz399001"),
    "深成指": ("深证成指", "sz399001"),
    "sz399001": ("深证成指", "sz399001"),
    "SZ.399001": ("深证成指", "sz399001"),
    "创业板": ("创业板指", "sz399006"),
    "创业板指数": ("创业板指", "sz399006"),
    "sz399006": ("创业板指", "sz399006"),
    "SZ.399006": ("创业板指", "sz399006"),
}

for _item in MINGDAO_MACRO_WATCHLIST:
    if str(_item.get("kind") or "").strip() != "index":
        continue
    _name = str(_item.get("name") or "").strip()
    _symbol = str(_item.get("symbol") or "").strip()
    if not _name or not _symbol:
        continue
    INDEX_NAME_ALIASES.setdefault(_name, (_name, _symbol))
    INDEX_NAME_ALIASES.setdefault(_symbol.lower(), (_name, _symbol))
    INDEX_NAME_ALIASES.setdefault(_symbol.upper(), (_name, _symbol))

_SHELL_CACHE_TTL_SECONDS = 120.0
_SHELL_CACHE_LOCK = threading.Lock()
_SHELL_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None, "refreshed_at": 0.0}


def _invalidate_shell_cache() -> None:
    with _SHELL_CACHE_LOCK:
        _SHELL_CACHE.update({"expires_at": 0.0, "payload": None, "refreshed_at": 0.0})


def _shell_cache_usable(payload: Any, engine: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    if session and not session.get("ready") and engine.is_ready():
        return False
    return True
_CHART_LOAD_LOCK = threading.Lock()
_CHART_LOAD_JOBS: dict[str, dict[str, Any]] = {}
_CHART_LOAD_JOB_TTL_SECONDS = 120.0

BUY_SIGNAL_TOKENS = ("buy", "long", "entry", "候选", "买", "突破", "启动", "三买", "一买", "二买")
SELL_SIGNAL_TOKENS = ("sell", "short", "exit", "预警", "卖", "跌破", "止损", "风险")


def _canonical_freq(freq: str) -> str:
    return FREQ_ALIASES.get(str(freq or "daily").strip().lower(), str(freq or "daily").strip().lower() or "daily")


def _gateway_freq(freq: str) -> str:
    return GATEWAY_FREQS.get(_canonical_freq(freq), _canonical_freq(freq))


def _freq_label(freq: str) -> str:
    return {
        "5min": "5分钟",
        "15min": "15分钟",
        "30min": "30分钟",
        "daily": "日线",
        "weekly": "周线",
        "monthly": "月线",
    }.get(_canonical_freq(freq), str(freq or "daily"))


def _freq_badge(freq: str) -> str:
    return {
        "5min": "5m",
        "15min": "15m",
        "30min": "30m",
        "daily": "D",
        "weekly": "W",
        "monthly": "M",
    }.get(_canonical_freq(freq), str(freq or ""))


def _freq_bucket(freq: Any) -> str:
    value = str(freq or "").strip().lower()
    if value in {"5", "5m", "5min", "5分钟", "5分钟线"}:
        return "5min"
    if value in {"15", "15m", "15min", "15分钟", "15分钟线"}:
        return "15min"
    if value in {"30", "30m", "30min", "30分钟", "30分钟线"}:
        return "30min"
    if value in {"d", "day", "daily", "日", "日线", "1d"}:
        return "daily"
    if value in {"w", "week", "weekly", "周", "周线", "1w"}:
        return "weekly"
    return _canonical_freq(value or "daily")


def _market_now(market: str = "A") -> datetime:
    return naive_market_now(market)


def _market_today(market: str = "A") -> date:
    return market_today(market)


def _sync_now() -> datetime:
    """Naive Beijing timestamp for Mongo collections that still store local time."""
    return naive_market_now("A")


def _dt_to_unix(value: Any, *, market: Any = "", symbol: Any = "", source: Any = "") -> int:
    return to_unix_seconds(value, market=market, symbol=symbol, source=source)


def _signal_ts(value: Any, *, market: Any = "", symbol: Any = "", source: Any = "") -> int:
    numeric = _float(value)
    if numeric is not None and numeric > 0:
        return int(numeric / 1000) if numeric > 10_000_000_000 else int(numeric)
    return _dt_to_unix(value, market=market, symbol=symbol, source=source)


def _timestamp_date(ts: int, *, market: Any = "", symbol: Any = "", source: Any = "") -> str:
    start, _ = timestamp_range_to_dates(ts, ts, market=market, symbol=symbol, source=source)
    return start or ""


def _float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        parsed = float(value)
        if pd.isna(parsed):
            return default
        return parsed
    except Exception:
        return default


def _first_numeric(*values: Any) -> Optional[float]:
    for value in values:
        parsed = _float(value)
        if parsed is not None:
            return parsed
    return None


def _a_day_change_mode() -> str:
    now = _market_now("A")
    try:
        from signals.core.market_hours import get_session_mode
        session = get_session_mode()
    except Exception:
        session = None
    if bool(getattr(session, "a_live", False)):
        return "quote_intraday"
    if now.weekday() < 5 and (now.hour, now.minute) >= (9, 30) and (now.hour, now.minute) < (15, 0):
        return "quote_intraday"
    return "daily_close"


def _day_change_expected_day(mode: Optional[str] = None) -> str:
    resolved_mode = mode or _a_day_change_mode()
    if resolved_mode == "quote_intraday":
        return _market_today("A").isoformat()
    try:
        from signals.data.mongo_fallback import get_last_trading_day
        return str(get_last_trading_day("A"))[:10]
    except Exception:
        return _market_today("A").isoformat()


def _df_latest_date(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return ""
    try:
        return pd.to_datetime(df.sort_index().index.max()).date().isoformat()
    except Exception:
        return ""


def _daily_close_day_change_pct(df: pd.DataFrame) -> tuple[Optional[float], str, str]:
    expected_day = _day_change_expected_day("daily_close")
    latest_day = _df_latest_date(df)
    if expected_day and latest_day and latest_day != expected_day:
        return None, "", latest_day
    value = _compute_day_change_pct(df)
    return value, ("daily_bars_close" if value is not None else ""), latest_day


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _iso_dt(value: Any) -> str:
    if value is None:
        return ""
    try:
        return pd.to_datetime(value).isoformat()
    except Exception:
        return _text(value)


def _date_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(pd.to_datetime(value).date())
    except Exception:
        return _text(value)[:10]


def _normalize_chart_df(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    if df is None or df.empty:
        out = pd.DataFrame()
        if df is not None:
            out.attrs.update(getattr(df, "attrs", {}) or {})
        return out
    working = df.copy().sort_index()
    working.attrs.update(getattr(df, "attrs", {}) or {})
    if _canonical_freq(freq) == "weekly" and not working.empty:
        latest_idx = pd.to_datetime(working.index.max())
        today = _market_today("A")
        if latest_idx.date() > today:
            new_index = []
            for item in working.index:
                parsed = pd.to_datetime(item)
                new_index.append(pd.Timestamp(today) if parsed.date() > today else parsed)
            working.index = pd.DatetimeIndex(new_index)
            working.attrs["period_end"] = latest_idx.date().isoformat()
            working.attrs["data_as_of"] = today.isoformat()
            working.attrs["is_partial_period"] = True
            working.attrs["time_semantics"] = "period_data_as_of"
    return working


def _chart_cache_meta(df: pd.DataFrame, *, source: str, freq: str) -> dict[str, Any]:
    attrs = getattr(df, "attrs", {}) or {}
    latest_bar_time = _iso_dt(df.index.max()) if df is not None and not df.empty else ""
    data_as_of = _text(attrs.get("data_as_of")) or _text(attrs.get("as_of")) or _date_text(latest_bar_time)
    freshness = _text(attrs.get("gateway_freshness") or attrs.get("freshness"))
    is_stale = bool(attrs.get("gateway_is_stale") or attrs.get("is_stale") or freshness == "stale")
    if df is None or df.empty:
        cache_status = "empty"
    elif is_stale:
        cache_status = "stale"
    else:
        cache_status = "ready"
    return {
        "collection": _text(attrs.get("collection")) or source,
        "as_of": data_as_of,
        "data_as_of": data_as_of,
        "latest_bar_time": latest_bar_time,
        "period_end": _text(attrs.get("period_end")),
        "is_partial_period": bool(attrs.get("is_partial_period")),
        "cache_status": cache_status,
        "freshness": freshness or ("stale" if is_stale else ("fresh" if cache_status == "ready" else "empty")),
        "is_stale": is_stale,
        "stale_reason": _text(attrs.get("stale_reason")),
        "time_semantics": _text(attrs.get("time_semantics")) or ("period_data_as_of" if _canonical_freq(freq) == "weekly" and attrs.get("is_partial_period") else "bar_close_market_time"),
        "errors": list(attrs.get("gateway_errors") or []),
        "resampled_from_freq": _text(attrs.get("resampled_from_freq")),
        "resampled_to_freq": _text(attrs.get("resampled_to_freq")),
        "resample_source_latest_bar_time": _text(attrs.get("resample_source_latest_bar_time")),
        "direct_source": _text(attrs.get("direct_source")),
        "direct_latest_bar_time": _text(attrs.get("direct_latest_bar_time")),
    }


def _attach_gateway_meta(df: pd.DataFrame, response: Any, *, collection: str) -> pd.DataFrame:
    out = df if df is not None else pd.DataFrame()
    out.attrs["collection"] = collection or _text(getattr(response, "source", ""))
    out.attrs["gateway_as_of"] = getattr(response, "as_of", None)
    out.attrs["as_of"] = getattr(response, "as_of", None) or out.attrs.get("as_of")
    out.attrs["gateway_freshness"] = getattr(response, "freshness", "")
    out.attrs["gateway_is_stale"] = bool(getattr(response, "is_stale", False))
    out.attrs["gateway_errors"] = list(getattr(response, "errors", []) or [])
    if getattr(response, "is_stale", False):
        out.attrs["stale_reason"] = "older_than_request"
    return out


def _serialize_ohlcv_df(
    df: pd.DataFrame,
    *,
    limit: int = 720,
    market: Any = "",
    symbol: Any = "",
    source: Any = "",
) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    working = df.copy().sort_index()
    if limit > 0:
        working = working.tail(limit)
    rows: list[dict[str, Any]] = []
    for dt_idx, row in working.iterrows():
        close = _float(row.get("close"))
        if close is None:
            continue
        open_ = _float(row.get("open"), close)
        high = _float(row.get("high"), max(open_, close))
        low = _float(row.get("low"), min(open_, close))
        rows.append({
            "time": _dt_to_unix(dt_idx, market=market, symbol=symbol, source=source),
            "open": round(open_, 4),
            "high": round(high, 4),
            "low": round(low, 4),
            "close": round(close, 4),
            "volume": int(_float(row.get("vol") or row.get("volume"), 0) or 0),
            "amount": int(_float(row.get("amount") or row.get("turnover"), 0) or 0),
        })
    return rows


def _chart_from_df(df: pd.DataFrame, *, symbol: str, freq: str, source: str = "gateway") -> dict[str, Any]:
    market = infer_market(symbol=symbol, source=source)
    limit = 900 if _canonical_freq(freq) in {"5min", "15min", "30min"} else 720
    working = _normalize_chart_df(df, freq)
    cache_meta = _chart_cache_meta(working, source=source, freq=freq)
    return {
        "symbol": symbol,
        "freq": _freq_label(freq),
        "meta": {
            "freq": _canonical_freq(freq),
            "source": source,
            **cache_meta,
            "market": market,
            "market_timezone": market_timezone_name(market, symbol=symbol, source=source),
            "time_unit": "s",
            "bars": int(len(working)) if working is not None else 0,
        },
        "ohlcv": _serialize_ohlcv_df(working, limit=limit, market=market, symbol=symbol, source=source),
        "signals": [],
        "ma_lines": [],
    }


def _chart_has_ohlcv(chart: dict[str, Any]) -> bool:
    return bool(chart.get("ohlcv"))


def _board_heat_alias_candidates(kind: str, label: str) -> list[str]:
    raw = _text(label)
    candidates = [raw]
    if raw.endswith("概念"):
        candidates.append(raw[:-2])
    if "和其他" in raw:
        candidates.append(raw.split("和其他", 1)[0])
    for separator in (" · ", "·", "-", "/", "／"):
        if separator in raw:
            candidates.extend(part.strip() for part in raw.split(separator))
    output: list[str] = []
    for item in candidates:
        normalized = _text(item)
        if normalized and normalized not in output:
            output.append(normalized)
    return output


def _choose_board_heat_name(kind: str, label: str, names: list[str]) -> tuple[str, str]:
    candidates = _board_heat_alias_candidates(kind, label)
    name_set = {name for name in names if name}
    for candidate in candidates:
        if candidate in name_set:
            return candidate, "exact" if candidate == label else "alias"

    best_name = ""
    best_score = 0.0
    for name in name_set:
        for candidate in candidates:
            if name in candidate or candidate in name:
                score = 2.0 + min(len(name), len(candidate)) / max(len(name), len(candidate), 1)
            else:
                score = SequenceMatcher(None, candidate, name).ratio()
            if score > best_score:
                best_name = name
                best_score = score
    if best_name and best_score >= 0.62:
        return best_name, "fuzzy"
    return _text(label), "unresolved"


def resolve_board_heat_name(kind: str, label: str) -> dict[str, str]:
    query = _text(label)
    if not query:
        return {"query": "", "heat_name": "", "status": "missing_query"}
    try:
        db = _mongo_db()
        latest = db["board_heat_ticks"].find_one({"kind": kind}, sort=[("trade_minute", -1)])
        scope = {"kind": kind}
        if latest and latest.get("trade_minute") is not None:
            scope["trade_minute"] = latest.get("trade_minute")
        names = [
            _text(doc.get("name"))
            for doc in db["board_heat_ticks"].find(scope, {"_id": 0, "name": 1})
        ]
    except Exception:
        return {"query": query, "heat_name": query, "status": "mongo_unavailable"}

    heat_name, status = _choose_board_heat_name(kind, query, names)
    return {"query": query, "heat_name": heat_name or query, "status": status}


def _target_time_fields(*, market: str = "", symbol: str = "", source: str = "") -> dict[str, str]:
    resolved_market = infer_market(market, symbol=symbol, source=source)
    return {
        "market": resolved_market,
        "market_timezone": market_timezone_name(resolved_market, symbol=symbol, source=source),
    }


def _fallback_chart_when_empty(
    chart: dict[str, Any],
    *,
    symbol: str,
    requested_freq: str,
    loader,
) -> dict[str, Any]:
    """Legacy helper kept for older callers; trading terminal paths do not use it."""
    if requested_freq not in MINUTE_FREQS or _chart_has_ohlcv(chart):
        return chart
    fallback_df, fallback_source = loader("daily")
    fallback = _chart_from_df(
        fallback_df,
        symbol=symbol,
        freq="daily",
        source=f"{fallback_source};fallback_from={requested_freq}",
    )
    fallback["meta"] = {
        **fallback.get("meta", {}),
        "requested_freq": requested_freq,
        "fallback_reason": "empty_minute_ohlcv",
    }
    return fallback if _chart_has_ohlcv(fallback) else chart


def _not_ready_reason(kind: str, requested_freq: str, chart: dict[str, Any]) -> str:
    if _chart_has_ohlcv(chart):
        return ""
    canonical = _canonical_freq(requested_freq)
    if canonical in {"5min", "15min", "30min"}:
        if kind == "index":
            if not supports_a_index_minute_cache(chart.get("symbol")):
                return "index_minute_unsupported"
            return "index_minute_not_ready"
        if kind == "stock":
            return "stock_minute_not_ready"
        if kind in {"industry", "concept"}:
            return "board_heat_not_ready"
        return "minute_cache_stale"
    if canonical == "daily":
        return "daily_cache_missing"
    if canonical == "weekly":
        return "weekly_cache_missing"
    return "cache_missing"


def _mark_chart_readiness(chart: dict[str, Any], *, kind: str, requested_freq: str) -> dict[str, Any]:
    meta = dict(chart.get("meta") or {})
    meta["requested_freq"] = _canonical_freq(requested_freq)
    meta["effective_freq"] = meta.get("freq") or _canonical_freq(requested_freq)
    meta["fallback_reason"] = ""
    reason = _not_ready_reason(kind, requested_freq, chart)
    if reason:
        meta["cache_status"] = "not_ready"
        meta["not_ready_reason"] = reason
    else:
        meta["cache_status"] = meta.get("cache_status") if meta.get("cache_status") in {"stale", "ready"} else "ready"
        meta["not_ready_reason"] = ""
    chart["meta"] = meta
    return chart


def _resample_weekly(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    daily = df.sort_index().copy()
    daily["_source_dt"] = daily.index
    weekly = daily.resample("W-FRI").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "vol": "sum",
        "amount": "sum",
        "_source_dt": "last",
    })
    weekly = weekly.dropna(subset=["open", "high", "low", "close"], how="any")
    if not weekly.empty:
        new_index = []
        latest_period_end = ""
        latest_data_as_of = ""
        latest_partial = False
        for dt_idx, row in weekly.iterrows():
            period_end = pd.to_datetime(dt_idx)
            data_as_of = pd.to_datetime(row.get("_source_dt") or dt_idx)
            partial = data_as_of.date() < period_end.date()
            new_index.append(pd.Timestamp(data_as_of.date()) if partial else period_end)
            latest_period_end = period_end.date().isoformat()
            latest_data_as_of = data_as_of.date().isoformat()
            latest_partial = partial
        weekly.index = pd.DatetimeIndex(new_index)
        weekly = weekly.drop(columns=["_source_dt"], errors="ignore")
        weekly.attrs["period_end"] = latest_period_end
        weekly.attrs["data_as_of"] = latest_data_as_of
        weekly.attrs["is_partial_period"] = latest_partial
        weekly.attrs["time_semantics"] = "period_data_as_of" if latest_partial else "period_end"
    weekly.attrs["data_source"] = "daily_resampled_weekly"
    if not weekly.empty:
        weekly.attrs["as_of"] = str(weekly.attrs.get("data_as_of") or weekly.index.max().date())
    return weekly


def _df_latest_timestamp(df: pd.DataFrame) -> Optional[pd.Timestamp]:
    if df is None or df.empty:
        return None
    try:
        latest = pd.to_datetime(df.index.max(), errors="coerce")
    except Exception:
        return None
    if pd.isna(latest):
        return None
    return pd.Timestamp(latest)


def _a_share_bucket_close(value: Any, interval_minutes: int) -> pd.Timestamp | pd.NaT:
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except Exception:
        return pd.NaT
    if pd.isna(ts):
        return pd.NaT
    minute_of_day = int(ts.hour) * 60 + int(ts.minute)
    sessions = (
        (9 * 60 + 30, 11 * 60 + 30),
        (13 * 60, 15 * 60),
    )
    for start_minute, end_minute in sessions:
        if minute_of_day <= start_minute or minute_of_day > end_minute:
            continue
        offset = minute_of_day - start_minute
        bucket_offset = ((offset + interval_minutes - 1) // interval_minutes) * interval_minutes
        bucket_offset = max(bucket_offset, interval_minutes)
        bucket_minute = min(start_minute + bucket_offset, end_minute)
        return pd.Timestamp(ts.date()) + pd.Timedelta(minutes=bucket_minute)
    return pd.NaT


def _resample_stock_intraday_from_5min(df: pd.DataFrame, target_freq: str) -> pd.DataFrame:
    canonical = _canonical_freq(target_freq)
    interval = {"15min": 15, "30min": 30}.get(canonical)
    if interval is None or df is None or df.empty:
        return pd.DataFrame()
    working = df.copy()
    working["_source_dt"] = pd.to_datetime(working.index, errors="coerce")
    working = working.dropna(subset=["_source_dt"]).sort_values("_source_dt")
    if working.empty or "close" not in working.columns:
        return pd.DataFrame()
    for col in ("open", "high", "low"):
        if col not in working.columns:
            working[col] = working["close"]
    for col in ("vol", "amount"):
        if col not in working.columns:
            working[col] = 0
    for col in ("open", "high", "low", "close", "vol", "amount"):
        working[col] = pd.to_numeric(working[col], errors="coerce")
    working["open"] = working["open"].fillna(working["close"])
    working["high"] = working["high"].fillna(working["close"])
    working["low"] = working["low"].fillna(working["close"])
    working = working.dropna(subset=["open", "high", "low", "close"])
    if working.empty:
        return pd.DataFrame()

    working["_bucket_dt"] = working["_source_dt"].map(lambda value: _a_share_bucket_close(value, interval))
    working = working.dropna(subset=["_bucket_dt"])
    if working.empty:
        return pd.DataFrame()

    resampled = working.groupby("_bucket_dt", sort=True).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "vol": "sum",
        "amount": "sum",
    })
    resampled.index = pd.DatetimeIndex(pd.to_datetime(resampled.index, errors="coerce"))
    resampled = resampled[~resampled.index.isna()].sort_index()
    resampled = resampled.dropna(subset=["open", "high", "low", "close"], how="any")
    if resampled.empty:
        return pd.DataFrame()

    source_attrs = getattr(df, "attrs", {}) or {}
    resampled.attrs.update(source_attrs)
    latest_source_dt = pd.Timestamp(working["_source_dt"].max())
    latest_bucket_dt = pd.Timestamp(resampled.index.max())
    resampled.attrs["data_source"] = _text(source_attrs.get("data_source")) or "5min_resampled_intraday"
    resampled.attrs["collection"] = _text(source_attrs.get("collection")) or "bars"
    resampled.attrs["as_of"] = latest_source_dt.date().isoformat()
    resampled.attrs["data_as_of"] = latest_source_dt.date().isoformat()
    resampled.attrs["latest_bar_time"] = latest_bucket_dt.isoformat()
    resampled.attrs["time_semantics"] = "bar_close_market_time"
    resampled.attrs["is_partial_period"] = bool(latest_source_dt < latest_bucket_dt)
    resampled.attrs["resampled_from_freq"] = "5min"
    resampled.attrs["resampled_to_freq"] = canonical
    resampled.attrs["resample_source_latest_bar_time"] = latest_source_dt.isoformat()
    return resampled


def _stock_kline_df(symbol: str, canonical: str) -> tuple[pd.DataFrame, str, Any]:
    response = get_kline(DataRequest(
        domain="kline",
        mode="historical",
        market="A",
        symbol=symbol,
        freq=_gateway_freq(canonical),
        purpose="review",
        allow_stale=True,
    ))
    df = response.data if response.data is not None else pd.DataFrame()
    df = _attach_gateway_meta(df, response, collection=response.source)
    return df, response.source, response


def _stock_df(symbol: str, freq: str) -> tuple[pd.DataFrame, str]:
    canonical = _canonical_freq(freq)
    df, source, _ = _stock_kline_df(symbol, canonical)
    if canonical == "weekly" and (df is None or df.empty):
        daily_df, daily_source, _ = _stock_kline_df(symbol, "daily")
        weekly = _resample_weekly(daily_df)
        if weekly is not None and not weekly.empty:
            weekly.attrs["resampled_from_freq"] = "daily"
            weekly.attrs["resampled_to_freq"] = "weekly"
            return weekly, f"{daily_source};resampled_from=daily;resampled_to=weekly"
    if canonical in {"15min", "30min"}:
        five_df, five_source, _ = _stock_kline_df(symbol, "5min")
        resampled = _resample_stock_intraday_from_5min(five_df, canonical)
        direct_latest = _df_latest_timestamp(df)
        resampled_latest = _df_latest_timestamp(resampled)
        if resampled_latest is not None and (direct_latest is None or resampled_latest > direct_latest):
            resampled.attrs["direct_source"] = source
            resampled.attrs["direct_latest_bar_time"] = _iso_dt(direct_latest)
            return resampled, f"{five_source};resampled_from=5min;resampled_to={canonical}"
    return df, source


def _index_df(symbol: str, freq: str) -> tuple[pd.DataFrame, str]:
    response = get_index_bars(DataRequest(
        domain="index",
        mode="historical",
        market="A",
        symbol=symbol,
        freq=_gateway_freq(freq),
        purpose="review",
        allow_stale=True,
    ))
    df = response.data if response.data is not None else pd.DataFrame()
    df = _attach_gateway_meta(df, response, collection=response.source)
    if df is not None and not df.empty:
        return df, response.source
    return df, response.source


def _probe_symbol_candidates(symbol: str, *, kind: str = "stock") -> list[str]:
    raw = _text(symbol)
    if not raw:
        return []
    candidates = [raw]
    lower = raw.lower()
    upper = raw.upper()
    for value in (lower, upper):
        if value not in candidates:
            candidates.append(value)
    if kind == "index":
        compact = lower.replace(".", "")
        if compact.startswith(("sh", "sz")) and len(compact) == 8:
            market = compact[:2].upper()
            code = compact[2:]
            for value in (compact, f"{market}.{code}", f"{code}.{market}"):
                if value not in candidates:
                    candidates.append(value)
    else:
        normalized, raw_code = _normalize_stock_symbol(raw)
        for value in (normalized, raw_code):
            if value and value not in candidates:
                candidates.append(value)
    return candidates


def _cache_probe(symbol: str, *, kind: str, requested_freq: str) -> dict[str, Any]:
    freq_labels = {
        "daily": "日线",
        "weekly": "周线",
        "monthly": "月线",
        "5min": "5分钟",
        "15min": "15分钟",
        "30min": "30分钟",
    }
    freqs = ["日线", "周线", "5分钟", "15分钟", "30分钟"]
    requested_label = freq_labels.get(_canonical_freq(requested_freq), _gateway_freq(requested_freq))
    if requested_label not in freqs:
        freqs.insert(0, requested_label)
    candidates = _probe_symbol_candidates(symbol, kind=kind)
    if kind == "index" and _canonical_freq(requested_freq) in {"5min", "15min", "30min"} and not supports_a_index_minute_cache(symbol):
        return {
            "status": "unsupported",
            "kind": kind,
            "requested_freq": _canonical_freq(requested_freq),
            "requested_freq_label": requested_label,
            "symbol_candidates": candidates,
            "reason": "index_minute_cache_not_connected_for_market",
            "rows": [],
        }
    collections = ["index_bars", "bars"] if kind == "index" else ["bars"]
    rows: list[dict[str, Any]] = []
    try:
        db = _mongo_db()
        for collection in collections:
            for candidate in candidates:
                for freq in freqs:
                    query = {"meta.symbol": candidate, "meta.freq": freq}
                    count = db[collection].count_documents(query)
                    if not count:
                        continue
                    latest = db[collection].find_one(
                        query,
                        {"dt": 1, "meta": 1, "close": 1},
                        sort=[("dt", -1)],
                    ) or {}
                    meta = latest.get("meta") or {}
                    latest_dt = _serialize_dt(latest.get("dt"))
                    data_as_of = _text(meta.get("data_as_of")) or latest_dt[:10]
                    period_end = _text(meta.get("period_end"))
                    is_partial_period = bool(meta.get("is_partial_period"))
                    if freq == "周线" and latest.get("dt") is not None:
                        try:
                            dt_value = pd.to_datetime(latest.get("dt")).date()
                            if dt_value > _market_today("A"):
                                period_end = period_end or dt_value.isoformat()
                                data_as_of = _market_today("A").isoformat()
                                is_partial_period = True
                                latest_dt = data_as_of
                        except Exception:
                            pass
                    rows.append({
                        "collection": collection,
                        "symbol": candidate,
                        "freq": freq,
                        "count": int(count),
                        "latest_dt": latest_dt,
                        "data_as_of": data_as_of,
                        "period_end": period_end,
                        "is_partial_period": is_partial_period,
                        "source": meta.get("source", ""),
                        "close": latest.get("close"),
                    })
        return {
            "status": "hit" if rows else "miss",
            "kind": kind,
            "requested_freq": _canonical_freq(requested_freq),
            "requested_freq_label": requested_label,
            "symbol_candidates": candidates,
            "rows": rows[:24],
        }
    except Exception as exc:
        return {
            "status": "unavailable",
            "kind": kind,
            "requested_freq": _canonical_freq(requested_freq),
            "symbol_candidates": candidates,
            "error": exc.__class__.__name__,
        }


def _target_diagnostics(kind: str, symbol: str, requested_freq: str, *, probe_symbol: str = "") -> dict[str, Any]:
    actual_symbol = probe_symbol or symbol
    return {
        "requested_symbol_candidates": _probe_symbol_candidates(actual_symbol, kind="index" if kind == "index" else "stock"),
        "cache_probe": _cache_probe(
            actual_symbol,
            kind="index" if kind == "index" else "stock",
            requested_freq=requested_freq,
        ),
    }


def _board_heat_df(name: str, kind: str, freq: str) -> tuple[pd.DataFrame, str, dict[str, Any], dict[str, str]]:
    canonical = _canonical_freq(freq)
    resolution = resolve_board_heat_name(kind, name)
    heat_name = resolution.get("heat_name") or name
    if canonical not in {"5min", "15min", "30min"}:
        return pd.DataFrame(), "board_heat_ticks", {}, resolution
    bucket = {"5min": "5min", "15min": "15min", "30min": "30min"}[canonical]
    try:
        db = _mongo_db()
        docs = list(db["board_heat_ticks"].find(
            {"kind": kind, "name": heat_name},
            {"_id": 0},
        ).sort("trade_minute", 1))
    except Exception:
        return pd.DataFrame(), "board_heat_ticks", {}, resolution
    if not docs:
        return pd.DataFrame(), "board_heat_ticks", {}, resolution
    df = pd.DataFrame(docs)
    df["trade_minute"] = pd.to_datetime(df["trade_minute"], errors="coerce")
    df = df.dropna(subset=["trade_minute"]).sort_values("trade_minute").set_index("trade_minute")
    if df.empty or "change_pct" not in df.columns:
        return pd.DataFrame(), "board_heat_ticks", {}, resolution
    df["change_pct"] = pd.to_numeric(df["change_pct"], errors="coerce")
    if "market_value" not in df.columns:
        df["market_value"] = 0
    df["market_value"] = pd.to_numeric(df["market_value"], errors="coerce").fillna(0)
    grouped = df.resample(bucket).agg({
        "change_pct": ["first", "max", "min", "last"],
        "market_value": "last",
    }).dropna(subset=[("change_pct", "last")])
    if grouped.empty:
        return pd.DataFrame(), "board_heat_ticks", {}, resolution
    out = pd.DataFrame({
        "open": grouped[("change_pct", "first")],
        "high": grouped[("change_pct", "max")],
        "low": grouped[("change_pct", "min")],
        "close": grouped[("change_pct", "last")],
        "vol": grouped[("market_value", "last")].fillna(0),
        "amount": grouped[("market_value", "last")].fillna(0),
    })
    out.attrs["data_source"] = "board_heat_ticks"
    out.attrs["collection"] = "board_heat_ticks"
    out.attrs["as_of"] = str(out.index.max().date())
    out.attrs["data_as_of"] = str(out.index.max().date())
    out.attrs["time_semantics"] = "bar_close_market_time"
    latest = docs[-1] if docs else {}
    latest = {**latest, "heat_target_label": heat_name, "heat_resolution_status": resolution.get("status", "")}
    return out, "board_heat_ticks", latest, resolution


def _latest_board_heat_day_change(kind: str, name: str) -> tuple[Optional[float], str]:
    if not kind or not name:
        return None, ""
    try:
        doc = _mongo_db()["board_heat_ticks"].find_one(
            {"kind": kind, "name": name},
            {"_id": 0, "change_pct": 1, "trade_minute": 1},
            sort=[("trade_minute", -1)],
        ) or {}
    except Exception:
        doc = {}
    as_of = _date_text(doc.get("trade_minute"))
    if as_of != _day_change_expected_day():
        return None, as_of
    return _float(doc.get("change_pct")), as_of


def _board_heat_chart(name: str, kind: str, freq: str) -> tuple[dict[str, Any], dict[str, Any]]:
    df, source, latest, resolution = _board_heat_df(name, kind, freq)
    heat_name = resolution.get("heat_name") or name
    latest = {
        "heat_target_label": heat_name,
        "heat_resolution_status": resolution.get("status", ""),
        **latest,
    }
    chart = _chart_from_df(df, symbol=heat_name, freq=freq, source=source)
    chart["meta"] = {
        **chart.get("meta", {}),
        "kind": kind,
        "chart_type": "heat_ohlc",
        "display_name": "热度K线/涨跌幅OHLC",
        "is_price_kline": False,
        "value_axis": "change_pct",
        "axis_label": "涨跌幅/热度",
        "price_label": "heat_close",
        "chart_mode": "board_heat",
        "non_price_notice": "非价格K线；OHLC 来自板块 change_pct 重采样。",
        "chart_source": "board_heat_ticks",
        "collection": "board_heat_ticks",
        "ohlc_formula": {
            "open": "change_pct:first",
            "high": "change_pct:max",
            "low": "change_pct:min",
            "close": "change_pct:last",
            "volume": "market_value:last",
            "amount": "market_value:last",
        },
        "lineage": [
            "Eastmoney push2delay",
            "board_heat_ticks.change_pct",
            "resample_to_ohlc",
            "chart",
        ],
        "candidate_stocks_role": "representatives_only_not_price_source",
        "query_label": name,
        "heat_target_label": heat_name,
        "heat_resolution_status": resolution.get("status", ""),
    }
    return _mark_chart_readiness(chart, kind=kind, requested_freq=freq), latest


def _preset_start_date(info: dict[str, Any], today: date) -> Optional[date]:
    if "date" in info:
        try:
            return datetime.strptime(str(info["date"]), "%Y-%m-%d").date()
        except ValueError:
            return None
    offset = info.get("offset")
    if offset == "ytd":
        return date(today.year, 1, 1)
    if isinstance(offset, int):
        return today - timedelta(days=offset)
    return None


def _watchlist_range_columns(today: Optional[date] = None) -> list[dict[str, Any]]:
    today = today or _market_today("A")
    columns: list[dict[str, Any]] = []
    relative: list[tuple[int, date, str, dict[str, Any]]] = []
    absolute: list[tuple[date, str, dict[str, Any]]] = []
    for key, info in config.DATE_PRESETS.items():
        if not isinstance(info, dict):
            continue
        start = _preset_start_date(info, today)
        if not start or start > today:
            continue
        if "date" in info:
            absolute.append((start, key, info))
        else:
            rank = {"ytd": 0, "1w": 1, "1m": 2, "3m": 3}.get(key, 9)
            relative.append((rank, start, key, info))

    relative.sort(key=lambda item: item[0])
    for _, start, key, info in relative:
        base_label = str(info.get("label") or key)
        if key == "ytd":
            label = f"{today.year}今年以来"
        else:
            label = f"{base_label}({start.strftime('%Y-%m-%d')})"
        columns.append({
            "key": key,
            "label": label,
            "start_date": start.isoformat(),
            "aliases": [key, base_label, label, start.isoformat()],
            "tier": info.get("tier", "relative"),
        })

    absolute.sort(key=lambda item: item[0], reverse=True)

    for start, key, info in absolute:
        event_label = str(info.get("label") or key)
        event_title = event_label.split("—", 1)[0].strip()
        label = f"{start.strftime('%Y-%m-%d')} {event_title}"
        columns.append({
            "key": key,
            "label": label,
            "start_date": start.isoformat(),
            "aliases": [key, start.strftime("%m%d"), start.isoformat(), event_label, event_title, label],
            "tier": info.get("tier", "event"),
        })
    return columns


def _compute_range_returns(df: pd.DataFrame, columns: list[dict[str, Any]]) -> dict[str, Optional[float]]:
    if df is None or df.empty or "close" not in df.columns:
        return {}
    working = df.copy().sort_index()
    closes = pd.to_numeric(working["close"], errors="coerce").dropna()
    if closes.empty:
        return {}
    latest = float(closes.iloc[-1])
    result: dict[str, Optional[float]] = {}
    for column in columns:
        key = str(column.get("key") or "")
        start_date = str(column.get("start_date") or "")
        if not key or not start_date:
            continue
        mask = closes.index >= pd.Timestamp(start_date)
        if not mask.any():
            result[key] = None
            continue
        start_price = float(closes.loc[mask].iloc[0])
        if start_price <= 0:
            result[key] = None
            continue
        result[key] = round((latest - start_price) / start_price * 100, 2)
    return result


def _compute_day_change_pct(df: pd.DataFrame) -> Optional[float]:
    if df is None or df.empty or "close" not in df.columns:
        return None
    closes = pd.to_numeric(df.sort_index()["close"], errors="coerce").dropna()
    if len(closes) < 2:
        return None
    previous = float(closes.iloc[-2])
    latest = float(closes.iloc[-1])
    if previous <= 0:
        return None
    return round((latest - previous) / previous * 100, 2)


def _ma_signal_from_df(df: pd.DataFrame) -> str:
    if df is None or df.empty or "close" not in df.columns:
        return "数据待预热"
    closes = pd.to_numeric(df.sort_index()["close"], errors="coerce").dropna()
    if len(closes) < 22:
        return "数据待预热"
    latest = float(closes.iloc[-1])
    ma5 = float(closes.tail(5).mean())
    ma10 = float(closes.tail(10).mean())
    ma20 = float(closes.tail(20).mean())
    prev_ma20 = float(closes.iloc[-21:-1].tail(20).mean())
    if latest >= ma5 >= ma10 >= ma20 and ma20 >= prev_ma20:
        return "多头上行"
    if latest < ma5 and latest < ma10:
        return "跌破短均"
    if latest >= ma20 and ma20 >= prev_ma20:
        return "站上20日线"
    if abs(latest - ma20) / ma20 <= 0.015:
        return "贴近20日线"
    if ma20 < prev_ma20:
        return "20日线下行"
    return "震荡观察"


def _signal_or_fallback(row: dict[str, Any], df: pd.DataFrame) -> str:
    for key in ("daily_latest_signal", "latest_signal", "signal"):
        value = _text(row.get(key))
        if value and value.lower() not in {"none", "n/a"} and value != "无":
            return value
    f30 = _text(row.get("f30_latest_signal"))
    f15 = _text(row.get("f15_latest_signal"))
    minute_signals = [value for value in (f30, f15) if value and value != "无"]
    if minute_signals:
        return "/".join(minute_signals[:2])
    return _ma_signal_from_df(df)


def _unwrap_response(value: Any) -> Any:
    if isinstance(value, JSONResponse):
        return json.loads(value.body.decode("utf-8"))
    return value


def _ensure_engine():
    engine = get_engine()
    if (
        os.environ.get("SIGNALS_WEB_AUTOSTART_ENGINE", "false").lower() == "true"
        and not engine.is_ready()
        and not engine.state.is_running
    ):
        engine.run_all_async()
    return engine


def _serialize_session(status: Dict[str, Any]) -> Dict[str, Any]:
    active_markets = status.get("active_markets", [])
    primary_market = active_markets[0] if isinstance(active_markets, list) and active_markets else "A"
    return {
        "ready": status.get("ready", False),
        "running": status.get("running", False),
        "loading_phase": status.get("loading_phase", ""),
        "label": status.get("session_label", ""),
        "mode": status.get("session_mode", ""),
        "a_live": status.get("a_live", False),
        "hk_live": status.get("hk_live", False),
        "us_live": status.get("us_live", False),
        "active_markets": active_markets,
        "market_timezone": market_timezone_name(primary_market),
        "refresh_interval": status.get("refresh_interval", 0),
        "next_check_seconds": status.get("next_check_seconds", 0),
        "next_refresh_at": status.get("next_refresh_at", ""),
        "data_as_of": status.get("data_as_of", ""),
        "error": status.get("error", ""),
    }


def _looks_like_stock(raw: str) -> bool:
    value = raw.strip().upper()
    if not value:
        return False
    if value.startswith(("SH.", "SZ.", "BJ.", "HK.")):
        return True
    return value.isdigit() and len(value) in (5, 6)


def _normalize_stock_symbol(raw: str) -> Tuple[Optional[str], Optional[str]]:
    resolver = get_resolver()
    value = raw.strip().upper()
    if not value:
        return None, None

    if value.startswith(("SH.", "SZ.", "BJ.", "HK.")):
        return value, value.split(".", 1)[1]

    if value.isdigit():
        if len(value) == 5:
            return f"HK.{value}", value
        if len(value) == 6:
            if value.startswith(("5", "6", "9")):
                return f"SH.{value}", value
            if value.startswith(("0", "1", "2", "3")):
                return f"SZ.{value}", value
            if value.startswith(("8", "4")):
                return f"BJ.{value}", value

    code = resolver.get_code(raw.strip())
    if code:
        return code, code.split(".", 1)[1]

    matches = resolver.search(raw.strip())
    if len(matches) == 1:
        code = matches[0][0]
        return code, code.split(".", 1)[1]

    return None, None


def _resolve_target(raw: str, kind: str, engine) -> Dict[str, str]:
    value = raw.strip()
    if not value:
        reports = engine.get_index_reports()
        default_name = reports[0].name if reports else "沪深300"
        return {"kind": "index", "label": default_name}

    forced_kind = kind.lower()
    if value.startswith("industry:"):
        return {"kind": "industry", "label": value.split(":", 1)[1].strip()}
    if value.startswith("concept:"):
        return {"kind": "concept", "label": value.split(":", 1)[1].strip()}

    if forced_kind == "stock":
        symbol, raw_code = _normalize_stock_symbol(value)
        if not symbol:
            raise HTTPException(status_code=404, detail=f"无法识别股票: {value}")
        return {"kind": "stock", "label": symbol, "raw_code": raw_code}

    if forced_kind == "industry":
        return {"kind": "industry", "label": value}

    if forced_kind == "concept":
        return {"kind": "concept", "label": value}

    if forced_kind == "index":
        static_index = _resolve_static_index(value)
        if static_index is not None:
            return {"kind": "index", "label": static_index[0]}
        return {"kind": "index", "label": value}

    reports = engine.get_index_reports()
    for report in reports:
        if value == report.name or value.lower() == report.symbol.lower():
            return {"kind": "index", "label": report.name}

    if _looks_like_stock(value):
        symbol, raw_code = _normalize_stock_symbol(value)
        if symbol:
            return {"kind": "stock", "label": symbol, "raw_code": raw_code}

    ranking = engine.get_industry_ranking_by_name(value)
    if ranking:
        return {"kind": "industry", "label": ranking.name}

    resolved = engine.resolve_sector(value)
    industries = resolved.get("matched_industries") or []
    if len(industries) == 1:
        return {"kind": "industry", "label": industries[0]}
    concepts = resolved.get("matched_concepts") or []
    if len(concepts) == 1:
        concept = concepts[0]
        if isinstance(concept, dict):
            return {"kind": "concept", "label": str(concept.get("name") or concept.get("label") or value)}
        return {"kind": "concept", "label": str(concept)}

    symbol, raw_code = _normalize_stock_symbol(value)
    if symbol:
        return {"kind": "stock", "label": symbol, "raw_code": raw_code}

    raise HTTPException(status_code=404, detail=f"无法识别目标: {value}")


def _resolve_static_index(raw: str) -> Optional[tuple[str, str]]:
    import config

    value = str(raw or "").strip()
    if not value:
        value = "沪深300"
    alias = INDEX_NAME_ALIASES.get(value) or INDEX_NAME_ALIASES.get(value.lower())
    if alias is not None:
        return alias
    value_lower = value.lower()
    value_digits = value_lower.replace(".", "").replace("sh", "").replace("sz", "")
    entries: list[tuple[str, str]] = []
    for item in MINGDAO_MACRO_WATCHLIST:
        if _text(item.get("kind")) == "index":
            entries.append((_text(item.get("name")), _text(item.get("symbol"))))
    entries.extend((name, symbol) for name, symbol in config.INDEX_AK_CODES.items())
    entries.extend((name, symbol) for name, symbol in getattr(config, "INDEX_FUTU_CODES", {}).items())
    entries.extend((name, symbol) for name, symbol in getattr(config, "INDEX_US_CODES", {}).items())
    seen: set[tuple[str, str]] = set()
    for name, symbol in entries:
        if not name or not symbol:
            continue
        key = (name, symbol.lower())
        if key in seen:
            continue
        seen.add(key)
        symbol_lower = symbol.lower()
        compact_symbol = symbol_lower.replace(".", "")
        dot_symbol = f"{symbol_lower[:2]}.{symbol_lower[2:]}" if len(symbol_lower) >= 8 else symbol_lower
        if (
            value == name
            or value_lower == symbol_lower
            or value_lower == compact_symbol
            or value_lower == dot_symbol
            or value_digits == compact_symbol.replace("sh", "").replace("sz", "")
        ):
            return name, symbol
    return None


def _top_candidate_symbol(engine) -> str:
    scored = engine.get_scored_symbols()
    if scored:
        return scored[0].symbol
    resolver = get_resolver()
    reports = engine.get_index_reports()
    if reports:
        return resolver.get_code(reports[0].name) or ""
    return ""


def _stock_name(symbol: str, row: Optional[dict[str, Any]] = None) -> str:
    row = row or {}
    explicit = str(row.get("name") or row.get("stock_name") or "").strip()
    if explicit:
        return explicit
    try:
        name = get_resolver().get_name(symbol)
        return "" if name == symbol.split(".")[-1] else name
    except Exception:
        return ""


def _quote_symbol_candidates(symbol: str) -> list[str]:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return []
    pure = raw.split(".", 1)[-1] if "." in raw else raw
    candidates = [raw, pure]
    if "." in raw and raw.split(".", 1)[0] in {"SH", "SZ", "BJ"}:
        candidates.append(f"{raw.split('.', 1)[0].lower()}{pure}")
    if len(raw) >= 8 and raw[:2] in {"SH", "SZ", "BJ"}:
        candidates.append(f"{raw[:2]}.{raw[2:]}")
    if pure.isdigit() and len(pure) == 6:
        if pure.startswith(("5", "6", "9")):
            candidates.extend([f"SH.{pure}", f"sh{pure}"])
        elif pure.startswith(("4", "8")):
            candidates.extend([f"BJ.{pure}", f"bj{pure}"])
        else:
            candidates.extend([f"SZ.{pure}", f"sz{pure}"])
    return list(dict.fromkeys(candidates))


def _quote_dt_text(doc: dict[str, Any]) -> str:
    value = doc.get("dt") or doc.get("trade_date") or doc.get("snapshot_at")
    if hasattr(value, "date"):
        return value.date().isoformat()
    return str(value or "")[:10]


def _quote_age_seconds(doc: dict[str, Any]) -> Optional[float]:
    value = doc.get("snapshot_at")
    if value is None:
        return None
    try:
        ts = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if ts.tzinfo is not None:
            ts = ts.astimezone().replace(tzinfo=None)
        return max(0.0, (_market_now("A") - ts).total_seconds())
    except Exception:
        return None


def _quote_day_is_stale(quote_day: str, expected_day: str, day_change_mode: str) -> bool:
    if not quote_day or not expected_day or quote_day == expected_day:
        return False
    return True


def _quote_overlay_for_symbol(symbol: str) -> dict[str, Any]:
    candidates = _quote_symbol_candidates(symbol)
    if not candidates:
        return {"quote_status": "missing", "quote_status_label": "无行情"}
    try:
        doc = _mongo_db()["quote_snapshots"].find_one(
            {"symbol": {"$in": candidates}},
            {"_id": 0},
            sort=[("snapshot_at", -1), ("dt", -1)],
        ) or {}
    except Exception:
        doc = {}
    if not doc:
        return {"quote_status": "missing", "quote_status_label": "无行情"}

    day_change_mode = _a_day_change_mode()
    expected_day = _day_change_expected_day(day_change_mode)
    quote_day = _quote_dt_text(doc)
    age_seconds = _quote_age_seconds(doc)
    stale_reason = _text(doc.get("stale_reason"))
    quote_day_stale = _quote_day_is_stale(quote_day, expected_day, day_change_mode)
    is_stale = bool(doc.get("is_stale")) or doc.get("freshness") == "stale" or quote_day_stale
    if is_stale:
        status = "stale"
        label = "行情陈旧"
        if quote_day_stale:
            stale_reason = stale_reason or f"quote_day={quote_day}, expected={expected_day}"
    elif day_change_mode == "daily_close":
        status = "closed"
        label = "收盘"
    elif age_seconds is not None and age_seconds > 30:
        status = "delayed"
        label = "行情延迟"
    else:
        status = "realtime"
        label = "实时"

    overlay = {
        "day_change_mode": day_change_mode,
        "quote_status": status,
        "quote_status_label": label,
        "quote_source": doc.get("source") or "",
        "quote_as_of": quote_day,
        "quote_snapshot_at": doc.get("snapshot_at"),
        "quote_age_seconds": age_seconds,
        "quote_stale_reason": stale_reason,
    }
    if day_change_mode == "quote_intraday" and status in {"realtime", "delayed"}:
        latest_price = _first_numeric(doc.get("price"), doc.get("close"))
        change_pct = _float(doc.get("change_pct"))
        if latest_price is not None:
            overlay.update({
                "latest_price": latest_price,
                "realtime_price": latest_price,
            })
        if change_pct is not None:
            overlay.update({
                "day_change_pct": change_pct,
                "daily_change_pct": change_pct,
                "today_change_pct": change_pct,
                "day_change_source": "quote_snapshots",
                "day_change_as_of": quote_day,
            })
    return overlay


def _latest_daily_trading_values(symbol: str, chart: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    source = ""
    latest_row: dict[str, Any] = {}
    chart_dict = chart if isinstance(chart, dict) else {}
    chart_meta = chart_dict.get("meta") if isinstance(chart_dict.get("meta"), dict) else {}
    if _freq_bucket(chart_meta.get("freq")) == "daily" and isinstance(chart_dict.get("ohlcv"), list) and chart_dict.get("ohlcv"):
        latest_row = chart_dict["ohlcv"][-1] if isinstance(chart_dict["ohlcv"][-1], dict) else {}
        source = _text(chart_meta.get("source")) or "chart.daily"
        as_of = _timestamp_date(
            int(latest_row.get("time") or 0),
            market=_text(chart_meta.get("market")) or infer_market(symbol=symbol, source=source),
            symbol=symbol,
            source=source,
        )
    else:
        try:
            daily_df, source = _stock_df(symbol, "daily")
        except Exception:
            daily_df, source = pd.DataFrame(), ""
        if daily_df is None or daily_df.empty:
            return {}
        row = daily_df.iloc[-1]
        latest_row = {
            "volume": _float(row.get("vol") or row.get("volume"), 0) or 0,
            "amount": _float(row.get("amount") or row.get("turnover"), 0) or 0,
        }
        as_of = _date_text(daily_df.index[-1])

    volume = _float(latest_row.get("volume") or latest_row.get("vol"), 0) or 0
    amount = _float(latest_row.get("amount") or latest_row.get("turnover"), 0) or 0
    if volume <= 0 and amount <= 0:
        return {}
    return {
        "day_volume": int(volume),
        "daily_volume": int(volume),
        "latest_daily_volume": int(volume),
        "day_amount": int(amount),
        "daily_amount": int(amount),
        "latest_daily_amount": int(amount),
        "daily_trading_value_as_of": as_of,
        "daily_trading_value_source": source,
    }


def _apply_quote_overlay(row: dict[str, Any], symbol: str) -> dict[str, Any]:
    overlay = _quote_overlay_for_symbol(symbol)
    updated = dict(row)
    if overlay.get("day_change_mode") == "quote_intraday" and overlay.get("quote_status") in {"stale", "missing"}:
        updated.update({
            "day_change_pct": None,
            "daily_change_pct": None,
            "today_change_pct": None,
            "day_change_source": "",
            "day_change_as_of": overlay.get("quote_as_of") or "",
        })
    updated.update(overlay)
    return updated


def _enrich_stock_row(row: dict[str, Any], range_columns: list[dict[str, Any]]) -> dict[str, Any]:
    symbol = str(row.get("symbol") or row.get("code") or row.get("label") or "").strip()
    normalized, raw_code = _normalize_stock_symbol(symbol)
    normalized = normalized or symbol
    df, source = _stock_df(normalized, "daily") if normalized else (pd.DataFrame(), "")
    day_change_mode = _a_day_change_mode()
    daily_day_change, daily_day_source, daily_as_of = _daily_close_day_change_pct(df)
    daily_close_price = (
        float(df["close"].iloc[-1])
        if daily_as_of == _day_change_expected_day("daily_close") and df is not None and not df.empty and "close" in df.columns
        else None
    )
    cached_latest_price = (
        float(df["close"].iloc[-1])
        if df is not None and not df.empty and "close" in df.columns
        else None
    )
    metadata = dict(row.get("metadata") or {}) if isinstance(row.get("metadata"), dict) else {}
    latest_price = (daily_close_price or cached_latest_price) if day_change_mode == "daily_close" else (
        row.get("latest_price")
        or row.get("price")
        or metadata.get("price")
        or daily_close_price
        or cached_latest_price
    )
    enriched = dict(row)
    day_change_pct = daily_day_change if day_change_mode == "daily_close" else None
    day_change_source = daily_day_source if day_change_mode == "daily_close" else ""
    latest_signal = _text(row.get("latest_signal") or row.get("signal") or row.get("reason") or row.get("direction"))
    enriched.update({
        "kind": "stock",
        "label": normalized,
        "symbol": normalized,
        "code": normalized,
        "raw_code": raw_code or normalized.split(".")[-1],
        "name": _stock_name(normalized, row),
        "latest_price": latest_price,
        "day_change_pct": day_change_pct,
        "daily_change_pct": day_change_pct,
        "day_change_source": day_change_source,
        "day_change_mode": day_change_mode,
        "day_change_as_of": daily_as_of if day_change_mode == "daily_close" else "",
        "latest_signal": latest_signal or _ma_signal_from_df(df),
        "range_returns": _compute_range_returns(df, range_columns),
        "range_return_source": source,
        "available_freqs": UI_FREQS,
        "target_kind": "stock",
        "target_label": normalized,
        "target_symbol": normalized,
        "target_freq": DEFAULT_TERMINAL_FREQ,
    })
    return _apply_quote_overlay(enriched, normalized)


def _slim_shell_signal_reason(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    keys = (
        "reason_type",
        "source_collection",
        "source_role",
        "signal_family",
        "signal_side",
        "signal_type",
        "freq",
        "queue_lane",
        "actionability",
        "decision_effect",
        "confidence",
        "score",
        "as_of",
        "event_dt",
        "event_date",
        "event_latest_dt",
        "signal_age_trading_days",
        "weight",
    )
    out = {key: value.get(key) for key in keys if value.get(key) not in (None, "", [], {})}
    evidence = value.get("evidence") if isinstance(value.get("evidence"), dict) else {}
    for key in ("direction", "freq", "signal_type"):
        if evidence.get(key) not in (None, "", [], {}) and key not in out:
            out[key] = evidence.get(key)
    details = evidence.get("details")
    if isinstance(details, str) and details:
        out["details"] = details[:120]
    elif isinstance(details, dict):
        out["details"] = {
            key: details.get(key)
            for key in ("signal", "reason", "summary", "pattern")
            if details.get(key) not in (None, "", [], {})
        }
    resonance = value.get("resonance_context") if isinstance(value.get("resonance_context"), dict) else evidence.get("resonance_context")
    if isinstance(resonance, dict):
        out["resonance_context"] = {
            key: resonance.get(key)
            for key in ("grade", "tags", "aligned_freqs", "conflict_freqs", "primary_freq", "direction", "latest_dt", "summary")
            if resonance.get(key) not in (None, "", [], {})
        }
    return out


def _slim_shell_stock_row(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "kind",
        "label",
        "symbol",
        "code",
        "raw_code",
        "name",
        "latest_price",
        "day_change_pct",
        "daily_change_pct",
        "today_change_pct",
        "day_change_source",
        "day_change_mode",
        "day_change_as_of",
        "realtime_price",
        "latest_signal",
        "reason",
        "signal",
        "direction",
        "range_returns",
        "range_return_source",
        "available_freqs",
        "target_kind",
        "target_label",
        "target_symbol",
        "target_freq",
        "lane",
        "second_screen_role",
        "freshness",
        "lane_status",
        "source",
        "source_collection",
        "source_collections",
        "source_tags",
        "focus_reasons",
        "trace_summary",
        "signal_origin",
        "signal_family",
        "knowledge_confirmation",
        "resonance_context",
        "chain_context",
        "exit_condition",
        "invalidates_when",
        "action_status",
        "actionability",
        "queue_lane",
        "pool_type",
        "entry_gate_status",
        "next_action",
        "trader_action",
        "rank_score",
        "sort_score",
        "score",
        "rank_reason",
        "score_components",
        "coverage_status",
        "decision_effect",
        "blocked_by",
        "primary_blocker",
        "recommended_action",
        "missing_gates",
        "promotion_path",
        "strategy_semantics",
        "trade_stage",
        "stage_label",
        "current_position",
        "trade_intent",
        "trade_intent_label",
        "trade_role",
        "trade_role_label",
        "trade_identity",
        "trade_identity_label",
        "trader_read",
        "ai_trade_summary",
        "evidence_summary",
        "setup_side_label",
        "setup_explanation",
        "entry_logic_summary",
        "watch_sort_priority",
        "timeframe_reads",
        "entry_reason",
        "missing_condition",
        "invalidation",
        "chain_position",
        "intervention_side",
        "intervention_label",
        "opportunity_side",
        "opportunity_label",
        "strategy_lineage",
        "left_setup_reasons",
        "right_confirm_reasons",
        "left_signal_reasons",
        "right_signal_reasons",
        "risk_signal_reasons",
        "technical_signal_groups",
        "timeframe_signal_sides",
        "upper_timeframe_side",
        "trade_timeframe",
        "trade_timeframe_side",
        "execution_timeframe_side",
        "chain_phase",
        "theme_rank_bonus",
        "theme_alignment_level",
        "event_latest_dt",
        "signal_age_trading_days",
        "stale_context",
        "stale_signal_count",
        "buy_timeframes",
        "sell_timeframes",
        "quote_status",
        "quote_status_label",
        "quote_source",
        "quote_as_of",
        "quote_snapshot_at",
        "quote_age_seconds",
        "quote_stale_reason",
        "explanation",
        "manual_clue",
        "deletable",
        "can_trade_now",
    )
    out = {key: row.get(key) for key in keys if row.get(key) not in (None, "", [], {})}
    reasons = [
        _slim_shell_signal_reason(reason)
        for reason in row.get("inclusion_reasons") or []
        if isinstance(reason, dict)
    ]
    if reasons:
        out["inclusion_reasons"] = [reason for reason in reasons if reason][:3]
    for key in ("technical_evidence", "top_buy_reason", "top_risk_reason"):
        slim = _slim_shell_signal_reason(row.get(key))
        if slim:
            out[key] = slim
    return out


def _slim_shell_candidate_group_rows(rows: Any, limit: int = 4) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    keep = (
        "symbol",
        "raw_code",
        "code",
        "name",
        "leader_tier",
        "chain_role",
        "attention_score",
        "day_change_pct",
        "latest_signal",
        "why_watch",
        "target_kind",
        "target_label",
        "target_symbol",
        "target_freq",
    )
    output: list[dict[str, Any]] = []
    for item in rows[:limit]:
        if not isinstance(item, dict):
            continue
        output.append({key: item.get(key) for key in keep if item.get(key) not in (None, "", [], {})})
    return output


def _slim_shell_sector_row(row: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "kind",
        "label",
        "name",
        "code",
        "latest_price",
        "day_change_pct",
        "daily_change_pct",
        "day_change_source",
        "day_change_mode",
        "day_change_as_of",
        "range_returns",
        "range_return_source",
        "target_kind",
        "target_label",
        "target_symbol",
        "target_freq",
        "lane",
        "second_screen_role",
        "freshness",
        "lane_status",
        "source",
        "source_collection",
        "heat_source",
        "heat_target_label",
        "heat_resolution_status",
        "rank",
        "phase",
        "trading_signal",
        "heat_score",
        "momentum_5m",
        "momentum_15m",
        "momentum_30m",
        "chain_id",
        "chain_name",
        "node_id",
        "node_name",
        "layer",
        "stage",
        "integrated_domains",
        "latest_signal",
        "trader_action",
        "action_status",
        "invalidates_when",
        "explanation",
        "carrier",
        "mapping_chain",
        "data_truth",
    )
    out = {key: row.get(key) for key in keep if row.get(key) not in (None, "", [], {})}
    groups = row.get("candidate_groups")
    if isinstance(groups, dict):
        out["candidate_groups"] = {
            key: _slim_shell_candidate_group_rows(groups.get(key), 4 if key != "leaders" else 3)
            for key in ("leaders", "weighted", "elastic", "source_leaders", "constituents")
            if _slim_shell_candidate_group_rows(groups.get(key), 4 if key != "leaders" else 3)
        }
    preview = _slim_shell_candidate_group_rows(row.get("focus_stocks_preview"), 6)
    if preview:
        out["focus_stocks_preview"] = preview
    representatives = row.get("representatives")
    if isinstance(representatives, dict):
        out["representatives"] = {
            key: _slim_shell_candidate_group_rows(value, 3)
            for key, value in representatives.items()
            if _slim_shell_candidate_group_rows(value, 3)
        }
    return out


def _enrich_index_row(row: dict[str, Any], range_columns: list[dict[str, Any]]) -> dict[str, Any]:
    symbol = str(row.get("symbol") or row.get("code") or row.get("label") or row.get("name") or "").strip()
    df, source = _index_df(symbol, "daily") if symbol else (pd.DataFrame(), "")
    day_change_mode = _a_day_change_mode()
    daily_day_change, daily_day_source, daily_as_of = _daily_close_day_change_pct(df)
    daily_close_price = (
        float(df["close"].iloc[-1])
        if daily_as_of == _day_change_expected_day("daily_close") and df is not None and not df.empty and "close" in df.columns
        else None
    )
    cached_latest_price = (
        float(df["close"].iloc[-1])
        if df is not None and not df.empty and "close" in df.columns
        else None
    )
    day_change_pct = daily_day_change if day_change_mode == "daily_close" else None
    day_change_source = daily_day_source if day_change_mode == "daily_close" else ""
    enriched = dict(row)
    enriched.update({
        "kind": "index",
        "label": row.get("name") or row.get("label") or symbol,
        "name": row.get("name") or row.get("label") or symbol,
        "code": symbol,
        "latest_price": (daily_close_price or cached_latest_price) if day_change_mode == "daily_close" else (row.get("latest_price") or cached_latest_price),
        "day_change_pct": day_change_pct,
        "daily_change_pct": day_change_pct,
        "day_change_source": day_change_source,
        "day_change_mode": day_change_mode,
        "day_change_as_of": daily_as_of if day_change_mode == "daily_close" else "",
        "latest_signal": _signal_or_fallback(row, df),
        "range_returns": _compute_range_returns(df, range_columns),
        "range_return_source": source,
        "available_freqs": UI_FREQS,
        "target_kind": "index",
        "target_label": row.get("name") or row.get("label") or symbol,
        "target_symbol": symbol,
        "target_freq": DEFAULT_TERMINAL_FREQ,
    })
    return _apply_quote_overlay(enriched, symbol)


def _enrich_cluster_row(row: dict[str, Any], kind: str) -> dict[str, Any]:
    enriched = dict(row)
    label = str(enriched.get("label") or enriched.get("name") or "").strip()
    day_change_pct = _first_numeric(
        enriched.get("day_change_pct"),
        enriched.get("daily_change_pct"),
        enriched.get("today_change_pct"),
        enriched.get("change_pct"),
        enriched.get("gain_pct"),
        enriched.get("strength"),
    )
    enriched.update({
        "kind": kind,
        "label": label,
        "name": label,
        "code": str(enriched.get("code") or enriched.get("board_code") or ""),
        "latest_price": enriched.get("latest_price") or enriched.get("value"),
        "day_change_pct": day_change_pct,
        "daily_change_pct": day_change_pct,
        "range_returns": enriched.get("range_returns") or {},
        "target_kind": kind,
        "target_label": label,
        "target_symbol": str(enriched.get("code") or enriched.get("board_code") or label),
        "target_freq": DEFAULT_TERMINAL_FREQ,
    })
    return enriched


def _signal_text(signal: dict[str, Any]) -> str:
    return " ".join(
        str(signal.get(key) or "")
        for key in ("signal_type", "type", "reason", "details", "summary")
    ).lower()


def _is_buy_signal(signal: dict[str, Any]) -> bool:
    text = _signal_text(signal)
    if any(token in text for token in SELL_SIGNAL_TOKENS):
        return False
    return any(token in text for token in BUY_SIGNAL_TOKENS)


def _is_sell_signal(signal: dict[str, Any]) -> bool:
    text = _signal_text(signal)
    return any(token in text for token in SELL_SIGNAL_TOKENS)


def _signal_date(signal: dict[str, Any]) -> str:
    return str(signal.get("signal_date") or signal.get("date_str") or signal.get("updated_at") or "")[:10]


def _load_signal_pool_rows(limit: int = 200, symbol: Optional[str] = None) -> list[dict[str, Any]]:
    try:
        from signals.data.gateway import get_signal_pool

        response = get_signal_pool(DataRequest(
            domain="signal",
            mode="historical",
            market="A",
            symbol=symbol,
            purpose="review",
            allow_stale=True,
        ))
        rows = response.data or []
        return [dict(item) for item in rows[:limit] if isinstance(item, dict)]
    except Exception:
        return []


def _load_terminal_technical_signal_rows(symbol: str, *, limit: int = 300) -> list[dict[str, Any]]:
    if not symbol:
        return []
    try:
        db = _mongo_db()
        candidates = _probe_symbol_candidates(symbol, kind="stock")
        latest = db["terminal_technical_signals"].find_one(
            {"symbol": {"$in": candidates}, "market": "A", "as_of": {"$exists": True}},
            {"_id": 0, "as_of": 1},
            sort=[("as_of", -1), ("updated_at", -1)],
        ) or {}
        query: dict[str, Any] = {"symbol": {"$in": candidates}}
        latest_as_of = _text(latest.get("as_of"))
        if latest_as_of:
            query["as_of"] = latest_as_of
        docs = list(db["terminal_technical_signals"].find(
            query,
            {"_id": 0},
        ).sort([("dt", -1), ("updated_at", -1)]).limit(limit))
        return [dict(item) for item in docs if isinstance(item, dict)]
    except Exception:
        return []


def _manual_clue_signal_side(signal: dict[str, Any]) -> str:
    side = _text(signal.get("signal_side")).lower()
    if side in {"buy", "sell"}:
        return side
    if _is_sell_signal(signal):
        return "sell"
    if _is_buy_signal(signal):
        return "buy"
    return "context"


def _manual_clue_signal_reason(signal: dict[str, Any]) -> dict[str, Any]:
    evidence = signal.get("technical_evidence") if isinstance(signal.get("technical_evidence"), dict) else {}
    signal_type = _text(signal.get("signal_type") or signal.get("type") or signal.get("reason"))
    freq = _text(signal.get("freq") or signal.get("timeframe")) or _freq_label(DEFAULT_TERMINAL_FREQ)
    side = _manual_clue_signal_side(signal)
    event_dt = _iso_dt(signal.get("dt") or signal.get("signal_date") or signal.get("updated_at"))
    score = _float(signal.get("score"), _float(signal.get("total_score"), 0) or 0) or 0
    decision_effect = "exit_priority" if side == "sell" else "confirm" if side == "buy" else "context_only"
    queue_lane = "risk_exit_first" if side == "sell" else "manual_signal_review"
    return {
        "reason_type": "technical_trigger" if side in {"buy", "sell"} else "technical_signal",
        "weight": round(100 + abs(score), 3),
        "source_role": "technical_trigger" if side in {"buy", "sell"} else "context",
        "decision_effect": decision_effect,
        "actionability": "risk_review" if side == "sell" else "manual_review",
        "queue_lane": queue_lane,
        "source_collection": "terminal_technical_signals",
        "source_doc_id": _text(signal.get("dedupe_key")),
        "signal_type": signal_type,
        "signal_side": side,
        "signal_family": _text(signal.get("signal_family")),
        "freq": freq,
        "score": score,
        "confidence": _float(signal.get("confidence")),
        "as_of": _text(signal.get("as_of")),
        "event_dt": event_dt,
        "event_date": event_dt[:10] if event_dt else "",
        "signal_date": event_dt,
        "price": _float(signal.get("price")),
        "evidence": evidence,
        "evidence_sources": ["terminal_technical_signals"],
        "resonance_context": signal.get("resonance_context") if isinstance(signal.get("resonance_context"), dict) else {},
        "invalidates_when": _text(signal.get("invalidates_when")),
    }


def _manual_clue_bucket_side(left_items: list[dict[str, Any]], right_items: list[dict[str, Any]]) -> str:
    if left_items and right_items:
        return "mixed"
    if right_items:
        return "right"
    if left_items:
        return "left"
    return "none"


def _manual_clue_fallback_groups(reasons: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {"left": [], "right": [], "sell": [], "context": []}
    buckets: dict[str, dict[str, Any]] = {
        "upper": {"label": "日/周", "left": [], "right": []},
        "trade": {"label": "30m", "left": [], "right": []},
        "execution": {"label": "5m/15m", "left": [], "right": []},
    }
    for reason in reasons:
        signal_text = _text(reason.get("signal_type"))
        side = _text(reason.get("signal_side"))
        freq = reason.get("freq")
        bucket_key = "upper" if _freq_bucket(freq) in {"daily", "weekly"} else "trade" if _freq_bucket(freq) == "30min" else "execution" if _freq_bucket(freq) in {"5min", "15min"} else ""
        opportunity_side = "sell" if side == "sell" else "right" if any(token in signal_text for token in ("突破", "二买", "三买", "趋势", "扩大")) or _freq_bucket(freq) in {"5min", "15min"} else "left" if side == "buy" else "context"
        item = {
            "label": signal_text,
            "family": _text(reason.get("signal_family")) or "technical",
            "freq": _text(freq),
            "event_date": _text(reason.get("event_date")),
            "score": _float(reason.get("score"), 0) or 0,
            "confidence": _float(reason.get("confidence")),
            "source_collection": _text(reason.get("source_collection")),
        }
        groups.setdefault(opportunity_side, []).append(item)
        if bucket_key and opportunity_side in {"left", "right"}:
            buckets[bucket_key][opportunity_side].append(item)
    for bucket in buckets.values():
        bucket["side"] = _manual_clue_bucket_side(bucket["left"], bucket["right"])
    return groups, buckets


def _manual_clue_signal_groups(reasons: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    try:
        from signals.sync.modules import terminal_pool

        row = {"inclusion_reasons": reasons}
        groups = terminal_pool._technical_signal_groups(row)
        timeframe_sides = terminal_pool._timeframe_signal_sides(row)
        return groups, timeframe_sides
    except Exception:
        return _manual_clue_fallback_groups(reasons)


def _manual_clue_group_labels(groups: dict[str, list[dict[str, Any]]], side: str) -> list[str]:
    labels: list[str] = []
    for item in groups.get(side, []):
        label = " ".join([_text(item.get("freq")), _text(item.get("label"))]).strip()
        if label and label not in labels:
            labels.append(label)
    return labels[:5]


def _manual_clue_missing_label(code: str) -> str:
    return {
        "risk_clear": "有卖点或冲突，先排雷",
        "period_conflict": "周期冲突，等共振恢复",
        "hard_technical": "还没有硬技术信号",
        "upper_context": "等日/周背景确认",
        "trigger_30m": "等30m买点",
        "right_side": "等5m/15m下单确认",
    }.get(code, code)


def _manual_clue_has_conflict(reasons: list[dict[str, Any]]) -> bool:
    for reason in reasons:
        resonance = reason.get("resonance_context") if isinstance(reason.get("resonance_context"), dict) else {}
        if _text(resonance.get("grade")) == "conflict":
            return True
        tags = [_text(item) for item in resonance.get("tags") or []]
        if any("冲突" in item for item in tags):
            return True
        if resonance.get("conflict_freqs"):
            return True
    return False


def _manual_clue_promotion_path(
    *,
    has_technical: bool,
    timeframe_sides: dict[str, dict[str, Any]],
    missing_gates: list[str],
    source_detail: str,
) -> list[dict[str, Any]]:
    def gate_status(gate: str, present: bool) -> str:
        if gate in missing_gates:
            return "blocked" if gate in {"risk_clear", "period_conflict"} else "waiting"
        return "passed" if present else "context"

    upper_side = _text(timeframe_sides.get("upper", {}).get("side")) or "none"
    trade_side = _text(timeframe_sides.get("trade", {}).get("side")) or "none"
    execution_side = _text(timeframe_sides.get("execution", {}).get("side")) or "none"
    return [
        {"key": "source", "status": "passed", "detail": source_detail},
        {"key": "hard_technical", "status": "passed" if has_technical else "waiting", "detail": "terminal_technical_signals" if has_technical else ""},
        {"key": "upper_context", "status": gate_status("upper_context", upper_side != "none"), "detail": f"日/周 {upper_side}"},
        {"key": "trigger_30m", "status": gate_status("trigger_30m", trade_side != "none"), "detail": f"30m {trade_side}"},
        {"key": "right_side", "status": gate_status("right_side", execution_side != "none"), "detail": f"5m/15m {execution_side}"},
        {"key": "risk_clear", "status": "blocked" if any(gate in missing_gates for gate in ("risk_clear", "period_conflict")) else "passed", "detail": " / ".join(_manual_clue_missing_label(gate) for gate in missing_gates if gate in {"risk_clear", "period_conflict"}) or "无主要冲突"},
    ]


def _manual_clue_entry_summary(timeframe_sides: dict[str, dict[str, Any]], missing_condition: str) -> str:
    def labels(bucket: str) -> str:
        record = timeframe_sides.get(bucket, {}) if isinstance(timeframe_sides.get(bucket), dict) else {}
        items = []
        for side in ("right", "left"):
            for item in record.get(side) or []:
                if isinstance(item, dict):
                    text = " ".join([_text(item.get("freq")), _text(item.get("label"))]).strip()
                    if text:
                        items.append(text)
        return " / ".join(items[:3])

    return "；".join([
        f"日/周: {labels('upper') or '未确认'}",
        f"30m: {labels('trade') or '未确认'}",
        f"5m/15m: {labels('execution') or '未确认'}",
        f"还差: {missing_condition}" if missing_condition else "",
    ]).strip("；")


def _enrich_manual_clue_decision(row: dict[str, Any], symbol: str) -> dict[str, Any]:
    signals = _load_terminal_technical_signal_rows(symbol, limit=80)
    reasons: list[dict[str, Any]] = []
    seen: set[str] = set()
    for signal in signals:
        reason = _manual_clue_signal_reason(signal)
        if not _text(reason.get("signal_type")):
            continue
        key = "|".join([
            _text(reason.get("signal_side")),
            _text(reason.get("freq")),
            _text(reason.get("signal_type")),
            _text(reason.get("event_date")),
        ])
        if key in seen:
            continue
        seen.add(key)
        reasons.append(reason)
        if len(reasons) >= 12:
            break

    row.setdefault("source_collections", ["terminal_manual_clues"])
    row.setdefault("source_tags", ["用户探索", "临时线索"])
    row["inclusion_reasons"] = reasons
    row["focus_reasons"] = _manual_clue_group_labels({"right": reasons, "left": []}, "right")[:4]

    technical_groups, timeframe_sides = _manual_clue_signal_groups(reasons)
    compact_groups = {key: value for key, value in technical_groups.items() if value}
    row["technical_signal_groups"] = compact_groups
    row["left_signal_reasons"] = _manual_clue_group_labels(technical_groups, "left")
    row["right_signal_reasons"] = _manual_clue_group_labels(technical_groups, "right")
    row["risk_signal_reasons"] = _manual_clue_group_labels(technical_groups, "sell")
    row["timeframe_signal_sides"] = timeframe_sides
    row["upper_timeframe_side"] = _text(timeframe_sides.get("upper", {}).get("side")) or "none"
    row["trade_timeframe"] = "30m"
    row["trade_timeframe_side"] = _text(timeframe_sides.get("trade", {}).get("side")) or "none"
    row["execution_timeframe_side"] = _text(timeframe_sides.get("execution", {}).get("side")) or "none"

    row["timeframe_signals"] = {}
    row["sell_timeframe_signals"] = {}
    row["timeframe_signal_stack"] = {}
    for reason in reasons:
        side = "sell" if _text(reason.get("signal_side")) == "sell" else "buy"
        _add_timeframe_signal(row, reason, side=side)
    buy_signals = row.get("timeframe_signals") if isinstance(row.get("timeframe_signals"), dict) else {}
    sell_signals = row.get("sell_timeframe_signals") if isinstance(row.get("sell_timeframe_signals"), dict) else {}
    row["buy_timeframes"] = [buy_signals[freq] for freq in BUY_FREQS if freq in buy_signals]
    row["sell_timeframes"] = [sell_signals[freq] for freq in BUY_FREQS if freq in sell_signals]

    buy_reasons = [reason for reason in reasons if _text(reason.get("signal_side")) == "buy"]
    sell_reasons = [reason for reason in reasons if _text(reason.get("signal_side")) == "sell"]
    has_upper = row["upper_timeframe_side"] != "none"
    has_30m = row["trade_timeframe_side"] != "none"
    has_execution = row["execution_timeframe_side"] != "none"
    conflict = _manual_clue_has_conflict(reasons)
    missing_gates: list[str] = []
    if sell_reasons:
        missing_gates.append("risk_clear")
    if conflict:
        missing_gates.append("period_conflict")
    if not buy_reasons:
        missing_gates.append("hard_technical")
    else:
        if not has_upper:
            missing_gates.append("upper_context")
        if not has_30m:
            missing_gates.append("trigger_30m")
        if not has_execution:
            missing_gates.append("right_side")
    missing_gates = list(dict.fromkeys(missing_gates))
    missing_condition = " / ".join(_manual_clue_missing_label(gate) for gate in missing_gates) or "买点路径已走通，但手动探索不自动转确认买点"

    top_buy = buy_reasons[0] if buy_reasons else {}
    top_risk = sell_reasons[0] if sell_reasons else {}
    signal_badges = [
        *[f"卖{item.get('badge') or item.get('freq') or ''}" for item in row.get("sell_timeframes", []) if isinstance(item, dict)],
        *[item.get("badge") or item.get("freq") or "" for item in row.get("buy_timeframes", []) if isinstance(item, dict)],
    ]
    evidence_bits = []
    for side in ("right", "left", "sell"):
        evidence_bits.extend(_manual_clue_group_labels(technical_groups, side))
    chain_position = _stock_chain_position_summary(symbol)
    chain_text = " · ".join(_text(chain_position.get(key)) for key in ("chain", "node") if _text(chain_position.get(key)))
    trade_role = "risk_review" if sell_reasons or conflict else (_trade_role_for_stock_summary(chain_position) if chain_position else "ordinary_watch")
    trade_role_label = {
        "mainline_attack": "主线机会",
        "climax_risk": "过热禁追",
        "chain_watch": "产业链观察",
        "defensive_weight": "防守观察",
        "second_wave": "回踩再起",
        "risk_review": "风险复核",
        "ordinary_watch": "线索观察",
    }.get(trade_role, "线索观察")

    if sell_reasons or conflict:
        trader_read = f"手动探索：{chain_text + '，' if chain_text else ''}有技术信号但存在卖点或周期冲突，先做风险复核，不自动推买点。"
        trade_intent_label = "暂不参与"
        recommended_action = "先排雷"
    elif buy_reasons and has_30m and not has_execution:
        trader_read = f"手动探索：{chain_text + '，' if chain_text else ''}日/周或30m已有信号，等5m/15m下单确认。"
        trade_intent_label = "试仓候选"
        recommended_action = "等5m/15m确认"
    elif buy_reasons:
        trader_read = f"手动探索：{chain_text + '，' if chain_text else ''}已有技术线索，按缺口/30m/右侧确认逐级复核。"
        trade_intent_label = "盯盘池"
        recommended_action = "盯盘复核"
    else:
        trader_read = "手动探索：当前没有命中硬技术信号，只保留线索和图表缓存。"
        trade_intent_label = "线索来源"
        recommended_action = "先观察"

    source_collections = [*row.get("source_collections", []), "terminal_manual_clues"]
    source_tags = [*row.get("source_tags", []), "用户探索"]
    if reasons:
        source_collections.append("terminal_technical_signals")
        source_tags.append("技术信号")

    row.update({
        "source_collections": list(dict.fromkeys(source_collections)),
        "source_tags": list(dict.fromkeys(source_tags)),
        "focus_reasons": evidence_bits[:4],
        "technical_evidence": top_buy or top_risk or {"status": "missing"},
        "top_buy_reason": top_buy,
        "top_risk_reason": top_risk,
        "resonance_context": (top_buy or top_risk).get("resonance_context", {}) if isinstance(top_buy or top_risk, dict) else {},
        "latest_signal": "/".join([_text(item) for item in signal_badges if _text(item)]) or (evidence_bits[0] if evidence_bits else "手动线索"),
        "reason": " / ".join(evidence_bits[:2]) or "用户临时探索，不影响自动入池",
        "entry_gate_status": "manual_risk_review" if sell_reasons or conflict else "manual_review",
        "blocked_by": missing_gates,
        "missing_gates": missing_gates,
        "primary_blocker": missing_condition,
        "missing_condition": missing_condition,
        "promotion_path": _manual_clue_promotion_path(
            has_technical=bool(reasons),
            timeframe_sides=timeframe_sides,
            missing_gates=missing_gates,
            source_detail="terminal_manual_clues/terminal_technical_signals" if reasons else "terminal_manual_clues",
        ),
        "trade_stage": "clue_pool",
        "stage_label": "线索池",
        "current_position": trade_intent_label,
        "decision_stage": "strategy_candidate",
        "trade_role": trade_role,
        "trade_role_label": trade_role_label,
        "trade_identity": "manual_exploration",
        "trade_identity_label": "用户探索",
        "trade_intent": "skip_now" if sell_reasons or conflict else "probe_candidate" if has_30m and not has_execution else "clue_only",
        "trade_intent_label": trade_intent_label,
        "setup_side_label": trade_intent_label,
        "recommended_action": recommended_action,
        "next_action": recommended_action,
        "trader_action": recommended_action,
        "can_trade_now": False,
        "trader_read": trader_read,
        "ai_trade_summary": trader_read,
        "evidence_summary": "；".join([
            "手动线索: terminal_manual_clues",
            f"技术信号: terminal_technical_signals · {' / '.join(evidence_bits[:4])}" if evidence_bits else "",
            f"产业链: {chain_text}" if chain_text else "",
        ]).strip("；"),
        "entry_logic_summary": _manual_clue_entry_summary(timeframe_sides, missing_condition),
        "setup_explanation": "手动探索只负责补缓存和解释，不写回自动股票池排序。",
        "invalidation": (top_risk or top_buy).get("invalidates_when") or "删除手动线索，或图形证据走弱",
        "invalidates_when": (top_risk or top_buy).get("invalidates_when") or "删除手动线索，或图形证据走弱",
        "chain_position": chain_position,
        "chain_context": {"evidence": chain_position} if chain_position else {},
        "trace_summary": "manual_clue:terminal_manual_clues" + (" / technical:terminal_technical_signals" if reasons else ""),
        "explanation": trader_read,
    })
    return row


def _signal_source_text(signal: dict[str, Any]) -> str:
    parts = [
        signal.get("source"),
        signal.get("pool_status"),
        signal.get("signal_type"),
        signal.get("type"),
        signal.get("reason"),
        signal.get("details"),
    ]
    details = signal.get("details_json")
    if isinstance(details, dict):
        parts.append(json.dumps(details, ensure_ascii=False, default=str)[:300])
    return " ".join(str(item or "") for item in parts).lower()


def _is_custom_signal_record(signal: dict[str, Any]) -> bool:
    text = _signal_source_text(signal)
    return any(token in text for token in ("signal_records", "backtest", "custom", "自定义", "回测"))


def _is_higher_timeframe(signal_freq: str, effective_freq: str) -> bool:
    signal_order = CHART_FREQ_ORDER.get(_freq_bucket(signal_freq), 99)
    effective_order = CHART_FREQ_ORDER.get(_freq_bucket(effective_freq), 99)
    return signal_order < effective_order


def _chart_signal_display_scope(signal_freq: str, effective_freq: str) -> str:
    if not _text(signal_freq):
        return "current_timeframe"
    signal_bucket = _freq_bucket(signal_freq)
    effective_bucket = _freq_bucket(effective_freq)
    if not signal_bucket or signal_bucket == effective_bucket:
        return "current_timeframe"
    if effective_bucket in {"5min", "15min", "30min"} and _is_higher_timeframe(signal_bucket, effective_bucket):
        return "higher_timeframe_context"
    return "other_timeframe"


def _should_include_chart_signal(signal_freq: str, effective_freq: str) -> bool:
    return _chart_signal_display_scope(signal_freq, effective_freq) in {"current_timeframe", "higher_timeframe_context"}


def _signal_counts_by_scope(signals: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        scope = _text(signal.get("display_scope")) or "current_timeframe"
        counts[scope] = counts.get(scope, 0) + 1
    return counts


def _signal_counts_by_freq(signals: list[dict[str, Any]], *, custom_only: bool = False) -> dict[str, int]:
    counts: dict[str, int] = {}
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        if custom_only and not _is_custom_signal_record(signal):
            continue
        freq = _freq_bucket(signal.get("freq") or signal.get("timeframe")) or "unknown"
        counts[freq] = counts.get(freq, 0) + 1
    return counts


def _custom_signal_rows(symbol: str, *, limit: int = 500) -> list[dict[str, Any]]:
    if not symbol:
        return []
    rows = _load_signal_pool_rows(limit=limit, symbol=symbol)
    return [row for row in rows if _is_custom_signal_record(row)]


def _custom_signal_diagnostics(
    symbol: str,
    requested_freq: str,
    visible_signals: list[dict[str, Any]],
) -> dict[str, Any]:
    current_freq = _freq_bucket(requested_freq)
    rows = _custom_signal_rows(symbol)
    freqs = sorted({_freq_bucket(row.get("freq") or row.get("timeframe")) for row in rows if _freq_bucket(row.get("freq") or row.get("timeframe"))})
    current_or_context_rows = [
        row for row in rows
        if _should_include_chart_signal(_text(row.get("freq") or row.get("timeframe")), current_freq)
    ]
    visible_custom = [signal for signal in visible_signals if isinstance(signal, dict) and _is_custom_signal_record(signal)]
    hidden_reasons: list[str] = []
    if not rows:
        hidden_reasons.append("no_custom_signal_records")
    elif not current_or_context_rows:
        hidden_reasons.append("custom_signals_on_other_freq")
    elif not visible_custom:
        hidden_reasons.append("custom_signals_not_in_loaded_chart_range")
    return {
        "custom_signal_count": len(rows),
        "direct_custom_signal_count": len(rows),
        "visible_custom_signal_count": len(visible_custom),
        "hidden_custom_signal_count": max(0, len(rows) - len(visible_custom)),
        "available_custom_signal_freqs": freqs,
        "custom_signal_counts_by_freq": _signal_counts_by_freq(rows),
        "visible_custom_signal_counts_by_freq": _signal_counts_by_freq(visible_custom),
        "signal_counts_by_scope": _signal_counts_by_scope(visible_signals),
        "hidden_reasons": hidden_reasons,
    }


def _candidate_stock_symbol(row: dict[str, Any]) -> tuple[str, str]:
    for key in ("symbol", "code", "raw_code", "target_symbol", "label"):
        value = _text(row.get(key))
        if not value:
            continue
        symbol, raw_code = _normalize_stock_symbol(value)
        if symbol:
            return symbol, raw_code or symbol.split(".")[-1]
    name = _text(row.get("name") or row.get("stock_name"))
    if name:
        symbol, raw_code = _normalize_stock_symbol(name)
        if symbol:
            return symbol, raw_code or symbol.split(".")[-1]
    return "", ""


def _related_custom_signals_from_candidates(
    candidates: list[dict[str, Any]],
    requested_freq: str,
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    current_freq = _freq_bucket(requested_freq)
    related: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        symbol, _ = _candidate_stock_symbol(candidate)
        if not symbol or symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        rows = _custom_signal_rows(symbol, limit=200)
        if not rows:
            continue
        preferred = [
            row for row in rows
            if _freq_bucket(row.get("freq") or row.get("timeframe")) in {current_freq, ""}
        ] or rows
        for row in preferred[:2]:
            signal_type = _text(row.get("signal_type") or row.get("type") or row.get("reason"))
            related.append({
                "symbol": symbol,
                "name": _text(candidate.get("name") or candidate.get("stock_name")) or _stock_name(symbol),
                "relation": _text(candidate.get("relation") or candidate.get("role") or candidate.get("representative_type")),
                "type": signal_type,
                "signal_type": signal_type,
                "freq": _freq_bucket(row.get("freq") or row.get("timeframe")),
                "date_str": _signal_date(row),
                "signal_date": row.get("signal_date") or row.get("dt") or row.get("updated_at"),
                "source": row.get("source") or "signals.signal_pool",
                "details": _signal_details(row),
                "confidence": _float(row.get("confidence")),
            })
            if len(related) >= limit:
                return related
    return related


def _recent_custom_signal_candidates(*, limit: int = 10) -> list[dict[str, Any]]:
    rows = [row for row in _load_signal_pool_rows(limit=500) if _is_custom_signal_record(row)]
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        symbol, raw_code = _candidate_stock_symbol(row)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        output.append({
            "symbol": symbol,
            "raw_code": raw_code,
            "name": _stock_name(symbol, row),
            "relation": "近期自定义信号",
            "source": "signal_records",
            "representative_type": "custom_signal_candidate",
        })
        if len(output) >= limit:
            break
    return output


def _signal_details(signal: dict[str, Any]) -> str:
    details = signal.get("details_json")
    if isinstance(details, dict):
        reasons = details.get("reasons")
        if isinstance(reasons, list) and reasons:
            return ",".join(str(item) for item in reasons[:4])
        return json.dumps(details, ensure_ascii=False, default=str)[:240]
    return _text(signal.get("details") or signal.get("summary") or signal.get("reason"))


def _technical_signal_details(signal: dict[str, Any]) -> str:
    evidence = signal.get("technical_evidence") if isinstance(signal.get("technical_evidence"), dict) else {}
    return _text(evidence.get("details") or evidence.get("score_details") or signal.get("details") or signal.get("summary"))[:240]


def _terminal_technical_chart_signals(symbol: str, freq: str, chart: dict[str, Any]) -> list[dict[str, Any]]:
    chart_meta = chart.get("meta") if isinstance(chart.get("meta"), dict) else {}
    effective_freq = _freq_bucket(chart_meta.get("freq") or freq)
    market = _text(chart_meta.get("market")) or infer_market(symbol=symbol, source=_text(chart_meta.get("source")))
    source = _text(chart_meta.get("source")) or "signals"
    ohlcv = chart.get("ohlcv") if isinstance(chart.get("ohlcv"), list) else []
    start_ts = int(ohlcv[0]["time"]) if ohlcv and isinstance(ohlcv[0], dict) and ohlcv[0].get("time") else None
    end_ts = int(ohlcv[-1]["time"]) if ohlcv and isinstance(ohlcv[-1], dict) and ohlcv[-1].get("time") else None
    output: list[dict[str, Any]] = []
    for signal in _load_terminal_technical_signal_rows(symbol):
        signal_freq = _freq_bucket(signal.get("freq") or signal.get("timeframe"))
        display_scope = _chart_signal_display_scope(_text(signal.get("freq") or signal.get("timeframe")), effective_freq)
        if display_scope == "other_timeframe":
            continue
        signal_type = _text(signal.get("signal_type") or signal.get("type") or signal.get("reason"))
        if not signal_type:
            continue
        signal_dt = signal.get("dt") or signal.get("signal_date") or signal.get("updated_at")
        ts = _signal_ts(signal_dt, market=market, symbol=symbol, source=source)
        aligned_ts, aligned_price, aligned = _aligned_signal_bar(
            signal,
            signal_dt=signal_dt,
            ts=ts,
            ohlcv=ohlcv,
            effective_freq=effective_freq,
            market=market,
            symbol=symbol,
            source=source,
        )
        if start_ts and aligned_ts < start_ts:
            continue
        if end_ts and aligned_ts > end_ts + 86400:
            continue
        output.append({
            "dt": aligned_ts,
            "date_str": _date_text(signal_dt),
            "type": signal_type,
            "price": _float(signal.get("price"), aligned_price),
            "confidence": _float(signal.get("confidence")),
            "freq": signal_freq or _canonical_freq(freq),
            "details": _technical_signal_details(signal),
            "source": "terminal_technical_signals",
            "pool_status": signal.get("pool_status"),
            "chart_aligned": aligned,
            "display_scope": display_scope,
            "signal_side": signal.get("signal_side"),
        })
    return output


def _signal_pool_chart_signals(symbol: str, freq: str, chart: dict[str, Any]) -> list[dict[str, Any]]:
    chart_meta = chart.get("meta") if isinstance(chart.get("meta"), dict) else {}
    effective_freq = _freq_bucket(chart_meta.get("freq") or freq)
    market = _text(chart_meta.get("market")) or infer_market(symbol=symbol, source=_text(chart_meta.get("source")))
    source = _text(chart_meta.get("source")) or "signals"
    ohlcv = chart.get("ohlcv") if isinstance(chart.get("ohlcv"), list) else []
    start_ts = int(ohlcv[0]["time"]) if ohlcv and isinstance(ohlcv[0], dict) and ohlcv[0].get("time") else None
    end_ts = int(ohlcv[-1]["time"]) if ohlcv and isinstance(ohlcv[-1], dict) and ohlcv[-1].get("time") else None
    rows = _load_signal_pool_rows(limit=200, symbol=symbol)
    output: list[dict[str, Any]] = []
    for signal in rows:
        raw_signal_freq = _text(signal.get("freq") or signal.get("timeframe"))
        signal_freq = _freq_bucket(raw_signal_freq)
        display_scope = _chart_signal_display_scope(raw_signal_freq, effective_freq)
        if display_scope == "other_timeframe":
            continue
        signal_type = _text(signal.get("signal_type") or signal.get("type") or signal.get("reason"))
        if not signal_type:
            continue
        signal_dt = signal.get("signal_date") or signal.get("dt") or signal.get("updated_at")
        ts = _signal_ts(signal_dt, market=market, symbol=symbol, source=source)
        aligned_ts, aligned_price, aligned = _aligned_signal_bar(
            signal,
            signal_dt=signal_dt,
            ts=ts,
            ohlcv=ohlcv,
            effective_freq=effective_freq,
            market=market,
            symbol=symbol,
            source=source,
        )
        if start_ts and aligned_ts < start_ts:
            continue
        if end_ts and aligned_ts > end_ts + 86400:
            continue
        details = signal.get("details_json") if isinstance(signal.get("details_json"), dict) else {}
        price = _float(signal.get("price") or signal.get("close") or details.get("close"), aligned_price)
        output.append({
            "dt": aligned_ts,
            "date_str": str(signal_dt)[:10] if signal_dt else "",
            "type": signal_type,
            "price": price,
            "confidence": _float(signal.get("confidence")),
            "freq": signal_freq or _canonical_freq(freq),
            "details": _signal_details(signal),
            "source": signal.get("source") or "signals.signal_pool",
            "pool_status": signal.get("pool_status"),
            "chart_aligned": aligned,
            "display_scope": display_scope,
        })
    return output


def _volume_signal_details(volume_ratio: float, volume: float, amount: Optional[float]) -> str:
    parts = [
        f"量比{volume_ratio:.2f}",
        f"成交量{volume / 1_000_000:.2f}万手",
    ]
    if amount is not None and amount > 0:
        parts.append(f"成交额{amount / 100_000_000:.2f}亿")
    return " · ".join(parts)


def _volume_signal_chart_signals(symbol: str, freq: str, chart: dict[str, Any]) -> list[dict[str, Any]]:
    chart_meta = chart.get("meta") if isinstance(chart.get("meta"), dict) else {}
    effective_freq = _freq_bucket(chart_meta.get("freq") or freq)
    market = _text(chart_meta.get("market")) or infer_market(symbol=symbol, source=_text(chart_meta.get("source")))
    source = _text(chart_meta.get("source")) or "signals"
    ohlcv = chart.get("ohlcv") if isinstance(chart.get("ohlcv"), list) else []
    rows = [row for row in ohlcv if isinstance(row, dict) and _float(row.get("volume"), 0) and row.get("time")]
    if len(rows) < 12:
        return []

    output: list[dict[str, Any]] = []
    window = 20
    start = max(1, len(rows) - 160)
    for index in range(start, len(rows)):
        history = rows[max(0, index - window):index]
        volumes = [
            volume for volume in (_float(item.get("volume"), 0) or 0 for item in history)
            if volume and volume > 0
        ]
        if len(volumes) < 10:
            continue
        current_volume = _float(rows[index].get("volume"), 0) or 0
        if current_volume <= 0:
            continue
        baseline = sum(volumes) / len(volumes)
        if baseline <= 0:
            continue
        volume_ratio = current_volume / baseline
        current_amount = _float(rows[index].get("amount"))
        close = _float(rows[index].get("close"))
        previous_close = _float(rows[index - 1].get("close")) if index > 0 else None
        ts = int(rows[index].get("time") or 0)
        if volume_ratio >= 2.2:
            direction = "上攻" if close is not None and previous_close is not None and close >= previous_close else "下跌"
            signal_type = f"成交量异常放大:{direction}"
            signal_side = "buy" if direction == "上攻" else "sell"
        elif volume_ratio <= 0.35:
            signal_type = "成交量极致缩量"
            signal_side = "neutral"
        else:
            continue
        output.append({
            "dt": ts,
            "date_str": _timestamp_date(ts, market=market, symbol=symbol, source=source),
            "type": signal_type,
            "price": close,
            "confidence": round(min(0.95, max(0.58, abs(volume_ratio - 1.0) / 2.0 + 0.55)), 4),
            "freq": effective_freq or _canonical_freq(freq),
            "details": _volume_signal_details(volume_ratio, current_volume, current_amount),
            "source": "terminal_volume_signals",
            "pool_status": "volume_warning",
            "chart_aligned": False,
            "display_scope": "current_timeframe",
            "signal_side": signal_side,
        })
    return output[-12:]


def _aligned_signal_bar(
    signal: dict[str, Any],
    *,
    signal_dt: Any,
    ts: int,
    ohlcv: list[dict[str, Any]],
    effective_freq: str,
    market: str,
    symbol: str,
    source: str,
) -> tuple[int, Optional[float], bool]:
    if effective_freq in {"daily", "monthly"} or not ohlcv:
        return ts, None, False
    bar_by_time = {
        int(row.get("time") or 0): row
        for row in ohlcv
        if isinstance(row, dict) and row.get("time")
    }
    if ts in bar_by_time:
        row = bar_by_time[ts]
        return ts, _float(row.get("close")), False

    signal_date = str(signal_dt or "")[:10]
    if not signal_date:
        return ts, None, False
    if effective_freq == "weekly":
        try:
            parsed_signal_date = pd.to_datetime(signal_date).date()
        except Exception:
            return ts, None, False
        dated_rows: list[tuple[date, dict[str, Any]]] = []
        for row in ohlcv:
            if not isinstance(row, dict) or not row.get("time"):
                continue
            row_date_text = _timestamp_date(int(row["time"]), market=market, symbol=symbol, source=source)
            if not row_date_text:
                continue
            try:
                dated_rows.append((pd.to_datetime(row_date_text).date(), row))
            except Exception:
                continue
        dated_rows.sort(key=lambda item: item[0])
        for row_date, row in dated_rows:
            if parsed_signal_date <= row_date:
                if _is_sell_signal(signal):
                    price = _float(row.get("high") or row.get("close"))
                elif _is_buy_signal(signal):
                    price = _float(row.get("low") or row.get("close"))
                else:
                    price = _float(row.get("close"))
                return int(row.get("time") or ts), price, True
        return ts, None, False
    same_day = [
        row for row in ohlcv
        if isinstance(row, dict)
        and row.get("time")
        and _timestamp_date(int(row["time"]), market=market, symbol=symbol, source=source) == signal_date
    ]
    if not same_day:
        return ts, None, False
    row = same_day[-1]
    if _is_sell_signal(signal):
        price = _float(row.get("high") or row.get("close"))
    elif _is_buy_signal(signal):
        price = _float(row.get("low") or row.get("close"))
    else:
        price = _float(row.get("close"))
    return int(row.get("time") or ts), price, True


def _merge_signal_pool_into_chart(chart: dict[str, Any], symbol: str, freq: str) -> dict[str, Any]:
    technical_signals = _terminal_technical_chart_signals(symbol, freq, chart)
    pool_signals = _signal_pool_chart_signals(symbol, freq, chart)
    volume_signals = _volume_signal_chart_signals(symbol, freq, chart)
    if not technical_signals and not pool_signals and not volume_signals:
        return chart
    existing = chart.get("signals") if isinstance(chart.get("signals"), list) else []
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*existing, *technical_signals, *pool_signals, *volume_signals]:
        if not isinstance(item, dict):
            continue
        ts = _signal_ts(item.get("dt") or item.get("time") or item.get("timestamp") or item.get("date_str") or item.get("signal_date"))
        signal_type = _text(item.get("type") or item.get("signal_type") or item.get("reason"))
        if not ts or not signal_type:
            continue
        key = f"{ts}:{signal_type}:{_freq_bucket(item.get('freq'))}"
        if key in seen:
            continue
        seen.add(key)
        normalized = dict(item)
        normalized["dt"] = ts
        normalized["type"] = signal_type
        normalized.setdefault("date_str", str(item.get("date_str") or item.get("signal_date") or "")[:10])
        normalized.setdefault("display_scope", "current_timeframe")
        merged.append(normalized)
    merged.sort(key=lambda item: int(item.get("dt") or 0))
    updated = dict(chart)
    updated["signals"] = merged[-300:]
    return updated


def _add_timeframe_signal(target: dict[str, Any], signal: dict[str, Any], *, side: str = "buy") -> None:
    metadata = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
    freq = _freq_bucket(signal.get("freq") or signal.get("timeframe") or metadata.get("freq"))
    if freq not in BUY_FREQS:
        return
    side = "sell" if side == "sell" else "buy"
    stack = target.setdefault("timeframe_signal_stack", {})
    freq_stack = stack.setdefault(freq, {})
    current = freq_stack.get(side)
    next_score = _float(signal.get("total_score") or signal.get("score") or signal.get("confidence"), 0) or 0
    current_score = _float((current or {}).get("score"), -1) if isinstance(current, dict) else -1
    if current and current_score is not None and current_score >= next_score:
        return
    signal_type = _text(signal.get("signal_type") or signal.get("type") or signal.get("reason"))
    if not signal_type:
        signal_type = "卖出预警" if side == "sell" else "买点"
    payload = {
        "freq": freq,
        "badge": _freq_badge(freq),
        "side": side,
        "signal_type": signal_type,
        "score": next_score,
        "confidence": _float(signal.get("confidence")),
        "signal_date": _signal_date(signal),
        "price": _float(signal.get("price")),
    }
    freq_stack[side] = payload
    target.setdefault("timeframe_signals" if side == "buy" else "sell_timeframe_signals", {})[freq] = payload


def _build_focus_stock_rows(
    *,
    buy_rows: list[dict[str, Any]],
    sell_rows: Optional[list[dict[str, Any]]] = None,
    decision_rows: list[dict[str, Any]],
    range_columns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows_by_symbol: dict[str, dict[str, Any]] = {}

    def ensure(row: dict[str, Any]) -> Optional[dict[str, Any]]:
        symbol = str(row.get("symbol") or row.get("code") or row.get("label") or "").strip()
        normalized, raw_code = _normalize_stock_symbol(symbol)
        if not normalized:
            return None
        key = normalized.upper()
        if key not in rows_by_symbol:
            rows_by_symbol[key] = _enrich_stock_row({
                **row,
                "symbol": normalized,
                "raw_code": raw_code,
                "kind": "stock",
            }, range_columns)
            rows_by_symbol[key]["timeframe_signals"] = {}
            rows_by_symbol[key]["sell_timeframe_signals"] = {}
            rows_by_symbol[key]["timeframe_signal_stack"] = {}
            rows_by_symbol[key]["focus_reasons"] = []
            rows_by_symbol[key]["source_tags"] = []
        else:
            rows_by_symbol[key].update({
                "score": max(
                    _float(rows_by_symbol[key].get("score"), 0) or 0,
                    _float(row.get("score") or row.get("total_score") or row.get("fused_total"), 0) or 0,
                )
            })
        reason = _text(row.get("reason") or row.get("summary") or row.get("direction"))
        if reason and reason not in rows_by_symbol[key]["focus_reasons"]:
            rows_by_symbol[key]["focus_reasons"].append(reason)
        source = _text(row.get("source") or row.get("data_source"))
        if source and source not in rows_by_symbol[key]["source_tags"]:
            rows_by_symbol[key]["source_tags"].append(source)
        return rows_by_symbol[key]

    for row in buy_rows:
        item = ensure(dict(row))
        if not item:
            continue
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        signal = {
            "signal_type": row.get("reason") or metadata.get("trigger") or row.get("signal_type"),
            "freq": metadata.get("freq") or row.get("freq"),
            "score": row.get("score"),
            "confidence": row.get("confidence") or metadata.get("confidence"),
            "signal_date": metadata.get("signal_date") or row.get("signal_date"),
            "price": row.get("latest_price") or row.get("price") or metadata.get("price"),
        }
        if _is_buy_signal(signal) or not signal.get("signal_type"):
            _add_timeframe_signal(item, signal, side="buy")
            item["action_status"] = item.get("action_status") or "buy_candidate"

    for row in sell_rows or []:
        normalized = _normalize_stock_symbol(str(row.get("symbol") or row.get("code") or row.get("label") or ""))[0]
        if not normalized or normalized.upper() not in rows_by_symbol:
            continue
        item = ensure(dict(row))
        if not item:
            continue
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        signal = {
            "signal_type": row.get("reason") or metadata.get("trigger") or row.get("signal_type") or "卖出预警",
            "freq": metadata.get("freq") or row.get("freq"),
            "score": row.get("score") or row.get("risk_score"),
            "confidence": row.get("confidence") or metadata.get("confidence"),
            "signal_date": metadata.get("signal_date") or row.get("signal_date"),
            "price": row.get("latest_price") or row.get("price") or metadata.get("price"),
        }
        if _is_sell_signal(signal) or signal.get("signal_type"):
            _add_timeframe_signal(item, signal, side="sell")
            item["action_status"] = "risk_review" if item.get("timeframe_signals") else "exit_review"

    for signal in _load_signal_pool_rows():
        side = "sell" if _is_sell_signal(signal) else "buy" if _is_buy_signal(signal) else ""
        if not side:
            continue
        symbol = _normalize_stock_symbol(str(signal.get("symbol") or ""))[0]
        if not symbol:
            continue
        if side == "sell" and symbol.upper() not in rows_by_symbol:
            continue
        item = ensure({
            "symbol": symbol,
            "name": signal.get("name"),
            "reason": signal.get("signal_type") or signal.get("type"),
            "score": signal.get("total_score") or signal.get("score") or signal.get("confidence"),
            "price": signal.get("price"),
        })
        if item:
            _add_timeframe_signal(item, signal, side=side)
            if side == "sell":
                item["action_status"] = "risk_review" if item.get("timeframe_signals") else "exit_review"
            else:
                item["action_status"] = item.get("action_status") or "buy_candidate"

    for row in decision_rows:
        if row.get("symbol"):
            item = ensure(dict(row))
            if item:
                item["decision_status"] = row.get("action") or row.get("action_label")
                if item.get("action_status") != "exit_review":
                    item["action_status"] = "manual_review"

    output = list(rows_by_symbol.values())
    for row in output:
        signals = row.get("timeframe_signals") if isinstance(row.get("timeframe_signals"), dict) else {}
        sell_signals = row.get("sell_timeframe_signals") if isinstance(row.get("sell_timeframe_signals"), dict) else {}
        row["buy_timeframes"] = [
            signals[freq]
            for freq in BUY_FREQS
            if freq in signals
        ]
        row["sell_timeframes"] = [
            sell_signals[freq]
            for freq in BUY_FREQS
            if freq in sell_signals
        ]
        row["signal_stack"] = {
            freq: row.get("timeframe_signal_stack", {}).get(freq)
            for freq in BUY_FREQS
            if isinstance(row.get("timeframe_signal_stack"), dict) and row.get("timeframe_signal_stack", {}).get(freq)
        }
        row["reason"] = " · ".join(row.get("focus_reasons", [])[:2]) or row.get("reason") or row.get("direction") or ""
        if row.get("sell_timeframes") or row.get("buy_timeframes"):
            sell_badges = [f"卖{item.get('badge') or item.get('freq') or ''}" for item in row.get("sell_timeframes", []) if isinstance(item, dict)]
            buy_badges = [item.get("badge") or item.get("freq") or "" for item in row.get("buy_timeframes", []) if isinstance(item, dict)]
            row["latest_signal"] = "/".join([badge for badge in sell_badges + buy_badges if badge])
        elif row.get("reason"):
            row["latest_signal"] = row["reason"]
        if row.get("action_status") == "exit_review":
            trader_action = "减仓/止盈"
            invalidates_when = "重新站回关键均线且卖出信号解除"
        elif row.get("action_status") == "risk_review":
            trader_action = "风险复核"
            invalidates_when = "买点延续但卖出/风险信号解除"
        elif any(item.get("badge") == "5m" for item in row.get("buy_timeframes", []) if isinstance(item, dict)):
            trader_action = "可试仓"
            invalidates_when = "5m 买点失效或跌破短线防守位"
        elif row.get("buy_timeframes"):
            trader_action = "等待5m确认"
            invalidates_when = "5m 无法确认或上级周期转弱"
        elif row.get("action_status") == "manual_review":
            trader_action = "观察"
            invalidates_when = "人工复核条件不再成立"
        else:
            trader_action = "观察"
            invalidates_when = "异动消退或跌破对应周期关键位"
        row.update({
            "lane": "signal_lane",
            "second_screen_role": "actionable_focus_stock",
            "trader_action": trader_action,
            "invalidates_when": invalidates_when,
        })
    output = [row for row in output if row.get("action_status") != "exit_review"]
    output.sort(
        key=lambda item: (
            3 if item.get("action_status") == "exit_review" else 2 if item.get("buy_timeframes") else 1 if item.get("action_status") == "manual_review" else 0,
            len(item.get("sell_timeframes") or []) + len(item.get("buy_timeframes") or []),
            _float(item.get("score") or item.get("total_score") or item.get("fused_total"), 0) or 0,
        ),
        reverse=True,
    )
    return output[:24]


def _build_macro_index_rows(
    *,
    reports: list[dict[str, Any]],
    range_columns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    reports_by_name = {str(report.get("name") or report.get("label") or ""): report for report in reports}
    reports_by_symbol = {str(report.get("symbol") or report.get("code") or "").lower(): report for report in reports}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in MINGDAO_MACRO_WATCHLIST:
        name = _text(item.get("name"))
        symbol = _text(item.get("symbol"))
        kind = _text(item.get("kind")) or "index"
        if not name or not symbol:
            continue
        key = f"{kind}:{symbol.lower()}"
        if key in seen:
            continue
        seen.add(key)
        row = dict(reports_by_name.get(name) or reports_by_symbol.get(symbol.lower()) or {
            "name": name,
            "label": name,
            "symbol": symbol,
            "code": symbol,
        })
        row.setdefault("name", name)
        row.setdefault("label", name)
        row.setdefault("symbol", symbol)
        if kind == "stock":
            enriched = _enrich_stock_row({
                **row,
                "name": name,
                "label": symbol,
                "symbol": symbol,
                "kind": "stock",
            }, range_columns)
            if not enriched.get("latest_price"):
                continue
            target_kind = "stock"
            target_label = enriched.get("symbol") or symbol
            target_symbol = enriched.get("symbol") or symbol
        else:
            enriched = _enrich_index_row(row, range_columns)
            if not enriched.get("latest_price"):
                continue
            target_kind = "index"
            target_label = name
            target_symbol = symbol
        enriched.update({
            "group": "macro_indices",
            "lane": "quote_lane",
            "second_screen_role": "market_direction_anchor",
            "action_status": "观察",
            "trader_action": "观察关键指数方向和主题共振",
            "invalidates_when": "指数跌破对应周期防守均线或主题扩散失败",
            "theme_tags": MINGDAO_INDEX_THEMES.get(name, []),
            "latest_signal": (
                _signal_or_fallback(row, _index_df(symbol, "daily")[0] if kind != "stock" else _stock_df(str(target_symbol), "daily")[0])
            ),
            "signal_stack": {
                "daily": row.get("daily_latest_signal") or "",
                "30min": row.get("f30_latest_signal") or "",
                "15min": row.get("f15_latest_signal") or "",
            },
            "target_kind": target_kind,
            "target_label": target_label,
            "target_symbol": target_symbol,
            "target_freq": DEFAULT_TERMINAL_FREQ,
        })
        rows.append(enriched)
    return rows


def _preview_carrier(candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    priority = {
        "core": 5,
        "elastic": 4,
        "semantic_industry_chain": 3,
        "industry_leader": 2,
        "source_leader": 1,
    }
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for item in candidates:
        symbol = _text(item.get("symbol"))
        if not symbol:
            continue
        rep_type = _text(item.get("representative_type")) or _text(item.get("source"))
        ranked.append((priority.get(rep_type, 0), int(item.get("priority") or 0), item))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return ranked[0][2] if ranked else None


def _industry_carrier_candidates(name: str, leader_name: str = "") -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def add(item: dict[str, Any]) -> None:
        symbol = _text(item.get("symbol"))
        raw_code = _text(item.get("raw_code"))
        if not symbol:
            symbol, raw_code = _stock_symbol_from_code_or_name(item.get("code"), item.get("name"))
        if not symbol:
            return
        if any(_text(existing.get("symbol")).upper() == symbol.upper() for existing in candidates):
            return
        candidates.append({
            **item,
            "symbol": symbol,
            "raw_code": raw_code or symbol.split(".", 1)[-1],
            "name": _text(item.get("name")) or _stock_name(symbol),
        })

    if leader_name:
        add({
            "name": leader_name,
            "source": "source_leader",
            "representative_type": "source_leader",
            "relation": name,
            "priority": 32,
        })
    leader = _industry_leader_candidate(name)
    if leader:
        add({**leader, "representative_type": "industry_leader"})
    for item in _preferred_concept_carriers(name, [], [name]):
        add(item)
    for symbol in _industry_constituent_symbols(name):
        add({
            "symbol": symbol,
            "source": "industry_constituents",
            "representative_type": "industry_constituent",
            "relation": name,
            "priority": 8,
        })
    return candidates


def _candidate_symbol_fields(item: dict[str, Any]) -> tuple[str, str]:
    symbol = _text(item.get("symbol"))
    raw_code = _text(item.get("raw_code"))
    if not symbol:
        symbol, raw_code = _stock_symbol_from_code_or_name(item.get("code"), item.get("name"))
    if symbol and not raw_code:
        raw_code = symbol.split(".", 1)[-1]
    return symbol, raw_code


def _score_0_100(value: Any, default: float = 0) -> float:
    number = _float(value)
    if number is None:
        return default
    if 0 <= number <= 1:
        return round(number * 100, 2)
    return round(max(0, min(100, number)), 2)


def _board_heat_score(change_pct: Any) -> float:
    value = _float(change_pct, 0) or 0
    return round(max(0, min(100, value * 12.5)), 2)


def _source_confidence_score(item: dict[str, Any]) -> float:
    if item.get("confidence") is not None:
        return _score_0_100(item.get("confidence"), 65)
    source = _text(item.get("source"))
    rep_type = _text(item.get("representative_type"))
    if source == "semantic_industry_chain" or rep_type in {"core", "elastic"}:
        return 90
    if source in {"concept_rank", "concept_sina", "concept_em", "concept_ths", "strategy_snapshot"}:
        return 78
    if source in {"industry_leader_map", "industry_candidates"}:
        return 72
    return 58


def _role_score(item: dict[str, Any], leader_rank: int = 0) -> float:
    rep_type = _text(item.get("representative_type"))
    source = _text(item.get("source"))
    if rep_type == "core":
        return max(82, 102 - leader_rank * 6)
    if source == "industry_leader_map" or rep_type == "industry_leader":
        return 86
    if rep_type == "elastic":
        return 74
    if rep_type == "source_leader":
        return 80
    if rep_type in {"industry_constituent", "industry_candidate"}:
        return 48
    return 55


def _daily_df_for_candidate(
    symbol: str,
    daily_cache: Optional[dict[str, tuple[pd.DataFrame, str]]] = None,
) -> tuple[pd.DataFrame, str]:
    cache_key = _text(symbol).upper()
    if daily_cache is not None and cache_key in daily_cache:
        return daily_cache[cache_key]
    df, source = _stock_df(symbol, "daily")
    if daily_cache is not None and cache_key:
        daily_cache[cache_key] = (df, source)
    return df, source


def _trend_score_for_candidate(
    item: dict[str, Any],
    symbol: str,
    *,
    daily_cache: Optional[dict[str, tuple[pd.DataFrame, str]]] = None,
) -> tuple[float, Optional[float], str]:
    explicit = _float(item.get("score") or item.get("total_score") or item.get("fused_total"))
    if explicit is not None:
        return _score_0_100(explicit, 50), _float(item.get("day_change_pct")), _text(item.get("latest_signal"))
    day_change = _float(item.get("day_change_pct") or item.get("daily_change_pct") or item.get("change_pct"))
    latest_signal = _text(item.get("latest_signal") or item.get("signal") or item.get("reason"))
    if day_change is None and symbol:
        df, _ = _daily_df_for_candidate(symbol, daily_cache)
        day_change = _compute_day_change_pct(df)
        if not latest_signal:
            latest_signal = _ma_signal_from_df(df)
    if latest_signal:
        if any(token in latest_signal for token in ("买", "突破", "多头", "站上", "强")):
            base = 72
        elif any(token in latest_signal for token in ("卖", "跌破", "走弱", "退潮")):
            base = 32
        else:
            base = 55
    else:
        base = 50
    if day_change is not None:
        base += max(-20, min(25, day_change * 4))
    return round(max(0, min(100, base)), 2), day_change, latest_signal


def _candidate_leader_tier(item: dict[str, Any], leader_rank: int) -> str:
    rep_type = _text(item.get("representative_type"))
    source = _text(item.get("source"))
    if rep_type == "core":
        return ["龙头", "龙二", "龙三"][min(max(leader_rank - 1, 0), 2)]
    if source == "industry_leader_map" or rep_type == "industry_leader":
        return "行业龙头"
    if rep_type == "elastic":
        return "弹性"
    if rep_type == "source_leader":
        return "当日领涨"
    if rep_type in {"industry_constituent", "industry_candidate"}:
        return "成分候选"
    return "观察"


def _scored_candidate_payload(
    item: dict[str, Any],
    *,
    heat_score: float,
    leader_rank: int = 0,
    daily_cache: Optional[dict[str, tuple[pd.DataFrame, str]]] = None,
) -> Optional[dict[str, Any]]:
    symbol, raw_code = _candidate_symbol_fields(item)
    if not symbol:
        return None
    trend_score, day_change, latest_signal = _trend_score_for_candidate(item, symbol, daily_cache=daily_cache)
    role_score = _role_score(item, leader_rank)
    confidence = _source_confidence_score(item)
    weight_score = round(max(role_score, _score_0_100(item.get("priority"), 0)), 2)
    elasticity_score = round(
        max(
            82 if _text(item.get("representative_type")) == "elastic" else 0,
            min(100, 50 + (day_change or 0) * 6),
        ),
        2,
    )
    attention_score = round(
        heat_score * 0.35 + trend_score * 0.35 + role_score * 0.2 + confidence * 0.1,
        2,
    )
    leader_tier = _candidate_leader_tier(item, leader_rank)
    chain_role = _text(item.get("relation") or item.get("node_name") or item.get("representative_type"))
    risk_flags = []
    if not _text(item.get("bar_source")):
        df, source = _daily_df_for_candidate(symbol, daily_cache)
        if source:
            item = {**item, "bar_source": source, "bar_count": len(df)}
        else:
            risk_flags.append("K线未预热")
    if _text(item.get("source")) in {"industry_constituents", "industry_candidates"}:
        risk_flags.append("仅成分股证据")
    return {
        **_representative_payload({**item, "symbol": symbol, "raw_code": raw_code}),
        "code": symbol,
        "leader_tier": leader_tier,
        "chain_role": chain_role,
        "weight_score": weight_score,
        "elasticity_score": elasticity_score,
        "attention_score": attention_score,
        "trend_score": trend_score,
        "heat_score": heat_score,
        "source_confidence": confidence,
        "day_change_pct": day_change,
        "latest_signal": latest_signal,
        "why_watch": " · ".join([
            leader_tier,
            chain_role,
            _text(item.get("source_note") or item.get("source")),
        ]).strip(" ·"),
        "risk_flags": risk_flags,
        "invalidates_when": "板块热度回落、当日领涨股走弱或标的跌破短线防守位",
    }


def _candidate_score_limit() -> int:
    try:
        return max(20, int(os.getenv("WORKBENCH_CANDIDATE_SCORE_LIMIT", "64")))
    except Exception:
        return 64


def _candidate_prefilter_rank(item: dict[str, Any], index: int) -> tuple[float, float, float, int]:
    rep_type = _text(item.get("representative_type"))
    source = _text(item.get("source"))
    source_rank = {
        "core": 100,
        "industry_leader": 92,
        "source_leader": 88,
        "elastic": 82,
        "semantic_industry_chain": 78,
        "industry_candidate": 46,
        "industry_constituent": 42,
        "concept_constituent": 42,
    }.get(rep_type, 0)
    if source_rank == 0:
        source_rank = {
            "industry_leader_map": 90,
            "concept_rank": 84,
            "concept_ranking": 84,
            "concept_sina": 82,
            "concept_em": 82,
            "concept_ths": 82,
            "strategy_snapshot": 78,
            "semantic_industry_chain": 76,
            "industry_candidates": 44,
            "industry_constituents": 40,
            "concept_constituents": 40,
        }.get(source, 50)
    priority = _float(item.get("priority"), 0) or 0
    confidence = _source_confidence_score(item)
    return source_rank, priority, confidence, -index


def _prioritized_candidate_inputs(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    limit = _candidate_score_limit()
    ranked: list[tuple[tuple[float, float, float, int], dict[str, Any]]] = []
    seen: set[str] = set()
    for index, item in enumerate(candidates):
        symbol, _ = _candidate_symbol_fields(item)
        key = (_text(symbol).upper() or f"{_text(item.get('name'))}|{_text(item.get('source'))}|{index}")
        if key in seen:
            continue
        seen.add(key)
        ranked.append((_candidate_prefilter_rank(item, index), item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in ranked[:limit]]


def _candidate_groups(
    candidates: list[dict[str, Any]],
    *,
    heat_value: Any = None,
) -> dict[str, list[dict[str, Any]]]:
    heat_score = _board_heat_score(heat_value)
    groups: dict[str, list[dict[str, Any]]] = {
        "leaders": [],
        "weighted": [],
        "elastic": [],
        "source_leaders": [],
        "constituents": [],
    }
    core_rank = 0
    daily_cache: dict[str, tuple[pd.DataFrame, str]] = {}
    for item in _prioritized_candidate_inputs(candidates):
        rep_type = _text(item.get("representative_type"))
        source = _text(item.get("source"))
        leader_rank = 0
        if rep_type == "core":
            core_rank += 1
            leader_rank = core_rank
        payload = _scored_candidate_payload(item, heat_score=heat_score, leader_rank=leader_rank, daily_cache=daily_cache)
        if not payload:
            continue
        if rep_type == "core":
            groups["leaders"].append(payload)
            groups["weighted"].append(payload)
        elif source == "industry_leader_map" or rep_type == "industry_leader":
            groups["leaders"].append(payload)
            groups["weighted"].append(payload)
        elif rep_type == "elastic":
            groups["elastic"].append(payload)
        elif rep_type == "source_leader":
            groups["source_leaders"].append(payload)
        else:
            groups["constituents"].append(payload)
    for key, rows in groups.items():
        rows.sort(key=lambda item: _float(item.get("attention_score"), 0) or 0, reverse=True)
        groups[key] = rows[:8 if key != "leaders" else 3]
    return groups


def _flatten_candidate_groups(groups: dict[str, list[dict[str, Any]]], limit: int = 20) -> list[dict[str, Any]]:
    ordered_keys = ["leaders", "weighted", "elastic", "source_leaders", "constituents"]
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in ordered_keys:
        for item in groups.get(key) or []:
            symbol = _text(item.get("symbol")).upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            output.append(item)
            if len(output) >= limit:
                return output
    return output


def _mapping_chain_from_carrier(name: str, carrier: Optional[dict[str, Any]], *, kind: str) -> dict[str, Any]:
    if not carrier:
        return {
            "query": name,
            "domain": kind,
            "chain_id": None,
            "chain_name": "",
            "node_id": "",
            "node_name": "",
            "layer": "",
            "confidence": 0,
            "evidence_sources": [],
        }
    return {
        "query": name,
        "domain": kind,
        "chain_id": carrier.get("chain_id"),
        "chain_name": carrier.get("chain_name") or "",
        "node_id": carrier.get("node_id") or "",
        "node_name": carrier.get("node_name") or "",
        "layer": carrier.get("layer") or "",
        "stage": carrier.get("stage") or "",
        "confidence": carrier.get("confidence"),
        "evidence_sources": carrier.get("evidence_sources") or [carrier.get("source") or ""],
        "carrier": _representative_payload(carrier),
    }


def _sector_board_preview(row: dict[str, Any], kind: str) -> dict[str, Any]:
    enriched = _enrich_cluster_row(row, kind)
    label = str(enriched.get("label") or enriched.get("name") or "").strip()
    heat_resolution = resolve_board_heat_name(kind, label)
    heat_target_label = heat_resolution.get("heat_name") or label
    non_chain = non_chain_reason(label) if kind == "concept" else ""
    leader = _text(
        enriched.get("leader")
        or enriched.get("leader_name")
        or enriched.get("leading_stock")
        or enriched.get("leading_name")
    )
    candidates: list[dict[str, Any]] = []
    representatives: dict[str, list[dict[str, Any]]] = {"core": [], "elastic": [], "source_leader": []}
    if label:
        if kind == "concept":
            theme_candidates = _concept_theme_candidates(label)
            related = []
            try:
                from signals.layers.industry import _map_concept_to_industries

                for industry in _map_concept_to_industries(label):
                    if industry not in related:
                        related.append(industry)
            except Exception:
                related = []
            candidates = _concept_carrier_candidates(label, theme_candidates, related)
            representatives = _concept_representative_groups(candidates)
        else:
            candidates = _industry_carrier_candidates(label, leader)
    carrier = None if non_chain else (_cached_daily_carrier(candidates) or _preview_carrier(candidates))
    carrier_payload = _representative_payload(carrier) if carrier else {}
    candidate_groups = _candidate_groups(candidates, heat_value=enriched.get("change_pct") or enriched.get("gain_pct") or enriched.get("strength"))
    focus_stocks_preview = _flatten_candidate_groups(candidate_groups, limit=6)
    carrier_range_returns: dict[str, Optional[float]] = {}
    carrier_latest_price: Optional[float] = None
    carrier_day_change: Optional[float] = None
    carrier_range_source = ""
    if carrier_payload.get("symbol"):
        carrier_df, carrier_range_source = _stock_df(str(carrier_payload["symbol"]), "daily")
        carrier_range_returns = _compute_range_returns(carrier_df, _watchlist_range_columns())
        carrier_latest_price = (
            float(carrier_df["close"].iloc[-1])
            if carrier_df is not None and not carrier_df.empty and "close" in carrier_df.columns
            else None
        )
        carrier_day_change = _compute_day_change_pct(carrier_df)
    board_day_change, board_day_as_of = _latest_board_heat_day_change(kind, heat_target_label)
    board_day_change_source = "board_heat_ticks" if board_day_change is not None else ""
    board_range_returns = enriched.get("range_returns") or {}
    carrier_name = carrier_payload.get("name") or carrier_payload.get("symbol") or ""
    action_status = "观察" if carrier_payload else "退出复盘"
    explanation_parts = [
        f"{label} 异动" if label else "",
        f"当日领涨 {leader}" if leader else "",
        f"链主代表 {carrier_name}" if carrier_name else "暂无链主代表",
    ]
    latest_signal = (
        enriched.get("latest_signal")
        or non_chain
        or (f"链主{carrier_payload.get('name')}" if carrier_payload.get("name") else "待映射")
    )
    enriched.update({
        "group": "sector_boards",
        "domain": "concept" if kind == "concept" else "board",
        "chain_id": carrier_payload.get("chain_id") or "",
        "chain_name": carrier_payload.get("chain_name") or "",
        "node_id": carrier_payload.get("node_id") or "",
        "node_name": carrier_payload.get("node_name") or "",
        "integrated_domains": [{
            "kind": kind,
            "domain": "concept" if kind == "concept" else "board",
            "label": label,
            "change_pct": board_day_change,
            "leader": leader,
            "source": enriched.get("source") or enriched.get("data_source") or "",
        }],
        "evidence_sources": list(dict.fromkeys([
            *(_representative_payload(carrier).get("evidence_sources") if carrier else []),
            _text(enriched.get("source") or enriched.get("data_source")),
        ])),
        "non_chain_reason": non_chain,
        "lane": "board_lane",
        "second_screen_role": "hot_sector_explanation",
        "action_status": "非产业链观察" if non_chain else action_status,
        "trader_action": "仅观察事件/指数主题" if non_chain else ("观察板块扩散和链主/弹性代表" if carrier_payload else "退出复盘"),
        "invalidates_when": "事件窗口结束或指数样本主题热度回落" if non_chain else "当日领涨股走弱、板块排名回落或链主代表跌破短线防守位",
        "explanation": " · ".join([part for part in explanation_parts if part]),
        "leader": leader,
        "source": enriched.get("source") or enriched.get("data_source") or "",
        "latest_price": enriched.get("latest_price"),
        "day_change_pct": board_day_change,
        "daily_change_pct": board_day_change,
        "day_change_source": board_day_change_source,
        "day_change_mode": _a_day_change_mode(),
        "day_change_as_of": board_day_as_of,
        "range_returns": board_range_returns,
        "range_return_source": enriched.get("range_return_source") or "",
        "range_return_status": "board_kline" if board_range_returns else "board_kline_missing",
        "carrier_latest_price": carrier_latest_price,
        "carrier_day_change_pct": carrier_day_change,
        "carrier_range_returns": carrier_range_returns,
        "carrier_range_return_source": "carrier_stock" if carrier_range_returns else "",
        "carrier_range_return_symbol": carrier_payload.get("symbol") or "",
        "chart_target_status": "non_chain" if non_chain else ("carrier_stock" if carrier_payload else "unmapped"),
        "latest_signal": latest_signal,
        "target_kind": kind,
        "target_label": heat_target_label,
        "target_symbol": heat_target_label,
        "target_freq": DEFAULT_TERMINAL_FREQ,
        "display_label": label,
        "heat_target_label": heat_target_label,
        "heat_resolution_status": heat_resolution.get("status", ""),
        "fallback_target": {
            "kind": "stock",
            "label": carrier_payload.get("symbol"),
            "symbol": carrier_payload.get("symbol"),
            "name": carrier_payload.get("name"),
            "reason": "chain_core_representative" if carrier_payload else "",
        } if carrier_payload else {},
        "carrier": carrier_payload,
        "representatives": representatives,
        "candidate_groups": candidate_groups,
        "focus_stocks_preview": focus_stocks_preview,
        "mapping_chain": _mapping_chain_from_carrier(label, carrier, kind=kind),
    })
    return enriched


def _merge_candidate_groups(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {
        "leaders": [],
        "weighted": [],
        "elastic": [],
        "source_leaders": [],
        "constituents": [],
    }
    for row in rows:
        source_groups = row.get("candidate_groups") if isinstance(row.get("candidate_groups"), dict) else {}
        for key in groups:
            for item in source_groups.get(key) or []:
                if isinstance(item, dict):
                    groups[key].append(item)
    for key, values in groups.items():
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in sorted(values, key=lambda value: _float(value.get("attention_score"), 0) or 0, reverse=True):
            symbol = _text(item.get("symbol")).upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            deduped.append(item)
        groups[key] = deduped[:8 if key != "leaders" else 3]
    return groups


def _sector_group_key(item: dict[str, Any]) -> str:
    chain_id = _text(item.get("chain_id"))
    node_id = _text(item.get("node_id"))
    if chain_id:
        return f"chain:{chain_id}:{node_id or 'default'}"
    return f"{item.get('target_kind') or item.get('kind')}:{item.get('target_label') or item.get('label')}"


def _aggregate_sector_board_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in rows:
        buckets.setdefault(_sector_group_key(item), []).append(item)

    aggregated: list[dict[str, Any]] = []
    for items in buckets.values():
        items.sort(key=lambda item: _float(item.get("day_change_pct"), -999), reverse=True)
        base = dict(items[0])
        chain_name = _text(base.get("chain_name"))
        node_name = _text(base.get("node_name"))
        if chain_name:
            base["label"] = f"{chain_name} · {node_name}" if node_name else chain_name
            base["name"] = base["label"]
        integrated_domains: list[dict[str, Any]] = []
        evidence_sources: list[str] = []
        leaders: list[str] = []
        for item in items:
            domains = item.get("integrated_domains") if isinstance(item.get("integrated_domains"), list) else []
            integrated_domains.extend([dict(domain) for domain in domains if isinstance(domain, dict)])
            evidence_sources.extend([_text(source) for source in item.get("evidence_sources") or []])
            leader = _text(item.get("leader"))
            if leader and leader not in leaders:
                leaders.append(leader)
        candidate_groups = _merge_candidate_groups(items)
        base["integrated_domains"] = integrated_domains
        base["evidence_sources"] = [item for item in dict.fromkeys(evidence_sources) if item]
        base["focus_stocks_preview"] = _flatten_candidate_groups(candidate_groups, limit=6)
        base["candidate_groups"] = candidate_groups
        base["integrated_count"] = len(integrated_domains)
        base["leader"] = " / ".join(leaders[:2]) if leaders else base.get("leader", "")
        if chain_name:
            source_labels = [_text(item.get("label")) for item in integrated_domains]
            source_labels = [item for item in dict.fromkeys(source_labels) if item]
            base["explanation"] = " · ".join([
                f"{chain_name} 聚合",
                f"节点 {node_name}" if node_name else "",
                f"来源 {'/'.join(source_labels[:3])}" if source_labels else "",
            ]).strip(" ·")
            base["trader_action"] = "观察产业链共振和链主/弹性代表"
        aggregated.append(base)
    aggregated.sort(key=lambda item: _float(item.get("day_change_pct"), -999), reverse=True)
    return aggregated


def _build_sector_board_rows(
    *,
    industry_top: list[dict[str, Any]],
    concept_top: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind, source_rows in (("industry", industry_top), ("concept", concept_top)):
        for row in source_rows[:8]:
            item = _sector_board_preview(dict(row), kind)
            label = _text(item.get("label"))
            if not label:
                continue
            key = f"{kind}:{label}"
            if key in seen:
                continue
            seen.add(key)
            rows.append(item)
    return _aggregate_sector_board_rows(rows)[:16]


def _latest_freshness_doc(collection: str, *, domain: str = "", market: str = "A") -> dict[str, Any]:
    try:
        db = _mongo_db()
        query: dict[str, Any] = {"collection": collection, "market": market}
        if domain:
            query["domain"] = domain
        doc = db["data_freshness"].find_one(query, {"_id": 0}, sort=[("updated_at", -1)])
        return dict(doc or {})
    except Exception:
        return {}


def _data_truth_payload(
    *,
    collection: str,
    domain: str = "",
    source: str = "",
    chart_meta: Optional[dict[str, Any]] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    meta = chart_meta if isinstance(chart_meta, dict) else {}
    freshness = _latest_freshness_doc(collection, domain=domain)
    requested = _text(meta.get("requested_freq") or (extra or {}).get("requested_freq"))
    effective = _text(meta.get("effective_freq") or meta.get("freq") or (extra or {}).get("effective_freq"))
    return {
        "collection": collection,
        "source": source or _text(meta.get("source")) or _text(freshness.get("source")),
        "freshness": _text(freshness.get("freshness")) or _text(meta.get("cache_status")),
        "as_of": _text(meta.get("as_of") or meta.get("data_as_of") or freshness.get("as_of")),
        "latest_bar_time": _text(meta.get("latest_bar_time") or freshness.get("latest_dt")),
        "requested_freq": requested,
        "effective_freq": effective,
        "freq_fallback": bool(requested and effective and requested != effective),
        "mapping_status": _text((extra or {}).get("mapping_status") or meta.get("heat_resolution_status")),
        "stale_reason": _text(freshness.get("stale_reason") or meta.get("not_ready_reason")),
        **(extra or {}),
    }


def _chain_graph_doc(chain_id: Any, node_id: Any = None) -> dict[str, Any]:
    chain_key = _text(chain_id)
    if not chain_key:
        return {}
    try:
        db = _mongo_db()
        query: dict[str, Any] = {"market": "A", "chain_id": chain_key}
        node_key = _text(node_id)
        if node_key:
            query["node_id"] = node_key
        doc = db["concept_relationship_graph"].find_one(query, {"_id": 0}, sort=[("updated_at", -1)])
        return dict(doc or {})
    except Exception:
        return {}


def _viewpoint_context_from_graph(graph: dict[str, Any]) -> dict[str, Any]:
    rows = graph.get("viewpoint_context") if isinstance(graph.get("viewpoint_context"), list) else []
    output: dict[str, Any] = {
        "status": "context_only",
        "items": rows[:8],
        "pangge": None,
        "daozhang": None,
        "conflicts": [],
    }
    for item in rows:
        if not isinstance(item, dict):
            continue
        author = _text(item.get("author"))
        if author == "pangge" and output["pangge"] is None:
            output["pangge"] = item
        elif author == "daozhang" and output["daozhang"] is None:
            output["daozhang"] = item
        if _text(item.get("stance")) in {"block", "downgrade", "conflict"}:
            output["conflicts"].append(item)
    if output["pangge"] and output["daozhang"]:
        output["status"] = "dual_context"
    elif output["items"]:
        output["status"] = "single_context"
    return output


def _technical_linkage_from_groups(groups: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    rows = _flatten_candidate_groups(groups, limit=16)
    buy = 0
    sell = 0
    neutral = 0
    items: list[dict[str, Any]] = []
    for row in rows:
        signal = _text(row.get("latest_signal"))
        side = "neutral"
        if any(token in signal for token in ("买", "突破", "多头", "站上", "强")):
            side = "buy"
            buy += 1
        elif any(token in signal for token in ("卖", "跌破", "走弱", "退潮", "风险")):
            side = "sell"
            sell += 1
        else:
            neutral += 1
        items.append({
            "symbol": row.get("symbol") or row.get("code"),
            "name": row.get("name"),
            "role": row.get("chain_role") or row.get("leader_tier") or row.get("representative_type"),
            "latest_signal": signal,
            "signal_side": side,
            "day_change_pct": row.get("day_change_pct"),
            "attention_score": row.get("attention_score"),
            "risk_flags": row.get("risk_flags") or [],
        })
    grade = "conflict" if buy and sell else "confirmed" if buy else "risk" if sell else "watch"
    return {
        "grade": grade,
        "buy_count": buy,
        "sell_count": sell,
        "neutral_count": neutral,
        "items": items[:12],
        "summary": f"同向买点 {buy} / 风险 {sell} / 观察 {neutral}",
    }


def _chain_risk_flags(row: dict[str, Any], data_truth: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    phase = _text(row.get("phase"))
    if phase in {"diverging", "risk_off", "cooling"}:
        flags.append(phase)
    confidence = _float(row.get("mapping_confidence"))
    if confidence is not None and confidence < 65:
        flags.append("mapping_confidence_low")
    if _text(data_truth.get("freshness")) in {"stale", "empty", "missing", "degraded"}:
        flags.append("data_stale")
    if data_truth.get("freq_fallback"):
        flags.append("freq_fallback")
    if _float(row.get("up_count"), 0) > 0 and _float(row.get("down_count"), 0) > 0 and _float(row.get("up_count"), 0) < _float(row.get("down_count"), 0):
        flags.append("breadth_weak")
    return flags


def _candidate_groups_from_representatives(row: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {
        "leaders": [],
        "weighted": [],
        "elastic": [],
        "source_leaders": [],
        "constituents": [],
    }
    heat_score = _float(row.get("heat_score"), 0) or 0
    for rep in row.get("representatives") or []:
        if not isinstance(rep, dict):
            continue
        item = {
            "symbol": rep.get("symbol"),
            "name": rep.get("name"),
            "relation": rep.get("relation"),
            "source": "chain_heat_snapshots",
            "representative_type": rep.get("representative_type"),
            "attention_score": heat_score + _float(rep.get("priority"), 0) * 0.1,
            "chain_id": row.get("chain_id"),
            "chain_name": row.get("chain_name"),
            "node_id": row.get("node_id"),
            "node_name": row.get("node_name"),
            "layer": row.get("layer"),
            "stage": row.get("stage"),
        }
        payload = _scored_candidate_payload(item, heat_score=heat_score) or item
        if rep.get("representative_type") == "core":
            groups["leaders"].append(payload)
            groups["weighted"].append(payload)
        else:
            groups["elastic"].append(payload)
    for key, rows in groups.items():
        rows.sort(key=lambda item: _float(item.get("attention_score"), 0) or 0, reverse=True)
        groups[key] = rows[:8 if key != "leaders" else 3]
    return groups


def _chain_heat_sector_rows(limit: int = 16) -> list[dict[str, Any]]:
    try:
        db = _mongo_db()
        expected_day = _day_change_expected_day()
        day_start = datetime.fromisoformat(expected_day)
        day_end = day_start + timedelta(days=1)
        expected_query = {
            "market": "A",
            "$or": [
                {"trade_date": expected_day},
                {"dt": {"$gte": day_start, "$lt": day_end}},
                {"trade_minute": {"$gte": day_start, "$lt": day_end}},
            ],
        }
        latest = db["chain_heat_snapshots"].find_one(expected_query, {"trade_minute": 1}, sort=[("trade_minute", -1)])
        if not latest:
            latest = db["chain_heat_snapshots"].find_one({"market": "A"}, {"trade_minute": 1}, sort=[("trade_minute", -1)])
        if not latest or latest.get("trade_minute") is None:
            return []
        docs = list(db["chain_heat_snapshots"].find(
            {"market": "A", "trade_minute": latest["trade_minute"]},
            {"_id": 0},
        ).sort("rank", 1).limit(limit * 3))
        deduped_docs: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()
        for doc in docs:
            key = (_text(doc.get("chain_id")), _text(doc.get("node_id")) or "default")
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped_docs.append(doc)
        docs = deduped_docs[:limit]
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for doc in docs:
        integrated = doc.get("integrated_domains") if isinstance(doc.get("integrated_domains"), list) else []
        primary = integrated[0] if integrated and isinstance(integrated[0], dict) else {}
        target_kind = _text(primary.get("kind")) or "industry"
        target_label = _text(primary.get("name")) or _text(doc.get("node_name") or doc.get("chain_name"))
        label = " · ".join([item for item in [_text(doc.get("chain_name")), _text(doc.get("node_name"))] if item])
        candidate_groups = _candidate_groups_from_representatives(doc)
        graph = _chain_graph_doc(doc.get("chain_id"), doc.get("node_id"))
        viewpoint_context = _viewpoint_context_from_graph(graph)
        doc_trade_day = _date_text(doc.get("trade_date") or doc.get("dt") or doc.get("trade_minute"))
        data_truth = _data_truth_payload(
            collection="chain_heat_snapshots",
            domain="chain_heat",
            source="chain_heat_snapshots",
            extra={
                "as_of": doc_trade_day,
                "latest_bar_time": _iso_dt(doc.get("trade_minute")),
                "mapping_status": _text(doc.get("mapping_status")) or "mapped",
                "chart_mode_default": "chain_heat",
                "chain_key": _text(doc.get("chain_id")),
                "node_key": _text(doc.get("node_id")),
            },
        )
        technical_linkage = _technical_linkage_from_groups(candidate_groups)
        risk_flags = _chain_risk_flags(doc, data_truth)
        carrier = (candidate_groups.get("leaders") or candidate_groups.get("elastic") or [{}])[0]
        day_change_as_of = doc_trade_day
        day_change_pct = _float(doc.get("change_pct")) if day_change_as_of == _day_change_expected_day() else None
        row = {
            **doc,
            "group": "sector_boards",
            "domain": "chain_heat",
            "kind": target_kind,
            "label": label or target_label,
            "name": label or target_label,
            "code": _text(doc.get("chain_id")),
            "latest_price": doc.get("heat_score"),
            "day_change_pct": day_change_pct,
            "daily_change_pct": day_change_pct,
            "day_change_source": "chain_heat_snapshots" if day_change_pct is not None else "",
            "day_change_mode": _a_day_change_mode(),
            "day_change_as_of": day_change_as_of,
            "range_returns": {
                "momentum_5m": doc.get("momentum_5m"),
                "momentum_15m": doc.get("momentum_15m"),
                "momentum_30m": doc.get("momentum_30m"),
            },
            "range_return_source": "chain_heat_snapshots",
            "lane": "board_lane",
            "second_screen_role": "chain_heat_map",
            "action_status": doc.get("phase"),
            "trader_action": doc.get("trader_action"),
            "invalidates_when": doc.get("invalidates_when"),
            "explanation": " · ".join([
                _text(doc.get("range_pattern")),
                f"热度 {doc.get('heat_score')}",
                f"来源 {doc.get('integrated_count')} 个行业/概念",
            ]),
            "source": "chain_heat_snapshots",
            "latest_signal": doc.get("trading_signal"),
            "target_kind": target_kind,
            "target_label": target_label,
            "target_symbol": target_label,
            "target_freq": DEFAULT_TERMINAL_FREQ,
            "display_label": label or target_label,
            "chain_key": _text(doc.get("chain_id")),
            "node_key": _text(doc.get("node_id")),
            "chart_mode_default": "chain_heat",
            "heat_target_label": target_label,
            "heat_resolution_status": "chain_primary_domain",
            "carrier": carrier,
            "representatives": {
                "core": candidate_groups.get("leaders", []),
                "elastic": candidate_groups.get("elastic", []),
                "source_leader": [],
            },
            "candidate_groups": candidate_groups,
            "focus_stocks_preview": _flatten_candidate_groups(candidate_groups, limit=6),
            "technical_linkage": technical_linkage,
            "viewpoint_context": viewpoint_context,
            "data_truth": data_truth,
            "risk_flags": risk_flags,
            "concept_relationship_graph": {
                "graph_id": graph.get("graph_id"),
                "updated_at": graph.get("updated_at"),
                "construction_mode": graph.get("construction_mode"),
                "validation_status": graph.get("validation_status"),
                "confidence": graph.get("confidence"),
                "relations": (graph.get("relations") or [])[:10] if isinstance(graph.get("relations"), list) else [],
            },
            "mapping_chain": {
                "query": label or target_label,
                "chain_id": doc.get("chain_id"),
                "chain_name": doc.get("chain_name"),
                "node_id": doc.get("node_id"),
                "node_name": doc.get("node_name"),
                "layer": doc.get("layer"),
                "stage": doc.get("stage"),
                "mapping_status": "mapped",
                "evidence_sources": doc.get("evidence_sources") or [],
            },
        }
        rows.append(row)
    return rows


def _terminal_stock_pool_group_rows(range_columns: list[dict[str, Any]], group: str = "focus_stocks", limit: Optional[int] = None) -> list[dict[str, Any]]:
    if limit is None:
        env_name = {
            "focus_stocks": "TERMINAL_WORKBENCH_FOCUS_STOCK_LIMIT",
            "risk_stocks": "TERMINAL_WORKBENCH_RISK_STOCK_LIMIT",
            "watch_stocks": "TERMINAL_WORKBENCH_WATCH_STOCK_LIMIT",
        }.get(group, "TERMINAL_WORKBENCH_FOCUS_STOCK_LIMIT")
        default = "72" if group != "watch_stocks" else "36"
        limit = max(1, int(os.getenv(env_name, default)))
    try:
        db = _mongo_db()
        doc = db["terminal_stock_pool"].find_one(
            {"pool": "terminal_stock_pool", "market": "A"},
            {"stocks": 1, "focus_stocks": 1, "risk_stocks": 1, "watch_stocks": 1},
            sort=[("updated_at", -1)],
        ) or {}
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    source_rows = doc.get(group)
    if source_rows is None and group == "focus_stocks":
        source_rows = doc.get("stocks")
    for item in source_rows or []:
        if not isinstance(item, dict):
            continue
        reasons = [reason for reason in item.get("inclusion_reasons") or [] if isinstance(reason, dict)]
        has_technical = any(
            reason.get("reason_type") in {"technical_trigger", "technical_signal"}
            or reason.get("source_collection") == "terminal_technical_signals"
            for reason in reasons
        )
        fallback_only = bool(reasons) and all(reason.get("reason_type") == "fallback_watch" for reason in reasons)
        if group == "focus_stocks" and (fallback_only or (item.get("signal_origin") == "fallback_watch" and not has_technical)):
            continue
        row = _enrich_stock_row(dict(item), range_columns)
        row["lane"] = "signal_lane"
        row["second_screen_role"] = "actionable_focus_stock" if group == "focus_stocks" else group
        row["focus_reasons"] = [
            _text(reason.get("signal_type") or reason.get("reason_type"))
            for reason in item.get("inclusion_reasons") or []
            if isinstance(reason, dict)
        ][:4]
        row["source_tags"] = item.get("source_tags") or []
        row["inclusion_reasons"] = item.get("inclusion_reasons") or []
        row["technical_evidence"] = item.get("technical_evidence") if isinstance(item.get("technical_evidence"), dict) else {}
        row["knowledge_confirmation"] = item.get("knowledge_confirmation") if isinstance(item.get("knowledge_confirmation"), dict) else {"status": "none"}
        row["resonance_context"] = item.get("resonance_context") if isinstance(item.get("resonance_context"), dict) else {}
        row["trace_summary"] = " / ".join(
            f"{_text(reason.get('reason_type'))}:{_text(reason.get('source_collection'))}"
            for reason in row["inclusion_reasons"][:3]
            if isinstance(reason, dict)
        )
        row["signal_origin"] = item.get("signal_origin", "")
        row["signal_family"] = item.get("signal_family", "")
        row["chain_context"] = item.get("chain_context") if isinstance(item.get("chain_context"), dict) else {}
        row["exit_condition"] = item.get("exit_condition") or item.get("invalidates_when") or row.get("invalidates_when")
        row["invalidates_when"] = row["exit_condition"]
        row["reason"] = item.get("reason") or " · ".join(row["focus_reasons"][:2])
        row["latest_signal"] = item.get("latest_signal") or row.get("latest_signal")
        row["explanation"] = "纳入: " + " / ".join(row["focus_reasons"][:3]) if row["focus_reasons"] else ""
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _terminal_stock_pool_rows(range_columns: list[dict[str, Any]], limit: Optional[int] = None) -> list[dict[str, Any]]:
    return _terminal_stock_pool_group_rows(range_columns, "focus_stocks", limit)


def _manual_clue_rows(range_columns: list[dict[str, Any]], limit: Optional[int] = None) -> list[dict[str, Any]]:
    limit = limit or max(1, int(os.getenv("TERMINAL_WORKBENCH_MANUAL_CLUE_LIMIT", "36")))
    try:
        db = _mongo_db()
        docs = list(db["terminal_manual_clues"].find(
            {"active": {"$ne": False}},
            {"_id": 0},
        ).sort([("updated_at", -1), ("created_at", -1)]).limit(limit))
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for doc in docs:
        symbol = _text(doc.get("symbol"))
        normalized, raw_code = _normalize_stock_symbol(symbol)
        if not normalized:
            continue
        row = _enrich_stock_row({
            "symbol": normalized,
            "raw_code": raw_code,
            "name": doc.get("name") or _stock_name(normalized),
            "reason": "用户临时探索，不影响自动入池",
            "latest_signal": "手动线索",
            "source": "terminal_manual_clues",
        }, range_columns)
        row.update({
            "source_collection": "terminal_manual_clues",
            "source_tags": ["用户探索", "临时线索"],
            "source_collections": ["terminal_manual_clues"],
            "lane": "signal_lane",
            "freshness": "manual",
            "signal_origin": "user_manual_exploration",
            "signal_family": "manual_clue",
            "action_status": "manual_review",
            "actionability": "observe_only",
            "queue_lane": "manual_exploration",
            "pool_type": "clue_pool",
            "trade_stage": "clue_pool",
            "stage_label": "线索池",
            "trade_role": "ordinary_watch",
            "trade_role_label": "线索观察",
            "trade_identity": "manual_exploration",
            "trade_identity_label": "用户探索",
            "trader_action": "先观察",
            "missing_condition": "等30m承接，或5m/15m出现右侧确认",
            "can_trade_now": False,
            "invalidates_when": "删除手动线索，或图形证据走弱",
            "manual_clue": True,
            "deletable": True,
            "explanation": "手动加入线索池；只触发单票缓存和分析，不参与自动入池排序。",
            "trace_summary": "manual_clue:terminal_manual_clues",
        })
        row = _enrich_manual_clue_decision(row, normalized)
        rows.append(row)
    return rows


def _merge_stock_rows_by_symbol(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = _text(row.get("symbol") or row.get("code") or row.get("label")).upper()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        merged.append(row)
    return merged


def _focus_stock_pool_meta(focus_count: int) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "label": "确认买点",
        "source_collection": "terminal_stock_pool",
        "count": focus_count,
        "empty_reason": "",
        "terminal_technical_signal_count": 0,
    }
    try:
        db = _mongo_db()
        doc = db["terminal_stock_pool"].find_one(
            {"pool": "terminal_stock_pool", "market": "A"},
            {
                "updated_at": 1,
                "candidate_count": 1,
                "stock_limit": 1,
                "risk_limit": 1,
                "watch_limit": 1,
                "reason_counts": 1,
                "fallback_count": 1,
                "source_policy": 1,
                "selection_policy": 1,
                "ranking_version": 1,
                "stocks": 1,
                "focus_stocks": 1,
                "risk_stocks": 1,
                "watch_stocks": 1,
                "pool_counts": 1,
                "candidate_counts_by_source": 1,
                "candidate_counts_by_side": 1,
                "candidate_counts_by_freq": 1,
                "coverage_by_freq": 1,
                "coverage_status": 1,
                "is_full_market_complete": 1,
            },
            sort=[("updated_at", -1)],
        ) or {}
        tech_count = db["terminal_technical_signals"].count_documents({"market": "A"})
        freshness = db["data_freshness"].find_one(
            {"domain": "terminal_pool", "market": "A", "collection": "terminal_stock_pool"},
            sort=[("updated_at", -1)],
        ) or {}
        run = db["sync_runs"].find_one({"_id": {"$regex": "^postmarket:"}}, sort=[("updated_at", -1)]) or {}
        stocks_len = len(doc.get("focus_stocks") or doc.get("stocks") or [])
        empty_reason = ""
        if focus_count == 0:
            if tech_count == 0:
                empty_reason = "terminal_technical_signals=0"
            elif run.get("status") == "partial":
                empty_reason = "postmarket partial"
            else:
                empty_reason = _text(freshness.get("stale_reason")) or "terminal_stock_pool_empty"
        meta.update({
            "count": focus_count,
            "terminal_stock_pool_count": stocks_len,
            "candidate_count": int(doc.get("candidate_count") or 0),
            "stock_limit": int(doc.get("stock_limit") or 0),
            "risk_limit": int(doc.get("risk_limit") or 0),
            "watch_limit": int(doc.get("watch_limit") or 0),
            "fallback_count": int(doc.get("fallback_count") or 0),
            "reason_counts": doc.get("reason_counts") or {},
            "pool_counts": doc.get("pool_counts") or {},
            "candidate_counts_by_source": doc.get("candidate_counts_by_source") or {},
            "candidate_counts_by_side": doc.get("candidate_counts_by_side") or {},
            "candidate_counts_by_freq": doc.get("candidate_counts_by_freq") or {},
            "coverage_by_freq": doc.get("coverage_by_freq") or {},
            "coverage_status": _text(doc.get("coverage_status")),
            "is_full_market_complete": bool(doc.get("is_full_market_complete")),
            "selection_policy": _text(doc.get("selection_policy")),
            "ranking_version": _text(doc.get("ranking_version")),
            "source_policy": _text(doc.get("source_policy")),
            "updated_at": _serialize_dt(doc.get("updated_at")),
            "freshness": _text(freshness.get("freshness")),
            "stale_reason": _text(freshness.get("stale_reason")),
            "terminal_technical_signal_count": int(tech_count),
            "postmarket_status": _text(run.get("status")),
            "postmarket_run_id": _text(run.get("_id")),
            "empty_reason": empty_reason,
        })
    except Exception as exc:
        meta.update({"empty_reason": "metadata_unavailable", "error": exc.__class__.__name__})
    return meta


def _kline_cache_coverage() -> dict[str, Any]:
    freqs = ["日线", "周线", "5分钟", "15分钟", "30分钟"]
    coverage: dict[str, Any] = {"collections": []}
    try:
        db = _mongo_db()
        for collection in ("bars", "index_bars"):
            rows: list[dict[str, Any]] = []
            for freq in freqs:
                pipeline = [
                    {"$match": {"meta.freq": freq}},
                    {"$group": {
                        "_id": "$meta.symbol",
                        "count": {"$sum": 1},
                        "latest_dt": {"$max": "$dt"},
                    }},
                    {"$group": {
                        "_id": None,
                        "symbol_count": {"$sum": 1},
                        "bar_count": {"$sum": "$count"},
                        "latest_dt": {"$max": "$latest_dt"},
                    }},
                ]
                result = list(db[collection].aggregate(pipeline))
                row = result[0] if result else {}
                rows.append({
                    "freq": freq,
                    "symbol_count": int(row.get("symbol_count") or 0),
                    "bar_count": int(row.get("bar_count") or 0),
                    "latest_dt": _serialize_dt(row.get("latest_dt")),
                })
            coverage["collections"].append({"collection": collection, "rows": rows})
        latest_heat = db["board_heat_ticks"].find_one({}, {"trade_minute": 1}, sort=[("trade_minute", -1)]) or {}
        coverage["board_heat_ticks"] = {
            "latest_trade_minute": _serialize_dt(latest_heat.get("trade_minute")),
            "symbol_count": len(db["board_heat_ticks"].distinct("name")),
        }
        coverage["status"] = "ok"
    except Exception as exc:
        coverage["status"] = "unavailable"
        coverage["error"] = exc.__class__.__name__
    return coverage


def _build_trader_task_queue(
    *,
    decision_rows: list[dict[str, Any]],
    focus_stocks: list[dict[str, Any]],
    sector_boards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    allowed_lanes = {"risk_exit_first", "entry_ready", "entry_waiting_confirm"}
    lane_titles = {
        "risk_exit_first": "暂不参与",
        "entry_ready": "确认买点",
        "entry_waiting_confirm": "试仓候选",
    }

    def has_hard_technical(row: dict[str, Any]) -> bool:
        tech = row.get("technical_evidence") if isinstance(row.get("technical_evidence"), dict) else {}
        return bool(tech and tech.get("status") != "missing")

    def is_buy_review(action: str, row: dict[str, Any]) -> bool:
        text = " ".join([action, _text(row.get("title")), _text(row.get("reason")), _text(row.get("summary"))])
        return any(token in text for token in ("买", "入场", "可试仓", "entry_ready", "entry_waiting_confirm"))

    def normalize_lane(row: dict[str, Any], action: str) -> str:
        lane = _text(row.get("queue_lane") or row.get("lane"))
        if lane in allowed_lanes:
            return lane
        status = _text(row.get("action_status") or row.get("recommended_action"))
        text = " ".join([
            action,
            status,
            _text(row.get("latest_signal")),
            _text(row.get("reason")),
            _text(row.get("summary")),
            _text(row.get("trigger_reason")),
        ])
        if any(token in text for token in ("减仓", "止盈", "风险", "卖", "跌破", "阻断", "暂不参与")):
            return "risk_exit_first"
        if not has_hard_technical(row):
            return ""
        if action == "可试仓" or "entry_ready" in text:
            return "entry_ready"
        if "等待" in action or "确认" in action or "entry_waiting_confirm" in text:
            return "entry_waiting_confirm"
        return ""

    def add(task: dict[str, Any]) -> None:
        if not task.get("title"):
            return
        lane = _text(task.get("queue_lane"))
        if lane not in allowed_lanes:
            return
        task.setdefault("decision_id", f"task-{len(tasks) + 1}")
        task.setdefault("source", "second_screen")
        task.setdefault("action_label", task.get("trader_action") or task.get("action") or "观察")
        task.setdefault("invalidates_when", "触发条件失效或关键位被破坏")
        tasks.append(task)

    for row in focus_stocks:
        action = _text(row.get("trader_action")) or "观察"
        lane = normalize_lane(row, action)
        if lane not in allowed_lanes:
            continue
        tech = row.get("technical_evidence") if isinstance(row.get("technical_evidence"), dict) else {}
        if lane in {"entry_ready", "entry_waiting_confirm"} and tech.get("status") == "missing":
            continue
        add({
            "decision_id": f"focus:{row.get('symbol') or row.get('label')}",
            "title": f"{lane_titles[lane]} · {row.get('name') or row.get('symbol')}",
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "action": action,
            "action_label": action,
            "trade_stage": row.get("trade_stage"),
            "stage_label": row.get("stage_label"),
            "missing_condition": row.get("missing_condition"),
            "chain_position": row.get("chain_position") if isinstance(row.get("chain_position"), dict) else {},
            "queue_lane": lane,
            "priority": "high" if lane in {"risk_exit_first", "entry_ready"} else "medium",
            "summary": row.get("reason") or row.get("latest_signal") or "",
            "trigger_reason": row.get("latest_signal") or row.get("reason") or "",
            "chart_target": {"kind": "stock", "label": row.get("symbol"), "freq": "5min"},
            "invalidates_when": row.get("invalidates_when"),
            "technical_evidence": tech,
            "knowledge_confirmation": row.get("knowledge_confirmation") if isinstance(row.get("knowledge_confirmation"), dict) else {},
            "chain_context": row.get("chain_context") if isinstance(row.get("chain_context"), dict) else {},
        })

    for row in decision_rows:
        if not isinstance(row, dict):
            continue
        action = _text(row.get("action_label") or row.get("recommended_action") or row.get("action")) or "观察"
        if is_buy_review(action, row) and not has_hard_technical(row):
            continue
        lane = normalize_lane(row, action)
        if lane not in allowed_lanes:
            continue
        add({
            **row,
            "action": action,
            "action_label": action,
            "trade_stage": row.get("trade_stage"),
            "stage_label": row.get("stage_label"),
            "missing_condition": row.get("missing_condition"),
            "chain_position": row.get("chain_position") if isinstance(row.get("chain_position"), dict) else {},
            "queue_lane": lane,
            "title": _text(row.get("title")) or f"{lane_titles[lane]} · {_text(row.get('symbol') or row.get('decision_id'))}",
            "trigger_reason": _text(row.get("summary") or row.get("reason") or row.get("recommended_action")),
            "chart_target": row.get("chart_target") or {"kind": "stock", "label": row.get("symbol"), "freq": DEFAULT_TERMINAL_FREQ},
            "invalidates_when": row.get("invalidates_when") or "复核条件解除或关键位被破坏",
        })

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task in tasks:
        key = _text(task.get("decision_id") or task.get("title"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(task)
    return deduped[:12]


TRADE_ROLE_FILTERS = [
    {"key": "all", "label": "全部"},
    {"key": "mainline_attack", "label": "主线机会"},
    {"key": "climax_risk", "label": "过热禁追"},
    {"key": "second_wave", "label": "回踩再起"},
    {"key": "defensive_weight", "label": "防守观察"},
    {"key": "chain_watch", "label": "产业链观察"},
    {"key": "risk_review", "label": "风险复核"},
    {"key": "ordinary_watch", "label": "线索观察"},
]

TRADE_ROLE_DEFINITIONS = {
    "mainline_attack": {
        "definition": "板块/产业链处在升温或加速，且个股不被风控和过热条件阻断。",
        "source": "chain_heat_snapshots + terminal_stock_pool.theme_rank_bonus",
    },
    "climax_risk": {
        "definition": "板块/产业链一致过热，买点被过热条件阻断，只提示风险不推追高。",
        "source": "chain_heat_snapshots.phase + terminal_stock_pool.blocked_by",
    },
    "chain_watch": {
        "definition": "股票已映射到东财/同花顺板块图谱，但还没满足主线、回踩、防守或确认买点条件。",
        "source": "security_chain_memberships + terminal_stock_pool",
    },
    "defensive_weight": {
        "definition": "只接收上游数据明确标记的防守/稳仓/高股息/低波属性，不用行业词硬猜。",
        "source": "terminal_stock_pool.exposure_bucket",
    },
    "second_wave": {
        "definition": "板块退潮/分化后，或个股进入低吸/试仓阶段，等待重新放量和右侧确认。",
        "source": "chain_heat phase=cooling/risk_off/diverging + terminal_stock_pool",
    },
    "risk_review": {
        "definition": "进入 risk/skip_now 或卖点、冲突、阻断条件未解除。",
        "source": "terminal_stock_pool risk rows",
    },
    "ordinary_watch": {
        "definition": "只有线索或技术背景，没有可交易级别的产业链/买点/风控状态。",
        "source": "terminal_stock_pool",
    },
}


def _trade_role_for_shell_stock(row: dict[str, Any]) -> str:
    explicit = _text(row.get("trade_role") or row.get("trade_identity"))
    if explicit:
        return "chain_watch" if explicit == "holding_chain" else explicit
    chain = row.get("chain_position") if isinstance(row.get("chain_position"), dict) else {}
    context = row.get("chain_context") if isinstance(row.get("chain_context"), dict) else {}
    phase = _text(row.get("chain_phase") or context.get("phase") or chain.get("phase"))
    exposure_bucket = _text(row.get("exposure_bucket") or context.get("exposure_bucket") or chain.get("exposure_bucket"))
    has_chain = any(_text(chain.get(key) or context.get(key)) for key in ("chain", "chain_name", "node", "node_name", "board_or_concept"))
    if _text(row.get("pool_type")) == "risk" or _text(row.get("trade_stage")) == "skip_now":
        return "climax_risk" if phase == "consensus_climax" else "risk_review"
    if phase == "consensus_climax":
        return "climax_risk"
    if exposure_bucket in {"defensive", "防守", "稳仓", "高股息", "低波"}:
        return "defensive_weight"
    if phase in {"cooling", "diverging"} or _text(row.get("trade_stage")) in {"dip_watch", "probe_candidate"}:
        return "second_wave"
    if phase in {"accelerating", "warming"} or (_float(row.get("theme_rank_bonus")) or 0) >= 12:
        return "mainline_attack"
    if _text(row.get("trade_stage")) in {"confirmed_entry", "probe_candidate"}:
        return "mainline_attack"
    if has_chain:
        return "chain_watch"
    return "ordinary_watch"


def _shell_stock_chain_brief(row: dict[str, Any]) -> str:
    chain = row.get("chain_position") if isinstance(row.get("chain_position"), dict) else {}
    values = [
        _text(chain.get("chain") or chain.get("board_or_concept")),
        _text(chain.get("node") or chain.get("role")),
    ]
    deduped: list[str] = []
    for value in values:
        if value and value not in deduped:
            deduped.append(value)
    return " · ".join(deduped[:2])


def _first_stock_for_role(rows: list[dict[str, Any]], role: str) -> dict[str, Any]:
    for row in rows:
        if _trade_role_for_shell_stock(row) == role:
            return row
    return {}


def _sector_role_item(row: dict[str, Any], role: str, label: str, summary: str) -> dict[str, Any]:
    role_definition = TRADE_ROLE_DEFINITIONS.get(role, {})
    return {
        "role": role,
        "label": label,
        "name": _text(row.get("name") or row.get("label")),
        "summary": summary,
        "phase": _text(row.get("phase") or row.get("action_status")),
        "as_of": _text(row.get("day_change_as_of")),
        "definition": role_definition.get("definition", ""),
        "source": role_definition.get("source", ""),
    }


def _stock_role_item(row: dict[str, Any], role: str, label: str, fallback_summary: str) -> dict[str, Any]:
    role_definition = TRADE_ROLE_DEFINITIONS.get(role, {})
    return {
        "role": role,
        "label": label,
        "name": _text(row.get("name") or row.get("symbol") or row.get("code")),
        "summary": _text(row.get("trader_read") or row.get("ai_trade_summary") or row.get("setup_explanation")) or fallback_summary,
        "chain": _shell_stock_chain_brief(row),
        "stage": _text(row.get("stage_label") or row.get("trade_stage")),
        "definition": role_definition.get("definition", ""),
        "source": role_definition.get("source", ""),
    }


def _build_trade_map(
    *,
    sector_boards: list[dict[str, Any]],
    focus_stocks: list[dict[str, Any]],
    watch_stocks: list[dict[str, Any]],
    risk_stocks: list[dict[str, Any]],
    clue_stocks: list[dict[str, Any]],
) -> dict[str, Any]:
    stock_rows = [*focus_stocks, *watch_stocks, *clue_stocks, *risk_stocks]
    climax = next((row for row in sector_boards if _text(row.get("phase")) == "consensus_climax"), {})
    mainline = next((row for row in sector_boards if _text(row.get("phase")) in {"accelerating", "warming"}), {})
    retreat = next((row for row in sector_boards if _text(row.get("phase")) in {"cooling", "risk_off", "diverging"}), {})
    chain_watch = _first_stock_for_role(stock_rows, "chain_watch")
    defensive = _first_stock_for_role(stock_rows, "defensive_weight")
    second_wave = _first_stock_for_role(stock_rows, "second_wave")
    items: list[dict[str, Any]] = []
    if mainline:
        items.append(_sector_role_item(mainline, "mainline_attack", "主线机会", "升温/加速中，只看分歧承接。"))
    if climax:
        items.append(_sector_role_item(climax, "climax_risk", "过热风险", "一致过热，不追高，等分歧后的核心票。"))
    if retreat:
        items.append(_sector_role_item(retreat, "second_wave", "回踩再起", "退潮/分化后等重新放量。"))
    if chain_watch:
        items.append(_stock_role_item(chain_watch, "chain_watch", "产业链观察", "已入东财/同花顺图谱，等30m承接确认。"))
    if defensive:
        items.append(_stock_role_item(defensive, "defensive_weight", "防守观察", "偏稳仓节奏，不和进攻票混排。"))
    if second_wave and not any(item.get("role") == "second_wave" for item in items):
        items.append(_stock_role_item(second_wave, "second_wave", "回踩再起", "退潮后观察重新放量。"))
    counts = {item["key"]: 0 for item in TRADE_ROLE_FILTERS if item["key"] != "all"}
    for row in stock_rows:
        role = _trade_role_for_shell_stock(row)
        if role in counts:
            counts[role] += 1
    headline = " | ".join(
        f"{item.get('label')}: {item.get('name')}{('，' + item.get('summary')) if item.get('summary') else ''}"
        for item in items[:5]
        if item.get("name")
    )
    return {
        "as_of": _day_change_expected_day(),
        "day_change_mode": _a_day_change_mode(),
        "headline": headline,
        "items": items[:6],
        "role_filters": TRADE_ROLE_FILTERS,
        "role_definitions": TRADE_ROLE_DEFINITIONS,
        "role_counts": counts,
    }


def _build_ai_alerts(trade_map: dict[str, Any]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for item in trade_map.get("items") or []:
        if not isinstance(item, dict):
            continue
        role = _text(item.get("role"))
        name = _text(item.get("name"))
        if role == "climax_risk":
            alerts.append({
                "level": "warning",
                "role": role,
                "text": f"{name or '主线'}已一致过热，确认买点不再推追高。",
                "command": "排除过热票",
            })
        elif role == "chain_watch":
            alerts.append({
                "level": "info",
                "role": role,
                "text": f"{name or '产业链观察'}只表示板块图谱归属，不代表真实持仓；只等承接确认。",
                "command": "只看产业链观察",
            })
    return alerts[:3]


def _trade_command_suggestions() -> list[str]:
    return [
        "只看产业链观察",
        "排除过热票",
        "解释这只票为什么入池",
        "列出主线分歧后可看的核心票",
        "哪些票不符合我当前节奏",
    ]


def _build_watchlist_rows(
    *,
    reports: list[dict[str, Any]],
    buy_rows: list[dict[str, Any]],
    sell_rows: list[dict[str, Any]],
    decision_rows: list[dict[str, Any]],
    industry_top: list[dict[str, Any]],
    concept_top: list[dict[str, Any]],
    range_columns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(row: dict[str, Any], kind: str) -> None:
        label = str(row.get("symbol") or row.get("code") or row.get("label") or row.get("name") or "").strip()
        if not label:
            return
        key = f"{kind}:{label}"
        if key in seen:
            return
        seen.add(key)
        rows.append(row)

    for report in reports:
        row = _enrich_index_row(report, range_columns)
        add(row, "index")
    for row in buy_rows:
        enriched = _enrich_stock_row(dict(row), range_columns)
        add(enriched, "stock")
    for row in sell_rows:
        enriched = _enrich_stock_row(dict(row), range_columns)
        add(enriched, "stock")
    for row in decision_rows:
        if row.get("symbol"):
            enriched = _enrich_stock_row(dict(row), range_columns)
            add(enriched, "stock")
    for row in industry_top:
        add(_enrich_cluster_row(dict(row), "industry"), "industry")
    for row in concept_top:
        add(_enrich_cluster_row(dict(row), "concept"), "concept")
    return rows[:60]


def _serialize_trade_record(trade) -> Dict[str, Any]:
    return {
        "id": trade.id,
        "symbol": trade.symbol,
        "name": trade.name,
        "direction": trade.direction,
        "entry_date": trade.entry_date,
        "entry_price": trade.entry_price,
        "entry_signal": trade.entry_signal,
        "exit_date": trade.exit_date,
        "exit_price": trade.exit_price,
        "position_pct": trade.position_pct,
        "pnl_pct": trade.pnl_pct,
        "holding_days": trade.holding_days,
        "total_score": trade.total_score,
        "error_type": trade.error_type,
        "is_open": trade.is_open,
    }


def _trade_context(symbol: Optional[str]) -> Dict[str, Any]:
    log = get_trade_log()
    summary = log.get_summary()
    trades = log.list_trades(status="all", limit=200)
    missed = log.list_missed_signals(limit=50)

    related_trades = []
    related_missed = []
    if symbol:
        symbol_suffix = symbol.split(".", 1)[-1]
        for trade in trades:
            if trade.symbol == symbol or trade.symbol.endswith(symbol_suffix):
                related_trades.append(_serialize_trade_record(trade))
        for item in missed:
            if item.symbol == symbol or item.symbol.endswith(symbol_suffix):
                related_missed.append(
                    {
                        "symbol": item.symbol,
                        "name": item.name,
                        "signal_type": item.signal_type,
                        "signal_date": item.signal_date,
                        "signal_price": item.signal_price,
                        "max_price_after": item.max_price_after,
                        "potential_pnl_pct": item.potential_pnl_pct,
                    }
                )

    return {
        "summary": {
            "total_trades": summary.total_trades,
            "win_rate": summary.win_rate,
            "avg_pnl_pct": summary.avg_pnl_pct,
            "avg_score": summary.avg_score,
            "avg_holding_days": summary.avg_holding_days,
            "error_counts": summary.error_counts,
        },
        "related_trades": related_trades[:12],
        "missed_signals": related_missed[:8],
    }


def _review_context(engine, kind: str, label: str, symbol: Optional[str] = None) -> Dict[str, Any]:
    rv = engine.review_state
    payload: Dict[str, Any] = {
        "completed": rv.completed,
        "is_running": rv.is_running,
        "phase": rv.phase,
        "phase_detail": rv.phase_detail,
        "error": rv.error,
        "start_date": rv.start_date,
        "start_label": rv.start_label,
        "timing": rv.timing,
    }
    if kind == "stock" and symbol:
        timeline = rv.replay_timelines.get(symbol, [])
        payload["timeline"] = [serialize_signal_change(item) for item in timeline]
        for scored in rv.scored_symbols:
            if scored.symbol == symbol:
                payload["reviewed_symbol"] = serialize_scored_symbol(scored)
                break
    elif kind == "index":
        for report in rv.index_reports:
            if report.name == label:
                payload["reviewed_report"] = serialize_index_report(report)
                break
    elif kind == "industry":
        ranking = engine.get_industry_ranking_by_name(label)
        if ranking:
            payload["industry"] = {
                "name": ranking.name,
                "rotation_line": ranking.rotation_line,
                "phase": ranking.rhythm_phase,
                "phase_hint": ranking.rhythm_hint,
                "gain_pct": round(ranking.gain_pct, 2),
                "composite_score": round(ranking.composite_score, 1),
            }
    return payload


def _plan_for_index(engine, name: str) -> Optional[Dict[str, Any]]:
    try:
        from signals.core.planner import generate_plan

        analyzer = engine.get_symbol_analyzer(name, "daily")
        report = next((item for item in engine.get_index_reports() if item.name == name), None)
        if analyzer is None or report is None:
            return None
        plan = generate_plan(analyzer, getattr(report, "ma_context", None))
        plan.name = name
        return _serialize_plan(plan)
    except Exception:
        return None


def _build_shell_payload_uncached(engine) -> Dict[str, Any]:
    status = engine.get_status()
    session = _serialize_session(status)
    strategy_snapshot = _safe_strategy_snapshot()
    range_columns = _watchlist_range_columns()
    sync_lanes = _sync_lane_status()
    market_context = serialize_market_context(engine.get_market_context()) if engine.get_market_context() else None
    reports_raw = [
        serialize_index_report(report)
        for report in engine.get_index_reports()
        if getattr(report, "data_available", False)
    ]
    reports = [_enrich_index_row(report, range_columns) for report in reports_raw]
    macro_indices = _build_macro_index_rows(reports=reports_raw, range_columns=range_columns)
    strategy_candidates = [
        dict(item)
        for item in strategy_snapshot.get("candidates", [])
        if isinstance(item, dict)
    ]
    strategy_clues = [
        item
        for item in strategy_candidates
        if _text(item.get("decision_stage")) == "strategy_candidate"
    ]
    sell_warnings = [
        _enrich_stock_row(dict(item), range_columns) if isinstance(item, dict) and item.get("symbol") else dict(item)
        for item in strategy_snapshot.get("warnings", [])
        if isinstance(item, dict)
    ]
    decision_rows_raw = [
        dict(item)
        for item in strategy_snapshot.get("decision_queue", [])
        if isinstance(item, dict)
    ]
    snapshot_cluster = _cluster_from_strategy_snapshot(strategy_snapshot)
    industry_top = snapshot_cluster.get("industry_top") or _gateway_rank_rows("board", top=8)
    concept_top = snapshot_cluster.get("concept_top") or _gateway_rank_rows("concept", top=8)
    cluster: dict[str, Any] = {}
    if not industry_top or not concept_top:
        try:
            cluster = _unwrap_response(cluster_service.get_latest(top=8))
        except Exception:
            cluster = {}
        industry_top = industry_top or (cluster.get("industry") or {}).get("top") or []
        concept_top = concept_top or (cluster.get("concept") or {}).get("top") or []
    sector_boards = _chain_heat_sector_rows()
    focus_stocks = _terminal_stock_pool_rows(range_columns)
    risk_stocks = _terminal_stock_pool_group_rows(range_columns, "risk_stocks")
    watch_stocks = _terminal_stock_pool_group_rows(range_columns, "watch_stocks")
    clue_stocks = _terminal_stock_pool_group_rows(range_columns, "clue_stocks")
    manual_clues = _manual_clue_rows(range_columns)
    scored_raw = _merge_stock_rows_by_symbol(manual_clues + (clue_stocks or strategy_clues))
    scored = [
        _enrich_stock_row(dict(item), range_columns) if item.get("symbol") and not item.get("latest_price") else dict(item)
        for item in scored_raw
    ]
    focus_stocks_meta = _focus_stock_pool_meta(len(focus_stocks))
    for rows, lane in (
        (macro_indices, "quote_lane"),
        (sector_boards, "board_lane"),
        (focus_stocks, "signal_lane"),
        (risk_stocks, "signal_lane"),
        (watch_stocks, "signal_lane"),
        (clue_stocks, "signal_lane"),
        (manual_clues, "signal_lane"),
    ):
        for row in rows:
            row["lane_status"] = sync_lanes.get(lane, {})
            row["freshness"] = row["lane_status"].get("freshness", "unknown")

    decision_queue = _build_trader_task_queue(
        decision_rows=decision_rows_raw,
        focus_stocks=focus_stocks + risk_stocks,
        sector_boards=sector_boards,
    )
    scored_shell = [_slim_shell_stock_row(row) for row in scored]
    sell_warnings_shell = [_slim_shell_stock_row(row) for row in sell_warnings]
    sector_boards_shell = [_slim_shell_sector_row(row) for row in sector_boards]
    focus_stocks_shell = [_slim_shell_stock_row(row) for row in focus_stocks]
    risk_stocks_shell = [_slim_shell_stock_row(row) for row in risk_stocks]
    watch_stocks_shell = [_slim_shell_stock_row(row) for row in watch_stocks]
    trade_map = _build_trade_map(
        sector_boards=sector_boards_shell,
        focus_stocks=focus_stocks_shell,
        watch_stocks=watch_stocks_shell,
        risk_stocks=risk_stocks_shell,
        clue_stocks=scored_shell,
    )
    ai_alerts = _build_ai_alerts(trade_map)

    watchlist_directions: List[str] = []
    for report in reports[:5]:
        watchlist_directions.append(report["name"])
    for item in industry_top[:6]:
        label = item.get("label")
        if label and label not in watchlist_directions:
            watchlist_directions.append(label)
    watchlist = _build_watchlist_rows(
        reports=reports,
        buy_rows=scored_shell,
        sell_rows=sell_warnings_shell,
        decision_rows=decision_queue,
        industry_top=industry_top,
        concept_top=concept_top,
        range_columns=range_columns,
    )

    notices = []
    if not session["ready"]:
        notices.append("分析引擎正在启动，首屏数据会逐步填充。")
    if cluster.get("data_warning"):
        notices.append(cluster["data_warning"])

    return {
        "session": session,
        "market": market_context,
        "indices": reports[:8],
        "buy_candidates": scored_shell,
        "sell_warnings": sell_warnings_shell,
        "cluster_summary": {
            "industry_top": industry_top,
            "concept_top": concept_top,
            "market_status": cluster.get("market_status") or {},
            "data_warning": cluster.get("data_warning", ""),
        },
        "watchlist_groups": {
            "macro_indices": macro_indices,
            "sector_boards": sector_boards_shell,
            "buy_candidates": scored_shell,
            "focus_stocks": focus_stocks_shell,
            "risk_stocks": risk_stocks_shell,
            "watch_stocks": watch_stocks_shell,
        },
        "watchlist_groups_meta": {
            "macro_indices": {
                "label": "宏观指数",
                "source_collection": "index_bars",
                "count": len(macro_indices),
            },
            "sector_boards": {
                "label": "异动板块",
                "source_collection": "chain_heat_snapshots",
                "count": len(sector_boards),
                "representative_stock_role": "preview_only_not_focus_pool",
            },
            "buy_candidates": {
                "label": "线索池",
                "source_collection": "terminal_manual_clues + terminal_stock_pool.clue_stocks + strategy_snapshots.strategy_candidate",
                "count": len(scored),
                "role": "source_clue_only_not_entry",
                "manual_clues": len(manual_clues),
                "empty_reason": "" if scored else "当前没有纯线索；已有硬技术的标的会进入盯盘池或确认买点。",
            },
            "focus_stocks": focus_stocks_meta,
            "risk_stocks": {
                "label": "暂不参与",
                "source_collection": "terminal_stock_pool.risk_stocks",
                "count": len(risk_stocks),
                "role": "skip_now_not_opportunity",
                **{key: value for key, value in focus_stocks_meta.items() if key in {"pool_counts", "candidate_counts_by_source", "candidate_counts_by_side", "candidate_counts_by_freq", "coverage_by_freq", "coverage_status", "selection_policy", "ranking_version"}},
            },
            "watch_stocks": {
                "label": "盯盘池",
                "source_collection": "terminal_stock_pool.watch_stocks",
                "count": len(watch_stocks),
                "role": "watch_pool_dip_watch_probe_candidate",
                **{key: value for key, value in focus_stocks_meta.items() if key in {"pool_counts", "candidate_counts_by_source", "candidate_counts_by_side", "candidate_counts_by_freq", "coverage_by_freq", "coverage_status", "selection_policy", "ranking_version"}},
            },
        },
        "watchlist": watchlist,
        "watchlist_range_columns": range_columns,
        "kline_cache_coverage": _kline_cache_coverage(),
        "sync_lanes": sync_lanes,
        "trade_map": trade_map,
        "ai_alerts": ai_alerts,
        "command_suggestions": _trade_command_suggestions(),
        "daily_brief": strategy_snapshot.get("daily_brief", {}),
        "decision_queue": decision_queue,
        "strategy_kpis": strategy_snapshot.get("strategy_kpis", {}),
        "source_confidence": strategy_snapshot.get("source_confidence", {}),
        "watchlist_directions": watchlist_directions[:10],
        "default_target": {
            "kind": "index",
            "label": macro_indices[0]["name"] if macro_indices else "沪深300",
            "freq": "30min",
        },
        "legacy_url": "/legacy",
        "notices": notices,
    }


def _build_shell_payload(engine) -> Dict[str, Any]:
    now = time.monotonic()
    cached_payload = _SHELL_CACHE.get("payload")
    if _shell_cache_usable(cached_payload, engine) and now < float(_SHELL_CACHE.get("expires_at") or 0):
        payload = dict(cached_payload)
        payload["cache"] = {
            "status": "hit",
            "age_seconds": round(now - float(_SHELL_CACHE.get("refreshed_at") or now), 2),
            "ttl_seconds": _SHELL_CACHE_TTL_SECONDS,
        }
        return payload

    acquired = _SHELL_CACHE_LOCK.acquire(blocking=False)
    if not acquired:
        if _shell_cache_usable(cached_payload, engine):
            payload = dict(cached_payload)
            payload["cache"] = {
                "status": "stale_refreshing",
                "age_seconds": round(now - float(_SHELL_CACHE.get("refreshed_at") or now), 2),
                "ttl_seconds": _SHELL_CACHE_TTL_SECONDS,
            }
            return payload
        with _SHELL_CACHE_LOCK:
            cached_payload = _SHELL_CACHE.get("payload")
            if _shell_cache_usable(cached_payload, engine):
                payload = dict(cached_payload)
                payload["cache"] = {
                    "status": "waited_hit",
                    "age_seconds": round(time.monotonic() - float(_SHELL_CACHE.get("refreshed_at") or time.monotonic()), 2),
                    "ttl_seconds": _SHELL_CACHE_TTL_SECONDS,
                }
                return payload

    try:
        refreshed_now = time.monotonic()
        cached_payload = _SHELL_CACHE.get("payload")
        if _shell_cache_usable(cached_payload, engine) and refreshed_now < float(_SHELL_CACHE.get("expires_at") or 0):
            payload = dict(cached_payload)
            payload["cache"] = {
                "status": "hit_after_lock",
                "age_seconds": round(refreshed_now - float(_SHELL_CACHE.get("refreshed_at") or refreshed_now), 2),
                "ttl_seconds": _SHELL_CACHE_TTL_SECONDS,
            }
            return payload
        payload = _build_shell_payload_uncached(engine)
        _SHELL_CACHE.update({
            "payload": dict(payload),
            "refreshed_at": refreshed_now,
            "expires_at": refreshed_now + (_SHELL_CACHE_TTL_SECONDS if payload.get("session", {}).get("ready") else 2.0),
        })
        payload["cache"] = {
            "status": "refreshed",
            "age_seconds": 0,
            "ttl_seconds": _SHELL_CACHE_TTL_SECONDS,
        }
        return payload
    finally:
        if acquired:
            _SHELL_CACHE_LOCK.release()


def _safe_strategy_snapshot() -> Dict[str, Any]:
    try:
        from signals.data.mongo_fallback import get_db

        db = get_db()
        if db is not None:
            doc = db["strategy_snapshots"].find_one(
                {"snapshot": {"$exists": True}},
                {"_id": 0, "snapshot": 1},
                sort=[("updated_at", -1), ("as_of", -1)],
            )
            if doc and isinstance(doc.get("snapshot"), dict):
                snapshot = dict(doc["snapshot"])
                snapshot.setdefault("read_model_source", "mongodb.strategy_snapshots")
                return snapshot
    except Exception:
        pass
    try:
        snapshot = get_strategy_snapshot()
        return dict(snapshot) if isinstance(snapshot, dict) else {}
    except Exception as exc:
        return {
            "daily_brief": {"summary": f"strategy_snapshot_error:{exc.__class__.__name__}"},
            "candidates": [],
            "warnings": [],
            "themes": [],
            "decision_queue": [],
            "strategy_kpis": {},
            "source_confidence": {"overall": 0, "sources": []},
        }


def _gateway_rank_rows(domain: str, top: int = 8) -> list[dict[str, Any]]:
    try:
        from signals.data.gateway import get_board_rank, get_concept_rank

        fn = get_concept_rank if domain == "concept" else get_board_rank
        response = fn(DataRequest(
            domain="concept" if domain == "concept" else "board",
            mode="realtime",
            market="A",
            purpose="cluster",
            allow_stale=True,
        ))
        data = response.data
        if isinstance(data, pd.DataFrame):
            records = data.head(top).to_dict("records")
        elif isinstance(data, list):
            records = data[:top]
        else:
            records = []
        rows: list[dict[str, Any]] = []
        for item in records:
            if not isinstance(item, dict):
                continue
            label = _text(
                item.get("board_name")
                or item.get("concept_name")
                or item.get("name")
                or item.get("label")
                or item.get("板块名称")
            )
            if not label:
                continue
            rows.append({
                "label": label,
                "name": label,
                "kind": "concept" if domain == "concept" else "industry",
                "domain": domain,
                "source": response.source or item.get("source") or "gateway_rank",
                "change_pct": item.get("change_pct") or item.get("gain_pct") or item.get("涨跌幅"),
                "leader": item.get("leader_name") or item.get("leader") or item.get("leading_stock") or item.get("领涨股票"),
                "leader_change_pct": item.get("leader_change_pct") or item.get("leading_gain"),
                "turnover_pct": item.get("turnover_pct"),
                "up_count": item.get("up_count"),
                "down_count": item.get("down_count"),
            })
        return rows
    except Exception:
        return []


def _cluster_from_strategy_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    themes = [
        item for item in snapshot.get("themes", [])
        if isinstance(item, dict)
    ]
    return {
        "industry_top": [
            {
                "label": item.get("name", ""),
                "name": item.get("name", ""),
                "kind": "industry",
                "domain": "board",
                "source": item.get("evidence", [{}])[0].get("source", "strategy_snapshot")
                if isinstance(item.get("evidence"), list) and item.get("evidence")
                else "strategy_snapshot",
                "change_pct": item.get("change_pct", item.get("strength", 0)),
                "leader": item.get("leader", ""),
                "phase": item.get("phase", ""),
            }
            for item in themes
            if item.get("domain") == "board"
        ][:6],
        "concept_top": [
            {
                "label": item.get("name", ""),
                "name": item.get("name", ""),
                "kind": "concept",
                "domain": "concept",
                "source": item.get("evidence", [{}])[0].get("source", "strategy_snapshot")
                if isinstance(item.get("evidence"), list) and item.get("evidence")
                else "strategy_snapshot",
                "change_pct": item.get("change_pct", item.get("strength", 0)),
                "leader": item.get("leader", ""),
                "phase": item.get("phase", ""),
            }
            for item in themes
            if item.get("domain") == "concept"
        ][:6],
    }


def _concept_theme_candidates(name: str) -> list[dict[str, Any]]:
    snapshot = get_strategy_snapshot()
    themes = [
        item for item in snapshot.get("themes", [])
        if isinstance(item, dict) and item.get("domain") == "concept"
    ]
    exact = [item for item in themes if item.get("name") == name]
    if exact:
        return exact
    return [
        item for item in themes
        if name and (name in str(item.get("name", "")) or str(item.get("name", "")) in name)
    ]


def _preferred_concept_carriers(
    concept_name: str,
    theme_candidates: list[dict[str, Any]],
    related_industries: list[str],
) -> list[dict[str, Any]]:
    from signals.core.concept_carriers import preferred_concept_carriers

    return preferred_concept_carriers(
        concept_name,
        aliases=[_text(item.get("name")) for item in theme_candidates],
        related_industries=related_industries,
    )


def _mongo_db():
    from signals.sync.db import get_db

    return get_db()


def _serialize_dt(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat(timespec="seconds")
        except TypeError:
            return value.isoformat()
    return str(value)


def _sync_lane_status() -> dict[str, dict[str, Any]]:
    status = {
        lane: {
            "lane": lane,
            **meta,
            "status": "unknown",
            "freshness": "unknown",
            "last_success_at": "",
            "last_run_at": "",
            "next_due_at": "",
            "degraded_reason": "",
            "modules": [],
        }
        for lane, meta in SECOND_SCREEN_LANES.items()
    }
    try:
        db = _mongo_db()
        docs = list(db["sync_log"].find(
            {"lane": {"$in": list(SECOND_SCREEN_LANES)}},
            {"_id": 0, "module": 1, "market": 1, "lane": 1, "status": 1, "last_run": 1, "next_due_at": 1, "degraded_reason": 1, "error_msg": 1},
        ).sort("last_run", -1).limit(80))
    except Exception:
        return status
    for doc in docs:
        lane = _text(doc.get("lane"))
        if lane not in status:
            continue
        item = status[lane]
        module = _text(doc.get("module"))
        if module and module not in item["modules"]:
            item["modules"].append(module)
        if not item["last_run_at"]:
            item["last_run_at"] = _serialize_dt(doc.get("last_run"))
            item["next_due_at"] = _serialize_dt(doc.get("next_due_at"))
            item["status"] = _text(doc.get("status")) or "unknown"
            item["freshness"] = "fresh" if item["status"] == "ok" else "stale" if item["status"] in {"degraded", "error"} else item["status"]
            item["degraded_reason"] = _text(doc.get("degraded_reason") or doc.get("error_msg"))
        if doc.get("status") == "ok" and not item["last_success_at"]:
            item["last_success_at"] = _serialize_dt(doc.get("last_run"))
    return status


def _stock_symbol_from_code_or_name(code: Any = "", name: Any = "") -> tuple[str, str]:
    for value in (_text(code), _text(name)):
        if not value:
            continue
        normalized, raw_code = _normalize_stock_symbol(value)
        if normalized and raw_code:
            return normalized, raw_code
    return "", ""


def _ensure_daily_bars(symbol: str, raw_code: str) -> bool:
    df, _ = _stock_df(symbol, "daily")
    if df is not None and not df.empty:
        return True
    code = raw_code or symbol.split(".", 1)[-1]
    if not code or not code.isdigit():
        return False
    try:
        from signals.sync.modules.stock_daily import _sync_one_stock

        now = _sync_now()
        docs = _sync_one_stock(
            code,
            (now - timedelta(days=730)).strftime("%Y%m%d"),
            now.strftime("%Y%m%d"),
        )
        if not docs:
            return False
        db = _mongo_db()
        existing_dts = {
            item.get("dt")
            for item in db["bars"].find(
                {
                    "meta.symbol": code,
                    "meta.freq": "日线",
                    "dt": {"$in": [doc["dt"] for doc in docs]},
                },
                {"dt": 1},
            )
        }
        new_docs = [doc for doc in docs if doc["dt"] not in existing_dts]
        if new_docs:
            db["bars"].insert_many(new_docs, ordered=False)
        db["sync_log"].update_one(
            {"_id": f"stock_daily:{code}"},
            {"$set": {
                "module": "stock_daily",
                "symbol": code,
                "last_dt": docs[-1]["dt"],
                "last_run": _sync_now(),
                "status": "ok",
                "bar_count": len(docs),
                "written": len(new_docs),
                "source": "concept_carrier_preheat",
            }},
            upsert=True,
        )
        return True
    except Exception:
        return False


def _ensure_minute_bars(symbol: str, raw_code: str, freq: str) -> bool:
    requested = _canonical_freq(freq)
    minute_freq = {
        "5min": "5分钟",
        "15min": "15分钟",
        "30min": "30分钟",
    }.get(requested)
    if not minute_freq:
        return True
    df, _, _ = _stock_kline_df(symbol, requested)
    attrs = getattr(df, "attrs", {}) or {}
    if df is not None and not df.empty and not bool(attrs.get("gateway_is_stale")):
        return True
    code = raw_code or symbol.split(".", 1)[-1]
    if not code or not code.isdigit():
        return False
    try:
        from signals.sync.modules.stock_minute import _sync_one_minute

        docs = _sync_one_minute(code, minute_freq)
        if not docs:
            return False
        db = _mongo_db()
        db["bars"].delete_many({"meta.symbol": code, "meta.freq": minute_freq})
        db["bars"].insert_many(docs, ordered=False)
        db["sync_log"].update_one(
            {"_id": f"stock_minute:{code}:{minute_freq}"},
            {"$set": {
                "module": "stock_minute",
                "symbol": code,
                "last_dt": docs[-1]["dt"],
                "last_run": _sync_now(),
                "status": "ok",
                "bar_count": len(docs),
                "source": docs[-1].get("meta", {}).get("source"),
            }},
            upsert=True,
        )
        return True
    except Exception:
        return False


def _stock_chart_load_eta_seconds(freq: str) -> int:
    canonical = _canonical_freq(freq)
    if canonical in {"5min", "15min", "30min"}:
        return 10
    if canonical == "daily":
        return 15
    if canonical in {"weekly", "monthly"}:
        return 20
    return 15


def _stock_chart_load_source(freq: str) -> str:
    canonical = _canonical_freq(freq)
    if canonical in {"5min", "15min", "30min"}:
        return "stock_minute:5min"
    if canonical in {"daily", "weekly", "monthly"}:
        return "stock_daily"
    return "stock_cache"


def _stock_chart_load_key(symbol: str, freq: str) -> str:
    return f"stock:{_text(symbol).upper()}:{_canonical_freq(freq)}"


def _stock_chart_load_meta(symbol: str, freq: str, job: dict[str, Any], *, triggered: bool = False) -> dict[str, Any]:
    eta = int(job.get("load_eta_seconds") or _stock_chart_load_eta_seconds(freq))
    return {
        "load_status": _text(job.get("load_status")) or "running",
        "load_triggered": bool(triggered or job.get("load_triggered")),
        "load_target_symbol": _text(symbol),
        "load_target_freq": _canonical_freq(freq),
        "load_source": _text(job.get("load_source")) or _stock_chart_load_source(freq),
        "load_eta_seconds": eta,
        "load_retry_after_seconds": max(5, min(30, eta + 2)),
        "load_started_at": _text(job.get("load_started_at")),
        "load_finished_at": _text(job.get("load_finished_at")),
        "load_elapsed_seconds": job.get("load_elapsed_seconds"),
        "load_error": _text(job.get("load_error")),
    }


def _clear_stock_chart_load_job(symbol: str, freq: str) -> None:
    key = _stock_chart_load_key(symbol, _canonical_freq(freq))
    with _CHART_LOAD_LOCK:
        _CHART_LOAD_JOBS.pop(key, None)


def _load_stock_chart_data(symbol: str, raw_code: str, freq: str) -> bool:
    canonical = _canonical_freq(freq)
    if canonical in {"5min", "15min", "30min"}:
        return _ensure_minute_bars(symbol, raw_code, canonical)
    if canonical in {"daily", "weekly", "monthly"}:
        return _ensure_daily_bars(symbol, raw_code)
    return False


def _manual_clue_preheat_freqs(freq: str) -> list[str]:
    canonical = _canonical_freq(freq)
    ordered = [canonical]
    if canonical in {"5min", "15min", "30min"}:
        ordered.extend(["daily", "30min", "15min", "5min"])
    elif canonical in {"daily", "weekly", "monthly"}:
        ordered.extend(["daily", "30min", "15min", "5min"])
    else:
        ordered.extend(["daily", DEFAULT_TERMINAL_FREQ, "15min", "5min"])
    output: list[str] = []
    for item in ordered:
        normalized = _canonical_freq(item)
        if normalized not in output:
            output.append(normalized)
    return output


def _trigger_manual_clue_cache_load(symbol: str, raw_code: str, freq: str) -> dict[str, Any]:
    jobs = [
        _trigger_stock_chart_load(symbol, raw_code, item)
        for item in _manual_clue_preheat_freqs(freq)
    ]
    requested = _canonical_freq(freq)
    primary = next((job for job in jobs if _text(job.get("load_target_freq")) == requested), jobs[0] if jobs else {})
    return {
        **primary,
        "load_bundle": jobs,
        "load_bundle_freqs": [job.get("load_target_freq") for job in jobs if job.get("load_target_freq")],
    }


def _trigger_stock_chart_load(symbol: str, raw_code: str, freq: str) -> dict[str, Any]:
    canonical = _canonical_freq(freq)
    key = _stock_chart_load_key(symbol, canonical)
    now_monotonic = time.monotonic()
    eta = _stock_chart_load_eta_seconds(canonical)
    with _CHART_LOAD_LOCK:
        existing = _CHART_LOAD_JOBS.get(key)
        if existing:
            age = now_monotonic - float(existing.get("monotonic_started_at") or now_monotonic)
            status = _text(existing.get("load_status"))
            if status in {"triggered", "running"}:
                return _stock_chart_load_meta(symbol, canonical, existing)
            if status == "failed" and age < 30:
                return _stock_chart_load_meta(symbol, canonical, existing)
            if status == "ready" and age < _CHART_LOAD_JOB_TTL_SECONDS:
                return _stock_chart_load_meta(symbol, canonical, existing)

        job = {
            "load_status": "triggered",
            "load_triggered": True,
            "load_source": _stock_chart_load_source(canonical),
            "load_eta_seconds": eta,
            "load_started_at": _serialize_dt(_sync_now()),
            "monotonic_started_at": now_monotonic,
            "load_error": "",
        }
        _CHART_LOAD_JOBS[key] = job

    def _runner() -> None:
        started = time.monotonic()
        with _CHART_LOAD_LOCK:
            if key in _CHART_LOAD_JOBS:
                _CHART_LOAD_JOBS[key]["load_status"] = "running"
        error = ""
        ok = False
        try:
            ok = _load_stock_chart_data(symbol, raw_code, canonical)
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
            ok = False
        with _CHART_LOAD_LOCK:
            current = _CHART_LOAD_JOBS.get(key, {})
            current.update({
                "load_status": "ready" if ok else "failed",
                "load_finished_at": _serialize_dt(_sync_now()),
                "load_elapsed_seconds": round(time.monotonic() - started, 2),
                "load_error": error if error else ("" if ok else "provider_returned_empty"),
                "monotonic_started_at": started,
            })
            _CHART_LOAD_JOBS[key] = current

    threading.Thread(target=_runner, name=f"stock-chart-load-{key}", daemon=True).start()
    return _stock_chart_load_meta(symbol, canonical, job, triggered=True)


def _attach_chart_load_meta(chart: dict[str, Any], load_meta: dict[str, Any]) -> dict[str, Any]:
    if not load_meta:
        return chart
    meta = dict(chart.get("meta") or {})
    meta.update(load_meta)
    chart["meta"] = meta
    return chart


def _concept_rank_rows(concept_name: str, theme_candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = [concept_name] + [_text(item.get("name")) for item in theme_candidates]
    names = [name for index, name in enumerate(names) if name and name not in names[:index]]
    rows: list[dict[str, Any]] = []
    try:
        db = _mongo_db()
    except Exception:
        return rows
    for collection in ("concept_sina", "concept_em", "concept_ths", "concept_ranking"):
        if collection not in db.list_collection_names():
            continue
        for name in names:
            query = {"$or": [
                {"board_name": {"$regex": name}},
                {"concept": {"$regex": name}},
                {"concept_name": {"$regex": name}},
            ]}
            for row in db[collection].find(query).sort("dt", -1).limit(8):
                item = dict(row)
                item.setdefault("source", collection)
                rows.append(item)
    return rows


def _industry_constituent_symbols(industry_name: str) -> list[str]:
    symbols: list[str] = []
    try:
        db = _mongo_db()
    except Exception:
        return symbols
    query = {"$or": [{"_id": industry_name}, {"board_name": industry_name}, {"concept_name": industry_name}]}
    for collection in ("board_constituents", "concept_constituents"):
        if collection not in db.list_collection_names():
            continue
        for row in db[collection].find(query).sort("updated_at", -1).limit(4):
            for symbol in row.get("symbols") or []:
                normalized, _ = _normalize_stock_symbol(str(symbol))
                if normalized and normalized not in symbols:
                    symbols.append(normalized)
    return symbols


def _concept_constituent_symbols(concept_name: str, theme_candidates: list[dict[str, Any]]) -> list[str]:
    names = [concept_name] + [_text(item.get("name")) for item in theme_candidates]
    names = [name for index, name in enumerate(names) if name and name not in names[:index]]
    symbols: list[str] = []
    try:
        db = _mongo_db()
    except Exception:
        return symbols
    if "concept_constituents" not in db.list_collection_names():
        return symbols
    for name in names:
        query = {"$or": [{"_id": name}, {"concept_name": name}, {"board_name": name}, {"concept": name}]}
        for row in db["concept_constituents"].find(query).sort("updated_at", -1).limit(4):
            for symbol in row.get("symbols") or []:
                normalized, _ = _normalize_stock_symbol(str(symbol))
                if normalized and normalized not in symbols:
                    symbols.append(normalized)
    return symbols


def _industry_leader_candidate(industry_name: str) -> Optional[dict[str, Any]]:
    try:
        from signals.layers.industry import _INDUSTRY_LEADERS
    except Exception:
        return None
    leader = _INDUSTRY_LEADERS.get(industry_name)
    if not leader:
        return None
    symbol, name = leader
    normalized, raw_code = _normalize_stock_symbol(symbol)
    if not normalized:
        return None
    return {
        "symbol": normalized,
        "raw_code": raw_code or normalized.split(".", 1)[-1],
        "name": name,
        "source": "industry_leader_map",
        "relation": f"{industry_name} 龙头",
        "priority": 64,
    }


def _available_daily_carrier(
    candidates: list[dict[str, Any]],
    *,
    preserve_order: bool = False,
) -> Optional[dict[str, Any]]:
    available: list[dict[str, Any]] = []
    for item in candidates:
        symbol = _text(item.get("symbol"))
        raw_code = _text(item.get("raw_code"))
        if not symbol:
            symbol, raw_code = _stock_symbol_from_code_or_name(item.get("code"), item.get("name"))
        if not symbol:
            continue
        df, source = _stock_df(symbol, "daily")
        if df is None or df.empty:
            continue
        available.append({
            **item,
            "symbol": symbol,
            "raw_code": raw_code or symbol.split(".", 1)[-1],
            "name": _text(item.get("name")) or _stock_name(symbol),
            "bar_count": int(len(df)),
            "bar_source": source,
        })
    if not available:
        return None
    if preserve_order:
        return available[0]
    available.sort(key=lambda item: (int(item.get("priority") or 0), int(item.get("bar_count") or 0)), reverse=True)
    return available[0]


def _cached_daily_carrier(
    candidates: list[dict[str, Any]],
    *,
    preserve_order: bool = False,
) -> Optional[dict[str, Any]]:
    available: list[dict[str, Any]] = []
    for item in candidates:
        symbol = _text(item.get("symbol"))
        raw_code = _text(item.get("raw_code"))
        if not symbol:
            symbol, raw_code = _stock_symbol_from_code_or_name(item.get("code"), item.get("name"))
        if not symbol:
            continue
        df, source = _stock_df(symbol, "daily")
        if df is None or df.empty:
            continue
        available.append({
            **item,
            "symbol": symbol,
            "raw_code": raw_code or symbol.split(".", 1)[-1],
            "name": _text(item.get("name")) or _stock_name(symbol),
            "bar_count": int(len(df)),
            "bar_source": source,
        })
    if not available:
        return None
    if preserve_order:
        return available[0]
    available.sort(key=lambda item: (int(item.get("priority") or 0), int(item.get("bar_count") or 0)), reverse=True)
    return available[0]


def _concept_carrier_candidates(
    concept_name: str,
    theme_candidates: list[dict[str, Any]],
    related_industries: list[str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    non_chain = bool(non_chain_reason(concept_name))

    def add(
        symbol: str = "",
        raw_code: str = "",
        name: str = "",
        source: str = "",
        relation: str = "",
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        if not symbol:
            symbol, raw_code = _stock_symbol_from_code_or_name(raw_code, name)
        if not symbol:
            return
        key = symbol.upper()
        rep_type = _text((extra or {}).get("representative_type"))
        if any(
            _text(item.get("symbol")).upper() == key
            and _text(item.get("representative_type")) == rep_type
            for item in candidates
        ):
            return
        candidates.append({
            "symbol": symbol,
            "raw_code": raw_code or symbol.split(".", 1)[-1],
            "name": name or _stock_name(symbol),
            "source": source,
            "relation": relation,
        })
        if extra:
            candidates[-1].update(extra)

    if not non_chain:
        for item in _preferred_concept_carriers(concept_name, theme_candidates, related_industries):
            add(
                symbol=_text(item.get("symbol")),
                name=_text(item.get("name")),
                source=_text(item.get("source")),
                relation=_text(item.get("relation")),
                extra={
                    "priority": item.get("priority"),
                    "base_priority": item.get("base_priority"),
                    "chain_id": item.get("chain_id"),
                    "chain_name": item.get("chain_name"),
                    "node_id": item.get("node_id"),
                    "node_name": item.get("node_name"),
                    "layer": item.get("layer"),
                    "stage": item.get("stage"),
                    "representative_type": item.get("representative_type"),
                    "source_note": item.get("source_note"),
                    "confidence": item.get("confidence"),
                    "hit_terms": item.get("hit_terms"),
                    "evidence_sources": item.get("evidence_sources"),
                },
            )

    for row in _concept_rank_rows(concept_name, theme_candidates):
        add(
            raw_code=_text(row.get("leader_code")),
            name=_text(row.get("leader_name") or row.get("leader")),
            source=_text(row.get("source")) or "concept_rank",
            relation=_text(row.get("board_name") or row.get("concept") or row.get("concept_name") or concept_name),
            extra={
                "representative_type": "source_leader",
                "source_rank": row.get("rank"),
                "source_dt": str(row.get("dt") or row.get("date") or ""),
            },
        )
    for theme in theme_candidates:
        add(
            name=_text(theme.get("leader")),
            source="strategy_snapshot",
            relation=_text(theme.get("name")) or concept_name,
            extra={"representative_type": "source_leader"},
        )

    for symbol in _concept_constituent_symbols(concept_name, theme_candidates):
        add(
            symbol=symbol,
            source="concept_constituents",
            relation=concept_name,
            extra={"representative_type": "concept_constituent"},
        )

    if not non_chain:
        for industry in related_industries:
            leader = _industry_leader_candidate(industry)
            if leader:
                before = len(candidates)
                add(
                    symbol=_text(leader.get("symbol")),
                    raw_code=_text(leader.get("raw_code")),
                    name=_text(leader.get("name")),
                    source=_text(leader.get("source")),
                    relation=_text(leader.get("relation")),
                    extra={"representative_type": "industry_leader"},
                )
                if len(candidates) > before:
                    candidates[-1]["priority"] = leader.get("priority")
            for symbol in _industry_constituent_symbols(industry):
                add(
                    symbol=symbol,
                    source="industry_constituents",
                    relation=industry,
                    extra={"representative_type": "industry_constituent"},
                )
    return candidates


def _representative_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": item.get("symbol"),
        "raw_code": item.get("raw_code"),
        "name": item.get("name"),
        "relation": item.get("relation"),
        "source": item.get("source"),
        "source_note": item.get("source_note"),
        "representative_type": item.get("representative_type"),
        "priority": item.get("priority"),
        "base_priority": item.get("base_priority"),
        "chain_id": item.get("chain_id"),
        "chain_name": item.get("chain_name"),
        "node_id": item.get("node_id"),
        "node_name": item.get("node_name"),
        "layer": item.get("layer"),
        "stage": item.get("stage"),
        "confidence": item.get("confidence"),
        "hit_terms": item.get("hit_terms") or [],
        "evidence_sources": item.get("evidence_sources") or [],
        "bar_source": item.get("bar_source"),
        "bar_count": item.get("bar_count"),
    }


def _concept_representative_groups(candidates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {"core": [], "elastic": [], "source_leader": [], "constituent": []}
    seen: dict[str, set[str]] = {key: set() for key in groups}
    for item in candidates:
        rep_type = _text(item.get("representative_type"))
        if rep_type not in groups:
            if item.get("source") in {"concept_constituents", "industry_constituents"}:
                rep_type = "constituent"
            elif item.get("source") in {"concept_rank", "strategy_snapshot", "concept_sina", "concept_em", "concept_ths"}:
                rep_type = "source_leader"
            else:
                continue
        symbol = _text(item.get("symbol")).upper()
        if not symbol or symbol in seen[rep_type]:
            continue
        seen[rep_type].add(symbol)
        groups[rep_type].append(_representative_payload(item))
    return {key: value[:8] for key, value in groups.items()}


def _ordered_candidate_stocks(
    candidates: list[dict[str, Any]],
    *,
    heat_value: Any = None,
) -> list[dict[str, Any]]:
    groups = _candidate_groups(candidates, heat_value=heat_value)
    return _flatten_candidate_groups(groups, limit=20)


def _summary_from_index(report: Dict[str, Any], chart: Dict[str, Any]) -> Dict[str, Any]:
    chart_report = chart.get("report") or {}
    ma_context = report.get("ma_context") or {}
    engine = get_engine()
    market_context = engine.get_market_context()
    style_switch = getattr(market_context, "style_switch", None) if market_context else None
    summary = {
        "title": report.get("name", ""),
        "subtitle": report.get("symbol", ""),
        "latest_price": report.get("latest_price", 0),
        "conclusion": chart_report.get("conclusion") or report.get("summary", ""),
        "daily_trend": report.get("daily_trend", ""),
        "f30_trend": report.get("f30_trend", ""),
        "f15_trend": report.get("f15_trend", ""),
        "latest_signal": chart_report.get("daily_latest_signal") or report.get("daily_latest_signal", ""),
        "key_levels": chart_report.get("key_levels") or ma_context.get("key_levels") or [],
        "style_switch": style_switch.suggestion if style_switch else "",
    }
    summary.update(_quote_overlay_for_symbol(str(report.get("symbol") or "")))
    if summary.get("today_change_pct") is not None:
        summary["gain_pct"] = summary.get("today_change_pct")
    return summary


def _summary_from_static_index(name: str, symbol: str, chart: Dict[str, Any]) -> Dict[str, Any]:
    last_close = chart.get("ohlcv", [{}])[-1].get("close", 0) if chart.get("ohlcv") else 0
    summary = {
        "title": name,
        "subtitle": symbol,
        "latest_price": last_close,
        "conclusion": "引擎热身中，先使用本地指数K线缓存。",
        "daily_trend": "",
        "f30_trend": "",
        "f15_trend": "",
        "latest_signal": "",
        "key_levels": [],
    }
    summary.update(_quote_overlay_for_symbol(symbol))
    if summary.get("today_change_pct") is not None:
        summary["gain_pct"] = summary.get("today_change_pct")
    chain_position = _stock_chain_position_summary(symbol)
    if chain_position:
        trade_role = _trade_role_for_stock_summary(chain_position)
        chain_text = " · ".join(
            value
            for value in [_text(chain_position.get("chain")), _text(chain_position.get("node") or chain_position.get("role"))]
            if value
        )
        summary.update({
            "chain_position": chain_position,
            "trade_role": trade_role,
            "trade_role_label": {
                "chain_watch": "产业链观察",
                "mainline_attack": "主线机会",
                "climax_risk": "过热禁追",
                "defensive_weight": "防守观察",
                "second_wave": "回踩再起",
            }.get(trade_role, "观察"),
            "trader_read": _stock_summary_trade_read(chain_position, trade_role),
            "evidence_summary": "；".join([
                f"产业链: {chain_text}" if chain_text else "",
                f"产业链来源: {_text(chain_position.get('source_note'))}" if _text(chain_position.get("source_note")) else "",
                f"图表: {summary.get('conclusion') or summary.get('latest_signal') or '等待确认'}",
            ]).strip("；"),
        })
    return summary


def _summary_from_industry(name: str, detail: Dict[str, Any], ranking) -> Dict[str, Any]:
    report = detail.get("report") or {}
    info = detail.get("industry_info") or {}
    conclusion = "震荡观察"
    if report.get("has_buy_signal"):
        conclusion = "行业趋势偏强，可结合候选股观察入场。"
    elif report.get("has_sell_signal"):
        conclusion = "行业处于分歧或退潮，优先防守。"
    return {
        "title": name,
        "subtitle": info.get("rotation_line", ""),
        "latest_price": detail.get("ohlcv", [{}])[-1].get("close", 0) if detail.get("ohlcv") else 0,
        "conclusion": conclusion,
        "daily_trend": report.get("daily_trend", ""),
        "f30_trend": "",
        "f15_trend": "",
        "latest_signal": report.get("daily_latest_signal", ""),
        "key_levels": [],
        "gain_pct": info.get("gain_pct", 0),
        "composite_score": info.get("composite_score", 0),
        "phase": info.get("phase", ""),
        "phase_hint": info.get("phase_hint", ""),
        "candidate_count": len(ranking.candidates) if ranking else 0,
    }


def _stock_chain_position_summary(symbol: str) -> dict[str, Any]:
    try:
        from signals.core.chain_map import get_all_chain_positions

        positions = get_all_chain_positions(symbol)
    except Exception:
        positions = []
    if not positions:
        return {}
    primary = positions[0]
    return {
        "chain": _text(getattr(primary, "chain_name", "")),
        "node": _text(getattr(primary, "role", "")),
        "role": _text(getattr(primary, "role", "")),
        "layer": _text(getattr(primary, "position", "")),
        "stage": _text(getattr(primary, "position", "")),
        "source": "industry_chains.yaml",
        "source_note": "代表标的静态映射",
        "confidence": "representative_only",
        "related_chains": list(getattr(primary, "related_chains", []) or [])[:3],
    }


def _trade_role_for_stock_summary(chain_position: dict[str, Any]) -> str:
    phase = _text(chain_position.get("phase"))
    if phase == "consensus_climax":
        return "climax_risk"
    if phase in {"cooling", "diverging"}:
        return "second_wave"
    if phase in {"accelerating", "warming"}:
        return "mainline_attack"
    return "chain_watch"


def _stock_summary_trade_read(chain_position: dict[str, Any], role: str) -> str:
    chain = " · ".join(
        value
        for value in [_text(chain_position.get("chain")), _text(chain_position.get("node") or chain_position.get("role"))]
        if value
    )
    if role == "chain_watch":
        return f"{chain or '产业链'}：不在当前买点池时只按产业链观察，不代表真实持仓；等重新进入盯盘/确认买点。"
    if role == "mainline_attack":
        return f"{chain or '主线'}：按主线机会观察，等分歧承接和右侧买点确认。"
    if role == "climax_risk":
        return f"{chain or '主线'}：一致过热，不追高，等分歧后核心票重新承接。"
    if role == "defensive_weight":
        return f"{chain or '防守观察'}：偏稳仓/防守，不和进攻票混排，没进池前只看图表位置。"
    if role == "second_wave":
        return f"{chain or '回踩再起'}：当前不在池内，先按回踩后二次启动观察，等右侧重新确认。"
    return f"{chain or '观察标的'}：当前不在机会池，先看图表证据，不作为执行买点。"


def _summary_from_stock(symbol: str, stock: Dict[str, Any], chart: Dict[str, Any]) -> Dict[str, Any]:
    scored = stock.get("scored") or {}
    ma_context = stock.get("ma_context") or {}
    risk = stock.get("risk") or {}
    last_close = chart.get("ohlcv", [{}])[-1].get("close", 0) if chart.get("ohlcv") else 0
    conclusion = scored.get("direction", "")
    if risk.get("description"):
        conclusion = f"{conclusion} · {risk['description']}".strip(" ·")
    summary = {
        "title": stock.get("name") or symbol,
        "subtitle": symbol,
        "latest_price": last_close,
        "conclusion": conclusion or "等待更多确认",
        "daily_trend": ma_context.get("trend_summary", ""),
        "f30_trend": "",
        "f15_trend": "",
        "latest_signal": chart.get("signals", [{}])[-1].get("type", "") if chart.get("signals") else "",
        "key_levels": ma_context.get("key_levels") or [],
        "score": scored.get("total_score"),
        "fused_total": scored.get("fused_total"),
        "risk_reward": risk.get("risk_reward"),
        "position_pct": risk.get("position_pct"),
    }
    day_change_mode = _a_day_change_mode()
    if day_change_mode == "daily_close":
        try:
            daily_df, _daily_source = _stock_df(symbol, "daily")
            daily_day_change, daily_day_source, daily_as_of = _daily_close_day_change_pct(daily_df)
            latest_daily_close = (
                float(daily_df["close"].iloc[-1])
                if daily_as_of == _day_change_expected_day("daily_close")
                and daily_df is not None
                and not daily_df.empty
                and "close" in daily_df.columns
                else None
            )
        except Exception:
            daily_day_change, daily_day_source, daily_as_of, latest_daily_close = None, "", "", None
        summary.update({
            "day_change_pct": daily_day_change,
            "daily_change_pct": daily_day_change,
            "day_change_source": daily_day_source,
            "day_change_mode": day_change_mode,
            "day_change_as_of": daily_as_of,
        })
        if latest_daily_close is not None:
            summary["latest_price"] = latest_daily_close
    summary.update(_latest_daily_trading_values(symbol, chart))
    summary.update(_quote_overlay_for_symbol(symbol))
    if summary.get("today_change_pct") is not None:
        summary["gain_pct"] = summary.get("today_change_pct")
    chain_position = _stock_chain_position_summary(symbol)
    if chain_position:
        trade_role = _trade_role_for_stock_summary(chain_position)
        chain_text = " · ".join(
            value
            for value in [_text(chain_position.get("chain")), _text(chain_position.get("node") or chain_position.get("role"))]
            if value
        )
        summary.update({
            "chain_position": chain_position,
            "trade_role": trade_role,
            "trade_role_label": {
                "chain_watch": "产业链观察",
                "mainline_attack": "主线机会",
                "climax_risk": "过热禁追",
                "defensive_weight": "防守观察",
                "second_wave": "回踩再起",
            }.get(trade_role, "观察"),
            "trader_read": _stock_summary_trade_read(chain_position, trade_role),
            "evidence_summary": "；".join([
                f"产业链: {chain_text}" if chain_text else "",
                f"产业链来源: {_text(chain_position.get('source_note'))}" if _text(chain_position.get("source_note")) else "",
                f"图表: {summary.get('conclusion') or summary.get('latest_signal') or '等待确认'}",
            ]).strip("；"),
        })
    return summary


async def _build_index_target(engine, name: str, freq: str) -> Dict[str, Any]:
    requested_freq = _canonical_freq(freq)
    report_obj = next((item for item in engine.get_index_reports() if item.name == name), None)
    if report_obj is None:
        static_index = _resolve_static_index(name)
        if static_index is None:
            raise HTTPException(status_code=404, detail=f"未找到指数: {name}")
        return await _build_static_index_target(static_index[0], static_index[1], requested_freq)

    report = serialize_index_report(report_obj)
    df, source = _index_df(str(report.get("symbol") or name), requested_freq)
    chart = _chart_from_df(df, symbol=str(report.get("symbol") or name), freq=requested_freq, source=source)
    chart = _mark_chart_readiness(chart, kind="index", requested_freq=requested_freq)
    plan = _plan_for_index(engine, name)
    analysis_target = _top_candidate_symbol(engine)
    candidate_stocks = [serialize_scored_symbol(item) for item in engine.get_scored_symbols()[:10]]
    related_custom_signals = _related_custom_signals_from_candidates(candidate_stocks, requested_freq)

    return {
        "target": {
            "kind": "index",
            "label": name,
            "symbol": report.get("symbol", ""),
            **_target_time_fields(market="A", symbol=report.get("symbol", ""), source=chart.get("meta", {}).get("source", "")),
            "requested_freq": requested_freq,
            "effective_freq": requested_freq,
            "available_freqs": UI_FREQS,
            "fallback_reason": "",
            "not_ready_reason": chart.get("meta", {}).get("not_ready_reason", ""),
            **_target_diagnostics("index", str(report.get("symbol") or name), requested_freq),
        },
        "chart": chart,
        "summary": _summary_from_index(report, chart),
        "signals": chart.get("signals", []),
        "plan": plan,
        "review": _review_context(engine, "index", name),
        "trade": _trade_context(None),
        "analysis_target": analysis_target,
        "candidate_stocks": candidate_stocks,
        "custom_signal_count": 0,
        "direct_custom_signal_count": 0,
        "visible_custom_signal_count": 0,
        "hidden_custom_signal_count": 0,
        "available_custom_signal_freqs": [],
        "hidden_reasons": ["index_has_no_direct_custom_signal"],
        "related_custom_signals": related_custom_signals,
    }


async def _build_static_index_target(name: str, symbol: str, freq: str) -> Dict[str, Any]:
    requested_freq = _canonical_freq(freq)
    df, source = _index_df(symbol, requested_freq)
    chart = _chart_from_df(df, symbol=symbol, freq=requested_freq, source=source)
    chart = _mark_chart_readiness(chart, kind="index", requested_freq=requested_freq)
    summary = _summary_from_static_index(name, symbol, chart)
    summary["latest_signal"] = summary.get("latest_signal") or _ma_signal_from_df(df)
    try:
        engine = _ensure_engine()
        candidate_stocks = [serialize_scored_symbol(item) for item in engine.get_scored_symbols()[:10]]
    except Exception:
        engine = None
        candidate_stocks = []
    if not candidate_stocks:
        candidate_stocks = _recent_custom_signal_candidates(limit=10)
    related_custom_signals = _related_custom_signals_from_candidates(candidate_stocks, requested_freq)
    return {
        "target": {
            "kind": "index",
            "label": name,
            "symbol": symbol,
            **_target_time_fields(market="A", symbol=symbol, source=chart.get("meta", {}).get("source", "")),
            "requested_freq": requested_freq,
            "effective_freq": requested_freq,
            "available_freqs": UI_FREQS,
            "fallback_reason": "",
            "not_ready_reason": chart.get("meta", {}).get("not_ready_reason", ""),
            **_target_diagnostics("index", symbol, requested_freq),
        },
        "chart": chart,
        "summary": summary,
        "signals": chart.get("signals", []),
        "plan": None,
        "review": _review_context(engine, "index", name) if engine is not None else {},
        "trade": _trade_context(None),
        "analysis_target": "",
        "candidate_stocks": candidate_stocks,
        "custom_signal_count": 0,
        "direct_custom_signal_count": 0,
        "visible_custom_signal_count": 0,
        "hidden_custom_signal_count": 0,
        "available_custom_signal_freqs": [],
        "hidden_reasons": ["index_has_no_direct_custom_signal"],
        "related_custom_signals": related_custom_signals,
    }


async def _build_industry_target(engine, name: str, freq: str) -> Dict[str, Any]:
    requested_freq = _canonical_freq(freq)
    ranking = engine.get_industry_ranking_by_name(name)
    candidate_stocks = []
    analysis_target = ""
    if ranking:
        candidate_stocks = [
            {
                "code": candidate.code,
                "name": candidate.name,
                "role": candidate.role,
                "priority": candidate.priority,
                "detail": candidate.detail,
            }
            for candidate in ranking.candidates[:10]
        ]
        if candidate_stocks:
            analysis_target = candidate_stocks[0]["code"]

    leader_name = candidate_stocks[0]["name"] if candidate_stocks else ""
    carrier_candidates = _industry_carrier_candidates(name, leader_name)
    ranking_candidate_items: list[dict[str, Any]] = []
    for candidate in candidate_stocks:
        symbol, raw_code = _stock_symbol_from_code_or_name(candidate.get("code"), candidate.get("name"))
        if not symbol:
            continue
        ranking_candidate_items.append({
            "symbol": symbol,
            "raw_code": raw_code,
            "name": candidate.get("name"),
            "relation": candidate.get("role") or f"{name} 成分候选",
            "source": "industry_candidates",
            "representative_type": "industry_candidate",
            "priority": candidate.get("priority"),
            "detail": candidate.get("detail"),
        })
    all_candidates = carrier_candidates + ranking_candidate_items
    heat_value = getattr(ranking, "gain_pct", None) if ranking else None
    candidate_groups = _candidate_groups(all_candidates, heat_value=heat_value)
    ordered_candidates = _flatten_candidate_groups(candidate_groups, limit=20)
    carrier = _preview_carrier(carrier_candidates)

    if requested_freq in {"5min", "15min", "30min"}:
        chart, latest_heat = _board_heat_chart(name, "industry", requested_freq)
        heat_target_label = _text(latest_heat.get("heat_target_label")) or name
        heat_resolution_status = _text(latest_heat.get("heat_resolution_status")) or ("exact" if heat_target_label == name else "unresolved")
        heat_ready = _chart_has_ohlcv(chart)
        data_truth = _data_truth_payload(
            collection="board_heat_ticks",
            domain="board_heat",
            source="board_heat_ticks",
            chart_meta=chart.get("meta") if isinstance(chart.get("meta"), dict) else {},
            extra={
                "mapping_status": "direct_board_heat" if heat_ready else "heat_not_ready",
                "heat_target_label": heat_target_label,
                "heat_resolution_status": heat_resolution_status,
            },
        )
        related_custom_signals = _related_custom_signals_from_candidates(ordered_candidates, requested_freq)
        return {
            "target": {
                "kind": "industry",
                "label": name,
                "symbol": heat_target_label,
                "heat_target_label": heat_target_label,
                "heat_resolution_status": heat_resolution_status,
                **_target_time_fields(market="A", symbol=heat_target_label, source="board_heat_ticks"),
                "requested_freq": requested_freq,
                "effective_freq": requested_freq,
                "available_freqs": UI_FREQS,
                "mapping_status": "direct_board_heat" if heat_ready else "heat_not_ready",
                "unmapped_reason": "",
                "fallback_reason": "",
                "not_ready_reason": chart.get("meta", {}).get("not_ready_reason", ""),
                "cache_probe": {
                    "status": "hit" if heat_ready else "miss",
                    "kind": "industry",
                    "requested_freq": requested_freq,
                    "collection": "board_heat_ticks",
                    "latest_dt": _serialize_dt(latest_heat.get("trade_minute")),
                    "source": latest_heat.get("source", ""),
                    "query_label": name,
                    "heat_target_label": heat_target_label,
                    "heat_resolution_status": heat_resolution_status,
                },
            },
            "chart": chart,
            "summary": {
                "title": name,
                "subtitle": "行业热度K线/涨跌幅OHLC",
                "latest_price": chart.get("ohlcv", [{}])[-1].get("close", 0) if chart.get("ohlcv") else 0,
                "conclusion": "行业图形来自东财板块快照 change_pct 重采样，不是成分股价格K线。" if heat_ready else "行业分钟热度缓存未就绪。",
                "gain_pct": latest_heat.get("change_pct"),
                "composite_score": getattr(ranking, "composite_score", 0) if ranking else 0,
                "leader": latest_heat.get("leader_name", ""),
                "up_count": latest_heat.get("up_count"),
                "down_count": latest_heat.get("down_count"),
                "mapping_status": "direct_board_heat" if heat_ready else "heat_not_ready",
                "heat_target_label": heat_target_label,
                "heat_resolution_status": heat_resolution_status,
                "not_ready_reason": chart.get("meta", {}).get("not_ready_reason", ""),
            },
            "signals": [],
            "plan": None,
            "review": _review_context(engine, "industry", name),
            "trade": _trade_context(analysis_target or None),
            "analysis_target": analysis_target,
            "candidate_groups": candidate_groups,
            "candidate_stocks": ordered_candidates,
            "data_truth": data_truth,
            "viewpoint_context": {"status": "context_only", "items": []},
            "custom_signal_count": 0,
            "direct_custom_signal_count": 0,
            "visible_custom_signal_count": 0,
            "hidden_custom_signal_count": 0,
            "available_custom_signal_freqs": [],
            "hidden_reasons": ["board_heat_chart_has_no_direct_custom_signal"],
            "related_custom_signals": related_custom_signals,
        }

    async def fallback_to_carrier(reason: str) -> Dict[str, Any]:
        fallback = carrier
        if not fallback and candidate_stocks:
            symbol, raw_code = _stock_symbol_from_code_or_name(candidate_stocks[0].get("code"), candidate_stocks[0].get("name"))
            if symbol:
                fallback = {
                    "symbol": symbol,
                    "raw_code": raw_code,
                    "name": candidate_stocks[0].get("name"),
                    "relation": name,
                    "source": "industry_candidates",
                    "representative_type": "source_leader",
                }
        if not fallback:
            raise HTTPException(status_code=404, detail=f"无法获取 {name} K线数据，且未找到可承接代表股")
        payload = await _build_stock_target(fallback["symbol"], fallback.get("raw_code", ""), requested_freq)
        stock_title = payload.get("summary", {}).get("title") or fallback.get("name") or fallback["symbol"]
        mapping_chain = _mapping_chain_from_carrier(name, fallback, kind="industry")
        payload["target"] = {
            **payload.get("target", {}),
            "kind": "industry",
            "label": name,
            "symbol": fallback["symbol"],
            **_target_time_fields(symbol=fallback["symbol"], source=payload.get("chart", {}).get("meta", {}).get("source", "")),
            "requested_freq": requested_freq,
            "carrier_kind": "stock",
            "carrier_symbol": fallback["symbol"],
            "mapping_status": "mapped",
            "unmapped_reason": "",
            **_target_diagnostics("stock", fallback["symbol"], requested_freq),
        }
        payload["summary"] = {
            **payload.get("summary", {}),
            "title": name,
            "subtitle": f"行业承接 -> {stock_title}({fallback['symbol']})",
            "conclusion": f"{name} 行业板块 K 线暂不可用，已用代表股 {stock_title} 承接图形复核。",
            "candidate_count": len(candidate_stocks),
            "carrier": _representative_payload(fallback),
            "mapping_chain": mapping_chain,
            "fallback_reason": reason,
        }
        payload["candidate_groups"] = candidate_groups
        payload["candidate_stocks"] = ordered_candidates
        payload["analysis_target"] = fallback["symbol"]
        return payload

    try:
        detail = _unwrap_response(get_industry_detail(name))
    except HTTPException:
        return await fallback_to_carrier("industry_ohlcv_unavailable")
    if not detail.get("ohlcv"):
        return await fallback_to_carrier("industry_ohlcv_empty")

    return {
        "target": {
            "kind": "industry",
            "label": name,
            "symbol": name,
            **_target_time_fields(market="A", symbol=name, source="industry"),
            "requested_freq": requested_freq,
            "effective_freq": "daily",
            "available_freqs": ["daily"],
            "mapping_status": "direct_industry_kline",
            "unmapped_reason": "",
        },
        "chart": detail,
        "summary": _summary_from_industry(name, detail, ranking),
        "signals": detail.get("signals", []),
        "plan": None,
        "review": _review_context(engine, "industry", name),
        "trade": _trade_context(analysis_target or None),
        "analysis_target": analysis_target,
        "candidate_groups": candidate_groups,
        "candidate_stocks": ordered_candidates,
        "custom_signal_count": 0,
        "direct_custom_signal_count": 0,
        "visible_custom_signal_count": 0,
        "hidden_custom_signal_count": 0,
        "available_custom_signal_freqs": [],
        "hidden_reasons": ["industry_chart_has_no_direct_custom_signal"],
        "related_custom_signals": _related_custom_signals_from_candidates(ordered_candidates, requested_freq),
    }


async def _build_concept_target(engine, name: str, freq: str) -> Dict[str, Any]:
    requested_freq = _canonical_freq(freq)
    concept = next((item for item in engine.get_concepts() if getattr(item, "name", "") == name), None)
    theme_candidates = _concept_theme_candidates(name)
    theme = theme_candidates[0] if theme_candidates else {}
    related = list(getattr(concept, "related_industries", []) or [])
    if not related:
        try:
            from signals.layers.industry import _map_concept_to_industries

            for concept_key in [name] + [_text(item.get("name")) for item in theme_candidates]:
                for industry in _map_concept_to_industries(concept_key):
                    if industry not in related:
                        related.append(industry)
        except Exception:
            related = []

    carrier_candidates = _concept_carrier_candidates(name, theme_candidates, related)
    representatives = _concept_representative_groups(carrier_candidates)
    heat_value = getattr(concept, "gain_pct", None) or theme.get("change_pct") or theme.get("strength")
    candidate_groups = _candidate_groups(carrier_candidates, heat_value=heat_value)
    ordered_candidates = _flatten_candidate_groups(candidate_groups, limit=20)
    non_chain = non_chain_reason(name)
    if requested_freq in {"5min", "15min", "30min"}:
        chart, latest_heat = _board_heat_chart(name, "concept", requested_freq)
        heat_target_label = _text(latest_heat.get("heat_target_label")) or name
        heat_resolution_status = _text(latest_heat.get("heat_resolution_status")) or ("exact" if heat_target_label == name else "unresolved")
        heat_ready = _chart_has_ohlcv(chart)
        data_truth = _data_truth_payload(
            collection="board_heat_ticks",
            domain="board_heat",
            source="board_heat_ticks",
            chart_meta=chart.get("meta") if isinstance(chart.get("meta"), dict) else {},
            extra={
                "mapping_status": "direct_board_heat" if heat_ready else ("non_chain" if non_chain else "heat_not_ready"),
                "heat_target_label": heat_target_label,
                "heat_resolution_status": heat_resolution_status,
            },
        )
        related_custom_signals = _related_custom_signals_from_candidates(ordered_candidates, requested_freq)
        return {
            "target": {
                "kind": "concept",
                "label": name,
                "symbol": heat_target_label,
                "heat_target_label": heat_target_label,
                "heat_resolution_status": heat_resolution_status,
                **_target_time_fields(market="A", symbol=heat_target_label, source="board_heat_ticks"),
                "requested_freq": requested_freq,
                "effective_freq": requested_freq,
                "available_freqs": UI_FREQS,
                "mapping_status": "direct_board_heat" if heat_ready else ("non_chain" if non_chain else "heat_not_ready"),
                "unmapped_reason": non_chain or "",
                "fallback_reason": "",
                "not_ready_reason": chart.get("meta", {}).get("not_ready_reason", ""),
                "cache_probe": {
                    "status": "hit" if heat_ready else "miss",
                    "kind": "concept",
                    "requested_freq": requested_freq,
                    "collection": "board_heat_ticks",
                    "latest_dt": _serialize_dt(latest_heat.get("trade_minute")),
                    "source": latest_heat.get("source", ""),
                    "query_label": name,
                    "heat_target_label": heat_target_label,
                    "heat_resolution_status": heat_resolution_status,
                },
            },
            "chart": chart,
            "summary": {
                "title": name,
                "subtitle": "概念热度K线/涨跌幅OHLC",
                "latest_price": chart.get("ohlcv", [{}])[-1].get("close", 0) if chart.get("ohlcv") else 0,
                "conclusion": "概念图形来自东财概念快照 change_pct 重采样，不是成分股价格K线。" if heat_ready else "概念分钟热度缓存未就绪。",
                "gain_pct": latest_heat.get("change_pct") or heat_value,
                "leader": latest_heat.get("leader_name", ""),
                "up_count": latest_heat.get("up_count"),
                "down_count": latest_heat.get("down_count"),
                "representatives": representatives,
                "candidate_groups": candidate_groups,
                "mapping_chain": {
                    "query": name,
                    "concepts": [name],
                    "industries": related[:5],
                    "mapping_status": "direct_board_heat" if heat_ready else "heat_not_ready",
                    "unmapped_reason": non_chain or chart.get("meta", {}).get("not_ready_reason", ""),
                    "candidate_count": len(carrier_candidates),
                    "heat_target_label": heat_target_label,
                    "heat_resolution_status": heat_resolution_status,
                },
                "mapping_status": "direct_board_heat" if heat_ready else "heat_not_ready",
                "heat_target_label": heat_target_label,
                "heat_resolution_status": heat_resolution_status,
                "not_ready_reason": chart.get("meta", {}).get("not_ready_reason", ""),
            },
            "signals": [],
            "plan": None,
            "review": _review_context(engine, "concept", name),
            "trade": _trade_context(None),
            "analysis_target": "",
            "candidate_groups": candidate_groups,
            "candidate_stocks": ordered_candidates,
            "data_truth": data_truth,
            "viewpoint_context": {"status": "context_only", "items": []},
            "custom_signal_count": 0,
            "direct_custom_signal_count": 0,
            "visible_custom_signal_count": 0,
            "hidden_custom_signal_count": 0,
            "available_custom_signal_freqs": [],
            "hidden_reasons": ["board_heat_chart_has_no_direct_custom_signal"],
            "related_custom_signals": related_custom_signals,
        }
    constituent_candidates = [
        item for item in carrier_candidates
        if item.get("source") in {"concept_constituents", "industry_constituents"}
        or item.get("representative_type") in {"concept_constituent", "industry_constituent"}
    ]
    source_leader_candidates = [
        item for item in carrier_candidates
        if item.get("representative_type") in {"source_leader", "industry_leader"}
        or item.get("source") in {"concept_rank", "concept_ranking", "concept_sina", "concept_em", "concept_ths", "strategy_snapshot", "industry_leader_map"}
    ]
    core_candidates = [
        item for item in carrier_candidates
        if item.get("representative_type") == "core"
    ]
    semantic_candidates = [
        item for item in carrier_candidates
        if item.get("source") == "semantic_industry_chain"
    ]
    elastic_candidates = [
        item for item in carrier_candidates
        if item.get("representative_type") == "elastic"
    ]
    carrier = None if non_chain else (
        _available_daily_carrier(core_candidates, preserve_order=True)
        or _available_daily_carrier(semantic_candidates)
        or _available_daily_carrier(elastic_candidates)
        or _available_daily_carrier(source_leader_candidates)
        or _available_daily_carrier(constituent_candidates, preserve_order=True)
    )
    if carrier:
        payload = await _build_stock_target(carrier["symbol"], carrier["raw_code"], requested_freq)
        if not _chart_has_ohlcv(payload.get("chart", {})):
            df, source = _stock_df(carrier["symbol"], requested_freq)
            payload["chart"] = _chart_from_df(df, symbol=carrier["symbol"], freq=requested_freq, source=source)
        relation = _text(carrier.get("relation")) or name
        stock_title = payload.get("summary", {}).get("title") or carrier.get("name") or carrier["symbol"]
        concept_chain = [_text(item.get("name")) for item in theme_candidates]
        concept_chain = [item for item in concept_chain if item]
        if name not in concept_chain:
            concept_chain.insert(0, name)
        chain_name = _text(carrier.get("chain_name"))
        chain_stage = _text(carrier.get("stage"))
        node_id = _text(carrier.get("node_id"))
        node_name = _text(carrier.get("node_name"))
        layer = _text(carrier.get("layer"))
        semantic_path = [item for item in ["/".join(concept_chain[:3]), chain_name, chain_stage, relation] if item]
        mapping_chain = {
            "query": name,
            "concepts": concept_chain[:5],
            "industries": related[:5],
            "chain_id": carrier.get("chain_id"),
            "chain_name": chain_name,
            "node_id": node_id,
            "node_name": node_name,
            "layer": layer,
            "stage": chain_stage,
            "confidence": carrier.get("confidence"),
            "evidence_sources": carrier.get("evidence_sources") or [],
            "industry_chain": {
                "chain_id": carrier.get("chain_id"),
                "chain_name": chain_name,
                "name": chain_name,
                "node_id": node_id,
                "node_name": node_name,
                "layer": layer,
                "stage": chain_stage,
                "confidence": carrier.get("confidence"),
                "hit_terms": carrier.get("hit_terms") or [],
                "evidence_sources": carrier.get("evidence_sources") or [],
            } if chain_name else {},
            "carrier": {
                "symbol": carrier["symbol"],
                "name": stock_title,
                "relation": relation,
                "source": carrier.get("source"),
                "chain_name": chain_name,
                "node_id": node_id,
                "node_name": node_name,
                "layer": layer,
                "stage": chain_stage,
                "representative_type": carrier.get("representative_type"),
                "bar_source": carrier.get("bar_source"),
                "bar_count": carrier.get("bar_count"),
            },
            "mapping_status": "mapped",
            "unmapped_reason": "",
            "candidate_count": len(carrier_candidates),
            "carrier_source_order": [
                "constituents",
                "ranking_or_source_leader",
                "semantic_core",
                "semantic_industry_chain",
            ],
        }
        payload["target"] = {
            **payload.get("target", {}),
            "kind": "concept",
            "label": name,
            "symbol": getattr(concept, "code", "") or name,
            **_target_time_fields(symbol=carrier["symbol"], source=payload.get("chart", {}).get("meta", {}).get("source", "")),
            "requested_freq": requested_freq,
            "carrier_kind": "stock",
            "carrier_symbol": carrier["symbol"],
            "mapping_status": "mapped",
            "unmapped_reason": "",
            **_target_diagnostics("stock", carrier["symbol"], requested_freq),
        }
        payload["summary"] = {
            **payload.get("summary", {}),
            "title": name,
            "subtitle": f"{name} -> {' -> '.join(semantic_path)} -> {stock_title}",
            "conclusion": f"{name} 已映射到 {' -> '.join(semantic_path)}，选择 {stock_title}({carrier['symbol']}) 作为图形复核标的。",
            "gain_pct": getattr(concept, "gain_pct", None) or theme.get("change_pct"),
            "composite_score": getattr(concept, "composite_score", None) or theme.get("strength"),
            "carrier": mapping_chain["carrier"],
            "representatives": representatives,
            "candidate_groups": candidate_groups,
            "mapping_chain": mapping_chain,
            "mapping_status": "mapped",
            "unmapped_reason": "",
        }
        payload["analysis_target"] = carrier["symbol"]
        payload["candidate_groups"] = candidate_groups
        payload["candidate_stocks"] = ordered_candidates
        return payload

    unmapped_reason = non_chain or ("no_carrier_with_daily_cache" if carrier_candidates else "carrier_candidates_empty")
    return {
        "target": {
            "kind": "concept",
            "label": name,
            "symbol": getattr(concept, "code", "") or name,
            **_target_time_fields(market="A", symbol=getattr(concept, "code", "") or name, source="concept"),
            "requested_freq": requested_freq,
            "effective_freq": "daily",
            "available_freqs": ["daily"],
            "mapping_status": "non_chain" if non_chain else "unmapped",
            "unmapped_reason": unmapped_reason,
            **_target_diagnostics("stock", name, requested_freq),
        },
        "chart": _chart_from_df(pd.DataFrame(), symbol=name, freq="daily", source="concept_unmapped"),
        "summary": {
            "title": name,
            "subtitle": "概念板块",
            "latest_price": 0,
            "conclusion": "暂未找到可映射行业或领涨股，等待概念成分/板块 K 线预热。",
            "key_levels": [],
            "representatives": representatives,
            "candidate_groups": candidate_groups,
            "non_chain_reason": non_chain,
            "mapping_chain": {
                "query": name,
                "concepts": [name],
                "industries": related[:5],
                "chain_id": None,
                "chain_name": "",
                "node_id": "",
                "node_name": "",
                "layer": "",
                "confidence": 0,
                "evidence_sources": [],
                "mapping_status": "non_chain" if non_chain else "unmapped",
                "unmapped_reason": unmapped_reason,
                "candidate_count": len(carrier_candidates),
            },
            "mapping_status": "non_chain" if non_chain else "unmapped",
            "unmapped_reason": unmapped_reason,
        },
        "signals": [],
        "plan": None,
        "review": _review_context(engine, "concept", name),
        "trade": _trade_context(None),
        "analysis_target": "",
        "candidate_groups": candidate_groups,
        "candidate_stocks": ordered_candidates,
        "custom_signal_count": 0,
        "direct_custom_signal_count": 0,
        "visible_custom_signal_count": 0,
        "hidden_custom_signal_count": 0,
        "available_custom_signal_freqs": [],
        "hidden_reasons": ["concept_has_no_direct_custom_signal"],
        "related_custom_signals": _related_custom_signals_from_candidates(ordered_candidates, requested_freq),
    }


async def _build_stock_target(symbol: str, raw_code: str, freq: str) -> Dict[str, Any]:
    requested_freq = _canonical_freq(freq)
    if requested_freq in {"daily", "weekly", "monthly"}:
        df, source = _stock_df(symbol, requested_freq)
        chart = _chart_from_df(df, symbol=symbol, freq=requested_freq, source=source)
    elif requested_freq == "30min":
        df, source = _stock_df(symbol, requested_freq)
        chart = _chart_from_df(df, symbol=symbol, freq=requested_freq, source=source)
    else:
        df, source = _stock_df(symbol, requested_freq)
        chart = _chart_from_df(df, symbol=symbol, freq=requested_freq, source=source)
    chart_meta = dict(chart.get("meta") or {})
    should_load = not _chart_has_ohlcv(chart) or (
        requested_freq in {"5min", "15min", "30min"} and bool(chart_meta.get("is_stale"))
    )
    if should_load:
        chart = _attach_chart_load_meta(chart, _trigger_stock_chart_load(symbol, raw_code, requested_freq))
        load_status = _text((chart.get("meta") or {}).get("load_status"))
        if load_status in {"ready", "failed"} and not _chart_has_ohlcv(chart):
            _clear_stock_chart_load_job(symbol, requested_freq)
            chart = _attach_chart_load_meta(chart, _trigger_stock_chart_load(symbol, raw_code, requested_freq))
    chart = _mark_chart_readiness(chart, kind="stock", requested_freq=requested_freq)
    chart = _merge_signal_pool_into_chart(chart, symbol, chart.get("meta", {}).get("freq", requested_freq))
    custom_signal_diagnostics = _custom_signal_diagnostics(symbol, requested_freq, chart.get("signals", []))
    try:
        stock = _unwrap_response(analyze_stock(symbol))
    except Exception as exc:
        stock = {
            "symbol": symbol,
            "name": _stock_name(symbol),
            "errors": [f"analyze_stock_error:{exc.__class__.__name__}"],
            "ma_context": {},
            "scored": {},
            "risk": {},
            "scenarios": [],
            "layered_position": {},
        }
    engine = _ensure_engine()
    return {
        "target": {
            "kind": "stock",
            "label": stock.get("name") or symbol,
            "symbol": symbol,
            **_target_time_fields(symbol=symbol, source=chart.get("meta", {}).get("source", "")),
            "requested_freq": requested_freq,
            "effective_freq": requested_freq,
            "available_freqs": UI_FREQS,
            "fallback_reason": "",
            "not_ready_reason": chart.get("meta", {}).get("not_ready_reason", ""),
            **_target_diagnostics("stock", symbol, requested_freq),
        },
        "chart": chart,
        "summary": _summary_from_stock(symbol, stock, chart),
        "signals": chart.get("signals", []),
        "plan": {
            "scenarios": stock.get("scenarios", []),
            "layered_position": stock.get("layered_position", {}),
        },
        "review": _review_context(engine, "stock", symbol, symbol=symbol),
        "trade": _trade_context(symbol),
        "analysis_target": symbol,
        "candidate_stocks": [],
        "stock_analysis": stock,
        **custom_signal_diagnostics,
        "related_custom_signals": [],
    }


def _refresh_manual_clue_quote(db, symbol: str) -> dict[str, Any]:
    try:
        from signals.data.mongo_fallback import get_last_trading_day
        from signals.sync.modules.quote_snapshots import _fetch_em_quote, _quote_doc_from_em

        now = _sync_now()
        trading_day = str(get_last_trading_day("A") or market_today("A"))[:10]
        payload, latency_ms, error = _fetch_em_quote(db, symbol)
        if not payload:
            return {"quote_status": "failed", "quote_error": error, "latency_ms": round(latency_ms, 2)}
        doc = _quote_doc_from_em(symbol, payload, now, trading_day)
        if not doc:
            return {"quote_status": "empty", "quote_error": "provider_payload_empty", "latency_ms": round(latency_ms, 2)}
        db["quote_snapshots"].replace_one({"_id": doc["_id"]}, doc, upsert=True)
        return {"quote_status": "ok", "quote_source": doc.get("source"), "latency_ms": round(latency_ms, 2)}
    except Exception as exc:
        return {"quote_status": "failed", "quote_error": f"{exc.__class__.__name__}: {exc}"[:240]}


def _timestamp_range_to_dates(
    start: Optional[int],
    end: Optional[int],
    *,
    market: Any = "",
    symbol: Any = "",
    source: Any = "",
) -> Tuple[Optional[str], Optional[str]]:
    return timestamp_range_to_dates(start, end, market=market, symbol=symbol, source=source)


def _in_date_range(date_str: str, start: Optional[str], end: Optional[str]) -> bool:
    if not date_str:
        return True
    normalized = date_str[:10]
    if start and normalized < start:
        return False
    if end and normalized > end:
        return False
    return True


def _filter_backtest_payload(payload: Dict[str, Any], start: Optional[str], end: Optional[str]) -> Dict[str, Any]:
    if not start and not end:
        return payload

    signals = [
        item for item in payload.get("signals", [])
        if _in_date_range(item.get("date_str") or item.get("signal_date") or item.get("dt_str", ""), start, end)
    ]
    trades = [
        item for item in payload.get("sim_trades", [])
        if _in_date_range(item.get("entry_date", ""), start, end)
    ]
    filtered = dict(payload)
    filtered["signals"] = signals
    filtered["sim_trades"] = trades
    filtered["range"] = {"start": start, "end": end}
    return filtered


async def _call_backtest_run(code: str, freq: str, lookback: int = 360) -> Any:
    return await backtest_service.backtest_run(
        code=code,
        freq=freq,
        signal_group="all",
        lookback=lookback,
        factor="",
        gap_pct_min=2.0,
        volume_ratio_min=1.5,
        trend_lookback=20,
        bb_period=20,
        squeeze_threshold=0.05,
    )


async def _call_backtest_analyze(code: str, freq: str, lookback: int = 180) -> Any:
    return await backtest_service.backtest_analyze(
        code=code,
        freq=freq,
        signal_group="all",
        lookback=lookback,
        factor="",
        gap_pct_min=2.0,
        volume_ratio_min=1.5,
        trend_lookback=20,
        bb_period=20,
        squeeze_threshold=0.05,
        run_count=3,
        body_ratio=0.5,
        accel_count=3,
        stop_loss=5.0,
        trail_stop=50.0,
        max_hold=20,
        slippage=0.1,
        take_profit=0,
        ma_exit_period=0,
        profit_drawdown=0,
        batch_exit="0",
        batch1_ratio=50,
        batch1_target=5,
        batch2_target=10,
        atr_exit_period=0,
        atr_exit_mult=2.0,
    )


@router.get("/shell")
async def get_workbench_shell():
    engine = _ensure_engine()
    return await run_in_threadpool(_build_shell_payload, engine)


@router.post("/manual-clues")
async def add_workbench_manual_clue(payload: dict[str, Any] = Body(...)):
    symbol_text = _text(payload.get("symbol") or payload.get("label") or payload.get("query"))
    freq = _canonical_freq(_text(payload.get("freq")) or DEFAULT_TERMINAL_FREQ)
    symbol, raw_code = _normalize_stock_symbol(symbol_text)
    if not symbol or not raw_code:
        raise HTTPException(status_code=400, detail=f"无法识别股票标的: {symbol_text}")

    def _add() -> dict[str, Any]:
        db = _mongo_db()
        now = _sync_now()
        doc = {
            "symbol": symbol,
            "raw_code": raw_code,
            "name": _stock_name(symbol),
            "freq": freq,
            "active": True,
            "source": "user_search",
            "reason": "用户从搜索栏临时纳入线索池",
            "updated_at": now,
        }
        db["terminal_manual_clues"].update_one(
            {"symbol": symbol},
            {"$set": doc, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        quote = _refresh_manual_clue_quote(db, symbol)
        _invalidate_shell_cache()
        load_meta = _trigger_manual_clue_cache_load(symbol, raw_code, freq)
        return {
            "status": "ok",
            "symbol": symbol,
            "raw_code": raw_code,
            "name": doc["name"],
            "freq": freq,
            "manual_clue": True,
            "load": load_meta,
            "quote": quote,
        }

    return await run_in_threadpool(_add)


@router.delete("/manual-clues/{symbol:path}")
async def delete_workbench_manual_clue(symbol: str, confirm: bool = Query(False)):
    normalized, raw_code = _normalize_stock_symbol(symbol)
    if not normalized:
        raise HTTPException(status_code=400, detail=f"无法识别股票标的: {symbol}")
    if confirm is not True:
        raise HTTPException(status_code=409, detail="删除临时线索需要二次确认")

    def _delete() -> dict[str, Any]:
        db = _mongo_db()
        result = db["terminal_manual_clues"].delete_many({
            "$or": [
                {"symbol": normalized},
                {"raw_code": raw_code},
                {"symbol": symbol},
            ]
        })
        _invalidate_shell_cache()
        return {"status": "ok", "symbol": normalized, "deleted": int(getattr(result, "deleted_count", 0) or 0)}

    return await run_in_threadpool(_delete)


@router.get("/cluster")
async def get_workbench_cluster(
    top: int = Query(5, ge=1, le=12),
    direction: str = Query("", description="观察池方向"),
    mode: str = Query("belief", description="belief / panic"),
    scan_top: int = Query(20, ge=1, le=60),
):
    latest = _unwrap_response(cluster_service.get_latest(top=top))
    history = _unwrap_response(cluster_service.get_history())
    scan = None
    if direction.strip():
        scan = _unwrap_response(cluster_service.get_watchlist(direction=direction.strip(), mode=mode, top=scan_top))
    return {
        "latest": latest,
        "history": history,
        "scan": scan,
    }


@router.get("/symbol/{symbol:path}")
async def get_workbench_symbol(
    symbol: str,
    kind: str = Query("auto", description="auto / index / industry / concept / stock"),
    freq: str = Query("30min", description="5min / 15min / 30min / daily / weekly"),
):
    engine = _ensure_engine()
    if not engine.is_ready() and kind in {"auto", "index"}:
        static_index = _resolve_static_index(symbol)
        if static_index is not None:
            return await _build_static_index_target(static_index[0], static_index[1], freq)
        status = engine.get_status()
        return JSONResponse(
            status_code=503,
            content={
                "error": "分析引擎尚未就绪",
                "session": _serialize_session(status),
            },
        )

    resolved = _resolve_target(symbol, kind, engine)
    if resolved["kind"] == "index":
        return await _build_index_target(engine, resolved["label"], freq)
    if resolved["kind"] == "industry":
        return await _build_industry_target(engine, resolved["label"], freq)
    if resolved["kind"] == "concept":
        return await _build_concept_target(engine, resolved["label"], freq)
    return await _build_stock_target(resolved["label"], resolved["raw_code"], freq)


@router.get("/backtest")
async def get_workbench_backtest(
    symbol: str = Query(..., description="股票代码或 Futu symbol"),
    freq: str = Query("daily", description="daily / weekly / monthly"),
    start_ts: Optional[int] = Query(None, description="选区开始秒级时间戳"),
    end_ts: Optional[int] = Query(None, description="选区结束秒级时间戳"),
):
    normalized, raw_code = _normalize_stock_symbol(symbol)
    if not normalized or not raw_code:
        raise HTTPException(status_code=404, detail=f"无法识别股票: {symbol}")

    payload = _unwrap_response(
        await _call_backtest_analyze(
            raw_code,
            freq if freq in {"daily", "weekly", "monthly"} else "daily",
            lookback=360,
        )
    )
    start, end = _timestamp_range_to_dates(start_ts, end_ts, symbol=normalized)
    filtered = _filter_backtest_payload(payload, start, end)
    filtered["target"] = {
        "symbol": normalized,
        "code": raw_code,
        **_target_time_fields(symbol=normalized),
        "requested_freq": freq,
        "effective_freq": freq if freq in {"daily", "weekly", "monthly"} else "daily",
    }
    return filtered
