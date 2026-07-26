from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import os
import re
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
from signals.core.backtest_terminal import build_backtest_terminal
from signals.core.chart_patterns import classify_latest_chart_pattern
from signals.core.concept_carriers import load_industry_chains, non_chain_reason
from signals.core.ma_levels import KEY_MA_COLORS, KEY_MA_PERIODS
from signals.core.macro_universe import (
    MACRO_GROUP_INDUSTRY_ETFS,
    MACRO_GROUP_MAJOR_INDICES,
    canonical_macro_industry_etf_symbol,
    macro_industry_etf_name,
    macro_group_label,
    macro_group_type_label,
    macro_index_themes,
    macro_watchlist,
    supports_a_index_minute_cache,
)
from signals.core.stock_names import get_resolver
from signals.data.gateway import get_index_bars, get_kline
from signals.data.models import DataRequest
from signals.core.trade_log import get_trade_log
from signals.core.trading_dates import A_SHARE_AUCTION_OPEN
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
logger = logging.getLogger(__name__)

UI_FREQS = ["30min", "15min", "5min", "daily", "weekly"]
DEFAULT_TERMINAL_FREQ = "daily"
MINUTE_FREQS = {"5min", "5m", "15min", "15m", "30min", "30m"}
REALTIME_DAY_CHANGE_FREQS = ("5min",)
BUY_FREQS = ["weekly", "daily", "30min", "15min", "5min"]
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
        "purpose": "主观察列表、候选池、暂不参与池和策略快照。",
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
_SHELL_CACHE_LOCK = threading.RLock()
_SHELL_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None, "refreshed_at": 0.0, "quote_watermark": ""}
_SHELL_QUOTE_REFRESH_GROUPS = ("indices", "watchlist", "buy_candidates", "sell_warnings")
_SHELL_REBUILD_WATERMARK_KEYS = {
    "terminal_stock_pool",
    "stock_minute",
    "market_pools",
    "strategy_snapshots",
    "board_heat_ticks",
    "chain_heat_snapshots",
}
_VISIBLE_QUOTE_REFRESH_LOCK = threading.Lock()
_VISIBLE_QUOTE_REFRESH_LAST: dict[str, float] = {}
_SYMBOL_PAYLOAD_CACHE_TTL_SECONDS = 60.0
_SYMBOL_PAYLOAD_CACHE_MAX_ITEMS = 160
_SYMBOL_PAYLOAD_CACHE_LOCK = threading.RLock()
_SYMBOL_PAYLOAD_CACHE: dict[str, dict[str, Any]] = {}
_SYMBOL_PAYLOAD_BUILD_LOCKS: dict[str, threading.Lock] = {}


def _invalidate_shell_cache() -> None:
    with _SHELL_CACHE_LOCK:
        _SHELL_CACHE.update({"expires_at": 0.0, "payload": None, "refreshed_at": 0.0, "quote_watermark": ""})


def _shell_manual_clue_row_matches(row: Any, keys: set[str]) -> bool:
    if not isinstance(row, dict) or not keys:
        return False
    source_collections = row.get("source_collections")
    is_manual = bool(row.get("manual_clue") or row.get("deletable"))
    is_manual = is_manual or _text(row.get("source_collection")) == "terminal_manual_clues"
    is_manual = is_manual or (
        isinstance(source_collections, list)
        and any(_text(item) == "terminal_manual_clues" for item in source_collections)
    )
    if not is_manual:
        return False
    values = [
        row.get("symbol"),
        row.get("raw_code"),
        row.get("code"),
        row.get("target_symbol"),
        row.get("target_label"),
        row.get("label"),
    ]
    for value in values:
        text = _text(value).strip()
        if text and text.upper() in keys:
            return True
    return False


def _remove_shell_manual_clue_rows(rows: Any, keys: set[str]) -> tuple[Any, int]:
    if not isinstance(rows, list):
        return rows, 0
    kept: list[Any] = []
    removed = 0
    for row in rows:
        if _shell_manual_clue_row_matches(row, keys):
            removed += 1
            continue
        kept.append(row)
    return kept, removed


def _sync_shell_group_meta_counts(payload: dict[str, Any]) -> None:
    groups = payload.get("watchlist_groups")
    meta = payload.get("watchlist_groups_meta")
    if not isinstance(groups, dict) or not isinstance(meta, dict):
        return
    for group_key, rows in groups.items():
        group_meta = meta.get(group_key)
        if isinstance(rows, list) and isinstance(group_meta, dict):
            group_meta["count"] = len(rows)
            if group_key == "buy_candidates":
                group_meta["manual_clues"] = sum(1 for row in rows if isinstance(row, dict) and row.get("manual_clue"))


def _remove_manual_clue_from_shell_cache(symbols: set[str]) -> dict[str, Any]:
    keys = {_text(symbol).strip().upper() for symbol in symbols if _text(symbol).strip()}
    if not keys:
        return {"cache_updated": False, "cache_removed": 0}
    with _SHELL_CACHE_LOCK:
        payload = _SHELL_CACHE.get("payload")
        if not isinstance(payload, dict):
            return {"cache_updated": False, "cache_removed": 0}
        updated = dict(payload)
        removed_total = 0
        for key in ("buy_candidates", "watchlist", "decision_queue"):
            rows, removed = _remove_shell_manual_clue_rows(updated.get(key), keys)
            if removed:
                updated[key] = rows
                removed_total += removed

        groups = updated.get("watchlist_groups")
        if isinstance(groups, dict):
            updated_groups: dict[str, Any] = dict(groups)
            for group_key in ("buy_candidates", "focus_stocks", "risk_stocks", "watch_stocks"):
                rows, removed = _remove_shell_manual_clue_rows(updated_groups.get(group_key), keys)
                if removed:
                    updated_groups[group_key] = rows
                    removed_total += removed
            updated["watchlist_groups"] = updated_groups

        if removed_total <= 0:
            return {"cache_updated": False, "cache_removed": 0}
        _sync_shell_group_meta_counts(updated)
        _SHELL_CACHE["payload"] = updated
        return {"cache_updated": True, "cache_removed": removed_total}


def _symbol_payload_cache_key(symbol: str, kind: str, freq: str) -> str:
    return "|".join((
        _text(kind).strip().lower() or "auto",
        _canonical_freq(freq),
        _text(symbol).strip().upper(),
    ))


def _symbol_payload_build_lock(key: str) -> threading.Lock:
    with _SYMBOL_PAYLOAD_CACHE_LOCK:
        lock = _SYMBOL_PAYLOAD_BUILD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SYMBOL_PAYLOAD_BUILD_LOCKS[key] = lock
        return lock


def _get_symbol_payload_cache(key: str) -> Optional[Dict[str, Any]]:
    now = time.monotonic()
    with _SYMBOL_PAYLOAD_CACHE_LOCK:
        entry = _SYMBOL_PAYLOAD_CACHE.get(key)
        if not entry:
            return None
        if float(entry.get("expires_at") or 0.0) <= now:
            _SYMBOL_PAYLOAD_CACHE.pop(key, None)
            return None
        payload = entry.get("payload")
    if isinstance(payload, dict):
        return copy.deepcopy(payload)
    return None


def _set_symbol_payload_cache(key: str, payload: Dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        return
    now = time.monotonic()
    with _SYMBOL_PAYLOAD_CACHE_LOCK:
        _SYMBOL_PAYLOAD_CACHE[key] = {
            "expires_at": now + _SYMBOL_PAYLOAD_CACHE_TTL_SECONDS,
            "payload": copy.deepcopy(payload),
            "stored_at": now,
        }
        while len(_SYMBOL_PAYLOAD_CACHE) > _SYMBOL_PAYLOAD_CACHE_MAX_ITEMS:
            oldest_key = min(
                _SYMBOL_PAYLOAD_CACHE,
                key=lambda item: float(_SYMBOL_PAYLOAD_CACHE[item].get("stored_at") or 0.0),
            )
            _SYMBOL_PAYLOAD_CACHE.pop(oldest_key, None)


def _clear_symbol_payload_cache(symbol: str, kind: str, freq: str) -> None:
    key = _symbol_payload_cache_key(symbol, kind, freq)
    with _SYMBOL_PAYLOAD_CACHE_LOCK:
        _SYMBOL_PAYLOAD_CACHE.pop(key, None)


def _symbol_payload_cacheable(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        return True
    meta = chart.get("meta") if isinstance(chart.get("meta"), dict) else {}
    load_status = _text(meta.get("load_status"))
    if load_status in {"triggered", "running"}:
        return False
    if _text(meta.get("cache_status")) in {"not_ready", "spot_only"}:
        return False
    if not _chart_has_ohlcv(chart) and _text(meta.get("not_ready_reason")):
        return False
    return True


def _cached_build_workbench_symbol_payload(symbol: str, kind: str, freq: str) -> Dict[str, Any] | JSONResponse:
    key = _symbol_payload_cache_key(symbol, kind, freq)
    cached = _get_symbol_payload_cache(key)
    if cached is not None:
        return cached
    with _symbol_payload_build_lock(key):
        cached = _get_symbol_payload_cache(key)
        if cached is not None:
            return cached
        payload = _build_workbench_symbol_payload(symbol, kind, freq)
        if isinstance(payload, dict) and _symbol_payload_cacheable(payload):
            _set_symbol_payload_cache(key, payload)
        return payload


def _quote_snapshot_watermark() -> str:
    parts: list[str] = []
    try:
        db = _mongo_db()
    except Exception:
        return ""
    try:
        doc = db["quote_snapshots"].find_one(
            {"snapshot_at": {"$ne": None}},
            {"_id": 0, "snapshot_at": 1},
            sort=[("snapshot_at", -1)],
        ) or {}
    except Exception:
        doc = {}
    value = doc.get("snapshot_at")
    if isinstance(value, datetime):
        parts.append(f"quote_snapshots:{value.isoformat()}")
    elif value:
        parts.append(f"quote_snapshots:{value}")
    try:
        expected_day = _day_change_expected_day(_a_day_change_mode())
        date_key = str(expected_day or "").replace("-", "")[:8]
        spot_doc = db["fullmarket_spot_snapshots"].find_one(
            {"date_key": date_key, "snapshot_at": {"$ne": None}},
            {"_id": 0, "snapshot_at": 1},
            sort=[("snapshot_at", -1)],
        ) or {}
    except Exception:
        spot_doc = {}
    spot_value = spot_doc.get("snapshot_at")
    if isinstance(spot_value, datetime):
        parts.append(f"fullmarket_spot_snapshots:{spot_value.isoformat()}")
    elif spot_value:
        parts.append(f"fullmarket_spot_snapshots:{spot_value}")
    try:
        pool_doc = db["terminal_stock_pool"].find_one(
            {"pool": "terminal_stock_pool", "market": "A"},
            {"_id": 0, "updated_at": 1, "ranking_version": 1},
            sort=[("updated_at", -1)],
        ) or {}
    except Exception:
        pool_doc = {}
    pool_value = pool_doc.get("updated_at")
    pool_version = _text(pool_doc.get("ranking_version"))
    if isinstance(pool_value, datetime):
        parts.append(f"terminal_stock_pool:{pool_value.isoformat()}:{pool_version}")
    elif pool_value:
        parts.append(f"terminal_stock_pool:{pool_value}:{pool_version}")
    try:
        minute_doc = (
            db["sync_log"].find_one(
                {"_id": "stock_minute:selection:_meta"},
                {"_id": 0, "last_run": 1, "latest_dt": 1, "result": 1},
                sort=[("last_run", -1)],
            )
            or db["sync_log"].find_one(
                {"_id": "stock_minute:_meta"},
                {"_id": 0, "last_run": 1, "latest_dt": 1, "result": 1},
                sort=[("last_run", -1)],
            )
            or {}
        )
    except Exception:
        minute_doc = {}
    minute_value = minute_doc.get("latest_dt") or minute_doc.get("last_run") or (minute_doc.get("result") or {}).get("last_dt")
    if isinstance(minute_value, datetime):
        parts.append(f"stock_minute:{minute_value.isoformat()}")
    elif minute_value:
        parts.append(f"stock_minute:{minute_value}")
    for collection, field, query in (
        ("market_pools", "updated_at", {"market": "A"}),
        ("strategy_snapshots", "updated_at", {}),
        ("board_heat_ticks", "trade_minute", {}),
        ("chain_heat_snapshots", "trade_minute", {}),
    ):
        try:
            latest_doc = db[collection].find_one(
                query,
                {"_id": 0, field: 1, "updated_at": 1, "snapshot_at": 1},
                sort=[(field, -1), ("updated_at", -1), ("snapshot_at", -1)],
            ) or {}
        except Exception:
            latest_doc = {}
        latest_value = latest_doc.get(field) or latest_doc.get("updated_at") or latest_doc.get("snapshot_at")
        if isinstance(latest_value, datetime):
            parts.append(f"{collection}:{latest_value.isoformat()}")
        elif latest_value:
            parts.append(f"{collection}:{latest_value}")
    return "|".join(parts)


def _watermark_parts(value: Any) -> dict[str, str]:
    parts: dict[str, str] = {}
    for item in str(value or "").split("|"):
        if ":" not in item:
            continue
        key, rest = item.split(":", 1)
        if key:
            parts[key] = rest
    return parts


def _watermark_from_parts(parts: dict[str, str]) -> str:
    return "|".join(f"{key}:{value}" for key, value in parts.items() if key and value)


def _quote_overlay_watermark(current: Any, cached: Any) -> str:
    current_parts = _watermark_parts(current)
    if not current_parts:
        return str(cached or "")
    cached_parts = _watermark_parts(cached)
    merged = dict(cached_parts)
    for key, value in current_parts.items():
        if key not in _SHELL_REBUILD_WATERMARK_KEYS:
            merged[key] = value
    for key, value in current_parts.items():
        if key in _SHELL_REBUILD_WATERMARK_KEYS and key not in merged:
            merged[key] = value
    return _watermark_from_parts(merged)


def _shell_watermark_requires_rebuild(current: Any, cached: Any) -> bool:
    current_parts = _watermark_parts(current)
    cached_parts = _watermark_parts(cached)
    for key in _SHELL_REBUILD_WATERMARK_KEYS:
        if current_parts.get(key) != cached_parts.get(key):
            return True
    return False


def _shell_cache_usable(payload: Any, engine: Any, quote_watermark: Optional[str] = None) -> bool:
    if not isinstance(payload, dict):
        return False
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    if session and not session.get("ready") and engine.is_ready() and not _shell_payload_has_read_model(payload):
        return False
    if quote_watermark is not None and _shell_watermark_requires_rebuild(quote_watermark, _SHELL_CACHE.get("quote_watermark")):
        return False
    return True


def _shell_payload_has_read_model(payload: dict[str, Any]) -> bool:
    groups = payload.get("watchlist_groups")
    if isinstance(groups, dict) and any(bool(value) for value in groups.values() if isinstance(value, list)):
        return True
    return any(
        payload.get(key)
        for key in (
            "buy_candidates",
            "sell_warnings",
            "cluster_summary",
            "daily_brief",
            "strategy_kpis",
        )
    )


def _shell_cache_ttl_seconds(payload: dict[str, Any]) -> float:
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    has_read_model = _shell_payload_has_read_model(payload)
    if session.get("ready") or has_read_model:
        return _SHELL_CACHE_TTL_SECONDS
    return 2.0


def _shell_row_quote_symbol(row: dict[str, Any]) -> str:
    for key in ("target_symbol", "symbol", "raw_code", "code"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _refresh_shell_row_quote_overlay(row: Any, overlay_cache: dict[str, dict[str, Any]]) -> Any:
    if not isinstance(row, dict):
        return row
    symbol = _shell_row_quote_symbol(row)
    if not symbol:
        return row
    kind = str(row.get("target_kind") or row.get("kind") or "").strip().lower()
    if kind and kind not in {"stock", "index"}:
        return row
    if symbol not in overlay_cache:
        overlay_cache[symbol] = _quote_overlay_for_symbol(symbol)
    return _apply_quote_overlay(row, symbol, overlay_cache[symbol])


def _refresh_shell_payload_quote_overlays(payload: dict[str, Any]) -> dict[str, Any]:
    updated = dict(payload)
    overlay_cache: dict[str, dict[str, Any]] = {}
    for group in _SHELL_QUOTE_REFRESH_GROUPS:
        rows = updated.get(group)
        if isinstance(rows, list):
            updated[group] = [_refresh_shell_row_quote_overlay(row, overlay_cache) for row in rows]

    groups = updated.get("watchlist_groups")
    if isinstance(groups, dict):
        refreshed_groups: dict[str, Any] = {}
        for group_key, rows in groups.items():
            if isinstance(rows, list):
                refreshed_groups[group_key] = [_refresh_shell_row_quote_overlay(row, overlay_cache) for row in rows]
            else:
                refreshed_groups[group_key] = rows
        updated["watchlist_groups"] = refreshed_groups
    return updated


def _payload_from_shell_cache(
    cached_payload: dict[str, Any],
    status: str,
    now: float,
    quote_watermark: str,
    *,
    current_session: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = dict(cached_payload)
    if current_session is not None:
        payload["session"] = current_session
        if current_session.get("ready"):
            notices = payload.get("notices")
            if isinstance(notices, list):
                payload["notices"] = [
                    item
                    for item in notices
                    if "正在启动" not in str(item) and "正在构建" not in str(item)
                ]
            cluster = payload.get("cluster_summary")
            if isinstance(cluster, dict) and "正在构建" in str(cluster.get("data_warning") or ""):
                refreshed_cluster = dict(cluster)
                refreshed_cluster["data_warning"] = ""
                payload["cluster_summary"] = refreshed_cluster
    cache_quote_watermark = str(_SHELL_CACHE.get("quote_watermark") or "")
    cache_status = status
    if quote_watermark != cache_quote_watermark:
        payload = _refresh_shell_payload_quote_overlays(payload)
        cache_status = f"{status}_quote_overlay"
        with _SHELL_CACHE_LOCK:
            _SHELL_CACHE.update({"payload": dict(payload), "quote_watermark": quote_watermark})
    payload["cache"] = {
        "status": cache_status,
        "age_seconds": round(now - float(_SHELL_CACHE.get("refreshed_at") or now), 2),
        "ttl_seconds": _SHELL_CACHE_TTL_SECONDS,
        "quote_watermark": quote_watermark,
    }
    return payload


def _build_shell_placeholder_payload(status: str, now: float, quote_watermark: str) -> dict[str, Any]:
    session = _serialize_session({
        "ready": False,
        "running": True,
        "loading_phase": "building",
        "session_label": "启动中",
        "session_mode": "startup",
        "active_markets": ["A"],
    })
    range_columns = _watchlist_range_columns()
    major_indices = _macro_shell_raw_rows(MACRO_GROUP_MAJOR_INDICES)
    industry_etfs = _macro_shell_raw_rows(MACRO_GROUP_INDUSTRY_ETFS)
    focus_stocks: list[dict[str, Any]] = []
    risk_stocks: list[dict[str, Any]] = []
    watch_stocks: list[dict[str, Any]] = []
    buy_candidates: list[dict[str, Any]] = []
    trade_map = _build_trade_map(
        sector_boards=[],
        focus_stocks=focus_stocks,
        watch_stocks=watch_stocks,
        risk_stocks=risk_stocks,
        clue_stocks=buy_candidates,
    )
    return {
        "session": session,
        "market": None,
        "indices": [],
        "buy_candidates": buy_candidates,
        "sell_warnings": [],
        "cluster_summary": {
            "industry_top": [],
            "concept_top": [],
            "market_status": {},
            "data_warning": "Signals shell 正在构建，稍后自动刷新。",
        },
        "watchlist_groups": {
            "major_indices": major_indices,
            "industry_etfs": industry_etfs,
            "all_etfs": [],
            "macro_indices": [*major_indices, *industry_etfs],
            "sector_boards": [],
            "buy_candidates": buy_candidates,
            "focus_stocks": focus_stocks,
            "risk_stocks": risk_stocks,
            "watch_stocks": watch_stocks,
        },
        "watchlist_groups_meta": {
            "major_indices": {
                "label": "大盘指数",
                "source_collection": "macro_universe",
                "count": len(major_indices),
            },
            "industry_etfs": {
                "label": "行业ETF",
                "source_collection": "macro_universe",
                "count": len(industry_etfs),
            },
            "all_etfs": {
                "label": "全量ETF",
                "source_collection": "strategy_snapshots.etf_analysis",
                "count": 0,
                "review_count": 0,
                "role": "all_market_etf_review_universe",
            },
            "buy_candidates": {
                "label": "线索池",
                "source_collection": "terminal_manual_clues + terminal_stock_pool.clue_stocks",
                "count": len(buy_candidates),
            },
            "focus_stocks": {
                "label": "买点池",
                "source_collection": "terminal_stock_pool.focus_stocks",
                "count": len(focus_stocks),
            },
            "watch_stocks": {
                "label": "盯盘池",
                "source_collection": "terminal_stock_pool.watch_stocks",
                "count": len(watch_stocks),
            },
            "risk_stocks": {
                "label": "暂不参与",
                "source_collection": "terminal_stock_pool.risk_stocks",
                "count": len(risk_stocks),
            },
        },
        "watchlist": [],
        "etf_analysis": {},
        "watchlist_range_columns": range_columns,
        "kline_cache_coverage": {},
        "sync_lanes": {},
        "trade_map": trade_map,
        "ai_alerts": [],
        "command_suggestions": [],
        "daily_brief": {},
        "decision_queue": [],
        "strategy_kpis": {},
        "source_confidence": {},
        "watchlist_directions": [],
        "default_target": {
            "kind": "index",
            "label": "上证指数",
            "freq": DEFAULT_TERMINAL_FREQ,
        },
        "legacy_url": "/legacy",
        "notices": ["Signals shell 正在构建，稍后自动刷新。"],
        "cache": {
            "status": status,
            "age_seconds": 0,
            "ttl_seconds": 0,
            "quote_watermark": quote_watermark,
            "building": True,
        },
    }
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
    if (
        now.weekday() < 5
        and (now.hour, now.minute) >= (A_SHARE_AUCTION_OPEN.hour, A_SHARE_AUCTION_OPEN.minute)
        and (now.hour, now.minute) < (15, 0)
    ):
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


def _normalize_chart_df(df: pd.DataFrame, freq: str, *, live_render: bool = False) -> pd.DataFrame:
    if df is None or df.empty:
        out = pd.DataFrame()
        if df is not None:
            out.attrs.update(getattr(df, "attrs", {}) or {})
        return out
    working = df.copy().sort_index()
    working.attrs.update(getattr(df, "attrs", {}) or {})
    canonical = _canonical_freq(freq)
    if live_render and canonical in MINUTE_FREQS and _a_day_change_mode() == "quote_intraday" and not working.empty:
        expected_day = _day_change_expected_day("quote_intraday")
        latest_day = _date_text(working.index.max())
        if expected_day and latest_day and latest_day < expected_day:
            attrs = dict(working.attrs)
            working = working.iloc[0:0].copy()
            working.attrs.update(attrs)
            working.attrs["gateway_is_stale"] = True
            working.attrs["freshness"] = "stale"
            working.attrs["stale_reason"] = f"minute_cache_older_than_realtime_day:{latest_day}->{expected_day}"
            working.attrs["as_of"] = latest_day
            working.attrs["data_as_of"] = latest_day
        return working
    if canonical == "weekly" and not working.empty:
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


def _append_live_daily_quote_bar(df: pd.DataFrame, *, symbol: str, freq: str) -> tuple[pd.DataFrame, str]:
    if _canonical_freq(freq) != "daily" or _a_day_change_mode() != "quote_intraday":
        return df, ""
    try:
        overlay = _quote_overlay_for_symbol(symbol)
    except Exception:
        return df, ""
    if overlay.get("quote_status") not in {"realtime", "delayed"}:
        return df, ""
    latest_price = _first_numeric(overlay.get("latest_price"), overlay.get("realtime_price"), overlay.get("quote_price"))
    if latest_price is None:
        return df, ""
    expected_day = _day_change_expected_day("quote_intraday")
    if not expected_day:
        return df, ""
    working = df.copy().sort_index() if df is not None else pd.DataFrame()
    working.attrs.update(getattr(df, "attrs", {}) or {})
    today_idx = pd.Timestamp(expected_day)
    previous_close = _float(working["close"].iloc[-1]) if working is not None and not working.empty and "close" in working.columns else latest_price
    open_price = _first_numeric(overlay.get("quote_open_price"), previous_close, latest_price) or latest_price
    row = {
        "open": open_price,
        "high": max(open_price, latest_price),
        "low": min(open_price, latest_price),
        "close": latest_price,
        "vol": 0,
        "amount": 0,
    }
    if working.empty:
        working = pd.DataFrame([row], index=[today_idx])
    else:
        same_day = pd.to_datetime(working.index, errors="coerce").date == today_idx.date()
        if any(same_day):
            idx = working.loc[same_day].index[-1]
            existing = working.loc[idx].to_dict()
            row["open"] = _first_numeric(existing.get("open"), row["open"]) or row["open"]
            row["high"] = max(_first_numeric(existing.get("high"), row["high"]) or row["high"], row["high"])
            row["low"] = min(_first_numeric(existing.get("low"), row["low"]) or row["low"], row["low"])
            working.loc[idx, ["open", "high", "low", "close", "vol", "amount"]] = [
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                _first_numeric(existing.get("vol"), row["vol"]) or row["vol"],
                _first_numeric(existing.get("amount"), row["amount"]) or row["amount"],
            ]
        else:
            working.loc[today_idx, list(row)] = list(row.values())
            working = working.sort_index()
    working.attrs["as_of"] = expected_day
    working.attrs["data_as_of"] = expected_day
    working.attrs["latest_bar_time"] = today_idx.isoformat()
    working.attrs["freshness"] = "fresh"
    working.attrs["gateway_freshness"] = "fresh"
    working.attrs["gateway_is_stale"] = False
    working.attrs["is_stale"] = False
    working.attrs["stale_reason"] = ""
    working.attrs["live_quote_overlay"] = True
    working.attrs["time_semantics"] = "realtime_daily_partial"
    return working, "live_quote_daily_overlay"


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


def _chart_from_df(
    df: pd.DataFrame,
    *,
    symbol: str,
    freq: str,
    source: str = "gateway",
    live_render: bool = False,
) -> dict[str, Any]:
    market = infer_market(symbol=symbol, source=source)
    canonical_freq = _canonical_freq(freq)
    if canonical_freq == "weekly":
        limit = 280
    elif canonical_freq == "daily":
        limit = 1320
    elif canonical_freq in {"5min", "15min", "30min"}:
        limit = 900
    else:
        limit = 1320
    working = _normalize_chart_df(df, freq, live_render=live_render)
    live_overlay_source = ""
    if live_render:
        working, live_overlay_source = _append_live_daily_quote_bar(working, symbol=symbol, freq=freq)
    effective_source = f"{source};{live_overlay_source}" if live_overlay_source else source
    cache_meta = _chart_cache_meta(working, source=effective_source, freq=freq)
    ma_lines = _compute_chart_ma_lines(working, limit=limit, market=market, symbol=symbol, source=source)
    return {
        "symbol": symbol,
        "freq": _freq_label(freq),
        "meta": {
            "freq": _canonical_freq(freq),
            "source": effective_source,
            **cache_meta,
            "market": market,
            "market_timezone": market_timezone_name(market, symbol=symbol, source=source),
            "time_unit": "s",
            "bars": int(len(working)) if working is not None else 0,
        },
        "ohlcv": _serialize_ohlcv_df(working, limit=limit, market=market, symbol=symbol, source=source),
        "signals": [],
        "ma_lines": ma_lines,
    }


def _compute_chart_ma_lines(
    df: pd.DataFrame,
    *,
    limit: int,
    market: str,
    symbol: str,
    source: str,
) -> list[dict[str, Any]]:
    if df is None or df.empty or "close" not in df.columns:
        return []
    closes = pd.to_numeric(df["close"], errors="coerce")
    out: list[dict[str, Any]] = []
    for period in KEY_MA_PERIODS:
        if len(closes) < period:
            continue
        ma_vals = closes.rolling(period).mean().dropna().tail(limit)
        data = [
            {
                "time": _dt_to_unix(dt_idx, market=market, symbol=symbol, source=source),
                "value": round(float(value), 4),
            }
            for dt_idx, value in ma_vals.items()
            if pd.notna(value)
        ]
        out.append({
            "label": f"MA{period}",
            "color": KEY_MA_COLORS.get(period, "#2962ff"),
            "data": data,
        })
    return out


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
    chart = _chart_from_df(df, symbol=heat_name, freq=freq, source=source, live_render=True)
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
            label = f"{today.year}年以来"
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


_PRICE_REBASE_FACTORS = (0.2, 0.25, 1 / 3, 0.5, 2.0, 3.0, 4.0, 5.0)
_PRICE_REBASE_TOLERANCE = 0.08


def _nearest_price_rebase_factor(ratio: float) -> Optional[float]:
    if ratio <= 0 or not math.isfinite(ratio):
        return None
    for factor in _PRICE_REBASE_FACTORS:
        if abs(ratio - factor) / factor <= _PRICE_REBASE_TOLERANCE:
            return factor
    return None


def _range_return_closes(df: pd.DataFrame, *, adjust_price_discontinuities: bool = False) -> pd.Series:
    working = df.copy().sort_index()
    closes = pd.to_numeric(working["close"], errors="coerce").dropna()
    if not adjust_price_discontinuities or len(closes) < 2:
        return closes

    adjusted = closes.copy()
    close_values = closes.to_numpy(dtype=float)
    for index in range(1, len(close_values)):
        previous = close_values[index - 1]
        current = close_values[index]
        if previous <= 0 or current <= 0:
            continue
        factor = _nearest_price_rebase_factor(current / previous)
        if factor is None:
            continue
        adjusted.iloc[:index] = adjusted.iloc[:index] * factor
    return adjusted


def _compute_range_returns(
    df: pd.DataFrame,
    columns: list[dict[str, Any]],
    *,
    adjust_price_discontinuities: bool = False,
) -> dict[str, Optional[float]]:
    if df is None or df.empty or "close" not in df.columns:
        return {}
    closes = _range_return_closes(df, adjust_price_discontinuities=adjust_price_discontinuities)
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


def _normalize_board_heat_kind(kind: str) -> str:
    if kind in {"concept", "theme"}:
        return "concept"
    if kind == "industry":
        return "industry"
    return ""


def _board_heat_range_result_from_docs(
    heat_name: str,
    resolution: dict[str, Any],
    docs: list[dict[str, Any]],
    range_columns: list[dict[str, Any]],
) -> tuple[dict[str, Optional[float]], str, dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for doc in docs:
        timestamp = pd.to_datetime(doc.get("trade_minute") or doc.get("dt") or doc.get("trade_date"), errors="coerce")
        close = _first_numeric(doc.get("price"), doc.get("close"))
        if pd.isna(timestamp) or close is None:
            continue
        records.append({"dt": timestamp, "close": close})
    if not records:
        return {}, "", {"status": "missing_price_history", "heat_name": heat_name, "resolution_status": resolution.get("status", "")}
    df = pd.DataFrame(records).sort_values("dt").drop_duplicates(subset=["dt"], keep="last").set_index("dt")
    returns = _compute_range_returns(df, range_columns)
    first_date = _date_text(df.index.min())
    latest_date = _date_text(df.index.max())
    partial_keys: list[str] = []
    if first_date:
        first_ts = pd.Timestamp(first_date)
        for column in range_columns:
            key = _text(column.get("key"))
            start_date = _text(column.get("start_date"))
            if not key or not start_date:
                continue
            try:
                if pd.Timestamp(start_date) < first_ts:
                    partial_keys.append(key)
            except Exception:
                continue
    status = "partial_history" if partial_keys else "ok"
    return returns, "board_heat_ticks_price", {
        "status": status,
        "heat_name": heat_name,
        "resolution_status": resolution.get("status", ""),
        "first_date": first_date,
        "latest_date": latest_date,
        "partial_keys": partial_keys,
    }


def _board_heat_range_returns(
    kind: str,
    name: str,
    range_columns: list[dict[str, Any]],
) -> tuple[dict[str, Optional[float]], str, dict[str, Any]]:
    normalized_kind = _normalize_board_heat_kind(kind)
    heat_name = _text(name)
    if not normalized_kind or not heat_name:
        return {}, "", {"status": "missing_target"}
    resolution = resolve_board_heat_name(normalized_kind, heat_name)
    heat_name = _text(resolution.get("heat_name")) or heat_name
    try:
        docs = list(_mongo_db()["board_heat_ticks"].find(
            {"kind": normalized_kind, "name": heat_name},
            {"_id": 0, "trade_minute": 1, "trade_date": 1, "dt": 1, "price": 1, "close": 1},
        ).sort("trade_minute", 1))
    except Exception:
        return {}, "", {"status": "query_failed", "heat_name": heat_name}
    return _board_heat_range_result_from_docs(heat_name, resolution, docs, range_columns)


def _board_heat_range_returns_batch(
    targets: list[tuple[str, str]],
    range_columns: list[dict[str, Any]],
) -> dict[tuple[str, str], tuple[dict[str, Optional[float]], str, dict[str, Any]]]:
    normalized: list[tuple[str, str]] = []
    for kind, name in targets:
        normalized_kind = _normalize_board_heat_kind(_text(kind))
        heat_name = _text(name)
        if normalized_kind and heat_name and (normalized_kind, heat_name) not in normalized:
            normalized.append((normalized_kind, heat_name))
    output: dict[tuple[str, str], tuple[dict[str, Optional[float]], str, dict[str, Any]]] = {}
    if not normalized:
        return output
    query_parts: list[dict[str, Any]] = []
    for kind in sorted({kind for kind, _ in normalized}):
        names = sorted({name for item_kind, name in normalized if item_kind == kind})
        query_parts.append({"kind": kind, "name": {"$in": names}})
    try:
        docs = list(_mongo_db()["board_heat_ticks"].find(
            {"$or": query_parts},
            {"_id": 0, "kind": 1, "name": 1, "trade_minute": 1, "trade_date": 1, "dt": 1, "price": 1, "close": 1},
        ).sort([("kind", 1), ("name", 1), ("trade_minute", 1)]))
    except Exception:
        for key in normalized:
            output[key] = ({}, "", {"status": "query_failed", "heat_name": key[1]})
        return output
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for doc in docs:
        key = (_text(doc.get("kind")), _text(doc.get("name")))
        if key in normalized:
            grouped.setdefault(key, []).append(doc)
    for key in normalized:
        output[key] = _board_heat_range_result_from_docs(
            key[1],
            {"query": key[1], "heat_name": key[1], "status": "exact"},
            grouped.get(key, []),
            range_columns,
        )
    return output


def _range_return_column_keys(columns: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for column in columns:
        key = str(column.get("key") or "").strip()
        if key and column.get("start_date"):
            keys.append(key)
    return keys


def _range_return_status_from_returns(returns: dict[str, Any], columns: list[dict[str, Any]]) -> str:
    if not returns:
        return "missing"
    required_keys = _range_return_column_keys(columns)
    missing_keys = [key for key in required_keys if key not in returns or returns.get(key) is None]
    return "partial_history" if missing_keys else "ok"


def _stock_current_no_trade_close(symbol: str) -> tuple[pd.Timestamp, float, str] | None:
    expected_day = _day_change_expected_day("quote_intraday") or _market_today("A").isoformat()
    date_key = expected_day.replace("-", "")[:8]
    if not date_key:
        return None
    candidates = _quote_symbol_candidates(symbol)
    codes = _fullmarket_code_candidates(candidates)
    dot_symbols = [candidate.upper() for candidate in candidates if "." in str(candidate or "")]
    try:
        row = _mongo_db()["fullmarket_spot_snapshots"].find_one(
            {
                "date_key": date_key,
                "$or": [
                    {"code": {"$in": codes}},
                    {"symbol": {"$in": dot_symbols}},
                ],
            },
            {"_id": 0},
            sort=[("snapshot_at", -1)],
        ) or {}
    except Exception:
        return None
    if _date_text(row.get("trade_date") or expected_day) != expected_day:
        return None
    price = _first_numeric(row.get("price"), row.get("latest"), row.get("close"))
    prev_close = _float(row.get("prev_close"))
    vol = _first_numeric(row.get("vol"), row.get("volume"))
    amount = _first_numeric(row.get("amount"), row.get("turnover"))
    if price is not None or prev_close is None or prev_close <= 0:
        return None
    if (vol is not None and vol > 0) or (amount is not None and amount > 0):
        return None
    return pd.Timestamp(expected_day), float(prev_close), _text(row.get("source")) or "fullmarket_spot_snapshots"


def _with_current_no_trade_close(symbol: str, df: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    carry = _stock_current_no_trade_close(symbol)
    if not carry:
        return df, ""
    carry_dt, carry_close, source = carry
    working = df.copy() if df is not None else pd.DataFrame()
    latest_dt = _df_latest_timestamp(working)
    if latest_dt is not None and pd.Timestamp(latest_dt).normalize() >= carry_dt.normalize():
        return working, ""
    carried_row = {
        "open": carry_close,
        "high": carry_close,
        "low": carry_close,
        "close": carry_close,
        "vol": 0,
        "amount": 0,
    }
    if working.empty:
        return pd.DataFrame([carried_row], index=pd.DatetimeIndex([carry_dt], name="dt")), source
    working.loc[carry_dt] = {key: carried_row.get(key, 0) for key in working.columns}
    return working.sort_index(), source


def _compute_stock_range_returns_required(
    symbol: str,
    range_columns: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    required_keys = _range_return_column_keys(range_columns)
    if not required_keys:
        return {}, ""
    if not symbol:
        raise RuntimeError("range_returns_required_without_symbol")
    try:
        df, computed_source = _stock_df(symbol, "daily")
    except Exception as exc:
        raise RuntimeError(f"range_returns_kline_load_failed:{symbol}:{exc.__class__.__name__}: {exc}") from exc
    if df is None or df.empty or "close" not in df.columns:
        raise RuntimeError(f"range_returns_kline_empty:{symbol}")
    df, carry_source = _with_current_no_trade_close(symbol, df)
    computed = _compute_range_returns(df, range_columns, adjust_price_discontinuities=True)
    missing_keys = [key for key in required_keys if key not in computed or computed.get(key) is None]
    if missing_keys:
        raise RuntimeError(f"range_returns_incomplete:{symbol}:{','.join(missing_keys)}")
    source = computed_source or "daily_bars"
    if carry_source:
        source = f"{source};current_no_trade={carry_source}"
    return computed, source


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


def _previous_close_from_daily_df(df: pd.DataFrame, latest_ts: Any) -> Optional[float]:
    if df is None or df.empty or "close" not in df.columns:
        return None
    try:
        latest_date = pd.to_datetime(latest_ts).date()
        working = df.sort_index().copy()
        parsed_index = pd.to_datetime(working.index, errors="coerce")
        prior = working[parsed_index.date < latest_date]
        if prior.empty and len(working) > 1:
            prior = working.iloc[:-1]
        closes = pd.to_numeric(prior["close"], errors="coerce").dropna()
    except Exception:
        return None
    if closes.empty:
        return None
    previous = float(closes.iloc[-1])
    return previous if previous > 0 else None


def _intraday_day_change_from_df(
    df: pd.DataFrame,
    *,
    previous_close: Optional[float] = None,
) -> tuple[Optional[float], Optional[float], str]:
    if df is None or df.empty or "close" not in df.columns:
        return None, None, ""
    working = df.sort_index().copy()
    try:
        latest_ts = pd.to_datetime(working.index.max())
    except Exception:
        return None, None, ""
    try:
        parsed_index = pd.to_datetime(working.index, errors="coerce")
        valid_times = [ts.time() for ts in parsed_index if not pd.isna(ts)]
        if not valid_times or all(item.hour == 0 and item.minute == 0 and item.second == 0 for item in valid_times):
            return None, None, latest_ts.date().isoformat()
    except Exception:
        return None, None, latest_ts.date().isoformat()
    same_day = working[parsed_index.date == latest_ts.date()]
    if same_day.empty:
        same_day = working
    closes = pd.to_numeric(same_day["close"], errors="coerce").dropna()
    if closes.empty:
        return None, None, latest_ts.date().isoformat()
    latest = float(closes.iloc[-1])
    if previous_close is not None and previous_close > 0:
        baseline = previous_close
    elif "open" in same_day.columns:
        opens = pd.to_numeric(same_day["open"], errors="coerce").dropna()
        baseline = float(opens.iloc[0]) if not opens.empty else None
    else:
        baseline = float(closes.iloc[0]) if len(closes) >= 1 else None
    if baseline is None or baseline <= 0:
        return None, latest, latest_ts.date().isoformat()
    return round((latest - baseline) / baseline * 100, 2), latest, latest_ts.date().isoformat()


def _shortest_realtime_day_change(kind: str, symbol: str) -> dict[str, Any]:
    target = _text(symbol)
    if not target:
        return {}
    expected_day = _day_change_expected_day("quote_intraday") if _a_day_change_mode() == "quote_intraday" else ""
    for freq in REALTIME_DAY_CHANGE_FREQS:
        try:
            if kind == "index":
                df, source = _index_df(target, freq)
            elif kind == "stock":
                df, source = _stock_df(target, freq)
            elif kind in {"industry", "concept"}:
                chart, latest_heat = _board_heat_chart(target, kind, freq)
                value = _float(latest_heat.get("change_pct"))
                if value is None:
                    continue
                latest_price = _first_numeric(
                    latest_heat.get("change_pct"),
                    (chart.get("ohlcv") or [{}])[-1].get("close") if chart.get("ohlcv") else None,
                )
                as_of = _date_text(latest_heat.get("trade_minute")) or _date_text(
                    (chart.get("ohlcv") or [{}])[-1].get("time") if chart.get("ohlcv") else None
                )
                return {
                    "day_change_pct": value,
                    "daily_change_pct": value,
                    "today_change_pct": value,
                    "gain_pct": value,
                    "latest_price": latest_price,
                    "day_change_source": "board_heat_ticks",
                    "day_change_mode": "minute_intraday",
                    "day_change_as_of": as_of,
                    "day_change_freq": freq,
                }
            else:
                return {}
        except Exception:
            continue
        latest_ts = _df_latest_timestamp(df)
        if expected_day and latest_ts is not None and _date_text(latest_ts) != expected_day:
            continue
        previous_close: Optional[float] = None
        try:
            if latest_ts is not None and kind == "index":
                daily_df, _daily_source = _index_df(target, "daily")
                previous_close = _previous_close_from_daily_df(daily_df, latest_ts)
            elif latest_ts is not None and kind == "stock":
                daily_df, _daily_source = _stock_df(target, "daily")
                previous_close = _previous_close_from_daily_df(daily_df, latest_ts)
        except Exception:
            previous_close = None
        value, latest_price, as_of = _intraday_day_change_from_df(df, previous_close=previous_close)
        if value is None:
            continue
        return {
            "day_change_pct": value,
            "daily_change_pct": value,
            "today_change_pct": value,
            "gain_pct": value,
            "latest_price": latest_price,
            "day_change_source": f"{source or 'bars'}:{freq}",
            "day_change_mode": "minute_intraday",
            "day_change_as_of": as_of,
            "day_change_freq": freq,
            "day_change_basis": "prev_close" if previous_close is not None and previous_close > 0 else "open",
        }
    return {}


def _has_minute_day_change(row: dict[str, Any]) -> bool:
    has_minute = _text(row.get("day_change_mode")) == "minute_intraday" or _text(row.get("day_change_freq")) in REALTIME_DAY_CHANGE_FREQS
    if not has_minute:
        return False
    if _a_day_change_mode() != "quote_intraday":
        return True
    as_of = _date_text(row.get("day_change_as_of") or row.get("as_of") or row.get("dt") or row.get("date"))
    expected_day = _day_change_expected_day("quote_intraday")
    return bool(as_of and expected_day and as_of == expected_day)


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


def _ma5_line_label(freq: Any) -> str:
    bucket = _freq_bucket(freq)
    if bucket == "weekly":
        return "5周线"
    if bucket == "monthly":
        return "5月线"
    if bucket == "daily":
        return "5日线"
    return "5根线"


def _weekly_close_subject(date_text: str) -> str:
    try:
        bar_day = date.fromisoformat(str(date_text)[:10])
        today = _market_today("A")
        days_since_friday = (today.weekday() - 4) % 7
        last_friday = today - timedelta(days=days_since_friday)
        if bar_day == last_friday and bar_day.weekday() == 4:
            return "上周五收盘价"
    except Exception:
        pass
    return f"{date_text}周线收盘价" if date_text else "最新周线收盘价"


def _current_timeframe_ma_state(chart: dict[str, Any], freq: Any = "") -> dict[str, Any]:
    ohlcv = chart.get("ohlcv") if isinstance(chart, dict) else []
    if not isinstance(ohlcv, list) or len(ohlcv) < 5:
        return {}
    chart_meta = chart.get("meta") if isinstance(chart.get("meta"), dict) else {}
    bucket = _freq_bucket(freq or chart_meta.get("freq") or chart.get("freq"))
    rows = [row for row in ohlcv if isinstance(row, dict) and _float(row.get("close")) is not None]
    if len(rows) < 5:
        return {}
    latest = rows[-1]
    pattern = classify_latest_chart_pattern(rows, bucket)
    primary = pattern.get("primary_chart_signal") if isinstance(pattern, dict) else {}
    if not isinstance(primary, dict) or not primary.get("signal_type"):
        return {}
    latest_ts = int(latest.get("time") or 0)
    symbol = _text(chart.get("symbol"))
    source = _text(chart_meta.get("source"))
    market = _text(chart_meta.get("market")) or infer_market(symbol=symbol, source=source)
    date_text = _timestamp_date(latest_ts, market=market, symbol=symbol, source=source) if latest_ts else ""
    level_interactions = pattern.get("level_interactions") if isinstance(pattern.get("level_interactions"), list) else []
    primary_level = primary.get("level") if isinstance(primary.get("level"), dict) else {}
    ma5_level = next(
        (item for item in level_interactions if isinstance(item, dict) and int(item.get("period") or 0) == 5),
        {},
    )
    latest_close = _float(latest.get("close"), _float(primary_level.get("latest_close"), 0)) or 0
    line_label = _ma5_line_label(bucket)
    signal_type = _text(primary.get("signal_type"))
    if signal_type == f"未站稳{line_label}" and bucket == "weekly":
        summary = f"{_weekly_close_subject(date_text)}没站稳{line_label}"
    elif signal_type == f"未站稳{line_label}":
        summary = f"收盘价没站稳{line_label}"
    else:
        summary = _text(primary.get("label")) or signal_type
    detail = _text(primary.get("details"))
    ma5_value = _float(ma5_level.get("value"))
    ma5_distance = _float(ma5_level.get("distance_pct"))
    return {
        "summary": summary,
        "signal_type": signal_type,
        "details": detail,
        "freq": bucket,
        "date_str": date_text,
        "dt": latest_ts,
        "price": round(latest_close, 4),
        "ma5": round(ma5_value, 4) if ma5_value is not None else None,
        "latest_close": round(latest_close, 4),
        "distance_pct": round(ma5_distance, 4) if ma5_distance is not None else None,
        "signal_side": _text(primary.get("side")) or "sell",
        "priority": int(primary.get("priority") or 0),
        "chart_pattern": pattern,
    }


def _current_timeframe_ma_signal(chart: dict[str, Any], freq: Any = "") -> dict[str, Any]:
    state = _current_timeframe_ma_state(chart, freq)
    if not state:
        return {}
    return {
        "dt": state["dt"],
        "date_str": state["date_str"],
        "type": state["signal_type"],
        "signal_type": state["signal_type"],
        "price": state["price"],
        "confidence": 0.86 if state.get("priority", 0) >= 80 else 0.76,
        "freq": state["freq"],
        "details": state["details"],
        "source": "workbench.current_timeframe_ma",
        "pool_status": "current_timeframe_ma",
        "chart_aligned": True,
        "display_scope": "current_timeframe",
        "signal_side": state.get("signal_side") or "sell",
        "signal_family": "ma_state",
        "ma_state": state,
    }


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


def _index_chart_pattern_state(symbol: str, freq: str) -> dict[str, Any]:
    if not _text(symbol):
        return {}
    try:
        df, source = _index_df(symbol, freq)
        chart = _chart_from_df(df, symbol=symbol, freq=freq, source=source)
        return _current_timeframe_ma_state(chart, freq)
    except Exception:
        return {}


def _index_weekly_ma_state(symbol: str) -> dict[str, Any]:
    return _index_chart_pattern_state(symbol, "weekly")


def _index_key_signal(row: dict[str, Any], symbol: str, daily_df: pd.DataFrame) -> tuple[str, dict[str, Any]]:
    explicit = _signal_or_fallback(row, daily_df)
    raw_explicit = [
        _text(row.get("daily_latest_signal")),
        _text(row.get("latest_signal")),
        _text(row.get("signal")),
        _text(row.get("f30_latest_signal")),
        _text(row.get("f15_latest_signal")),
    ]
    has_explicit = any(value and value.lower() not in {"none", "n/a"} and value != "无" for value in raw_explicit)
    if has_explicit and explicit:
        return explicit, {}
    states = [
        _index_chart_pattern_state(symbol, "daily"),
        _index_chart_pattern_state(symbol, "weekly"),
    ]
    states = [state for state in states if state.get("signal_type")]
    if states:
        selected = sorted(
            states,
            key=lambda item: (
                int(item.get("priority") or 0),
                1 if _text(item.get("freq")) == "weekly" else 0,
            ),
            reverse=True,
        )[0]
        return _text(selected.get("signal_type")), selected
    return explicit, {}


def _timeframe_signal_value(row: dict[str, Any], key: str) -> str:
    value = _text(row.get(key))
    if not value or value.lower() in {"none", "n/a"} or value == "无":
        return ""
    return value


def _index_timeframe_signal_entries(row: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    buy_entries: list[dict[str, Any]] = []
    sell_entries: list[dict[str, Any]] = []
    for freq, key in (
        ("daily", "daily_latest_signal"),
        ("30min", "f30_latest_signal"),
        ("15min", "f15_latest_signal"),
    ):
        signal = _timeframe_signal_value(row, key)
        if not signal:
            continue
        side = _manual_clue_signal_side({"signal_type": signal})
        if side not in {"buy", "sell"}:
            continue
        payload = {
            "freq": freq,
            "badge": _freq_badge(freq),
            "side": side,
            "signal_type": signal,
            "score": _float(row.get("score") or row.get("composite_score"), 0) or 0,
            "confidence": _float(row.get("confidence")),
            "signal_date": _text(row.get(f"{key}_date") or row.get("daily_last_dt") or row.get("snapshot_dt")),
            "price": _float(row.get("latest_price") or row.get("snapshot_price")),
        }
        if side == "sell":
            sell_entries.append(payload)
        else:
            buy_entries.append(payload)
    return buy_entries, sell_entries


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
    if value.startswith(("SH.", "SZ.", "BJ.")):
        return True
    if _normalize_hk_code_text(value):
        return True
    return value.isdigit() and len(value) == 6


def _normalize_hk_code_text(raw: str) -> Optional[str]:
    value = str(raw or "").strip().upper().replace(" ", "")
    if not value:
        return None
    if value.endswith(".HK"):
        value = value[:-3]
    if value.startswith(("HK.", "HK:", "HK-")):
        value = value[3:]
    elif value.startswith("HK") and value[2:].isdigit():
        value = value[2:]
    if value.isdigit() and 1 <= len(value) <= 5:
        return value.zfill(5)
    return None


def _canonical_a_share_symbol(raw: str) -> Tuple[Optional[str], Optional[str]]:
    value = str(raw or "").strip().upper()
    code = value.split(".", 1)[1] if "." in value else value
    if not (code.isdigit() and len(code) == 6):
        return None, None
    if code.startswith(("5", "6", "9")):
        return f"SH.{code}", code
    if code.startswith(("0", "1", "2", "3")):
        return f"SZ.{code}", code
    if code.startswith(("8", "4")):
        return f"BJ.{code}", code
    return None, None


def _normalize_stock_symbol(raw: str) -> Tuple[Optional[str], Optional[str]]:
    resolver = get_resolver()
    value = raw.strip().upper()
    if not value:
        return None, None

    macro_symbol = canonical_macro_industry_etf_symbol(value)
    if macro_symbol:
        return macro_symbol, macro_symbol.split(".", 1)[1]

    if value.startswith(("SH.", "SZ.", "BJ.")):
        canonical, raw_code = _canonical_a_share_symbol(value)
        return canonical or value, raw_code or value.split(".", 1)[1]

    hk_code = _normalize_hk_code_text(value)
    if hk_code:
        return f"HK.{hk_code}", hk_code

    if value.isdigit():
        if len(value) == 6:
            canonical, raw_code = _canonical_a_share_symbol(value)
            if canonical:
                return canonical, raw_code

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
    symbol_text = _text(symbol).upper()
    symbol_suffix = symbol_text.split(".")[-1]
    explicit_upper = explicit.upper()
    explicit_suffix = explicit_upper.split(".")[-1]
    if explicit and explicit_upper not in {symbol_text, symbol_suffix} and explicit_suffix != symbol_suffix:
        return explicit
    macro_name = macro_industry_etf_name(symbol)
    if macro_name:
        return macro_name
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


def _intraday_quote_max_age_seconds() -> float:
    try:
        return max(30.0, float(os.getenv("TERMINAL_WORKBENCH_INTRADAY_QUOTE_MAX_AGE_SECONDS", "180")))
    except (TypeError, ValueError):
        return 180.0


def _quote_day_is_stale(quote_day: str, expected_day: str, day_change_mode: str) -> bool:
    if not quote_day or not expected_day or quote_day == expected_day:
        return False
    return True


def _quote_intraday_open_change_pct(doc: dict[str, Any]) -> Optional[float]:
    price = _first_numeric(doc.get("price"), doc.get("close"))
    open_price = _float(doc.get("open"))
    if price is None or open_price is None or open_price <= 0:
        return None
    return round((price - open_price) / open_price * 100, 4)


def _fullmarket_code_candidates(candidates: list[str]) -> list[str]:
    codes: list[str] = []
    for candidate in candidates:
        raw = str(candidate or "").strip().upper()
        pure = raw.split(".", 1)[-1] if "." in raw else raw
        if len(pure) >= 8 and pure[:2] in {"SH", "SZ", "BJ"}:
            pure = pure[2:]
        if pure.isdigit() and len(pure) == 6:
            codes.append(pure)
    return list(dict.fromkeys(codes))


def _fullmarket_spot_quote_doc(symbol: str, candidates: list[str], expected_day: str) -> dict[str, Any]:
    date_key = str(expected_day or "").replace("-", "")[:8]
    if not date_key:
        return {}
    codes = _fullmarket_code_candidates(candidates)
    dot_symbols = [candidate.upper() for candidate in candidates if "." in str(candidate or "")]
    if supports_a_index_minute_cache(symbol):
        codes = []
    if not codes and not dot_symbols:
        return {}
    try:
        row = _mongo_db()["fullmarket_spot_snapshots"].find_one(
            {
                "date_key": date_key,
                "$or": [
                    {"code": {"$in": codes}},
                    {"symbol": {"$in": dot_symbols}},
                ],
            },
            {
                "_id": 0,
                "code": 1,
                "symbol": 1,
                "trade_date": 1,
                "snapshot_at": 1,
                "source": 1,
                "name": 1,
                "latest": 1,
                "price": 1,
                "change": 1,
                "change_pct": 1,
                "turnover_pct": 1,
                "amplitude_pct": 1,
                "vol": 1,
                "amount": 1,
                "open": 1,
                "high": 1,
                "low": 1,
                "prev_close": 1,
            },
            sort=[("snapshot_at", -1)],
        ) or {}
    except Exception:
        return {}
    trade_day = _date_text(row.get("trade_date") or expected_day)
    if trade_day != expected_day:
        return {}
    price = _first_numeric(row.get("price"), row.get("latest"), row.get("close"))
    if price is None or price <= 0:
        return {}
    code = _text(row.get("code")) or (codes[0] if codes else _text(symbol).split(".")[-1])
    return {
        "symbol": _text(row.get("symbol")) or symbol,
        "code": code,
        "name": row.get("name") or "",
        "dt": trade_day,
        "trade_date": trade_day,
        "snapshot_at": row.get("snapshot_at"),
        "source": "fullmarket_spot_snapshots",
        "freshness": "fresh",
        "is_stale": False,
        "stale_reason": "",
        "open": row.get("open"),
        "high": row.get("high"),
        "low": row.get("low"),
        "close": price,
        "price": price,
        "prev_close": row.get("prev_close"),
        "change": row.get("change"),
        "change_pct": row.get("change_pct"),
        "turnover_pct": row.get("turnover_pct"),
        "amplitude_pct": row.get("amplitude_pct"),
        "vol": row.get("vol"),
        "amount": row.get("amount"),
        "_day_change_source": "fullmarket_spot_snapshots",
    }


def _quote_overlay_for_symbol(symbol: str) -> dict[str, Any]:
    day_change_mode = _a_day_change_mode()
    expected_day = _day_change_expected_day(day_change_mode)
    candidates = _quote_symbol_candidates(symbol)
    if not candidates:
        return {"day_change_mode": day_change_mode, "quote_status": "missing", "quote_status_label": "无行情"}
    try:
        doc = _mongo_db()["quote_snapshots"].find_one(
            {"symbol": {"$in": candidates}},
            {"_id": 0},
            sort=[("snapshot_at", -1), ("dt", -1)],
        ) or {}
    except Exception:
        doc = {}
    if not doc:
        doc = _fullmarket_spot_quote_doc(symbol, candidates, expected_day)
    if not doc:
        return {"day_change_mode": day_change_mode, "quote_status": "missing", "quote_status_label": "无行情"}

    quote_day = _quote_dt_text(doc)
    age_seconds = _quote_age_seconds(doc)
    stale_reason = _text(doc.get("stale_reason"))
    quote_day_stale = _quote_day_is_stale(quote_day, expected_day, day_change_mode)
    quote_age_stale = (
        day_change_mode == "quote_intraday"
        and age_seconds is not None
        and age_seconds > _intraday_quote_max_age_seconds()
    )
    is_stale = bool(doc.get("is_stale")) or doc.get("freshness") == "stale" or quote_day_stale or quote_age_stale
    if is_stale:
        fallback_doc = _fullmarket_spot_quote_doc(symbol, candidates, expected_day)
        if fallback_doc:
            doc = fallback_doc
            quote_day = _quote_dt_text(doc)
            age_seconds = _quote_age_seconds(doc)
            stale_reason = _text(doc.get("stale_reason"))
            quote_day_stale = _quote_day_is_stale(quote_day, expected_day, day_change_mode)
            quote_age_stale = (
                day_change_mode == "quote_intraday"
                and age_seconds is not None
                and age_seconds > _intraday_quote_max_age_seconds()
            )
            is_stale = bool(doc.get("is_stale")) or doc.get("freshness") == "stale" or quote_day_stale or quote_age_stale
    if is_stale:
        status = "stale"
        label = "行情陈旧"
        if quote_day_stale:
            stale_reason = stale_reason or f"quote_day={quote_day}, expected={expected_day}"
        elif quote_age_stale:
            stale_reason = stale_reason or f"quote_age_seconds={round(age_seconds or 0, 1)}"
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
    quote_latest_price = _first_numeric(doc.get("price"), doc.get("close"))
    quote_change_pct = _float(doc.get("change_pct"))
    quote_open_price = _float(doc.get("open"))
    quote_open_change_pct = _quote_intraday_open_change_pct(doc)
    day_change_source = _text(doc.get("_day_change_source")) or "quote_snapshots"
    if not is_stale:
        if quote_latest_price is not None:
            overlay["quote_price"] = quote_latest_price
        if quote_change_pct is not None:
            overlay["quote_change_pct"] = quote_change_pct
            overlay["quote_prev_close_change_pct"] = quote_change_pct
            overlay["day_change_source"] = day_change_source
            overlay["day_change_as_of"] = quote_day
            overlay["day_change_basis"] = "prev_close"
        if quote_open_price is not None and quote_open_price > 0:
            overlay["quote_open_price"] = quote_open_price
        if quote_open_change_pct is not None:
            overlay["quote_open_change_pct"] = quote_open_change_pct
    if day_change_mode == "quote_intraday" and status in {"realtime", "delayed"}:
        if quote_latest_price is not None:
            overlay.update({
                "latest_price": quote_latest_price,
                "realtime_price": quote_latest_price,
            })
        if quote_change_pct is not None:
            overlay.update({
                "day_change_pct": quote_change_pct,
                "daily_change_pct": quote_change_pct,
                "today_change_pct": quote_change_pct,
                "gain_pct": quote_change_pct,
                "day_change_source": day_change_source,
                "day_change_as_of": quote_day,
                "day_change_basis": "prev_close",
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


def _apply_quote_overlay(row: dict[str, Any], symbol: str, overlay: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    overlay = overlay if isinstance(overlay, dict) else _quote_overlay_for_symbol(symbol)
    updated = dict(row)
    overlay_mode = _text(overlay.get("day_change_mode")) or _a_day_change_mode()
    row_kind = _text(updated.get("target_kind") or updated.get("kind")).lower()
    quote_change = _float(overlay.get("day_change_pct"))
    if overlay_mode == "quote_intraday" and _has_minute_day_change(updated):
        quote_only = {
            key: value for key, value in overlay.items()
            if key not in {"latest_price", "realtime_price", "day_change_pct", "daily_change_pct", "today_change_pct", "gain_pct", "day_change_source", "day_change_mode", "day_change_as_of", "day_change_freq", "day_change_basis"}
        }
        updated.update(quote_only)
        return updated
    if overlay_mode == "quote_intraday" and overlay.get("quote_status") in {"realtime", "delayed"} and quote_change is not None:
        updated.update(overlay)
        updated.update({
            "day_change_pct": quote_change,
            "daily_change_pct": quote_change,
            "today_change_pct": quote_change,
            "gain_pct": quote_change,
            "day_change_source": overlay.get("day_change_source") or "quote_snapshots",
            "day_change_as_of": overlay.get("day_change_as_of") or overlay.get("quote_as_of") or updated.get("day_change_as_of"),
            "day_change_freq": "",
        })
        return updated
    if overlay_mode == "quote_intraday" and overlay.get("quote_status") in {"realtime", "delayed"} and row_kind != "index":
        quote_price = _first_numeric(overlay.get("latest_price"), overlay.get("realtime_price"), overlay.get("quote_price"))
        if quote_price is not None:
            updated.update(overlay)
            updated.update({
                "latest_price": quote_price,
                "realtime_price": quote_price,
                "day_change_pct": None,
                "daily_change_pct": None,
                "today_change_pct": None,
                "gain_pct": None,
                "day_change_source": "",
                "day_change_as_of": overlay.get("quote_as_of") or "",
            })
            return updated
    if overlay.get("quote_status") in {"stale", "missing"} and row_kind != "index":
        current_daily_close = (
            overlay_mode == "daily_close"
            and _text(updated.get("day_change_source")) == "daily_bars_close"
            and _text(updated.get("day_change_as_of")) == _day_change_expected_day("daily_close")
        )
        if current_daily_close:
            updated.update(overlay)
            return updated
        updated.update({
            "latest_price": None,
            "realtime_price": None,
            "day_change_pct": None,
            "daily_change_pct": None,
            "today_change_pct": None,
            "gain_pct": None,
            "day_change_source": "",
            "day_change_as_of": overlay.get("quote_as_of") or "",
        })
    if overlay_mode == "daily_close" and overlay.get("quote_status") == "closed":
        quote_price = _first_numeric(overlay.get("quote_price"), overlay.get("latest_price"), overlay.get("realtime_price"))
        quote_change = _first_numeric(overlay.get("quote_change_pct"), overlay.get("day_change_pct"))
        if quote_price is not None:
            updated.update({
                "latest_price": quote_price,
                "realtime_price": quote_price,
            })
        if quote_change is not None:
            updated.update({
                "day_change_pct": quote_change,
                "daily_change_pct": quote_change,
                "today_change_pct": quote_change,
                "gain_pct": quote_change,
                "day_change_source": overlay.get("day_change_source") or "quote_snapshots",
                "day_change_as_of": overlay.get("quote_as_of") or updated.get("day_change_as_of"),
                "day_change_freq": "",
                "day_change_basis": "prev_close",
            })
    updated.update(overlay)
    return updated


def _enrich_stock_row(
    row: dict[str, Any],
    range_columns: list[dict[str, Any]],
    *,
    lightweight: bool = False,
    require_range_returns: bool = True,
) -> dict[str, Any]:
    symbol = str(row.get("symbol") or row.get("code") or row.get("label") or "").strip()
    normalized, raw_code = _normalize_stock_symbol(symbol)
    normalized = normalized or symbol
    metadata = dict(row.get("metadata") or {}) if isinstance(row.get("metadata"), dict) else {}
    latest_signal = _text(row.get("latest_signal") or row.get("signal") or row.get("reason") or row.get("direction"))
    if lightweight:
        day_change_mode = _a_day_change_mode()
        minute_change = _shortest_realtime_day_change("stock", normalized) if day_change_mode != "daily_close" else {}
        row_day_change_pct = _first_numeric(
            row.get("day_change_pct"),
            row.get("today_change_pct"),
            row.get("daily_change_pct"),
            row.get("gain_pct"),
            row.get("change_pct"),
            metadata.get("change_pct"),
        )
        day_change_pct = minute_change.get("day_change_pct") if minute_change else row_day_change_pct
        row_latest_price = _first_numeric(
            row.get("latest_price"),
            row.get("price"),
            row.get("close"),
            metadata.get("price"),
            metadata.get("close"),
        )
        latest_price = minute_change.get("latest_price") if minute_change else row_latest_price
        range_returns: dict[str, Any]
        range_return_source: str
        range_return_status = _text(row.get("range_return_status"))
        if require_range_returns:
            range_returns, range_return_source = _compute_stock_range_returns_required(
                normalized,
                range_columns,
            )
            range_return_status = range_return_status or ("ok" if range_returns else "")
        else:
            range_returns = dict(row.get("range_returns") or {}) if isinstance(row.get("range_returns"), dict) else {}
            range_return_source = _text(row.get("range_return_source"))
            if not range_returns and _range_return_column_keys(range_columns):
                range_return_status = range_return_status or "lazy"
        enriched = dict(row)
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
            "today_change_pct": minute_change.get("today_change_pct") if minute_change else day_change_pct,
            "gain_pct": minute_change.get("gain_pct") if minute_change else (day_change_pct if day_change_pct is not None else row.get("gain_pct")),
            "day_change_source": minute_change.get("day_change_source") if minute_change else (row.get("day_change_source") or ("row_snapshot" if day_change_pct is not None else "")),
            "day_change_mode": minute_change.get("day_change_mode") if minute_change else (row.get("day_change_mode") or day_change_mode),
            "day_change_as_of": minute_change.get("day_change_as_of") if minute_change else (row.get("day_change_as_of") or row.get("as_of") or row.get("event_date") or ""),
            "day_change_freq": minute_change.get("day_change_freq") if minute_change else (row.get("day_change_freq") or ""),
            "day_change_basis": minute_change.get("day_change_basis") or row.get("day_change_basis") or "",
            "latest_signal": latest_signal or "待观察",
            "range_returns": range_returns,
            "range_return_source": range_return_source,
            "range_return_status": range_return_status,
            "available_freqs": UI_FREQS,
            "target_kind": "stock",
            "target_label": normalized,
            "target_symbol": normalized,
            "target_freq": DEFAULT_TERMINAL_FREQ,
        })
        return _apply_quote_overlay(enriched, normalized)

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
    minute_change = (
        _shortest_realtime_day_change("stock", normalized)
        if day_change_mode != "daily_close" or daily_day_change is None
        else {}
    )
    latest_price = minute_change.get("latest_price") or ((daily_close_price or cached_latest_price) if day_change_mode == "daily_close" else (
        row.get("latest_price")
        or row.get("price")
        or metadata.get("price")
        or daily_close_price
        or cached_latest_price
    ))
    enriched = dict(row)
    day_change_pct = minute_change.get("day_change_pct") if minute_change else (daily_day_change if day_change_mode == "daily_close" else None)
    day_change_source = minute_change.get("day_change_source") if minute_change else (daily_day_source if day_change_mode == "daily_close" else "")
    effective_day_change_mode = minute_change.get("day_change_mode") if minute_change else day_change_mode
    day_change_as_of = minute_change.get("day_change_as_of") if minute_change else (daily_as_of if day_change_mode == "daily_close" else "")
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
        "today_change_pct": minute_change.get("today_change_pct") if minute_change else (daily_day_change if day_change_mode == "daily_close" else None),
        "gain_pct": minute_change.get("gain_pct") if minute_change else (daily_day_change if day_change_mode == "daily_close" else row.get("gain_pct")),
        "day_change_source": day_change_source,
        "day_change_mode": effective_day_change_mode,
        "day_change_as_of": day_change_as_of,
        "day_change_freq": minute_change.get("day_change_freq") if minute_change else "",
        "latest_signal": latest_signal or _ma_signal_from_df(df),
        "range_returns": _compute_range_returns(df, range_columns, adjust_price_discontinuities=True),
        "range_return_source": source,
        "available_freqs": UI_FREQS,
        "target_kind": "stock",
        "target_label": normalized,
        "target_symbol": normalized,
        "target_freq": DEFAULT_TERMINAL_FREQ,
    })
    return _apply_quote_overlay(enriched, normalized)


def _enrich_shell_stock_row(
    row: dict[str, Any],
    range_columns: list[dict[str, Any]],
    *,
    require_range_returns: bool,
) -> dict[str, Any]:
    try:
        return _enrich_stock_row(
            row,
            range_columns,
            lightweight=True,
            require_range_returns=require_range_returns,
        )
    except TypeError as exc:
        if "require_range_returns" not in str(exc):
            raise
        return _enrich_stock_row(row, range_columns, lightweight=True)
    except RuntimeError as exc:
        message = str(exc)
        if not message.startswith("range_returns_"):
            raise
        enriched = _enrich_stock_row(
            row,
            range_columns,
            lightweight=True,
            require_range_returns=False,
        )
        current_status = _text(enriched.get("range_return_status"))
        if not current_status or current_status == "lazy":
            enriched["range_return_status"] = message.split(":", 1)[0]
        enriched["range_return_error"] = message
        return enriched


def _chain_position_from_membership_row(row: dict[str, Any]) -> dict[str, Any]:
    if not row:
        return {}
    return {
        "chain_id": _text(row.get("chain_id")),
        "chain": _text(row.get("chain_name")),
        "chain_name": _text(row.get("chain_name")),
        "node_id": _text(row.get("node_id")),
        "node": _text(row.get("node_name")),
        "node_name": _text(row.get("node_name")),
        "role": _text(row.get("role") or row.get("representative_relation") or row.get("node_name")),
        "layer": _text(row.get("layer")),
        "stage": _text(row.get("stage")),
        "source": "security_chain_memberships",
        "source_note": _text(row.get("source_note")) or "盘后全局产业链重塑主归属",
        "confidence": row.get("confidence"),
        "exposure_score": row.get("exposure_score"),
        "is_primary_chain": bool(row.get("is_primary_chain")),
        "trade_date": _text(row.get("trade_date")),
        "membership_type": _text(row.get("membership_type")),
        "reviewed_override": bool(row.get("reviewed_override")),
        "source_board_names": _source_board_names(row),
    }


def _terminal_stock_chain_position_map(source_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    codes: set[str] = set()
    symbol_by_code: dict[str, str] = {}
    for row in source_rows:
        symbol = _text(row.get("symbol") or row.get("code") or row.get("label"))
        normalized, raw_code = _normalize_stock_symbol(symbol)
        raw_code = _text(raw_code or (normalized.split(".", 1)[-1] if "." in normalized else ""))
        if raw_code:
            codes.add(raw_code)
            symbol_by_code[raw_code] = normalized or symbol
    if not codes:
        return {}
    try:
        db = _mongo_db()
        latest = db["security_chain_memberships"].find_one(
            {"market": "A", "trade_date": {"$exists": True}},
            {"trade_date": 1},
            sort=[("trade_date", -1)],
        ) or {}
        trade_date = _text(latest.get("trade_date"))
        if not trade_date:
            return {}
        rows = list(db["security_chain_memberships"].find(
            {
                "market": "A",
                "trade_date": trade_date,
                "raw_code": {"$in": sorted(codes)},
                "is_primary_chain": True,
            },
            {"_id": 0},
        ))
    except Exception:
        return {}
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        position = _chain_position_from_membership_row(row)
        raw_code = _text(row.get("raw_code"))
        symbol = _text(row.get("symbol") or symbol_by_code.get(raw_code))
        if symbol and position:
            output[symbol.upper()] = position
        if raw_code and position:
            output[raw_code] = position
    return output


def _light_shell_chain_context(chain_position: dict[str, Any]) -> dict[str, Any]:
    if not chain_position:
        return {}
    chain_name = _text(chain_position.get("chain_name") or chain_position.get("chain"))
    node_name = _text(chain_position.get("node_name") or chain_position.get("node") or chain_position.get("role"))
    return {
        **chain_position,
        "chain_name": chain_name,
        "node_name": node_name,
        "mapping_status": _text(chain_position.get("source")) or "security_chain_memberships",
        "mapping_chain": {
            "chain_id": _text(chain_position.get("chain_id")),
            "chain_name": chain_name,
            "node_id": _text(chain_position.get("node_id")),
            "node_name": node_name,
            "layer": _text(chain_position.get("layer")),
            "stage": _text(chain_position.get("stage")),
            "confidence": chain_position.get("confidence"),
            "source_note": chain_position.get("source_note"),
        },
        "data_truth": {
            "collection": "security_chain_memberships",
            "as_of": _text(chain_position.get("trade_date")),
            "mapping_status": _text(chain_position.get("source")) or "security_chain_memberships",
        },
    }


def _refresh_shell_stock_chain_assignment(row: dict[str, Any], chain_positions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    symbol = _text(row.get("symbol") or row.get("code") or row.get("label"))
    if not symbol:
        return row
    normalized, raw_code = _normalize_stock_symbol(symbol)
    chain_position = chain_positions.get((normalized or symbol).upper()) or chain_positions.get(_text(raw_code))
    if not chain_position:
        return row
    previous = row.get("chain_position") if isinstance(row.get("chain_position"), dict) else {}
    merged = {**previous, **chain_position}
    node_label = _text(chain_position.get("node") or chain_position.get("node_name") or chain_position.get("role"))
    if node_label:
        merged["board_or_concept"] = node_label
    row["chain_position"] = merged
    row["chain_context"] = _light_shell_chain_context(merged)
    return row


def _slim_shell_fib_ma_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    keep = (
        "period",
        "name",
        "value",
        "previous_value",
        "above",
        "near",
        "reclaim",
        "pullback_touch",
        "pullback_acceptance",
        "pullback_breakdown",
        "touch_reclaim",
        "interaction",
        "distance_pct",
        "low_distance_pct",
        "touch_distance_pct",
        "acceptance_score",
    )
    return {key: item.get(key) for key in keep if item.get(key) not in (None, "", [], {})}


def _slim_shell_ma_alignment(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    keep = {
        "latest_close",
        "latest_low",
        "ma_stack",
        "ma20_direction",
        "fib_ma_array_state",
        "above_count",
        "reclaim_count",
        "fib_above_count",
        "fib_reclaim_count",
        "fib_touch_count",
        "fib_accept_count",
        "fib_breakdown_count",
        "fib_accept_periods",
        "fib_touch_periods",
        "fib_breakdown_periods",
        "fib_touch_reclaim_periods",
        "fib_array_summary",
        "fib_support_score",
        "score",
        "summary",
        "tags",
    }
    for period in (5, 8, 10, 13, 20, 21):
        keep.update({
            f"ma{period}",
            f"previous_ma{period}",
            f"above_ma{period}",
            f"near_ma{period}",
            f"reclaim_ma{period}",
            f"distance_ma{period}_pct",
            f"low_distance_ma{period}_pct",
        })
    out = {key: value.get(key) for key in sorted(keep) if value.get(key) not in (None, "", [], {})}
    fib_items = [
        item
        for item in (_slim_shell_fib_ma_item(raw) for raw in value.get("fib_ma_array") or [])
        if item
    ]
    if fib_items:
        out["fib_ma_array"] = fib_items[:6]
    return out


def _shell_ma_acceptance_summary(ma_alignment: Any) -> dict[str, Any]:
    ma = _slim_shell_ma_alignment(ma_alignment)
    if not ma:
        return {}
    fib_items = [item for item in ma.get("fib_ma_array") or [] if isinstance(item, dict)]
    accept_items = [item for item in fib_items if item.get("pullback_acceptance")]
    if not accept_items:
        periods = [int(period) for period in ma.get("fib_accept_periods") or [] if _text(period)]
        for period in periods:
            item = {
                "period": period,
                "name": f"MA{period}",
                "value": ma.get(f"ma{period}"),
                "previous_value": ma.get(f"previous_ma{period}"),
                "above": ma.get(f"above_ma{period}"),
                "distance_pct": ma.get(f"distance_ma{period}_pct"),
                "low_distance_pct": ma.get(f"low_distance_ma{period}_pct"),
                "pullback_acceptance": True,
            }
            accept_items.append({key: val for key, val in item.items() if val not in (None, "", [], {})})
    if not accept_items:
        return {}
    def touch_sort(item: dict[str, Any]) -> float:
        value = _float(item.get("touch_distance_pct"))
        return abs(value) if value is not None else 9999.0

    accept_items.sort(key=touch_sort)
    primary = accept_items[0]
    periods = [
        int(period)
        for period in ma.get("fib_accept_periods") or [item.get("period") for item in accept_items]
        if _text(period)
    ]
    summary = _text(ma.get("fib_array_summary")) or " / ".join(f"MA{period}回踩承接" for period in periods)
    detail_parts = []
    value = _float(primary.get("value"))
    latest_low = _float(ma.get("latest_low"))
    latest_close = _float(ma.get("latest_close"))
    touch_distance = _float(primary.get("touch_distance_pct"))
    distance = _float(primary.get("distance_pct"))
    primary_label = _text(primary.get("name")) or f"MA{primary.get('period')}"
    if value is not None:
        detail_parts.append(f"{primary_label} {value:.2f}")
    if latest_low is not None:
        detail_parts.append(f"低点 {latest_low:.2f}")
    if latest_close is not None:
        detail_parts.append(f"收/现 {latest_close:.2f}")
    if touch_distance is not None:
        detail_parts.append(f"触线 {touch_distance:+.3f}%")
    if distance is not None:
        detail_parts.append(f"现价距线 {distance:+.2f}%")
    return {
        "summary": summary,
        "periods": periods,
        "primary": primary,
        "items": accept_items[:4],
        "state": _text(ma.get("fib_ma_array_state")),
        "score": _float(ma.get("fib_support_score"), _float(ma.get("score"))),
        "detail": " / ".join(part for part in detail_parts if part),
    }


def _ma_alignment_from_price_df(df: pd.DataFrame) -> dict[str, Any]:
    if df is None or df.empty or "close" not in df.columns:
        return {}
    try:
        from signals.sync.modules.technical_signal_scan import _ma_alignment_from_daily_bars

        working = df.sort_index().copy()
        working["close"] = pd.to_numeric(working["close"], errors="coerce")
        for col in ("open", "high", "low"):
            if col not in working.columns:
                working[col] = working["close"]
            else:
                working[col] = pd.to_numeric(working[col], errors="coerce").fillna(working["close"])
        working = working.dropna(subset=["close"])
        if len(working) < 5:
            return {}
        bars = list(working[["open", "high", "low", "close"]].itertuples(index=False))
        return _ma_alignment_from_daily_bars(bars)
    except Exception:
        return {}


def _index_ma_fields_from_daily_df(df: pd.DataFrame) -> dict[str, Any]:
    ma_alignment = _ma_alignment_from_price_df(df)
    slim_ma = _slim_shell_ma_alignment(ma_alignment)
    if not slim_ma:
        return {}
    fields: dict[str, Any] = {"ma_alignment": slim_ma}
    ma_acceptance = _shell_ma_acceptance_summary(ma_alignment)
    if ma_acceptance:
        fields["ma_acceptance"] = ma_acceptance
    return fields


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
    entry_factor = evidence.get("entry_factor") if isinstance(evidence.get("entry_factor"), dict) else value.get("entry_factor")
    if isinstance(entry_factor, dict):
        entry_factor_keys = (
            "group",
            "type",
            "price",
            "today_high",
            "previous_high",
            "breakout_pct",
            "five_day_gain_pct",
            "volume_ratio",
            "date",
            "date_str",
        )
        out["entry_factor"] = {
            key: entry_factor.get(key)
            for key in entry_factor_keys
            if entry_factor.get(key) not in (None, "", [], {})
        }
    ma_alignment = value.get("ma_alignment") if isinstance(value.get("ma_alignment"), dict) else evidence.get("ma_alignment")
    if isinstance(ma_alignment, dict):
        slim_ma = _slim_shell_ma_alignment(ma_alignment)
        if slim_ma:
            out["ma_alignment"] = slim_ma
        ma_acceptance = _shell_ma_acceptance_summary(ma_alignment)
        if ma_acceptance:
            out["ma_acceptance"] = ma_acceptance
    return out


def _slim_shell_chain_position(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    keep = (
        "chain",
        "node",
        "role",
        "board_or_concept",
        "chain_name",
        "node_name",
        "phase",
        "source",
        "mapping_status",
        "chain_id",
        "node_id",
        "confidence",
        "exposure_score",
        "membership_type",
        "reviewed_override",
        "source_board_names",
        "trade_date",
        "rank",
    )
    return {key: value.get(key) for key in keep if value.get(key) not in (None, "", [], {})}


def _slim_shell_chain_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    keep = (
        "board_or_concept",
        "chain_name",
        "node_name",
        "phase",
        "source",
        "mapping_status",
        "chain_id",
        "node_id",
        "confidence",
        "summary",
    )
    out = {key: value.get(key) for key in keep if value.get(key) not in (None, "", [], {})}
    evidence = _slim_shell_chain_position(value.get("evidence"))
    if evidence:
        out["evidence"] = evidence
    return out


def _slim_shell_domain(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    keep = (
        "kind",
        "kind_label",
        "name",
        "code",
        "symbol",
        "change_pct",
        "day_change_pct",
        "rank",
        "source",
        "status",
        "label",
    )
    return {key: value.get(key) for key in keep if value.get(key) not in (None, "", [], {})}


def _slim_shell_domain_list(values: Any, limit: int = 3) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    return [row for row in (_slim_shell_domain(item) for item in values[:limit]) if row]


def _slim_shell_overlay_list(values: Any, limit: int = 2) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in values[:limit]:
        if not isinstance(item, dict):
            continue
        row = _slim_shell_domain(item)
        matched = item.get("matched_names")
        if isinstance(matched, list):
            row["matched_names"] = [_text(value) for value in matched[:2] if _text(value)]
        if row:
            rows.append(row)
    return rows


def _slim_shell_event_overlay_list(values: Any, child_key: str, limit: int = 4) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in values[:limit]:
        if not isinstance(item, dict):
            continue
        source = _slim_shell_domain(item.get("source"))
        children = _slim_shell_overlay_list(item.get(child_key), 3)
        if source or children:
            rows.append({"source": source, child_key: children})
    return rows


def _shell_signal_label_from_reason(reason: Any) -> str:
    if not isinstance(reason, dict):
        return ""
    signal = _text(reason.get("signal_type") or reason.get("reason_type"))
    freq = _text(reason.get("freq"))
    if signal and freq and not signal.startswith(freq):
        return f"{freq} {signal}"
    return signal or freq


def _shell_dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = _text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def _shell_stock_reason_labels(row: dict[str, Any], *, side: str = "buy", limit: int = 3) -> list[str]:
    direct_key = "right_signal_reasons" if side == "buy" else "risk_signal_reasons" if side == "risk" else "left_signal_reasons"
    direct = [
        _text(item)
        for item in row.get(direct_key) or []
        if _text(item)
    ]
    if direct:
        return _shell_dedupe_preserve_order(direct)[:limit]
    reasons = []
    for reason in row.get("inclusion_reasons") or []:
        if not isinstance(reason, dict):
            continue
        reason_side = _text(reason.get("signal_side")).lower()
        if side == "buy" and reason_side == "sell":
            continue
        if side == "risk" and reason_side not in {"sell", "risk"}:
            continue
        if side == "left" and "left" not in _text(reason.get("decision_effect")).lower() and _text(reason.get("signal_side")).lower() != "left":
            continue
        label = _shell_signal_label_from_reason(reason)
        if label:
            reasons.append(label)
    return _shell_dedupe_preserve_order(reasons)[:limit]


def _shell_stock_timeframe_labels(row: dict[str, Any], bucket_key: str, *, limit: int = 2) -> list[str]:
    sides = row.get("timeframe_signal_sides") if isinstance(row.get("timeframe_signal_sides"), dict) else {}
    bucket = sides.get(bucket_key) if isinstance(sides.get(bucket_key), dict) else {}
    labels: list[str] = []
    for side_key in ("right", "left"):
        for item in bucket.get(side_key) or []:
            if not isinstance(item, dict):
                continue
            label = " ".join([_text(item.get("freq")), _text(item.get("label"))]).strip()
            if label:
                labels.append(label)
    return _shell_dedupe_preserve_order(labels)[:limit]


def _shell_stock_entry_factor(row: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for key in ("top_buy_reason", "technical_evidence"):
        value = row.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    candidates.extend(reason for reason in row.get("inclusion_reasons") or [] if isinstance(reason, dict))
    for reason in candidates:
        evidence = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
        entry_factor = evidence.get("entry_factor") if isinstance(evidence.get("entry_factor"), dict) else reason.get("entry_factor")
        signal = _text(reason.get("signal_type") or evidence.get("signal_type"))
        if isinstance(entry_factor, dict) and (_text(entry_factor.get("group")) == "200d_new_high_breakout" or "200日新高" in signal):
            return entry_factor
        if "200日新高" in signal:
            return {"group": "200d_new_high_breakout"}
    return {}


def _shell_stock_pct_text(value: Any, *, digits: int = 1) -> str:
    numeric = _float(value)
    if numeric is None:
        return ""
    return f"{numeric:+.{digits}f}%"


def _shell_stock_metric_text(label: str, value: Any, *, suffix: str = "", digits: int = 1) -> str:
    numeric = _float(value)
    if numeric is None:
        return ""
    return f"{label}{numeric:.{digits}f}{suffix}"


def _shell_stock_breakout_summary(entry_factor: dict[str, Any]) -> str:
    if not entry_factor:
        return ""
    parts = []
    breakout = _shell_stock_pct_text(entry_factor.get("breakout_pct"), digits=1)
    if breakout:
        parts.append(f"突破{breakout}")
    five_day = _shell_stock_pct_text(entry_factor.get("five_day_gain_pct"), digits=1)
    if five_day:
        parts.append(f"5日{five_day}")
    volume = _shell_stock_metric_text("量比", entry_factor.get("volume_ratio"), digits=2)
    if volume:
        parts.append(volume)
    return "200日新高" + (f"({ ' / '.join(parts) })" if parts else "")


def _shell_summary_clean_condition(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    blocked = ("风险", "主线", "产业链", "角色", "普通缩量", "缩量")
    if any(token in text for token in blocked):
        return ""
    return text[:32]


def _shell_stock_trade_summary(
    row: dict[str, Any],
    *,
    entry_factor: dict[str, Any],
    display_badges: list[dict[str, Any]] | None = None,
) -> str:
    labels = [
        _text(item.get("label"))
        for item in (display_badges or [])
        if isinstance(item, dict) and _text(item.get("kind")) in _SHELL_HARD_BADGE_KINDS and _text(item.get("label"))
    ][:3]
    missing = _shell_summary_clean_condition(row.get("missing_condition") or row.get("primary_blocker"))
    lead = " / ".join(labels[:3]) if labels else "硬信号待确认"
    parts = [lead]
    if missing and missing not in lead:
        parts.append(missing)
    return "；".join(parts)[:64]


def _shell_stock_display_action(row: dict[str, Any]) -> str:
    gate = _text(row.get("entry_gate_status"))
    trade_stage = _text(row.get("trade_stage"))
    pool_type = _text(row.get("pool_type"))
    risk_marked = bool(row.get("risk_marked"))
    if pool_type == "risk" or trade_stage == "skip_now":
        return "暂不参与"
    if gate == "entry_confirmed" or trade_stage == "confirmed_entry":
        return "复核仓位/止损"
    if gate == "entry_attack_confirmed" or trade_stage == "attack_entry":
        return "进攻买点复核"
    if trade_stage == "left_attack" or gate == "left_attack_confirmed":
        return "低吸承接复核"
    if gate == "entry_waiting_right_side_confirm":
        return "等5m/15m确认"
    if gate == "entry_waiting_30m_confirm":
        return "等30m买点"
    if gate == "entry_waiting_upper_context":
        return "等日/周买点"
    if pool_type == "watch":
        return "盯盘等买点"
    if pool_type in {"clue", "clue_pool"} or trade_stage == "clue_pool":
        return "线索先观察"
    action = _text(row.get("trader_action") or row.get("recommended_action") or row.get("next_action"))
    if action:
        return f"{action}/风险标记" if risk_marked and "风险" not in action else action
    return "复核"


_SHELL_HARD_BADGE_KINDS = {"buy_point", "sell_point", "new_high", "ma_climb", "gap_volume_price"}


def _shell_badge_freq_label(freq: Any) -> str:
    value = _text(freq)
    return {
        "日线": "日",
        "daily": "日",
        "1d": "日",
        "D": "日",
        "d": "日",
        "周线": "周",
        "weekly": "周",
        "1w": "周",
        "W": "周",
        "w": "周",
        "月线": "月",
        "monthly": "月",
        "1M": "月",
        "M": "月",
        "30分钟": "30m",
        "30min": "30m",
        "30m": "30m",
        "15分钟": "15m",
        "15min": "15m",
        "15m": "15m",
        "5分钟": "5m",
        "5min": "5m",
        "5m": "5m",
    }.get(value, value)


def _shell_badge_priority(kind: str, score: Any = None, *, ma_score: Any = None, volume_ratio: Any = None) -> int:
    if kind in {"buy_point", "sell_point"}:
        return 900 + min(80, int(_float(score) or 0))
    if kind == "new_high":
        return 800 + min(80, int(_float(score) or 0))
    if kind == "ma_climb":
        return 700 + min(80, int(_float(ma_score) or _float(score) or 0))
    if kind == "gap_volume_price":
        ratio = _float(volume_ratio) or 0.0
        volume_bonus = min(60, int(max(0.0, ratio - 1.0) * 30))
        return 600 + volume_bonus + min(40, int(_float(score) or 0))
    return 0


def _normalize_shell_display_badge(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    kind = _text(value.get("kind"))
    if kind not in _SHELL_HARD_BADGE_KINDS:
        return {}
    label = _text(value.get("label"))
    if not label:
        return {}
    priority = _float(value.get("priority"))
    out: dict[str, Any] = {
        "kind": kind,
        "label": label[:14],
        "timeframe": _text(value.get("timeframe") or value.get("freq")),
        "priority": int(priority if priority is not None else _shell_badge_priority(kind)),
    }
    signal_type = _text(value.get("signal_type"))
    if signal_type:
        out["signal_type"] = signal_type
    tone = _text(value.get("tone"))
    if tone:
        out["tone"] = tone
    return {key: item for key, item in out.items() if item not in (None, "", [], {})}


def _shell_reason_text(reason: dict[str, Any]) -> str:
    evidence = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
    details = evidence.get("details")
    detail_text = " ".join(_text(item) for item in details.values()) if isinstance(details, dict) else _text(details)
    return " ".join([
        _text(reason.get("signal_type")),
        _text(reason.get("reason_type")),
        detail_text,
    ])


def _shell_reason_is_hard_technical(reason: dict[str, Any]) -> bool:
    return isinstance(reason, dict) and _text(reason.get("reason_type")) in {"technical_trigger", "technical_signal"}


def _shell_reason_entry_factor(reason: dict[str, Any]) -> dict[str, Any]:
    evidence = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
    entry_factor = evidence.get("entry_factor") if isinstance(evidence.get("entry_factor"), dict) else reason.get("entry_factor")
    if isinstance(entry_factor, dict):
        return entry_factor
    details = evidence.get("details") if isinstance(evidence.get("details"), dict) else {}
    return details.get("entry_factor") if isinstance(details.get("entry_factor"), dict) else {}


def _shell_reason_ma_climb(reason: dict[str, Any]) -> dict[str, Any]:
    if _text(reason.get("signal_family")) != "ma_climb":
        return {}
    evidence = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
    climb = evidence.get("ma_climb") if isinstance(evidence.get("ma_climb"), dict) else reason.get("ma_climb")
    return climb if isinstance(climb, dict) else {}


def _shell_badge_signal_label(reason: dict[str, Any], *, fallback: str) -> str:
    signal_type = _text(reason.get("signal_type")) or fallback
    for token in ("一买", "二买", "三买", "背驰买", "底背离", "趋势买", "一卖", "二卖", "三卖", "顶背离", "跌破", "死叉"):
        if token in signal_type:
            signal_type = token
            break
    prefix = _shell_badge_freq_label(reason.get("freq") or reason.get("timeframe"))
    return f"{prefix}{signal_type}" if prefix and not signal_type.startswith(prefix) else signal_type


def _shell_badge_ma_label(reason: dict[str, Any], climb: dict[str, Any]) -> str:
    ma_name = _text(climb.get("effective_ma_name"))
    if not ma_name:
        period = _text(climb.get("ma_period") or climb.get("period"))
        ma_name = f"MA{period}" if period else "MA"
    prefix = _shell_badge_freq_label(reason.get("freq") or reason.get("timeframe"))
    return f"{prefix}{ma_name}攀爬" if prefix else f"{ma_name}攀爬"


def _shell_display_badge_from_reason(reason: dict[str, Any]) -> dict[str, Any]:
    if not _shell_reason_is_hard_technical(reason):
        return {}
    signal_side = _text(reason.get("signal_side"))
    timeframe = _text(reason.get("freq") or reason.get("timeframe"))
    text = _shell_reason_text(reason)
    score = _float(reason.get("score")) or 0.0
    base = {"timeframe": timeframe, "signal_type": _text(reason.get("signal_type"))}
    if signal_side == "buy" and any(token in text for token in ("一买", "二买", "三买", "背驰买", "底背离", "趋势买")):
        return {**base, "kind": "buy_point", "label": _shell_badge_signal_label(reason, fallback="买点"), "priority": _shell_badge_priority("buy_point", score)}
    if signal_side == "sell" and any(token in text for token in ("一卖", "二卖", "三卖", "顶背离", "跌破", "死叉")):
        return {**base, "kind": "sell_point", "label": _shell_badge_signal_label(reason, fallback="卖点"), "priority": _shell_badge_priority("sell_point", score)}
    if any(token in text for token in ("200d_new_high_breakout", "200日新高", "新高突破")):
        return {**base, "kind": "new_high", "label": "200日新高", "priority": _shell_badge_priority("new_high", score)}
    climb = _shell_reason_ma_climb(reason)
    climb_score = _float(climb.get("climb_score")) or 0.0
    if climb.get("running") and climb_score >= 60.0:
        return {**base, "kind": "ma_climb", "label": _shell_badge_ma_label(reason, climb), "priority": _shell_badge_priority("ma_climb", ma_score=climb_score)}
    if any(token in text for token in ("缺口买:持续", "缺口买:突破", "持续缺口", "突破缺口")):
        entry_factor = _shell_reason_entry_factor(reason)
        volume_ratio = max(_float(entry_factor.get("volume_ratio")) or 0.0, _float(entry_factor.get("recent_volume_ratio")) or 0.0)
        return {**base, "kind": "gap_volume_price", "label": f"{_shell_badge_freq_label(timeframe)}强缺口量价".strip(), "priority": _shell_badge_priority("gap_volume_price", score, volume_ratio=volume_ratio)}
    return {}


def _shell_stock_display_badges(row: dict[str, Any], *, entry_factor: dict[str, Any]) -> list[dict[str, Any]]:
    structured = [
        _normalize_shell_display_badge(item)
        for item in row.get("display_badges") or []
        if isinstance(item, dict)
    ]
    structured = [item for item in structured if item]
    if structured:
        structured.sort(key=lambda item: int(item.get("priority") or 0), reverse=True)
        return structured[:3]

    badges: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    reason_candidates = [
        row.get("top_buy_reason"),
        row.get("technical_evidence"),
        *(row.get("inclusion_reasons") or []),
    ]
    for reason in reason_candidates:
        badge = _shell_display_badge_from_reason(reason) if isinstance(reason, dict) else {}
        badge = _normalize_shell_display_badge(badge)
        if not badge:
            continue
        key = (_text(badge.get("kind")), _text(badge.get("timeframe")))
        if key in seen:
            continue
        seen.add(key)
        badges.append(badge)
    if not badges and entry_factor:
        badges.append(_normalize_shell_display_badge({
            "kind": "new_high",
            "label": "200日新高",
            "timeframe": "日线",
            "priority": _shell_badge_priority("new_high"),
            "signal_type": "200日新高",
        }))
    badges = [item for item in badges if item]
    badges.sort(key=lambda item: int(item.get("priority") or 0), reverse=True)
    return badges[:3]


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
        "gain_pct",
        "day_change_source",
        "day_change_mode",
        "day_change_as_of",
        "day_change_basis",
        "day_change_freq",
        "realtime_price",
        "quote_price",
        "quote_open_price",
        "quote_open_change_pct",
        "quote_prev_close_change_pct",
        "quote_change_pct",
        "latest_signal",
        "reason",
        "signal",
        "direction",
        "range_returns",
        "range_return_source",
        "range_return_status",
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
        "resonance_context",
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
        "coverage_status",
        "decision_effect",
        "blocked_by",
        "primary_blocker",
        "recommended_action",
        "missing_gates",
        "promotion_path",
        "trade_stage",
        "stage_label",
        "current_position",
        "trade_intent",
        "trade_intent_label",
        "setup_mode",
        "setup_mode_label",
        "trade_role",
        "trade_role_label",
        "trade_identity",
        "trade_identity_label",
        "trader_read",
        "ai_trade_summary",
        "evidence_summary",
        "setup_side_label",
        "setup_rank_tier",
        "market_setup_bias",
        "index_setup_side",
        "index_setup_label",
        "stock_setup_side",
        "stock_setup_label",
        "setup_alignment",
        "alignment_policy",
        "alignment_score",
        "market_volume_state",
        "market_volume_label",
        "market_volume_ratio",
        "mainline_status",
        "mainline_confirmation_reason",
        "mainline_rank_tier",
        "left_allowed_reason",
        "setup_explanation",
        "entry_logic_summary",
        "daily_weekly_signal",
        "trade_cycle_signal",
        "execution_cycle_signal",
        "primary_timeframe_signal",
        "watch_sort_priority",
        "watch_backfill_source",
        "clue_quality_score",
        "hot_rank_tier",
        "hot_rank_sources",
        "hot_rank_ranks",
        "hot_rank_strategy_tags",
        "hot_rank_as_of",
        "promotion_gates",
        "entry_reason",
        "missing_condition",
        "invalidation",
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
        "risk_marked",
        "risk_marker",
        "risk_marker_reason_type",
        "risk_level",
        "technical_signal_groups",
        "timeframe_signal_sides",
        "upper_timeframe_side",
        "trade_timeframe",
        "trade_timeframe_side",
        "execution_timeframe_side",
        "chain_phase",
        "theme_rank_bonus",
        "theme_alignment_level",
        "sector_policy",
        "sector_policy_label",
        "sector_policy_reason",
        "sector_policy_matched_token",
        "sector_policy_source",
        "broad_market_label",
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
    ma_alignment = _slim_shell_ma_alignment(row.get("ma_alignment"))
    if ma_alignment:
        out["ma_alignment"] = ma_alignment
    ma_acceptance = _shell_ma_acceptance_summary(row.get("ma_alignment"))
    if ma_acceptance:
        out["ma_acceptance"] = ma_acceptance
    chain_context = _slim_shell_chain_context(row.get("chain_context"))
    if chain_context:
        out["chain_context"] = chain_context
    chain_position = _slim_shell_chain_position(row.get("chain_position"))
    if chain_position:
        out["chain_position"] = chain_position
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
    entry_factor = _shell_stock_entry_factor(row)
    display_badges = _shell_stock_display_badges(row, entry_factor=entry_factor)
    display_summary = _shell_stock_trade_summary(row, entry_factor=entry_factor, display_badges=display_badges)
    display_action = _shell_stock_display_action(row)
    if display_summary:
        out["display_summary"] = display_summary
    if display_action:
        out["display_action"] = display_action
    if display_badges:
        out["display_badges"] = display_badges
    breakout = _shell_stock_breakout_summary(entry_factor)
    if breakout:
        out["display_breakout"] = breakout
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
        "range_return_status",
        "range_return_meta",
        "intraday_momentum_returns",
        "late_session_signal",
        "late_session_reason",
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
        "taxonomy_node_id",
        "taxonomy_node_name",
        "market_logic",
        "market_logic_node",
        "layer",
        "stage",
        "source_driver",
        "source_kind_mix",
        "route_explain",
        "change_display_kind",
        "change_display_label",
        "change_explain",
        "primary_domain",
        "reference_domain",
        "domain_change_stats",
        "representative_confirmation",
        "chain_confirmation",
        "mismatch_flags",
        "display_rank_score",
        "non_chain_reason",
        "latest_signal",
        "trader_action",
        "action_status",
        "invalidates_when",
        "rank_reason",
        "trace_summary",
        "explanation",
        "carrier",
        "mapping_chain",
        "data_truth",
    )
    out = {key: row.get(key) for key in keep if row.get(key) not in (None, "", [], {})}
    for key in ("source_driver", "primary_domain", "reference_domain", "carrier", "representative_confirmation", "chain_confirmation"):
        slim = _slim_shell_domain(row.get(key))
        if slim:
            out[key] = slim
        else:
            out.pop(key, None)
    integrated = _slim_shell_domain_list(row.get("integrated_domains"), 3)
    if integrated:
        out["integrated_domains"] = integrated
    concept_overlays = _slim_shell_overlay_list(row.get("source_concept_overlays"), 2)
    if concept_overlays:
        out["source_concept_overlays"] = concept_overlays
    theme_overlays = _slim_shell_overlay_list(row.get("source_theme_overlays"), 1)
    if theme_overlays:
        out["source_theme_overlays"] = theme_overlays
    event_concepts = _slim_shell_event_overlay_list(row.get("source_event_concept_overlays"), "concepts", 4)
    if event_concepts:
        out["source_event_concept_overlays"] = event_concepts
    event_themes = _slim_shell_event_overlay_list(row.get("source_event_theme_overlays"), "themes", 4)
    if event_themes:
        out["source_event_theme_overlays"] = event_themes
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


def _enrich_index_row(
    row: dict[str, Any],
    range_columns: list[dict[str, Any]],
    *,
    include_range_returns: bool = True,
) -> dict[str, Any]:
    symbol = str(row.get("symbol") or row.get("code") or row.get("label") or row.get("name") or "").strip()
    df, source = _index_df(symbol, "daily") if symbol else (pd.DataFrame(), "")
    range_returns = _compute_range_returns(df, range_columns) if include_range_returns else {}
    buy_timeframes, sell_timeframes = _index_timeframe_signal_entries(row)
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
    minute_change = (
        _shortest_realtime_day_change("index", symbol)
        if day_change_mode != "daily_close" or daily_day_change is None
        else {}
    )
    day_change_pct = minute_change.get("day_change_pct") if minute_change else (daily_day_change if day_change_mode == "daily_close" else None)
    day_change_source = minute_change.get("day_change_source") if minute_change else (daily_day_source if day_change_mode == "daily_close" else "")
    effective_day_change_mode = minute_change.get("day_change_mode") if minute_change else day_change_mode
    day_change_as_of = minute_change.get("day_change_as_of") if minute_change else (daily_as_of if day_change_mode == "daily_close" else "")
    ma_fields = _index_ma_fields_from_daily_df(df)
    key_signal, key_ma_state = _index_key_signal(row, symbol, df)
    enriched = dict(row)
    enriched.update({
        "kind": "index",
        "label": row.get("name") or row.get("label") or symbol,
        "name": row.get("name") or row.get("label") or symbol,
        "code": symbol,
        "latest_price": minute_change.get("latest_price") or ((daily_close_price or cached_latest_price) if day_change_mode == "daily_close" else (row.get("latest_price") or cached_latest_price)),
        "day_change_pct": day_change_pct,
        "daily_change_pct": day_change_pct,
        "today_change_pct": minute_change.get("today_change_pct") if minute_change else (daily_day_change if day_change_mode == "daily_close" else None),
        "gain_pct": minute_change.get("gain_pct") if minute_change else (daily_day_change if day_change_mode == "daily_close" else row.get("gain_pct")),
        "day_change_source": day_change_source,
        "day_change_mode": effective_day_change_mode,
        "day_change_as_of": day_change_as_of,
        "day_change_freq": minute_change.get("day_change_freq") if minute_change else "",
        "latest_signal": key_signal,
        "buy_timeframes": buy_timeframes,
        "sell_timeframes": sell_timeframes,
        "range_returns": range_returns,
        "range_return_source": source,
        "range_return_status": _range_return_status_from_returns(range_returns, range_columns) if include_range_returns else "lazy",
        "available_freqs": UI_FREQS,
        "target_kind": "index",
        "target_label": row.get("name") or row.get("label") or symbol,
        "target_symbol": symbol,
        "target_freq": DEFAULT_TERMINAL_FREQ,
    })
    if ma_fields:
        enriched.update(ma_fields)
    if key_ma_state:
        enriched["current_timeframe_ma"] = key_ma_state
        signal_type = _text(key_ma_state.get("signal_type"))
        if signal_type.startswith("未站稳"):
            enriched["signal_detail"] = key_ma_state.get("summary") or key_ma_state.get("details")
        else:
            enriched["signal_detail"] = key_ma_state.get("details") or key_ma_state.get("summary")
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


def _load_terminal_technical_signal_rows(symbol: str, *, limit: int = 300, kind: str = "stock") -> list[dict[str, Any]]:
    if not symbol:
        return []
    try:
        db = _mongo_db()
        candidates = _probe_symbol_candidates(symbol, kind=kind)
        if kind == "index":
            for candidate in _probe_symbol_candidates(symbol, kind="stock"):
                if candidate and candidate not in candidates:
                    candidates.append(candidate)
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
    ma_alignment = signal.get("ma_alignment") if isinstance(signal.get("ma_alignment"), dict) else evidence.get("ma_alignment")
    signal_type = _text(signal.get("signal_type") or signal.get("type") or signal.get("reason"))
    freq = _text(signal.get("freq") or signal.get("timeframe")) or _freq_label(DEFAULT_TERMINAL_FREQ)
    side = _manual_clue_signal_side(signal)
    event_dt = _iso_dt(signal.get("dt") or signal.get("signal_date") or signal.get("updated_at"))
    score = _float(signal.get("score"), _float(signal.get("total_score"), 0) or 0) or 0
    decision_effect = "exit_priority" if side == "sell" else "confirm" if side == "buy" else "context_only"
    queue_lane = "risk_exit_first" if side == "sell" else "manual_signal_review"
    reason = {
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
    if isinstance(ma_alignment, dict):
        reason["ma_alignment"] = ma_alignment
    return reason


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
        "upper_context": "等日/周买点确认",
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
    missing_condition = " / ".join(_manual_clue_missing_label(gate) for gate in missing_gates) or "买点路径已走通，但手动探索不自动转买点池"

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
    trade_role = "risk_first" if sell_reasons or conflict else "clue"
    trade_role_label = {
        "risk_first": "暂不参与",
        "clue": "线索池",
    }.get(trade_role, "线索池")

    if sell_reasons or conflict:
        trader_read = f"手动探索：{chain_text + '，' if chain_text else ''}有技术信号但存在卖点或周期冲突，暂不参与；非持仓不推风险动作。"
        trade_intent_label = "暂不参与"
        recommended_action = "暂不参与"
    elif buy_reasons and has_upper and has_30m and not has_execution:
        trader_read = f"手动探索：{chain_text + '，' if chain_text else ''}日/周和30m已有信号，等5m/15m下单确认。"
        trade_intent_label = "试仓候选"
        recommended_action = "等5m/15m确认"
    elif buy_reasons and not has_upper:
        trader_read = f"手动探索：{chain_text + '，' if chain_text else ''}只有短周期技术线索，先等日/周买点确认。"
        trade_intent_label = "线索来源"
        recommended_action = "等日/周买点"
    elif buy_reasons:
        trader_read = f"手动探索：{chain_text + '，' if chain_text else ''}已有日/周技术线索，按30m和5m/15m确认逐级复核。"
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
        "trade_stage": "skip_now" if sell_reasons or conflict else "clue_pool",
        "stage_label": "暂不参与" if sell_reasons or conflict else "线索池",
        "current_position": trade_intent_label,
        "decision_stage": "risk_first" if sell_reasons or conflict else "strategy_candidate",
        "setup_mode": trade_role,
        "setup_mode_label": trade_role_label,
        "trade_role": trade_role,
        "trade_role_label": trade_role_label,
        "trade_identity": "manual_exploration",
        "trade_identity_label": "用户探索",
        "trade_intent": "skip_now" if sell_reasons or conflict else "probe_candidate" if has_upper and has_30m and not has_execution else "clue_only",
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
    if signal_bucket not in CHART_FREQ_ORDER or effective_bucket not in CHART_FREQ_ORDER:
        return "other_timeframe"
    if _is_higher_timeframe(signal_bucket, effective_bucket):
        return "higher_timeframe_context"
    return "lower_timeframe_context"


def _should_include_chart_signal(signal_freq: str, effective_freq: str) -> bool:
    return _chart_signal_display_scope(signal_freq, effective_freq) in {
        "current_timeframe",
        "higher_timeframe_context",
        "lower_timeframe_context",
    }


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
    buckets: list[list[dict[str, Any]]] = []
    seen_symbols: set[str] = set()
    seen_signals: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        symbol, _ = _candidate_stock_symbol(candidate)
        if not symbol or symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        candidate_reference_signals = candidate.get("reference_signals") if isinstance(candidate.get("reference_signals"), list) else []
        rows = (
            [dict(item) for item in candidate_reference_signals if isinstance(item, dict)]
            if candidate_reference_signals
            else [
                *(_load_terminal_technical_signal_rows(symbol, limit=80) or []),
                *_custom_signal_rows(symbol, limit=200),
            ]
        )
        if not rows:
            continue
        current_rows = [
            row for row in rows
            if _freq_bucket(row.get("freq") or row.get("timeframe")) in {current_freq, ""}
        ]
        preferred = [*current_rows, *[row for row in rows if row not in current_rows]]
        preferred.sort(
            key=lambda item: (
                0 if _freq_bucket(item.get("freq") or item.get("timeframe")) == current_freq else 1,
                CHART_FREQ_ORDER.get(_freq_bucket(item.get("freq") or item.get("timeframe")), 99),
                _text(item.get("dt") or item.get("signal_date") or item.get("updated_at")),
            )
        )
        bucket: list[dict[str, Any]] = []
        for row in preferred:
            signal_type = _text(row.get("signal_type") or row.get("type") or row.get("reason"))
            if not signal_type:
                continue
            freq = _freq_bucket(row.get("freq") or row.get("timeframe"))
            signal_date = row.get("signal_date") or row.get("dt") or row.get("updated_at")
            key = "|".join([symbol, freq, signal_type, _text(signal_date)[:10]])
            if key in seen_signals:
                continue
            seen_signals.add(key)
            bucket.append({
                "symbol": symbol,
                "name": _text(candidate.get("name") or candidate.get("stock_name")) or _stock_name(symbol),
                "relation": _text(candidate.get("relation") or candidate.get("role") or candidate.get("representative_type")),
                "type": signal_type,
                "signal_type": signal_type,
                "freq": freq,
                "date_str": _signal_date(row),
                "signal_date": signal_date,
                "source": row.get("source") or ("terminal_technical_signals" if row.get("signal_family") or row.get("technical_evidence") else "signals.signal_pool"),
                "details": _text(row.get("details")) or _signal_details(row),
                "confidence": _float(row.get("confidence")),
                "score": _float(row.get("score") or row.get("total_score")),
                "price": _float(row.get("price")),
                "signal_side": _manual_clue_signal_side(row),
            })
            if len(bucket) >= 6:
                break
        if bucket:
            buckets.append(bucket)
    related: list[dict[str, Any]] = []
    round_index = 0
    while len(related) < limit and any(round_index < len(bucket) for bucket in buckets):
        for bucket in buckets:
            if round_index >= len(bucket):
                continue
            related.append(bucket[round_index])
            if len(related) >= limit:
                return related
        round_index += 1
    return related


def _enrich_reference_candidate_signals(
    candidates: list[dict[str, Any]],
    requested_freq: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for candidate in candidates[:limit]:
        if not isinstance(candidate, dict):
            continue
        row = dict(candidate)
        symbol, raw_code = _candidate_stock_symbol(row)
        if not symbol:
            output.append(row)
            continue
        row.setdefault("symbol", symbol)
        row.setdefault("code", symbol)
        row.setdefault("raw_code", raw_code or symbol.split(".", 1)[-1])
        row.setdefault("name", _stock_name(symbol, row))
        row["timeframe_signals"] = dict(row.get("timeframe_signals") if isinstance(row.get("timeframe_signals"), dict) else {})
        row["sell_timeframe_signals"] = dict(row.get("sell_timeframe_signals") if isinstance(row.get("sell_timeframe_signals"), dict) else {})
        row["timeframe_signal_stack"] = dict(row.get("timeframe_signal_stack") if isinstance(row.get("timeframe_signal_stack"), dict) else {})

        reference_rows = [
            *(_load_terminal_technical_signal_rows(symbol, limit=80) or []),
            *_load_signal_pool_rows(limit=120, symbol=symbol),
        ]
        reference_signals: list[dict[str, Any]] = []
        seen: set[str] = set()
        for signal in reference_rows:
            side = _manual_clue_signal_side(signal)
            if side not in {"buy", "sell"}:
                continue
            signal_type = _text(signal.get("signal_type") or signal.get("type") or signal.get("reason"))
            if not signal_type:
                continue
            freq = _freq_bucket(signal.get("freq") or signal.get("timeframe"))
            key = "|".join([
                side,
                freq,
                signal_type,
                _text(signal.get("dt") or signal.get("signal_date") or signal.get("updated_at"))[:10],
            ])
            if key in seen:
                continue
            seen.add(key)
            _add_timeframe_signal(row, signal, side=side)
            reference_signals.append({
                "symbol": symbol,
                "name": row.get("name"),
                "type": signal_type,
                "signal_type": signal_type,
                "freq": freq,
                "side": side,
                "signal_side": side,
                "date_str": _signal_date(signal),
                "signal_date": signal.get("signal_date") or signal.get("dt") or signal.get("updated_at"),
                "source": signal.get("source") or ("terminal_technical_signals" if signal.get("signal_family") or signal.get("technical_evidence") else "signals.signal_pool"),
                "details": _signal_details(signal),
                "confidence": _float(signal.get("confidence")),
                "score": _float(signal.get("score") or signal.get("total_score")),
                "price": _float(signal.get("price")),
            })

        buy_signals = row.get("timeframe_signals") if isinstance(row.get("timeframe_signals"), dict) else {}
        sell_signals = row.get("sell_timeframe_signals") if isinstance(row.get("sell_timeframe_signals"), dict) else {}
        row["buy_timeframes"] = [buy_signals[freq] for freq in BUY_FREQS if freq in buy_signals]
        row["sell_timeframes"] = [sell_signals[freq] for freq in BUY_FREQS if freq in sell_signals]
        row["signal_stack"] = {
            freq: row.get("timeframe_signal_stack", {}).get(freq)
            for freq in BUY_FREQS
            if isinstance(row.get("timeframe_signal_stack"), dict) and row.get("timeframe_signal_stack", {}).get(freq)
        }
        if reference_signals:
            row["reference_signals"] = reference_signals[:8]
            row["reference_signal_count"] = len(reference_signals)
            sell_badges = [f"卖{item.get('badge') or item.get('freq') or ''}" for item in row.get("sell_timeframes", []) if isinstance(item, dict)]
            buy_badges = [item.get("badge") or item.get("freq") or "" for item in row.get("buy_timeframes", []) if isinstance(item, dict)]
            row["latest_signal"] = "/".join([badge for badge in sell_badges + buy_badges if badge]) or row.get("latest_signal")
            row["evidence_summary"] = "参考个股买卖点: " + " / ".join(
                [
                    " ".join([_freq_badge(item.get("freq")), _text(item.get("type") or item.get("signal_type"))]).strip()
                    for item in reference_signals[:4]
                    if isinstance(item, dict)
                ]
            )
        output.append(row)
    return output


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
    base = _text(evidence.get("details") or evidence.get("score_details") or signal.get("details") or signal.get("summary"))
    ma_alignment = signal.get("ma_alignment") if isinstance(signal.get("ma_alignment"), dict) else evidence.get("ma_alignment")
    ma_acceptance = _shell_ma_acceptance_summary(ma_alignment)
    if ma_acceptance:
        ma_detail = _text(ma_acceptance.get("detail"))
        base = " · ".join(part for part in [base, ma_acceptance.get("summary"), ma_detail] if _text(part))
    return base[:240]


def _terminal_technical_chart_signals(symbol: str, freq: str, chart: dict[str, Any], *, kind: str = "stock") -> list[dict[str, Any]]:
    chart_meta = chart.get("meta") if isinstance(chart.get("meta"), dict) else {}
    effective_freq = _freq_bucket(chart_meta.get("freq") or freq)
    market = _text(chart_meta.get("market")) or infer_market(symbol=symbol, source=_text(chart_meta.get("source")))
    source = _text(chart_meta.get("source")) or "signals"
    ohlcv = chart.get("ohlcv") if isinstance(chart.get("ohlcv"), list) else []
    start_ts = int(ohlcv[0]["time"]) if ohlcv and isinstance(ohlcv[0], dict) and ohlcv[0].get("time") else None
    end_ts = int(ohlcv[-1]["time"]) if ohlcv and isinstance(ohlcv[-1], dict) and ohlcv[-1].get("time") else None
    output: list[dict[str, Any]] = []
    for signal in _load_terminal_technical_signal_rows(symbol, kind=kind):
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
        ma_alignment = signal.get("ma_alignment") if isinstance(signal.get("ma_alignment"), dict) else (
            signal.get("technical_evidence", {}).get("ma_alignment")
            if isinstance(signal.get("technical_evidence"), dict)
            else {}
        )
        slim_ma = _slim_shell_ma_alignment(ma_alignment)
        ma_acceptance = _shell_ma_acceptance_summary(ma_alignment)
        row = {
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
        }
        if slim_ma:
            row["ma_alignment"] = slim_ma
        if ma_acceptance:
            row["ma_acceptance"] = ma_acceptance
        output.append(row)
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


def _trimmed_mean(values: list[float], *, trim_ratio: float = 0.1) -> Optional[float]:
    clean = sorted(value for value in values if value and value > 0)
    if not clean:
        return None
    trim = int(len(clean) * trim_ratio)
    if trim and len(clean) > trim * 2 + 2:
        clean = clean[trim:-trim]
    return sum(clean) / len(clean) if clean else None


def _log_zscore(value: float, values: list[float]) -> float:
    clean = [math.log(max(item, 1.0)) for item in values if item and item > 0]
    if len(clean) < 5 or value <= 0:
        return 0.0
    mean = sum(clean) / len(clean)
    variance = sum((item - mean) ** 2 for item in clean) / len(clean)
    std = math.sqrt(variance)
    if std <= 1e-9:
        return 0.0
    return (math.log(max(value, 1.0)) - mean) / std


def _zscore(value: float, values: list[float]) -> float:
    clean = [item for item in values if item is not None and math.isfinite(item)]
    if len(clean) < 5:
        return 0.0
    mean = sum(clean) / len(clean)
    variance = sum((item - mean) ** 2 for item in clean) / len(clean)
    std = math.sqrt(variance)
    if std <= 1e-9:
        return 0.0
    return (value - mean) / std


def _volume_signal_params(freq: str) -> dict[str, Any]:
    bucket = _freq_bucket(freq)
    if bucket == "weekly":
        return {
            "window": 20, "min_history": 12, "cooldown": 1,
            "mild_expand": 1.2, "expand": 1.6, "extreme_expand": 2.3,
            "mild_contract": 0.78, "contract": 0.6, "extreme_contract": 0.4,
            "expand_z": 1.3, "extreme_expand_z": 2.0, "contract_z": -1.0, "extreme_contract_z": -1.6,
        }
    if bucket == "daily":
        return {
            "window": 20, "min_history": 14, "cooldown": 2,
            "mild_expand": 1.25, "expand": 1.8, "extreme_expand": 2.8,
            "mild_contract": 0.8, "contract": 0.55, "extreme_contract": 0.35,
            "expand_z": 1.5, "extreme_expand_z": 2.2, "contract_z": -1.2, "extreme_contract_z": -1.8,
        }
    return {
        "window": 40, "min_history": 20, "cooldown": 3,
        "mild_expand": 1.35, "expand": 2.0, "extreme_expand": 3.0,
        "mild_contract": 0.78, "contract": 0.5, "extreme_contract": 0.3,
        "expand_z": 1.7, "extreme_expand_z": 2.4, "contract_z": -1.3, "extreme_contract_z": -2.0,
    }


def _volume_state(volume_ratio: float, volume_z: float, params: dict[str, Any]) -> tuple[str, int]:
    if volume_ratio >= params["extreme_expand"] or volume_z >= params["extreme_expand_z"]:
        return "extreme_expand", 4
    if volume_ratio >= params["expand"] or volume_z >= params["expand_z"]:
        return "expand", 3
    if volume_ratio >= params["mild_expand"] or volume_z >= 0.8:
        return "mild_expand", 2
    if volume_ratio <= params["extreme_contract"] or volume_z <= params["extreme_contract_z"]:
        return "extreme_contract", 4
    if volume_ratio <= params["contract"] or volume_z <= params["contract_z"]:
        return "contract", 3
    if volume_ratio <= params["mild_contract"] or volume_z <= -0.8:
        return "mild_contract", 2
    return "normal", 0


def _bar_shape(row: dict[str, Any], previous_close: Optional[float]) -> dict[str, float]:
    open_ = _float(row.get("open"))
    high = _float(row.get("high"))
    low = _float(row.get("low"))
    close = _float(row.get("close"))
    if open_ is None or high is None or low is None or close is None:
        return {}
    range_value = max(high - low, 0.000001)
    return_pct = ((close - previous_close) / previous_close) if previous_close else 0.0
    amplitude_pct = (range_value / previous_close) if previous_close else 0.0
    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "body_pct": abs(close - open_) / range_value,
        "upper_shadow_pct": max(0.0, high - max(open_, close)) / range_value,
        "lower_shadow_pct": max(0.0, min(open_, close) - low) / range_value,
        "close_location": (close - low) / range_value,
        "return_pct": return_pct,
        "amplitude_pct": amplitude_pct,
    }


def _volume_signal_details(
    *,
    volume_ratio: float,
    amount_ratio: Optional[float],
    volume_z: float,
    return_pct: float,
    return_z: float,
    range_ratio: float,
    body_pct: float,
    upper_shadow_pct: float,
    relation: str,
    context: str,
) -> str:
    parts = [
        relation,
        f"量比{volume_ratio:.2f}",
        f"z{volume_z:.2f}",
        f"涨跌{(return_pct * 100):+.2f}%",
        f"涨跌z{return_z:.2f}",
        f"波幅比{range_ratio:.2f}",
    ]
    if amount_ratio is not None and amount_ratio > 0:
        parts.append(f"额比{amount_ratio:.2f}")
    parts.extend([
        f"实体{body_pct * 100:.0f}%",
        f"上影{upper_shadow_pct * 100:.0f}%",
        context,
    ])
    return " · ".join(part for part in parts if part)


def _volume_context(
    *,
    state: str,
    severity: int,
    shape: dict[str, float],
    break_high: bool,
    break_low: bool,
    return_z: float,
    range_ratio: float,
) -> Optional[tuple[str, str, str, int, str]]:
    expand = state in {"mild_expand", "expand", "extreme_expand"}
    contract = state in {"mild_contract", "contract", "extreme_contract"}
    body_pct = shape.get("body_pct", 0.0)
    upper = shape.get("upper_shadow_pct", 0.0)
    lower = shape.get("lower_shadow_pct", 0.0)
    close_location = shape.get("close_location", 0.5)
    return_pct = shape.get("return_pct", 0.0)
    price_up = return_pct >= 0.012 or return_z >= 0.9 or (break_high and return_pct > 0)
    price_down = return_pct <= -0.012 or return_z <= -0.9 or (break_low and return_pct < 0)
    price_flat = not price_up and not price_down
    abnormal_range = range_ratio >= 1.45
    compressed_range = range_ratio <= 0.82
    if expand and break_low:
        return "量价共振下杀", "放量跌破前低/平台", "sell", 95, "量价同向向下"
    if expand and upper >= 0.45 and close_location <= 0.58:
        return "量价背离冲高回落", "上冲放量但收不住", "sell", 90, "价量背离"
    if expand and price_flat and body_pct <= 0.30:
        return "价平量增分歧", "放量但价格不跟", "sell", 84, "价量背离"
    if state == "extreme_expand" and body_pct <= 0.38 and upper >= 0.28 and lower >= 0.22:
        return "巨量分歧无方向", "巨量但多空方向不足", "neutral", 78, "量价分歧"
    if expand and price_up and break_high and body_pct >= 0.42 and close_location >= 0.60:
        return "量价齐升突破", "价涨放量突破近端高点", "buy", 88, "量价同向向上"
    if expand and abnormal_range and body_pct >= 0.42:
        if price_down:
            return "量价同步扩跌", "放量且波动向下扩张", "sell", 80, "量价同向向下"
        if price_up:
            return "量价同步扩张", "价涨放量且波动扩张", "buy", 78, "量价同向向上"
        return "量价扩张分歧", "放量扩波但方向不足", "neutral", 76, "量价分歧"
    if contract and price_up and break_high:
        return "价升量缩背离", "价格新高但量能不确认", "neutral", 74, "价量背离"
    if contract and price_down and not break_low:
        return "缩量回踩承接", "下跌缩量且未破位", "neutral", 70, "量价收敛"
    if state == "extreme_contract" and compressed_range and close_location >= 0.45:
        return "量价收敛企稳", "极致缩量且波动收敛", "neutral", 70, "量价收敛"
    if contract and not break_low and (return_pct <= 0 or lower >= 0.35):
        return "缩量回踩承接", "缩量回踩/抛压收敛", "neutral", 68, "量价收敛"
    if severity >= 4:
        return "极端量能分歧", "量能极端但方向不足", "neutral", 66, "量价分歧"
    return None


def _volume_signal_chart_signals(symbol: str, freq: str, chart: dict[str, Any]) -> list[dict[str, Any]]:
    chart_meta = chart.get("meta") if isinstance(chart.get("meta"), dict) else {}
    effective_freq = _freq_bucket(chart_meta.get("freq") or freq)
    market = _text(chart_meta.get("market")) or infer_market(symbol=symbol, source=_text(chart_meta.get("source")))
    source = _text(chart_meta.get("source")) or "signals"
    ohlcv = chart.get("ohlcv") if isinstance(chart.get("ohlcv"), list) else []
    rows = [row for row in ohlcv if isinstance(row, dict) and _float(row.get("volume"), 0) and row.get("time")]
    params = _volume_signal_params(effective_freq)
    if len(rows) < params["min_history"] + 2:
        return []

    output: list[dict[str, Any]] = []
    recent_states: list[str] = []
    last_emit_index = -999
    last_emit_type = ""
    last_emit_state = "normal"
    last_emit_severity = 0
    start = max(1, len(rows) - 180)
    for index in range(start, len(rows)):
        history = rows[max(0, index - params["window"]):index]
        if len(history) < params["min_history"]:
            continue
        volumes = [_float(item.get("volume"), 0) or 0 for item in history]
        amounts = [_float(item.get("amount"), 0) or 0 for item in history]
        volume = _float(rows[index].get("volume"), 0) or 0
        amount = _float(rows[index].get("amount"), 0) or 0
        volume_baseline = _trimmed_mean(volumes)
        amount_baseline = _trimmed_mean(amounts)
        if not volume_baseline or volume <= 0:
            continue
        volume_ratio = volume / volume_baseline
        amount_ratio = amount / amount_baseline if amount_baseline and amount > 0 else None
        volume_z = _log_zscore(volume, volumes)
        state, severity = _volume_state(volume_ratio, volume_z, params)
        recent_states.append(state)
        if len(recent_states) > 3:
            recent_states.pop(0)
        if state == "normal" or severity < 2:
            last_emit_state = "normal"
            continue
        if state in {"contract", "extreme_contract"} and sum(1 for item in recent_states if item in {"contract", "extreme_contract"}) < 2:
            continue
        previous_close = _float(rows[index - 1].get("close")) if index > 0 else None
        shape = _bar_shape(rows[index], previous_close)
        if not shape:
            continue
        recent_window = rows[max(0, index - min(20, params["window"])):index]
        recent_high = max((_float(item.get("high"), 0) or 0 for item in recent_window), default=0.0)
        recent_low = min((_float(item.get("low"), 0) or 0 for item in recent_window if _float(item.get("low"), 0)), default=0.0)
        break_high = bool(recent_high and shape["close"] > recent_high)
        break_low = bool(recent_low and shape["close"] < recent_low)
        ranges = []
        returns = []
        for pos, item in enumerate(history):
            prev = _float(history[pos - 1].get("close")) if pos > 0 else _float(item.get("close"))
            bar = _bar_shape(item, prev)
            if bar:
                ranges.append(bar.get("amplitude_pct", 0.0))
                returns.append(bar.get("return_pct", 0.0))
        range_baseline = _trimmed_mean(ranges) or 0.0
        range_ratio = shape["amplitude_pct"] / range_baseline if range_baseline > 0 else 1.0
        return_z = _zscore(shape["return_pct"], returns)
        context = _volume_context(
            state=state,
            severity=severity,
            shape=shape,
            break_high=break_high,
            break_low=break_low,
            return_z=return_z,
            range_ratio=range_ratio,
        )
        if context is None:
            continue
        signal_type, context_text, signal_side, priority, relation = context
        cooldown = int(params["cooldown"])
        if index - last_emit_index < cooldown:
            same_type = signal_type == last_emit_type
            flip_state = (state.endswith("expand") and "contract" in last_emit_state) or ("contract" in state and last_emit_state.endswith("expand"))
            if same_type or flip_state or severity <= last_emit_severity:
                continue
        ts = int(rows[index].get("time") or 0)
        details = _volume_signal_details(
            volume_ratio=volume_ratio,
            amount_ratio=amount_ratio,
            volume_z=volume_z,
            return_pct=shape["return_pct"],
            return_z=return_z,
            range_ratio=range_ratio,
            body_pct=shape["body_pct"],
            upper_shadow_pct=shape["upper_shadow_pct"],
            relation=relation,
            context=context_text,
        )
        output.append({
            "dt": ts,
            "date_str": _timestamp_date(ts, market=market, symbol=symbol, source=source),
            "type": signal_type,
            "price": shape.get("close"),
            "confidence": round(min(0.95, max(0.58, 0.5 + severity * 0.08 + min(abs(volume_z), 3.0) * 0.03)), 4),
            "freq": effective_freq or _canonical_freq(freq),
            "details": details,
            "source": "terminal_volume_price_anomalies",
            "pool_status": "volume_warning",
            "chart_aligned": False,
            "display_scope": "current_timeframe",
            "signal_side": signal_side,
            "signal_family": "volume",
            "render_pane": "volume",
            "display_pane": "volume",
            "volume_state": state,
            "volume_context": context_text,
            "volume_price_relation": relation,
            "volume_ratio": round(volume_ratio, 4),
            "amount_ratio": round(amount_ratio, 4) if amount_ratio is not None else None,
            "volume_z": round(volume_z, 4),
            "return_pct": round(shape["return_pct"], 6),
            "return_z": round(return_z, 4),
            "range_ratio": round(range_ratio, 4),
            "severity": "high" if priority >= 88 else "medium" if priority >= 76 else "low",
        })
        last_emit_index = index
        last_emit_type = signal_type
        last_emit_state = state
        last_emit_severity = severity
    return output[-12:]


def _index_report_chart_signals(report: dict[str, Any], chart: dict[str, Any], freq: str) -> list[dict[str, Any]]:
    chart_meta = chart.get("meta") if isinstance(chart.get("meta"), dict) else {}
    effective_freq = _freq_bucket(chart_meta.get("freq") or freq)
    symbol = _text(report.get("symbol") or chart.get("symbol"))
    market = _text(chart_meta.get("market")) or infer_market(symbol=symbol, source=_text(chart_meta.get("source")))
    source = _text(chart_meta.get("source")) or "index_bars"
    ohlcv = chart.get("ohlcv") if isinstance(chart.get("ohlcv"), list) else []
    if not ohlcv:
        return []
    latest_bar = next((row for row in reversed(ohlcv) if isinstance(row, dict) and row.get("time")), None)
    if not latest_bar:
        return []
    latest_ts = int(latest_bar.get("time") or 0)
    if not latest_ts:
        return []
    output: list[dict[str, Any]] = []
    for signal_freq, signal_key, trend_key in (
        ("daily", "daily_latest_signal", "daily_trend"),
        ("30min", "f30_latest_signal", "f30_trend"),
        ("15min", "f15_latest_signal", "f15_trend"),
    ):
        signal_type = _timeframe_signal_value(report, signal_key)
        if not signal_type:
            continue
        side = _manual_clue_signal_side({"signal_type": signal_type})
        price = (
            _float(latest_bar.get("high") or latest_bar.get("close"))
            if side == "sell"
            else _float(latest_bar.get("low") or latest_bar.get("close"))
            if side == "buy"
            else _float(latest_bar.get("close"))
        )
        output.append({
            "dt": latest_ts,
            "date_str": _timestamp_date(latest_ts, market=market, symbol=symbol, source=source),
            "type": signal_type,
            "price": price,
            "confidence": 0.72,
            "freq": signal_freq,
            "details": " · ".join(
                item for item in [_freq_badge(signal_freq), _text(report.get(trend_key)), "index_report"] if item
            ),
            "source": "signals.index_report",
            "pool_status": "index_timeframe_signal",
            "chart_aligned": True,
            "display_scope": _chart_signal_display_scope(signal_freq, effective_freq),
            "signal_side": side,
            "signal_family": "index_report",
        })
    return output


def _index_signal_context_from_row(row: dict[str, Any], *, name: str, symbol: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    stack = row.get("signal_stack") if isinstance(row.get("signal_stack"), dict) else {}
    context = {
        "name": _text(row.get("name") or row.get("label") or name),
        "symbol": _text(row.get("symbol") or row.get("target_symbol") or symbol),
        "latest_signal": _timeframe_signal_value(row, "latest_signal"),
        "daily_trend": _text(row.get("daily_trend")),
        "f30_trend": _text(row.get("f30_trend")),
        "f15_trend": _text(row.get("f15_trend")),
        "daily_latest_signal": _timeframe_signal_value(row, "daily_latest_signal")
        or _timeframe_signal_value(stack, "daily"),
        "f30_latest_signal": _timeframe_signal_value(row, "f30_latest_signal")
        or _timeframe_signal_value(stack, "30min")
        or _timeframe_signal_value(stack, "30m"),
        "f15_latest_signal": _timeframe_signal_value(row, "f15_latest_signal")
        or _timeframe_signal_value(stack, "15min")
        or _timeframe_signal_value(stack, "15m"),
    }
    if any(_timeframe_signal_value(context, key) for key in (
        "daily_latest_signal",
        "f30_latest_signal",
        "f15_latest_signal",
        "latest_signal",
    )):
        return context
    return {}


def _cached_static_index_signal_context(name: str, symbol: str) -> dict[str, Any]:
    payload = _SHELL_CACHE.get("payload")
    if not isinstance(payload, dict):
        return {}
    expected = {item.lower() for item in (name, symbol) if _text(item)}
    groups = payload.get("watchlist_groups") if isinstance(payload.get("watchlist_groups"), dict) else {}
    candidates: list[Any] = []
    for key in ("indices", "macro_indices"):
        if isinstance(payload.get(key), list):
            candidates.extend(payload[key])
        if isinstance(groups.get(key), list):
            candidates.extend(groups[key])
    for row in candidates:
        if not isinstance(row, dict):
            continue
        row_keys = {
            _text(row.get(key)).lower()
            for key in ("name", "label", "symbol", "code", "target_label", "target_symbol")
            if _text(row.get(key))
        }
        if expected and row_keys.isdisjoint(expected):
            continue
        context = _index_signal_context_from_row(row, name=name, symbol=symbol)
        if context:
            return context
    return {}


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
    if not ohlcv:
        return ts, None, False
    bar_by_time = {
        int(row.get("time") or 0): row
        for row in ohlcv
        if isinstance(row, dict) and row.get("time")
    }
    if ts in bar_by_time:
        row = bar_by_time[ts]
        return ts, _float(row.get("close")), False

    def price_for_row(row: dict[str, Any]) -> Optional[float]:
        if _is_sell_signal(signal):
            return _float(row.get("high") or row.get("close"))
        if _is_buy_signal(signal):
            return _float(row.get("low") or row.get("close"))
        return _float(row.get("close"))

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
                return int(row.get("time") or ts), price_for_row(row), True
        return ts, None, False
    same_day = [
        row for row in ohlcv
        if isinstance(row, dict)
        and row.get("time")
        and _timestamp_date(int(row["time"]), market=market, symbol=symbol, source=source) == signal_date
    ]
    if not same_day:
        return ts, None, False
    same_day.sort(key=lambda item: int(item.get("time") or 0))
    row = same_day[-1]
    has_intraday_time = len(str(signal_dt or "").strip()) > 10
    if has_intraday_time and ts and ts > 100_000:
        row = next((item for item in same_day if int(item.get("time") or 0) >= ts), same_day[-1])
    return int(row.get("time") or ts), price_for_row(row), True


def _merge_signal_pool_into_chart(chart: dict[str, Any], symbol: str, freq: str, *, kind: str = "stock") -> dict[str, Any]:
    technical_signals = _terminal_technical_chart_signals(symbol, freq, chart, kind=kind)
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


def _latest_terminal_ma_acceptance(symbol: str) -> dict[str, Any]:
    for signal in _load_terminal_technical_signal_rows(symbol, limit=40):
        if not isinstance(signal, dict):
            continue
        evidence = signal.get("technical_evidence") if isinstance(signal.get("technical_evidence"), dict) else {}
        ma_alignment = signal.get("ma_alignment") if isinstance(signal.get("ma_alignment"), dict) else evidence.get("ma_alignment")
        ma_acceptance = _shell_ma_acceptance_summary(ma_alignment)
        if not ma_acceptance:
            continue
        ma_acceptance.update({
            "source_collection": "terminal_technical_signals",
            "signal_type": _text(signal.get("signal_type") or signal.get("type") or signal.get("reason")),
            "freq": _text(signal.get("freq") or signal.get("timeframe")),
            "as_of": _text(signal.get("as_of")),
            "event_dt": _iso_dt(signal.get("dt") or signal.get("signal_date") or signal.get("updated_at")),
        })
        return ma_acceptance
    return {}


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
            trader_action = "暂不参与"
            invalidates_when = "卖出/风险信号解除后重新进入买点池"
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
        macro_group = _text(item.get("macro_group")) or (
            MACRO_GROUP_MAJOR_INDICES if kind == "index" else MACRO_GROUP_INDUSTRY_ETFS
        )
        if not name or not symbol:
            continue
        key = f"{kind}:{symbol.lower()}"
        if key in seen:
            continue
        seen.add(key)
        source_report = reports_by_name.get(name) or reports_by_symbol.get(symbol.lower())
        row = dict(source_report or {
            "name": name,
            "label": name,
            "symbol": symbol,
            "code": symbol,
        })
        row.setdefault("name", name)
        row.setdefault("label", name)
        row.setdefault("symbol", symbol)
        if not source_report and kind != "stock" and not _macro_shell_fallback_load_enabled():
            daily_df, daily_source = _index_df(symbol, "daily") if symbol else (pd.DataFrame(), "")
            key_signal, key_ma_state = _index_key_signal({}, symbol, daily_df)
            latest_price = (
                float(daily_df["close"].iloc[-1])
                if daily_df is not None and not daily_df.empty and "close" in daily_df.columns
                else None
            )
            range_returns = _compute_range_returns(daily_df, range_columns)
            enriched = {
                **row,
                "kind": "index",
                "latest_price": latest_price,
                "day_change_pct": None,
                "daily_change_pct": None,
                "latest_signal": key_signal or "待观察",
                "range_returns": range_returns,
                "range_return_source": daily_source,
                "range_return_status": _range_return_status_from_returns(range_returns, range_columns),
                "available_freqs": UI_FREQS,
            }
            if key_ma_state:
                enriched["current_timeframe_ma"] = key_ma_state
                signal_type = _text(key_ma_state.get("signal_type"))
                if signal_type.startswith("未站稳"):
                    enriched["signal_detail"] = key_ma_state.get("summary") or key_ma_state.get("details")
                else:
                    enriched["signal_detail"] = key_ma_state.get("details") or key_ma_state.get("summary")
            target_kind = "index"
            target_label = name
            target_symbol = symbol
        elif kind == "stock":
            enriched = _enrich_stock_row({
                **row,
                "name": name,
                "label": symbol,
                "symbol": symbol,
                "kind": "stock",
            }, range_columns)
            if not enriched.get("latest_price"):
                continue
            if not _text(enriched.get("range_return_status")):
                range_returns = enriched.get("range_returns") if isinstance(enriched.get("range_returns"), dict) else {}
                enriched["range_return_status"] = _range_return_status_from_returns(range_returns, range_columns)
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
            "macro_group": macro_group,
            "macro_group_label": macro_group_label(macro_group),
            "display_type_label": macro_group_type_label(macro_group),
            "lane": "quote_lane",
            "second_screen_role": "market_direction_anchor",
            "action_status": "观察",
            "trader_action": "观察关键指数方向和主题共振",
            "invalidates_when": "指数跌破对应周期防守均线或主题扩散失败",
            "theme_tags": MINGDAO_INDEX_THEMES.get(name, []),
            "latest_signal": enriched.get("latest_signal") or "待观察",
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


def _macro_shell_fallback_load_enabled() -> bool:
    return _text(os.getenv("WORKBENCH_MACRO_SHELL_LOAD_FALLBACK")).lower() in {"1", "true", "yes", "on"}


def _macro_shell_raw_rows(macro_group: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in MINGDAO_MACRO_WATCHLIST:
        if _text(item.get("macro_group")) != macro_group:
            continue
        name = _text(item.get("name"))
        symbol = _text(item.get("symbol"))
        kind = _text(item.get("kind")) or ("stock" if "." in symbol else "index")
        normalized, raw_code = _normalize_stock_symbol(symbol) if kind == "stock" else ("", "")
        target_symbol = normalized or symbol
        rows.append({
            "kind": kind,
            "label": name or target_symbol,
            "name": name or target_symbol,
            "symbol": target_symbol,
            "code": target_symbol,
            "raw_code": raw_code or target_symbol.split(".")[-1],
            "macro_group": macro_group,
            "macro_group_label": macro_group_label(macro_group),
            "display_type_label": macro_group_type_label(macro_group),
            "lane": "quote_lane",
            "second_screen_role": "market_direction_anchor",
            "latest_signal": "待观察",
            "range_returns": {},
            "range_return_status": "lazy",
            "target_kind": kind,
            "target_label": target_symbol if kind == "stock" else (name or target_symbol),
            "target_symbol": target_symbol,
            "target_freq": DEFAULT_TERMINAL_FREQ,
        })
    return rows


def _shell_etf_analysis(strategy_snapshot: dict[str, Any]) -> dict[str, Any]:
    etf_analysis = strategy_snapshot.get("etf_analysis")
    return dict(etf_analysis) if isinstance(etf_analysis, dict) else {}


def _shell_etf_universe_total(etf_analysis: dict[str, Any], review_count: int) -> int:
    universe = etf_analysis.get("universe")
    if isinstance(universe, dict):
        total = _float(universe.get("total"))
        if total is not None:
            return int(total)
    total = _float(etf_analysis.get("total"))
    return int(total) if total is not None else review_count


def _shell_etf_review_rows(strategy_snapshot: dict[str, Any], *, limit: int = 80) -> list[dict[str, Any]]:
    etf_analysis = _shell_etf_analysis(strategy_snapshot)
    review_rows = etf_analysis.get("review_universe")
    if not isinstance(review_rows, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in review_rows[: max(0, limit)]:
        if not isinstance(item, dict):
            continue
        symbol = _text(item.get("symbol") or item.get("code"))
        name = _text(item.get("name")) or symbol
        normalized, raw_code = _normalize_stock_symbol(symbol)
        target_symbol = normalized or symbol
        if not target_symbol:
            continue
        sources = item.get("sources") if isinstance(item.get("sources"), list) else []
        quote_source = "+".join(_text(source) for source in sources if _text(source))
        asset_class = _text(item.get("asset_class"))
        category = _text(item.get("category"))
        action_line = "全量ETF先看成交额/涨跌幅/资产类别，再送回测验证可重复性。"
        rows.append({
            **item,
            "kind": "stock",
            "label": target_symbol,
            "name": name or target_symbol,
            "symbol": target_symbol,
            "code": target_symbol,
            "raw_code": raw_code or target_symbol.split(".")[-1],
            "macro_group": "all_etfs",
            "macro_group_label": "全量ETF",
            "display_type_label": "全量ETF",
            "lane": "quote_lane",
            "second_screen_role": "all_market_etf_review",
            "latest_price": item.get("price"),
            "day_change_pct": item.get("change_pct"),
            "daily_change_pct": item.get("change_pct"),
            "today_change_pct": item.get("change_pct"),
            "gain_pct": item.get("change_pct"),
            "latest_signal": category or asset_class or "ETF观察",
            "signal_detail": " / ".join(part for part in (asset_class, category) if part),
            "action_status": "观察",
            "trader_action": action_line,
            "rank_reason": f"成交额/涨跌幅排序 · {category or asset_class or 'ETF'}",
            "invalidates_when": "成交额回落或同类ETF涨跌幅不再共振。",
            "range_returns": item.get("range_returns") if isinstance(item.get("range_returns"), dict) else {},
            "range_return_status": _text(item.get("range_return_status")) or "lazy",
            "target_kind": "stock",
            "target_label": target_symbol,
            "target_symbol": target_symbol,
            "target_freq": DEFAULT_TERMINAL_FREQ,
            "quote_source": quote_source or _text(item.get("source")) or "strategy_snapshot.etf_analysis",
            "source": "strategy_snapshot.etf_analysis",
            "source_collection": "strategy_snapshots.etf_analysis",
            "asset_class": asset_class,
            "category": category,
            "amount": item.get("amount"),
            "vol": item.get("vol"),
        })
    return rows


def _latest_etf_review_snapshot_item(symbol: str) -> tuple[dict[str, Any], str]:
    normalized, raw_code = _normalize_stock_symbol(symbol)
    keys = {
        _text(symbol).upper(),
        _text(normalized).upper(),
        _text(raw_code),
    }
    keys = {key for key in keys if key}
    if not keys:
        return {}, ""
    try:
        doc = _mongo_db()["strategy_snapshots"].find_one(
            {"snapshot.etf_analysis.review_universe": {"$exists": True}},
            {
                "_id": 0,
                "updated_at": 1,
                "as_of": 1,
                "snapshot.as_of": 1,
                "snapshot.etf_analysis.universe.as_of": 1,
                "snapshot.etf_analysis.review_universe": 1,
            },
            sort=[("updated_at", -1), ("as_of", -1)],
        ) or {}
    except Exception:
        return {}, ""
    snapshot = doc.get("snapshot") if isinstance(doc.get("snapshot"), dict) else {}
    etf_analysis = snapshot.get("etf_analysis") if isinstance(snapshot.get("etf_analysis"), dict) else {}
    universe = etf_analysis.get("universe") if isinstance(etf_analysis.get("universe"), dict) else {}
    as_of = _date_text(universe.get("as_of") or snapshot.get("as_of") or doc.get("as_of") or doc.get("updated_at") or _market_today("A"))
    for item in etf_analysis.get("review_universe") or []:
        if not isinstance(item, dict):
            continue
        item_symbol = _text(item.get("symbol") or item.get("code"))
        item_normalized, item_raw_code = _normalize_stock_symbol(item_symbol)
        item_keys = {
            _text(item_symbol).upper(),
            _text(item_normalized).upper(),
            _text(item_raw_code),
        }
        if keys.intersection(key for key in item_keys if key):
            return item, as_of
    return {}, ""


def _latest_etf_review_shell_item(symbol: str) -> tuple[dict[str, Any], str]:
    normalized, raw_code = _normalize_stock_symbol(symbol)
    keys = {
        _text(symbol).upper(),
        _text(normalized).upper(),
        _text(raw_code),
    }
    keys = {key for key in keys if key}
    if not keys:
        return {}, ""
    payload = _SHELL_CACHE.get("payload")
    if not isinstance(payload, dict):
        return {}, ""
    groups = payload.get("watchlist_groups") if isinstance(payload.get("watchlist_groups"), dict) else {}
    rows = groups.get("all_etfs") if isinstance(groups.get("all_etfs"), list) else []
    meta = payload.get("watchlist_groups_meta") if isinstance(payload.get("watchlist_groups_meta"), dict) else {}
    etf_meta = meta.get("all_etfs") if isinstance(meta.get("all_etfs"), dict) else {}
    as_of = _date_text(etf_meta.get("as_of") or _market_today("A"))
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_keys: set[str] = set()
        for key in ("symbol", "code", "raw_code", "target_symbol", "target_label", "label"):
            value = _text(row.get(key))
            if not value:
                continue
            row_symbol, row_raw_code = _normalize_stock_symbol(value)
            row_keys.update({
                value.upper(),
                _text(row_symbol).upper(),
                _text(row_raw_code),
            })
        if keys.intersection(key for key in row_keys if key):
            return row, as_of
    return {}, ""


def _etf_spot_snapshot_df(symbol: str) -> tuple[pd.DataFrame, str]:
    item, as_of = _latest_etf_review_snapshot_item(symbol)
    if not item:
        item, as_of = _latest_etf_review_shell_item(symbol)
    if not item:
        return pd.DataFrame(), ""
    close = _first_numeric(item.get("price"), item.get("latest_price"), item.get("close"))
    if close is None or close <= 0:
        return pd.DataFrame(), ""
    change_pct = _first_numeric(
        item.get("change_pct"),
        item.get("day_change_pct"),
        item.get("daily_change_pct"),
        item.get("today_change_pct"),
        item.get("gain_pct"),
    )
    previous_close = close
    if change_pct is not None and change_pct > -99.9:
        previous_close = close / (1 + change_pct / 100.0)
    open_price = _first_numeric(item.get("open"), previous_close, close) or close
    high = _first_numeric(item.get("high"), max(open_price, close)) or max(open_price, close)
    low = _first_numeric(item.get("low"), min(open_price, close)) or min(open_price, close)
    trade_day = _date_text(item.get("trade_date") or item.get("dt") or item.get("date") or as_of or _market_today("A"))
    try:
        index = pd.DatetimeIndex([pd.Timestamp(trade_day)])
    except Exception:
        index = pd.DatetimeIndex([pd.Timestamp(_market_today("A"))])
        trade_day = _market_today("A").isoformat()
    df = pd.DataFrame(
        [{
            "open": open_price,
            "high": max(high, open_price, close),
            "low": min(low, open_price, close),
            "close": close,
            "vol": _float(item.get("vol") or item.get("volume"), 0) or 0,
            "amount": _float(item.get("amount") or item.get("turnover"), 0) or 0,
        }],
        index=index,
    )
    df.attrs.update({
        "collection": "strategy_snapshots.etf_analysis",
        "as_of": trade_day,
        "data_as_of": trade_day,
        "latest_bar_time": pd.Timestamp(trade_day).isoformat(),
        "freshness": "spot",
        "gateway_freshness": "spot",
        "gateway_is_stale": False,
        "time_semantics": "spot_snapshot_daily_close",
        "spot_snapshot_only": True,
    })
    return df, "strategy_snapshots.etf_analysis.spot"


def _is_all_etf_review_row(row: dict[str, Any]) -> bool:
    return (
        _text(row.get("macro_group")) == "all_etfs"
        or _text(row.get("source_collection")) == "strategy_snapshots.etf_analysis"
        or _text(row.get("second_screen_role")) == "all_market_etf_review"
    )


def _split_macro_watchlist_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    major_indices: list[dict[str, Any]] = []
    industry_etfs: list[dict[str, Any]] = []
    for row in rows:
        macro_group = _text(row.get("macro_group"))
        target_kind = _text(row.get("target_kind") or row.get("kind"))
        if macro_group == MACRO_GROUP_INDUSTRY_ETFS or target_kind == "stock":
            industry_etfs.append(row)
        else:
            major_indices.append(row)
    return major_indices, industry_etfs


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

    for item in _chain_rebuild_board_candidates(name, "industry"):
        add(item)
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
    relation_type = _text(item.get("chain_relation_type"))
    if source == "semantic_industry_chain" or rep_type in {"core", "elastic", "upstream", "downstream"} or relation_type in {"upstream", "downstream"}:
        return 90
    if source in {"source_board_constituents", "chain_source_representatives"} or rep_type in {"source_leader", "concept_constituent", "industry_constituent", "new_high_constituent"}:
        return 82
    if source in {"concept_rank", "concept_sina", "concept_em", "concept_ths", "strategy_snapshot"}:
        return 78
    if source in {"industry_leader_map", "industry_candidates"}:
        return 72
    return 58


def _role_score(item: dict[str, Any], leader_rank: int = 0) -> float:
    rep_type = _text(item.get("representative_type"))
    relation_type = _text(item.get("chain_relation_type"))
    source = _text(item.get("source"))
    if relation_type in {"upstream", "downstream"} or rep_type in {"upstream", "downstream"}:
        return 76
    if rep_type == "core":
        return max(82, 102 - leader_rank * 6)
    if source == "industry_leader_map" or rep_type == "industry_leader":
        return 86
    if rep_type == "elastic":
        return 74
    if rep_type == "source_leader":
        return 80
    if rep_type in {"concept_constituent", "industry_constituent", "industry_candidate", "new_high_constituent"}:
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
    allow_kline: bool = True,
) -> tuple[float, Optional[float], str]:
    explicit = _float(item.get("score") or item.get("total_score") or item.get("fused_total"))
    if explicit is not None:
        return _score_0_100(explicit, 50), _float(item.get("day_change_pct")), _text(item.get("latest_signal"))
    day_change = _float(item.get("day_change_pct") or item.get("daily_change_pct") or item.get("change_pct"))
    latest_signal = _text(item.get("latest_signal") or item.get("signal") or item.get("reason"))
    if day_change is None and symbol and allow_kline:
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
    relation_type = _text(item.get("chain_relation_type"))
    source = _text(item.get("source"))
    if relation_type == "upstream" or rep_type == "upstream":
        return "上游"
    if relation_type == "downstream" or rep_type == "downstream":
        return "下游"
    if rep_type == "core":
        return ["龙头", "龙二", "龙三"][min(max(leader_rank - 1, 0), 2)]
    if source == "industry_leader_map" or rep_type == "industry_leader":
        return "行业龙头"
    if rep_type == "elastic":
        return "弹性"
    if rep_type == "source_leader":
        return "当日领涨"
    if rep_type == "new_high_constituent":
        return "新高"
    if rep_type in {"concept_constituent", "industry_constituent", "industry_candidate"}:
        return "成分候选"
    return "观察"


def _scored_candidate_payload(
    item: dict[str, Any],
    *,
    heat_score: float,
    leader_rank: int = 0,
    daily_cache: Optional[dict[str, tuple[pd.DataFrame, str]]] = None,
    lightweight: bool = False,
) -> Optional[dict[str, Any]]:
    symbol, raw_code = _candidate_symbol_fields(item)
    if not symbol:
        return None
    trend_score, day_change, latest_signal = _trend_score_for_candidate(
        item,
        symbol,
        daily_cache=daily_cache,
        allow_kline=not lightweight,
    )
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
    if not lightweight and not _text(item.get("bar_source")):
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
    relation_type = _text(item.get("chain_relation_type"))
    source = _text(item.get("source"))
    source_rank = {
        "core": 100,
        "upstream": 90,
        "downstream": 86,
        "industry_leader": 92,
        "source_leader": 88,
        "elastic": 82,
        "semantic_industry_chain": 78,
        "industry_candidate": 46,
        "industry_constituent": 42,
        "concept_constituent": 42,
    }.get(rep_type, 0)
    if relation_type == "upstream":
        source_rank = max(source_rank, 90)
    elif relation_type == "downstream":
        source_rank = max(source_rank, 86)
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
        relation_type = _text(item.get("chain_relation_type"))
        key = (_text(symbol).upper() or f"{_text(item.get('name'))}|{_text(item.get('source'))}|{index}")
        if relation_type in {"upstream", "downstream"}:
            key = f"{key}:{relation_type}"
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
        "upstream": [],
        "leaders": [],
        "weighted": [],
        "elastic": [],
        "downstream": [],
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
        relation_type = _text(item.get("chain_relation_type"))
        if relation_type == "upstream" or rep_type == "upstream":
            groups["upstream"].append(payload)
        elif relation_type == "downstream" or rep_type == "downstream":
            groups["downstream"].append(payload)
        elif rep_type == "core":
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
        if key == "leaders":
            tier_order = {"龙头": 0, "龙二": 1, "龙三": 2}
            rows.sort(key=lambda item: (
                tier_order.get(_text(item.get("leader_tier")), 9),
                -(_float(item.get("attention_score"), 0) or 0),
            ))
        else:
            rows.sort(key=lambda item: _float(item.get("attention_score"), 0) or 0, reverse=True)
        groups[key] = rows[:8 if key != "leaders" else 3]
    return groups


def _flatten_candidate_groups(groups: dict[str, list[dict[str, Any]]], limit: int = 20) -> list[dict[str, Any]]:
    ordered_keys = ["leaders", "source_leaders", "upstream", "weighted", "constituents", "elastic", "downstream"]
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


def _candidate_group_symbol_keys(item: dict[str, Any]) -> tuple[str, str]:
    for key in ("symbol", "code", "raw_code"):
        symbol, raw_code = _normalize_stock_symbol(_text(item.get(key)))
        if symbol or raw_code:
            return _text(symbol).upper(), _text(raw_code)
    return "", ""


def _latest_candidate_new_high_signals(db: Any, groups: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    raw_codes: set[str] = set()
    symbols: set[str] = set()
    for rows in groups.values():
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            symbol, raw_code = _candidate_group_symbol_keys(item)
            if symbol:
                symbols.add(symbol)
            if raw_code:
                raw_codes.add(raw_code)
    if not raw_codes and not symbols:
        return {}
    try:
        expected_day = _day_change_expected_day()
        window_start = datetime.fromisoformat(expected_day) - timedelta(days=35)
    except Exception:
        window_start = datetime.utcnow() - timedelta(days=35)
    query_or = []
    if raw_codes:
        query_or.extend([
            {"raw_code": {"$in": sorted(raw_codes)}},
            {"code": {"$in": sorted(raw_codes)}},
        ])
    if symbols:
        query_or.append({"symbol": {"$in": sorted(symbols)}})
    if not query_or:
        return {}
    try:
        cursor = db["terminal_technical_signals"].find(
            {
                "$and": [
                    {"$or": query_or},
                    {
                        "$or": [
                            {"signal_type": {"$regex": "200日新高|新高突破"}},
                            {"evidence.entry_factor.group": "200d_new_high_breakout"},
                            {"entry_factor.group": "200d_new_high_breakout"},
                        ],
                    },
                    {"signal_side": {"$in": ["buy", "left", "right", ""]}},
                    {
                        "$or": [
                            {"dt": {"$gte": window_start}},
                            {"signal_date": {"$gte": window_start}},
                            {"event_dt": {"$gte": window_start}},
                        ],
                    },
                ]
            },
            {
                "_id": 0,
                "symbol": 1,
                "raw_code": 1,
                "code": 1,
                "signal_type": 1,
                "freq": 1,
                "score": 1,
                "confidence": 1,
                "dt": 1,
                "signal_date": 1,
                "event_dt": 1,
                "evidence.entry_factor": 1,
                "entry_factor": 1,
            },
        ).sort([("dt", -1), ("signal_date", -1), ("event_dt", -1)]).limit(max(24, len(raw_codes) * 4))
        docs = list(cursor)
    except Exception:
        return {}
    lookup: dict[str, dict[str, Any]] = {}
    for doc in docs:
        symbol, raw_code = _candidate_group_symbol_keys(doc)
        entry_factor = doc.get("entry_factor") if isinstance(doc.get("entry_factor"), dict) else {}
        evidence = doc.get("evidence") if isinstance(doc.get("evidence"), dict) else {}
        if not entry_factor and isinstance(evidence.get("entry_factor"), dict):
            entry_factor = evidence.get("entry_factor") or {}
        raw_signal = _text(doc.get("signal_type"))
        signal_label = _shell_stock_breakout_summary(entry_factor) or (
            "200日新高" if any(token in raw_signal for token in ("200日新高", "新高突破")) else raw_signal
        ) or "200日新高"
        payload = {
            "new_high_signal": "200日新高",
            "new_high_label": signal_label,
            "new_high_freq": doc.get("freq"),
            "new_high_as_of": _date_text(doc.get("dt") or doc.get("signal_date") or doc.get("event_dt")),
            "new_high_score": _float(doc.get("score")),
            "new_high_confidence": _float(doc.get("confidence")),
        }
        for key in (symbol, raw_code):
            if key and key not in lookup:
                lookup[key] = payload
    return lookup


def _apply_candidate_new_high_signals(groups: dict[str, list[dict[str, Any]]], signals: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    if not signals:
        return {
            key: [row for row in rows if not row.get("new_high_candidate_probe")]
            for key, rows in groups.items()
        }
    enriched: dict[str, list[dict[str, Any]]] = {}
    for key, rows in groups.items():
        updated_rows: list[dict[str, Any]] = []
        for row in rows:
            symbol, raw_code = _candidate_group_symbol_keys(row)
            signal = signals.get(symbol) or signals.get(raw_code)
            if not signal:
                if row.get("new_high_candidate_probe"):
                    continue
                updated_rows.append(row)
                continue
            updated = dict(row)
            badge = _text(signal.get("new_high_label") or signal.get("new_high_signal")) or "200日新高"
            current_signal = _text(updated.get("latest_signal"))
            if not current_signal or current_signal == "待观察":
                updated["latest_signal"] = badge
            elif badge not in current_signal:
                updated["latest_signal"] = f"{badge} / {current_signal}"
            signal_badges = [item for item in updated.get("signal_badges") or [] if _text(item)]
            if badge not in signal_badges:
                signal_badges.insert(0, badge)
            updated["signal_badges"] = signal_badges[:4]
            updated["new_high_signal"] = signal.get("new_high_signal")
            updated["new_high_label"] = badge
            updated["new_high_freq"] = signal.get("new_high_freq")
            updated["new_high_as_of"] = signal.get("new_high_as_of")
            updated["attention_score"] = round((_float(updated.get("attention_score"), 0) or 0) + 8.0, 2)
            why_watch = _text(updated.get("why_watch"))
            if badge not in why_watch:
                updated["why_watch"] = " · ".join(part for part in [why_watch, badge] if part)
            updated_rows.append(updated)
        if key == "leaders":
            tier_order = {"龙头": 0, "龙二": 1, "龙三": 2}
            updated_rows.sort(key=lambda item: (
                tier_order.get(_text(item.get("leader_tier")), 9),
                -(_float(item.get("attention_score"), 0) or 0),
            ))
        else:
            updated_rows.sort(key=lambda item: (
                1 if item.get("new_high_signal") else 0,
                _float(item.get("day_change_pct"), 0) or 0,
                _float(item.get("attention_score"), 0) or 0,
            ), reverse=True)
        enriched[key] = updated_rows
    return enriched


def _new_high_probe_rows_for_chain_sources(db: Any, doc: dict[str, Any], *, limit: int = 80) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    source_driver = doc.get("source_driver") if isinstance(doc.get("source_driver"), dict) else {}
    if source_driver:
        sources.append(source_driver)
    for domain in doc.get("integrated_domains") or []:
        if isinstance(domain, dict):
            sources.append(domain)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources[:5]:
        source_name = _text(source.get("name"))
        if not source_name:
            continue
        try:
            symbols, stock_names = _constituent_symbols_for_source(db, source)
        except Exception:
            continue
        for raw in symbols:
            symbol, raw_code = _normalize_stock_symbol(_text(raw))
            if not symbol or not raw_code or raw_code in seen:
                continue
            seen.add(raw_code)
            rows.append({
                "symbol": symbol,
                "raw_code": raw_code,
                "code": symbol,
                "name": stock_names.get(raw_code) or stock_names.get(symbol) or _stock_name(symbol) or raw_code,
                "leader_tier": "新高",
                "chain_role": f"{source_name}成分",
                "representative_type": "new_high_constituent",
                "source": "terminal_technical_signals",
                "source_note": f"{source_name}新高成分",
                "latest_signal": "待观察",
                "why_watch": f"{source_name} · 新高待确认",
                "attention_score": 50.0,
                "new_high_candidate_probe": True,
            })
            if len(rows) >= limit:
                return rows
    return rows


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
        carrier_range_returns = _compute_range_returns(
            carrier_df,
            _watchlist_range_columns(),
            adjust_price_discontinuities=True,
        )
        carrier_latest_price = (
            float(carrier_df["close"].iloc[-1])
            if carrier_df is not None and not carrier_df.empty and "close" in carrier_df.columns
            else None
        )
        carrier_day_change = _compute_day_change_pct(carrier_df)
    minute_change = _shortest_realtime_day_change(kind, heat_target_label)
    board_day_change, board_day_as_of = _latest_board_heat_day_change(kind, heat_target_label)
    if minute_change:
        board_day_change = minute_change.get("day_change_pct")
        board_day_as_of = _text(minute_change.get("day_change_as_of"))
    board_day_change_source = _text(minute_change.get("day_change_source")) if minute_change else ("board_heat_ticks" if board_day_change is not None else "")
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
        "today_change_pct": minute_change.get("today_change_pct") if minute_change else None,
        "gain_pct": minute_change.get("gain_pct") if minute_change else enriched.get("gain_pct"),
        "day_change_source": board_day_change_source,
        "day_change_mode": minute_change.get("day_change_mode") if minute_change else _a_day_change_mode(),
        "day_change_as_of": board_day_as_of,
        "day_change_freq": minute_change.get("day_change_freq") if minute_change else "",
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
        "upstream": [],
        "leaders": [],
        "weighted": [],
        "elastic": [],
        "downstream": [],
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


_CHAIN_REPRESENTATIVE_TYPE_RANK = {
    "source_leader": 90,
    "new_high_constituent": 84,
    "concept_constituent": 80,
    "industry_constituent": 78,
    "industry_candidate": 72,
    "core": 70,
    "industry_leader": 68,
    "upstream": 64,
    "downstream": 62,
    "elastic": 58,
}


def _chain_representative_market_rank(rep: dict[str, Any]) -> tuple[float, float, float, float]:
    rep_type = _text(rep.get("representative_type"))
    return (
        _CHAIN_REPRESENTATIVE_TYPE_RANK.get(rep_type, 50),
        _float(rep.get("day_change_pct"), -999) or -999,
        _float(rep.get("priority"), 0) or 0,
        _float(rep.get("amount"), 0) or 0,
    )


def _chain_candidate_representatives(row: dict[str, Any]) -> list[dict[str, Any]]:
    representatives: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []

    def add(rep: dict[str, Any], domain: Optional[dict[str, Any]] = None, *, source_rep: bool = False) -> None:
        if not isinstance(rep, dict):
            return
        symbol = _text(rep.get("symbol") or rep.get("code") or rep.get("raw_code")).upper()
        name = _text(rep.get("name"))
        if not symbol and not name:
            return
        rep_type = _text(rep.get("representative_type")) or ("industry_constituent" if source_rep else "elastic")
        source_board_name = _text(rep.get("source_board_name"))
        source_board_kind = _text(rep.get("source_board_kind"))
        if domain:
            source_board_name = source_board_name or _text(domain.get("name"))
            source_board_kind = source_board_kind or _text(domain.get("kind"))
        item = {
            **rep,
            "representative_type": rep_type,
            "source": rep.get("source") or ("chain_source_representatives" if source_rep else "chain_heat_snapshots"),
            "source_board_name": source_board_name or rep.get("source_board_name"),
            "source_board_kind": source_board_kind or rep.get("source_board_kind"),
        }
        if source_rep and source_board_name and not _text(item.get("source_note")):
            item["source_note"] = f"{source_board_name}真实涨幅成分"
        if source_board_name and not _text(item.get("relation")):
            item["relation"] = f"{source_board_name}真实涨幅成分" if source_rep else source_board_name
        key = (symbol or name, rep_type)
        if key not in representatives:
            representatives[key] = item
            order.append(key)
            return
        if _chain_representative_market_rank(item) > _chain_representative_market_rank(representatives[key]):
            representatives[key] = item

    for rep in row.get("representatives") or []:
        add(rep)
    for domain in row.get("integrated_domains") or []:
        if not isinstance(domain, dict):
            continue
        for rep in domain.get("source_representatives") or []:
            add(rep, domain, source_rep=True)
        for rep in domain.get("representatives") or []:
            add(rep, domain)
    return [representatives[key] for key in order]


def _candidate_groups_from_representatives(row: dict[str, Any], *, lightweight: bool = False) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {
        "upstream": [],
        "leaders": [],
        "weighted": [],
        "elastic": [],
        "downstream": [],
        "source_leaders": [],
        "constituents": [],
    }
    heat_score = _float(row.get("heat_score"), 0) or 0
    daily_cache: dict[str, tuple[pd.DataFrame, str]] = {}
    core_rank = 0
    for rep in _chain_candidate_representatives(row):
        if not isinstance(rep, dict):
            continue
        representative_type = _text(rep.get("representative_type"))
        leader_rank = 0
        if representative_type == "core":
            core_rank += 1
            leader_rank = core_rank
        item = {
            **rep,
            "symbol": rep.get("symbol"),
            "name": rep.get("name"),
            "relation": rep.get("relation"),
            "source": rep.get("source") or "chain_heat_snapshots",
            "representative_type": representative_type,
            "attention_score": heat_score + _float(rep.get("priority"), 0) * 0.1,
            "chain_id": row.get("chain_id"),
            "chain_name": row.get("chain_name"),
            "node_id": row.get("node_id"),
            "node_name": row.get("node_name"),
            "layer": row.get("layer"),
            "stage": row.get("stage"),
        }
        payload = _scored_candidate_payload(
            item,
            heat_score=heat_score,
            leader_rank=leader_rank,
            daily_cache=daily_cache,
            lightweight=lightweight,
        ) or item
        if lightweight:
            payload = _enrich_stock_row(payload, [], lightweight=True)
        if representative_type == "upstream":
            groups["upstream"].append(payload)
        elif representative_type == "downstream":
            groups["downstream"].append(payload)
        elif representative_type == "core":
            groups["leaders"].append(payload)
            groups["weighted"].append(payload)
        elif representative_type == "source_leader":
            groups["source_leaders"].append(payload)
        elif representative_type in {"concept_constituent", "industry_constituent", "industry_candidate", "new_high_constituent"}:
            groups["constituents"].append(payload)
        else:
            groups["elastic"].append(payload)
    for key, rows in groups.items():
        if key == "leaders":
            tier_order = {"龙头": 0, "龙二": 1, "龙三": 2}
            rows.sort(key=lambda item: (
                tier_order.get(_text(item.get("leader_tier")), 9),
                -(_float(item.get("attention_score"), 0) or 0),
            ))
        elif key == "source_leaders":
            rows.sort(key=lambda item: (
                _float(item.get("day_change_pct"), 0) or 0,
                _float(item.get("attention_score"), 0) or 0,
            ), reverse=True)
        elif key == "constituents":
            rows.sort(key=lambda item: (
                _float(item.get("day_change_pct"), 0) or 0,
                _float(item.get("attention_score"), 0) or 0,
            ), reverse=True)
        else:
            rows.sort(key=lambda item: _float(item.get("attention_score"), 0) or 0, reverse=True)
        limit_by_key = {
            "leaders": 6,
            "source_leaders": 12,
            "constituents": 12,
            "weighted": 8,
            "upstream": 8,
            "elastic": 8,
            "downstream": 8,
        }
        groups[key] = rows[:limit_by_key.get(key, 8)]
    return groups


def _chain_heat_max_nodes_per_chain() -> int:
    try:
        return max(1, int(os.getenv("WORKBENCH_CHAIN_HEAT_MAX_NODES_PER_CHAIN", "2")))
    except Exception:
        return 2


def _chain_heat_shell_graph_enabled() -> bool:
    return _text(os.getenv("WORKBENCH_CHAIN_HEAT_SHELL_GRAPH")).lower() in {"1", "true", "yes", "on"}


def _chain_heat_shell_range_returns_enabled() -> bool:
    value = _text(os.getenv("WORKBENCH_CHAIN_HEAT_SHELL_RANGE_RETURNS")).lower()
    if value in {"0", "false", "no", "off"}:
        return False
    return True


def _chain_heat_shell_theme_rows_enabled() -> bool:
    return _text(os.getenv("WORKBENCH_CHAIN_HEAT_SHELL_THEME_ROWS")).lower() in {"1", "true", "yes", "on"}


def _chain_representative_quote_rows(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: dict[str, Any]) -> None:
        symbol = _text(item.get("symbol") or item.get("code") or item.get("raw_code"))
        if not symbol or symbol.upper() in seen:
            return
        seen.add(symbol.upper())
        rows.append(item)

    for doc in docs:
        leader_symbol = _text(doc.get("leader_symbol"))
        if leader_symbol:
            add({"symbol": leader_symbol, "name": doc.get("leader_name")})
        for rep in doc.get("representatives") or []:
            if isinstance(rep, dict):
                add(rep)
        for domain in doc.get("integrated_domains") or []:
            if not isinstance(domain, dict):
                continue
            leader_symbol = _text(domain.get("leader_symbol"))
            if leader_symbol:
                add({"symbol": leader_symbol, "name": domain.get("leader_name")})
            for rep in domain.get("source_representatives") or []:
                if isinstance(rep, dict):
                    add(rep)
            for rep in domain.get("representatives") or []:
                if isinstance(rep, dict):
                    add(rep)
    return rows


def _diversify_chain_heat_docs(
    docs: list[dict[str, Any]],
    *,
    limit: int,
    max_nodes_per_chain: int = 2,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    overflow: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    chain_counts: dict[str, int] = {}
    for doc in docs:
        chain_id = _text(doc.get("chain_id"))
        node_id = _text(doc.get("node_id")) or "default"
        key = (chain_id, node_id)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        chain_key = chain_id or node_id
        if chain_counts.get(chain_key, 0) < max_nodes_per_chain:
            selected.append(doc)
            chain_counts[chain_key] = chain_counts.get(chain_key, 0) + 1
        else:
            overflow.append(doc)
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        selected.extend(overflow[: limit - len(selected)])
    return selected[:limit]


def _chain_domain_payload(domain: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "kind",
        "name",
        "code",
        "change_pct",
        "up_count",
        "down_count",
        "leader_name",
        "leader_symbol",
        "leader_change_pct",
        "rank",
        "mapping_confidence",
        "hit_terms",
        "evidence_sources",
    )
    return {key: domain.get(key) for key in keep if domain.get(key) not in (None, "", [], {})}


_CHAIN_SOURCE_KIND_LABELS = {
    "industry": "行业",
    "concept": "概念",
    "theme": "主题",
}

_CHAIN_HEAT_SHELL_PROJECTION = {
    "_id": 0,
    "market": 1,
    "trade_date": 1,
    "dt": 1,
    "trade_minute": 1,
    "rank": 1,
    "chain_id": 1,
    "chain_name": 1,
    "node_id": 1,
    "node_name": 1,
    "layer": 1,
    "stage": 1,
    "phase": 1,
    "heat_score": 1,
    "change_pct": 1,
    "momentum_5m": 1,
    "momentum_15m": 1,
    "momentum_30m": 1,
    "range_pattern": 1,
    "integrated_count": 1,
    "integrated_domains": 1,
    "representatives": 1,
    "source_driver": 1,
    "source_events": 1,
    "source_concept_overlays": 1,
    "source_event_concept_overlays": 1,
    "source_theme_overlays": 1,
    "source_event_theme_overlays": 1,
    "market_logic": 1,
    "market_logic_node": 1,
    "source_kind_mix": 1,
    "route_explain": 1,
    "mapping_status": 1,
    "mapping_confidence": 1,
    "evidence_sources": 1,
    "trading_signal": 1,
    "trader_action": 1,
    "invalidates_when": 1,
    "up_count": 1,
    "down_count": 1,
}


def _chain_source_kind_label(kind: Any) -> str:
    text = _text(kind)
    return _CHAIN_SOURCE_KIND_LABELS.get(text, text or "来源")


def _chain_source_driver_payload(primary: dict[str, Any], fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    source = dict(fallback or {})
    for key in (
        "kind",
        "name",
        "code",
        "change_pct",
        "up_count",
        "down_count",
        "leader_name",
        "leader_symbol",
        "leader_change_pct",
        "rank",
        "mapping_confidence",
        "hit_terms",
        "evidence_sources",
    ):
        if primary.get(key) not in (None, "", [], {}):
            source[key] = primary.get(key)
    kind_label = _chain_source_kind_label(source.get("kind"))
    source["kind_label"] = kind_label
    if source.get("name"):
        source["label"] = f"{kind_label}:{source.get('name')}"
    return {key: value for key, value in source.items() if value not in (None, "", [], {})}


def _constituent_symbols_for_source(db: Any, source: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    name = _text(source.get("name"))
    if not name:
        return [], {}
    collection_name = "concept_constituents" if _text(source.get("kind")) == "concept" else "board_constituents"
    doc = db[collection_name].find_one(
        {"$or": [{"board_name": name}, {"concept_name": name}, {"name": name}]},
        {"_id": 0, "symbols": 1, "stock_names": 1},
        sort=[("updated_at", -1)],
    ) or {}
    symbols: list[str] = []
    stock_names: dict[str, str] = {}
    for raw_symbol in doc.get("symbols") or []:
        normalized, raw_code = _normalize_stock_symbol(_text(raw_symbol))
        code = raw_code or _text(raw_symbol)
        if code and code not in symbols:
            symbols.append(code)
        if normalized:
            stock_names[normalized] = _text((doc.get("stock_names") or {}).get(code))
        if code:
            stock_names[code] = _text((doc.get("stock_names") or {}).get(code))
    return symbols, {key: value for key, value in stock_names.items() if value}


def _latest_concept_heat(db: Any, name: str) -> dict[str, Any]:
    expected_day = _day_change_expected_day()
    query = {"kind": "concept", "name": name}
    if expected_day:
        day_start = datetime.fromisoformat(expected_day)
        day_end = day_start + timedelta(days=1)
        query = {
            **query,
            "$or": [
                {"trade_date": expected_day},
                {"dt": {"$gte": day_start, "$lt": day_end}},
                {"trade_minute": {"$gte": day_start, "$lt": day_end}},
            ],
        }
    return db["board_heat_ticks"].find_one(
        query,
        {
            "_id": 0,
            "kind": 1,
            "name": 1,
            "code": 1,
            "change_pct": 1,
            "up_count": 1,
            "down_count": 1,
            "leader_name": 1,
            "leader_change_pct": 1,
            "rank_idx": 1,
            "trade_date": 1,
            "trade_minute": 1,
        },
        sort=[("trade_minute", -1)],
    ) or {}


def _source_market_overlays(
    db: Any,
    source_events: list[dict[str, Any]],
    *,
    limit: int = 8,
    only_non_chain: bool = False,
) -> list[dict[str, Any]]:
    overlays_by_name: dict[str, dict[str, Any]] = {}
    for source_order, source in enumerate(source_events[:5]):
        source_name = _text(source.get("name"))
        symbols, stock_names = _constituent_symbols_for_source(db, source)
        if not source_name or not symbols:
            continue
        cursor = db["concept_constituents"].find(
            {"symbols": {"$in": symbols}},
            {"_id": 0, "concept_name": 1, "board_name": 1, "symbols": 1, "stock_names": 1},
        )
        for doc in cursor:
            concept_name = _text(doc.get("concept_name") or doc.get("board_name") or doc.get("name"))
            non_chain = non_chain_reason(concept_name)
            if not concept_name or concept_name == source_name:
                continue
            if only_non_chain:
                if not non_chain:
                    continue
            elif non_chain:
                continue
            heat = _latest_concept_heat(db, concept_name)
            change_pct = _float(heat.get("change_pct"))
            if change_pct is None or change_pct <= 0:
                continue
            matched_codes = [code for code in symbols if code in (doc.get("symbols") or [])]
            if not matched_codes:
                continue
            matched_names = [
                _text((doc.get("stock_names") or {}).get(code))
                or stock_names.get(code)
                or stock_names.get((_normalize_stock_symbol(code)[0] or ""))
                or code
                for code in matched_codes
            ]
            current = overlays_by_name.get(concept_name) or {
                "kind": "theme" if non_chain else "concept",
                "kind_label": "主题" if non_chain else "概念",
                "name": concept_name,
                "change_pct": change_pct,
                "leader_name": _text(heat.get("leader_name")),
                "leader_change_pct": _float(heat.get("leader_change_pct")),
                "up_count": _float(heat.get("up_count"), 0),
                "down_count": _float(heat.get("down_count"), 0),
                "rank": _float(heat.get("rank_idx")),
                "non_chain_reason": non_chain,
                "matched_symbols": [],
                "matched_names": [],
                "source_boards": [],
                "source_order": source_order,
                "primary_source": source_order == 0,
            }
            current["source_order"] = min(int(current.get("source_order") or source_order), source_order)
            current["primary_source"] = bool(current.get("primary_source")) or source_order == 0
            current["change_pct"] = max(_float(current.get("change_pct"), 0) or 0, change_pct)
            current["source_boards"] = list(dict.fromkeys([*current.get("source_boards", []), source_name]))
            current["matched_symbols"] = list(dict.fromkeys([*current.get("matched_symbols", []), *matched_codes]))[:8]
            current["matched_names"] = list(dict.fromkeys([*current.get("matched_names", []), *matched_names]))[:8]
            current["matched_count"] = len(current["matched_symbols"])
            overlays_by_name[concept_name] = current
    overlays = list(overlays_by_name.values())
    overlays.sort(
        key=lambda item: (
            1 if item.get("primary_source") else 0,
            _standalone_theme_rank_bonus(_text(item.get("name")), _text(item.get("non_chain_reason"))),
            _float(item.get("change_pct"), 0) or 0,
            _float(item.get("leader_change_pct"), 0) or 0,
            _float(item.get("matched_count"), 0) or 0,
        ),
        reverse=True,
    )
    return overlays[:limit]


def _source_concept_overlays(db: Any, source_events: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    return _source_market_overlays(db, source_events, limit=limit, only_non_chain=False)


def _source_theme_overlays(db: Any, source_events: list[dict[str, Any]], *, limit: int = 6) -> list[dict[str, Any]]:
    return _source_market_overlays(db, source_events, limit=limit, only_non_chain=True)


def _source_event_concept_overlays(db: Any, source_events: list[dict[str, Any]], *, concepts_per_source: int = 3) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for source in source_events[:6]:
        source_payload = _chain_source_driver_payload(source)
        concepts = _source_concept_overlays(db, [source_payload], limit=concepts_per_source)
        if not concepts:
            continue
        groups.append({
            "source": source_payload,
            "concepts": concepts,
        })
    return groups


def _source_event_theme_overlays(db: Any, source_events: list[dict[str, Any]], *, themes_per_source: int = 2) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for source in source_events[:6]:
        source_payload = _chain_source_driver_payload(source)
        themes = _source_theme_overlays(db, [source_payload], limit=themes_per_source)
        if not themes:
            continue
        groups.append({
            "source": source_payload,
            "themes": themes,
        })
    return groups


def _standalone_theme_rank_bonus(name: str, reason: str) -> float:
    text = _text(name)
    reason_text = _text(reason)
    if "次新" in text:
        return 1.0
    if "破发" in text or "上市时间" in reason_text:
        return 1.0
    return 0.0


def _format_signed_pct(value: Any) -> str:
    numeric = _float(value)
    return f"{numeric:+.2f}%" if numeric is not None else ""


def _late_session_momentum_label(doc: dict[str, Any]) -> str:
    m5 = _float(doc.get("momentum_5m"))
    m15 = _float(doc.get("momentum_15m"))
    m30 = _float(doc.get("momentum_30m"))
    if not ((m30 is not None and m30 >= 2.0) or (m15 is not None and m15 >= 1.2)):
        return ""
    if m5 is not None and m5 < -1.0 and (m15 is None or m15 < 1.8):
        return ""
    parts = []
    if m30 is not None:
        parts.append(f"30m {_format_signed_pct(m30)}")
    if m15 is not None:
        parts.append(f"15m {_format_signed_pct(m15)}")
    if m5 is not None:
        parts.append(f"5m {_format_signed_pct(m5)}")
    return "尾盘拉升" + (f" {' / '.join(parts)}" if parts else "")


def _chain_reference_domain(doc: dict[str, Any], domains: list[dict[str, Any]]) -> dict[str, Any]:
    if not domains:
        return {}
    chain_label = _text(doc.get("chain_name")).replace("产业链", "")
    node_terms = [
        item.strip()
        for item in _text(doc.get("node_name")).replace("链", "").replace("产业", "").split("/")
        if item.strip()
    ]
    candidates = [chain_label, *node_terms]
    for term in candidates:
        if not term:
            continue
        for domain in domains:
            name = _text(domain.get("name"))
            if _text(domain.get("kind")) == "industry" and name and (name == term or name in term or term in name):
                return domain
    for domain in domains:
        if _text(domain.get("kind")) == "industry":
            return domain
    return domains[0]


def _chain_domain_change_stats(domains: list[dict[str, Any]]) -> dict[str, Any]:
    values = [_float(domain.get("change_pct")) for domain in domains]
    numeric = [value for value in values if value is not None]
    if not numeric:
        return {"count": len(domains), "known_count": 0}
    return {
        "count": len(domains),
        "known_count": len(numeric),
        "positive_count": sum(1 for value in numeric if value > 0),
        "negative_count": sum(1 for value in numeric if value < 0),
        "avg_change_pct": round(sum(numeric) / len(numeric), 2),
        "max_change_pct": round(max(numeric), 2),
        "min_change_pct": round(min(numeric), 2),
    }


def _representative_confirmation(groups: dict[str, list[dict[str, Any]]], chain_change_pct: Optional[float]) -> dict[str, Any]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for key in ("leaders", "weighted", "elastic", "source_leaders", "constituents"):
        for row in groups.get(key) or []:
            if isinstance(row, dict):
                rows.append((key, row))
    seen: set[str] = set()
    changes: list[float] = []
    role_changes: dict[str, list[float]] = {}
    missing = 0
    for role, row in rows:
        symbol = _text(row.get("symbol") or row.get("code")).upper()
        key = symbol or _text(row.get("name"))
        if key in seen:
            continue
        seen.add(key)
        value = _first_numeric(row.get("day_change_pct"), row.get("daily_change_pct"), row.get("today_change_pct"))
        if value is None:
            missing += 1
        else:
            changes.append(value)
            role_changes.setdefault(role, []).append(value)
    positive = sum(1 for value in changes if value > 0)
    negative = sum(1 for value in changes if value < 0)
    avg_change_pct = round(sum(changes) / len(changes), 2) if changes else None
    role_stats = {
        role: {
            "known_count": len(values),
            "positive_count": sum(1 for value in values if value > 0),
            "negative_count": sum(1 for value in values if value < 0),
            "avg_change_pct": round(sum(values) / len(values), 2) if values else None,
            "max_change_pct": round(max(values), 2) if values else None,
            "min_change_pct": round(min(values), 2) if values else None,
        }
        for role, values in role_changes.items()
    }
    if not changes:
        status = "unknown"
        label = "代表股涨幅缺失"
    elif chain_change_pct is not None and chain_change_pct > 0 and positive == 0:
        status = "not_confirmed"
        label = "代表股未跟随"
    elif (
        chain_change_pct is not None
        and chain_change_pct >= 2.0
        and avg_change_pct is not None
        and avg_change_pct < max(0.8, chain_change_pct * 0.35)
    ):
        status = "source_only"
        label = "源强链弱"
    elif positive and negative:
        status = "mixed"
        label = "代表股分化"
    elif positive:
        status = "confirmed"
        label = "代表股跟随"
    else:
        status = "weak"
        label = "代表股偏弱"
    return {
        "status": status,
        "label": label,
        "known_count": len(changes),
        "missing_count": missing,
        "positive_count": positive,
        "negative_count": negative,
        "avg_change_pct": avg_change_pct,
        "role_stats": role_stats,
    }


def _chain_confirmation_payload(rep_confirmation: dict[str, Any]) -> dict[str, Any]:
    status = _text(rep_confirmation.get("status"))
    mapping = {
        "confirmed": ("confirmed", "产业链确认"),
        "mixed": ("mixed", "链内分化"),
        "source_only": ("source_only", "源强链弱"),
        "not_confirmed": ("not_confirmed", "链主未确认"),
        "weak": ("weak", "链主偏弱"),
        "unknown": ("unknown", "待确认"),
    }
    normalized, label = mapping.get(status, (status or "unknown", _text(rep_confirmation.get("label")) or "待确认"))
    role_stats = rep_confirmation.get("role_stats") if isinstance(rep_confirmation.get("role_stats"), dict) else {}
    leader_stats = role_stats.get("leaders") if isinstance(role_stats.get("leaders"), dict) else {}
    weighted_stats = role_stats.get("weighted") if isinstance(role_stats.get("weighted"), dict) else {}
    elastic_stats = role_stats.get("elastic") if isinstance(role_stats.get("elastic"), dict) else {}
    core_values = [
        _float(leader_stats.get("avg_change_pct")),
        _float(weighted_stats.get("avg_change_pct")),
    ]
    core_values = [value for value in core_values if value is not None]
    core_avg = round(sum(core_values) / len(core_values), 2) if core_values else None
    elastic_avg = _float(elastic_stats.get("avg_change_pct"))
    elastic_max = _float(elastic_stats.get("max_change_pct"))
    if (
        status == "mixed"
        and elastic_avg is not None
        and elastic_avg >= 2.0
        and (elastic_max is not None and elastic_max >= 5.0)
        and (core_avg is None or core_avg < 1.0)
    ):
        normalized = "elastic_rebound"
        label = "弹性补涨"
    return {
        "status": normalized,
        "label": label,
        "representative_status": status,
        "representative_label": _text(rep_confirmation.get("label")),
        "representative_avg_change_pct": rep_confirmation.get("avg_change_pct"),
        "core_avg_change_pct": core_avg,
        "elastic_avg_change_pct": elastic_avg,
        "elastic_max_change_pct": elastic_max,
        "positive_count": rep_confirmation.get("positive_count"),
        "negative_count": rep_confirmation.get("negative_count"),
        "known_count": rep_confirmation.get("known_count"),
        "role_stats": role_stats,
    }


def _chain_heat_display_rank_score(doc: dict[str, Any], context: dict[str, Any]) -> float:
    score = _float(doc.get("heat_score"), 0) or 0.0
    confirmation_status = _text((context.get("chain_confirmation") or {}).get("status"))
    if confirmation_status == "confirmed":
        score += 8.0
    elif confirmation_status in {"mixed", "elastic_rebound"}:
        score -= 4.0
    elif confirmation_status == "source_only":
        score -= 14.0
    elif confirmation_status in {"not_confirmed", "weak"}:
        score -= 18.0
    flags = set(context.get("mismatch_flags") or [])
    if "low_mapping_confidence" in flags:
        score -= 6.0
    if "driver_not_same_as_chain_label" in flags:
        score -= 3.0
    return round(score, 3)


def _chain_heat_trader_action(context: dict[str, Any]) -> str:
    confirmation = context.get("chain_confirmation") if isinstance(context.get("chain_confirmation"), dict) else {}
    source_driver = context.get("source_driver") if isinstance(context.get("source_driver"), dict) else {}
    status = _text(confirmation.get("status"))
    label = _text(confirmation.get("label")) or "待确认"
    source_kind = _chain_source_kind_label(source_driver.get("kind"))
    source_name = _text(source_driver.get("name")) or "源板块"
    source_text = f"{source_kind}{source_name}"
    if status == "confirmed":
        return f"{label}：链主/弹性跟随，复核扩散延续"
    if status == "elastic_rebound":
        return f"{label}：链主不强，先看弹性标的和三线扩散"
    if status == "source_only":
        return f"{label}：{source_text}在涨，等链主确认后再当主线"
    if status == "mixed":
        return f"{label}：只看强分支，不当整链共振"
    if status in {"not_confirmed", "weak"}:
        return f"{label}：{source_text}不代表整条产业链"
    return label


def _chain_heat_display_context(doc: dict[str, Any], candidate_groups: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    domains = [item for item in (doc.get("integrated_domains") or []) if isinstance(item, dict)]
    primary = domains[0] if domains else {}
    reference = _chain_reference_domain(doc, domains)
    primary_payload = _chain_domain_payload(primary) if primary else {}
    reference_payload = _chain_domain_payload(reference) if reference else {}
    source_driver = _chain_source_driver_payload(primary_payload, doc.get("source_driver") if isinstance(doc.get("source_driver"), dict) else None)
    primary_change = _float(primary_payload.get("change_pct"))
    reference_change = _float(reference_payload.get("change_pct"))
    rep_confirmation = _representative_confirmation(candidate_groups, primary_change)
    chain_confirmation = _chain_confirmation_payload(rep_confirmation)
    flags: list[str] = []
    label_text = " · ".join([_text(doc.get("chain_name")), _text(doc.get("node_name"))])
    primary_name = _text(primary_payload.get("name"))
    if primary_name and primary_name not in label_text:
        flags.append("driver_not_same_as_chain_label")
    if primary_change is not None and reference_change is not None and abs(primary_change - reference_change) >= 1.0:
        flags.append("driver_reference_divergence")
    if rep_confirmation.get("status") in {"not_confirmed", "weak"}:
        flags.append("representatives_not_confirmed")
    if rep_confirmation.get("status") == "source_only":
        flags.append("representatives_weak_confirmation")
    if _float(primary_payload.get("mapping_confidence"), 100) is not None and (_float(primary_payload.get("mapping_confidence"), 100) or 0) < 70:
        flags.append("low_mapping_confidence")
    primary_label = _text(source_driver.get("name")) or _text(primary_payload.get("name")) or "源板块"
    primary_kind_label = _chain_source_kind_label(source_driver.get("kind") or primary_payload.get("kind"))
    reference_label = _text(reference_payload.get("name"))
    explain_parts = [f"源[{primary_kind_label}] {primary_label}"]
    if primary_change is not None:
        explain_parts[-1] += f" {_format_signed_pct(primary_change)}"
    if reference_label and reference_label != primary_label:
        ref_text = f"参考行业 {reference_label}"
        if reference_change is not None:
            ref_text += f" {_format_signed_pct(reference_change)}"
        explain_parts.append(ref_text)
    explain_parts.append(chain_confirmation.get("label") or rep_confirmation.get("label") or "")
    route_explain = _text(doc.get("route_explain"))
    if not route_explain:
        route_target = "/".join([part for part in (_text(doc.get("chain_name")), _text(doc.get("node_name"))) if part])
        route_explain = f"源[{primary_kind_label}] {primary_label} -> {route_target}" if route_target else ""
    source_events = doc.get("source_events") or [
        _chain_source_driver_payload(_chain_domain_payload(domain))
        for domain in domains[:10]
        if isinstance(domain, dict)
    ]
    return {
        "change_display_kind": "chain_driver_change",
        "change_display_label": "驱动涨幅",
        "change_explain": "；".join([part for part in explain_parts if part]),
        "source_driver": source_driver,
        "source_events": source_events,
        "source_kind_mix": doc.get("source_kind_mix") or {},
        "route_explain": route_explain,
        "primary_domain": primary_payload,
        "reference_domain": reference_payload,
        "domain_change_stats": _chain_domain_change_stats(domains),
        "representative_confirmation": rep_confirmation,
        "chain_confirmation": chain_confirmation,
        "mismatch_flags": flags,
    }


def _theme_focus_stocks_preview(db: Any, theme_name: str, *, limit: int = 6) -> list[dict[str, Any]]:
    symbols, stock_names = _constituent_symbols_for_source(db, {"kind": "concept", "name": theme_name})
    if not symbols:
        return []
    codes: list[str] = []
    dot_symbols: list[str] = []
    for raw_symbol in symbols:
        normalized, raw_code = _normalize_stock_symbol(_text(raw_symbol))
        code = raw_code or _text(raw_symbol).split(".", 1)[-1]
        if code and code not in codes:
            codes.append(code)
        if normalized and normalized not in dot_symbols:
            dot_symbols.append(normalized)
    if not codes and not dot_symbols:
        return []
    symbol_query = {"$or": [{"code": {"$in": codes}}, {"symbol": {"$in": dot_symbols}}]}
    expected_day = _day_change_expected_day()
    if expected_day:
        symbol_query = {
            "$and": [
                symbol_query,
                {"$or": [{"trade_date": expected_day}, {"dt": expected_day}]},
            ]
        }
    try:
        docs = list(db["quote_snapshots"].find(
            symbol_query,
            {
                "_id": 0,
                "symbol": 1,
                "code": 1,
                "name": 1,
                "price": 1,
                "close": 1,
                "change_pct": 1,
                "turnover_pct": 1,
                "amount": 1,
                "trade_date": 1,
                "dt": 1,
            },
        ).sort([("change_pct", -1), ("amount", -1)]).limit(limit * 3))
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for doc in docs:
        normalized, raw_code = _normalize_stock_symbol(_text(doc.get("symbol") or doc.get("code")))
        raw_code = raw_code or _text(doc.get("code"))
        key = normalized or raw_code
        if not key or key in seen:
            continue
        seen.add(key)
        name = _text(doc.get("name")) or stock_names.get(raw_code) or stock_names.get(normalized or "") or key
        change_pct = _float(doc.get("change_pct"))
        rows.append({
            "kind": "stock",
            "symbol": normalized,
            "raw_code": raw_code,
            "code": normalized or raw_code,
            "name": name,
            "leader_tier": "theme_hot",
            "chain_role": f"{theme_name}热股",
            "attention_score": change_pct,
            "day_change_pct": change_pct,
            "latest_signal": f"{theme_name}叠加",
            "why_watch": f"{theme_name}主题内涨幅靠前",
            "target_kind": "stock",
            "target_label": normalized or raw_code,
            "target_symbol": normalized or raw_code,
            "target_freq": DEFAULT_TERMINAL_FREQ,
        })
        if len(rows) >= limit:
            break
    return rows


def _non_chain_theme_sector_rows(db: Any, *, limit: int = 4) -> list[dict[str, Any]]:
    expected_day = _day_change_expected_day()
    range_columns = _watchlist_range_columns()
    query: dict[str, Any] = {"kind": "concept"}
    if expected_day:
        day_start = datetime.fromisoformat(expected_day)
        day_end = day_start + timedelta(days=1)
        query["$or"] = [
            {"trade_date": expected_day},
            {"dt": {"$gte": day_start, "$lt": day_end}},
            {"trade_minute": {"$gte": day_start, "$lt": day_end}},
        ]
    latest = db["board_heat_ticks"].find_one(query, {"trade_minute": 1}, sort=[("trade_minute", -1)]) or {}
    latest_minute = latest.get("trade_minute")
    if latest_minute is not None:
        query = {"kind": "concept", "trade_minute": latest_minute}
    try:
        docs = list(db["board_heat_ticks"].find(
            query,
            {
                "_id": 0,
                "name": 1,
                "board_name": 1,
                "code": 1,
                "change_pct": 1,
                "up_count": 1,
                "down_count": 1,
                "leader_name": 1,
                "leader_symbol": 1,
                "leader_change_pct": 1,
                "rank_idx": 1,
                "trade_date": 1,
                "trade_minute": 1,
                "snapshot_at": 1,
            },
        ).sort([("change_pct", -1), ("leader_change_pct", -1)]).limit(800))
    except Exception:
        return []
    range_targets: list[tuple[str, str]] = []
    for doc in docs:
        name = _text(doc.get("name") or doc.get("board_name"))
        reason = non_chain_reason(name)
        change_pct = _float(doc.get("change_pct"))
        standalone_bonus = _standalone_theme_rank_bonus(name, reason)
        if name and reason and standalone_bonus > 0 and change_pct is not None and change_pct >= 1.5:
            range_targets.append(("concept", name))
            if len(range_targets) >= limit:
                break
    range_lookup = _board_heat_range_returns_batch(range_targets, range_columns)
    rows: list[dict[str, Any]] = []
    for doc in docs:
        name = _text(doc.get("name") or doc.get("board_name"))
        reason = non_chain_reason(name)
        change_pct = _float(doc.get("change_pct"))
        standalone_bonus = _standalone_theme_rank_bonus(name, reason)
        if not name or not reason or standalone_bonus <= 0 or change_pct is None or change_pct < 1.5:
            continue
        up_count = _float(doc.get("up_count"), 0) or 0
        leader_change = _float(doc.get("leader_change_pct"), 0) or 0
        display_rank_score = round(change_pct * 10 + min(up_count / 4, 28) + min(leader_change, 20) + standalone_bonus, 3)
        source_driver = {
            "kind": "theme",
            "kind_label": "主题",
            "name": name,
            "code": _text(doc.get("code")),
            "change_pct": change_pct,
            "up_count": up_count,
            "down_count": _float(doc.get("down_count"), 0),
            "leader_name": _text(doc.get("leader_name")),
            "leader_symbol": _text(doc.get("leader_symbol")),
            "leader_change_pct": _float(doc.get("leader_change_pct")),
            "rank": _float(doc.get("rank_idx")),
            "non_chain_reason": reason,
        }
        focus_preview = _theme_focus_stocks_preview(db, name, limit=6)
        matched_names = [_text(item.get("name")) for item in focus_preview if _text(item.get("name"))]
        matched_symbols = [_text(item.get("symbol") or item.get("code") or item.get("raw_code")) for item in focus_preview]
        range_returns, range_source, range_meta = range_lookup.get(("concept", name), ({}, "", {"status": "missing_target"}))
        overlay = {
            **source_driver,
            "matched_names": matched_names[:6],
            "matched_symbols": matched_symbols[:6],
            "matched_count": len(matched_symbols),
            "source_boards": [name],
        }
        doc_trade_day = _date_text(doc.get("trade_date") or doc.get("trade_minute") or doc.get("snapshot_at"))
        leader_label = _text(doc.get("leader_name"))
        hot_stock_label = " / ".join(matched_names[:2])
        rank_reason = "；".join([
            f"主题[{name}] {_format_signed_pct(change_pct)}",
            reason,
            f"热股 {hot_stock_label}" if hot_stock_label else (f"领涨 {leader_label}" if leader_label else ""),
        ])
        rows.append({
            "group": "sector_boards",
            "domain": "theme_heat",
            "kind": "theme",
            "label": f"主题热度 · {name}",
            "name": f"主题热度 · {name}",
            "code": name,
            "latest_price": display_rank_score,
            "day_change_pct": change_pct,
            "daily_change_pct": change_pct,
            "day_change_source": "board_heat_ticks",
            "day_change_mode": _a_day_change_mode(),
            "day_change_as_of": doc_trade_day,
            "range_returns": range_returns,
            "range_return_source": range_source or "board_heat_ticks",
            "range_return_status": range_meta.get("status") or ("board_heat_price" if range_returns else "board_heat_kline_missing"),
            "range_return_meta": range_meta,
            "lane": "board_lane",
            "second_screen_role": "event_theme_heat",
            "source": "board_heat_ticks",
            "heat_source": "board_heat_ticks",
            "rank": _float(doc.get("rank_idx")),
            "phase": "warming",
            "trading_signal": "theme_heat",
            "heat_score": display_rank_score,
            "display_rank_score": display_rank_score,
            "chain_id": "",
            "chain_name": "主题热度",
            "node_id": name,
            "node_name": name,
            "layer": "theme",
            "stage": "event_or_style",
            "integrated_domains": [source_driver],
            "source_driver": source_driver,
            "source_events": [source_driver],
            "source_concept_overlays": [],
            "source_event_concept_overlays": [],
            "source_theme_overlays": [overlay],
            "source_event_theme_overlays": [{"source": source_driver, "themes": [overlay]}],
            "source_kind_mix": {"theme": 1},
            "route_explain": f"主题[{name}] 独立观察，不强制映射产业链",
            "change_display_kind": "theme_change",
            "change_display_label": "主题涨幅",
            "change_explain": rank_reason,
            "primary_domain": source_driver,
            "reference_domain": {},
            "domain_change_stats": {"count": 1, "known_count": 1, "positive_count": 1, "negative_count": 0, "avg_change_pct": change_pct},
            "representative_confirmation": {"status": "theme_heat", "label": "主题独立观察", "known_count": len(focus_preview)},
            "chain_confirmation": {"status": "theme_heat", "label": "主题热度", "representative_status": "theme_heat", "representative_label": "主题独立观察", "known_count": len(focus_preview)},
            "non_chain_reason": reason,
            "mismatch_flags": ["non_chain_theme"],
            "latest_signal": "主题热度",
            "trader_action": "观察事件/风格扩散，不当成行业主线",
            "action_status": "主题热度",
            "invalidates_when": "主题涨幅回落或热股退潮",
            "rank_reason": rank_reason,
            "trace_summary": rank_reason,
            "explanation": rank_reason,
            "carrier": focus_preview[0] if focus_preview else {"name": leader_label, "day_change_pct": _float(doc.get("leader_change_pct"))},
            "representatives": {"core": [], "upstream": [], "elastic": focus_preview[:3], "downstream": [], "source_leader": []},
            "candidate_groups": {"leaders": [], "weighted": [], "elastic": focus_preview[:6], "source_leaders": [], "constituents": []},
            "focus_stocks_preview": focus_preview,
            "target_kind": "concept",
            "target_label": name,
            "target_symbol": name,
            "target_freq": DEFAULT_TERMINAL_FREQ,
            "display_label": f"主题热度 · {name}",
            "heat_target_label": name,
            "heat_resolution_status": "non_chain_theme",
            "data_truth": _data_truth_payload(
                collection="board_heat_ticks",
                domain="theme_heat",
                source="board_heat_ticks",
                extra={
                    "as_of": doc_trade_day,
                    "latest_bar_time": _iso_dt(doc.get("trade_minute")),
                    "mapping_status": "non_chain_theme",
                    "unmapped_reason": reason,
                    "chart_mode_default": "board_heat",
                },
            ),
        })
        if len(rows) >= limit:
            break
    return rows


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
            _CHAIN_HEAT_SHELL_PROJECTION,
        ).sort("rank", 1).limit(limit * 5))
        docs = _diversify_chain_heat_docs(
            docs,
            limit=limit,
            max_nodes_per_chain=_chain_heat_max_nodes_per_chain(),
        )
        representative_quote_rows = _chain_representative_quote_rows(docs)
        if representative_quote_rows:
            try:
                _refresh_realtime_quotes_for_rows(
                    db,
                    representative_quote_rows,
                    refresh_key="chain_heat_representatives",
                    limit=len(representative_quote_rows),
                )
            except Exception:
                pass
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    range_columns = _watchlist_range_columns()
    range_returns_enabled = _chain_heat_shell_range_returns_enabled()
    range_targets: list[tuple[str, str]] = []
    if range_returns_enabled:
        for doc in docs:
            integrated = doc.get("integrated_domains") if isinstance(doc.get("integrated_domains"), list) else []
            primary = integrated[0] if integrated and isinstance(integrated[0], dict) else {}
            target_kind = _text(primary.get("kind")) or "industry"
            target_label = _text(primary.get("name")) or _text(doc.get("node_name") or doc.get("chain_name"))
            range_targets.append((target_kind, target_label))
    range_lookup = _board_heat_range_returns_batch(range_targets, range_columns) if range_targets else {}
    candidate_groups_by_doc: list[dict[str, list[dict[str, Any]]]] = []
    candidate_signal_pool: dict[str, list[dict[str, Any]]] = {
        "upstream": [],
        "leaders": [],
        "weighted": [],
        "elastic": [],
        "downstream": [],
        "source_leaders": [],
        "constituents": [],
    }
    for doc in docs:
        groups = _candidate_groups_from_representatives(doc, lightweight=True)
        existing_codes = {
            _candidate_group_symbol_keys(item)[1]
            for item in _flatten_candidate_groups(groups, limit=64)
            if _candidate_group_symbol_keys(item)[1]
        }
        for probe in _new_high_probe_rows_for_chain_sources(db, doc):
            _, raw_code = _candidate_group_symbol_keys(probe)
            if raw_code and raw_code not in existing_codes:
                groups.setdefault("constituents", []).append(probe)
                existing_codes.add(raw_code)
        candidate_groups_by_doc.append(groups)
        for key, group_rows in groups.items():
            candidate_signal_pool.setdefault(key, []).extend(group_rows)
    candidate_new_high_signals = _latest_candidate_new_high_signals(db, candidate_signal_pool)
    for doc, base_candidate_groups in zip(docs, candidate_groups_by_doc):
        integrated = doc.get("integrated_domains") if isinstance(doc.get("integrated_domains"), list) else []
        primary = integrated[0] if integrated and isinstance(integrated[0], dict) else {}
        target_kind = _text(primary.get("kind")) or "industry"
        target_label = _text(primary.get("name")) or _text(doc.get("node_name") or doc.get("chain_name"))
        market_logic_node = doc.get("market_logic_node") if isinstance(doc.get("market_logic_node"), dict) else {}
        display_chain_name = _text(market_logic_node.get("chain_name") or doc.get("chain_name"))
        display_node_id = _text(market_logic_node.get("node_id") or doc.get("node_id"))
        display_node_name = _text(market_logic_node.get("node_name") or doc.get("node_name"))
        display_layer = _text(market_logic_node.get("layer") or doc.get("layer"))
        display_stage = _text(market_logic_node.get("stage") or doc.get("stage"))
        display_doc = dict(doc)
        display_doc.update({
            "taxonomy_node_id": doc.get("node_id"),
            "taxonomy_node_name": doc.get("node_name"),
            "chain_name": display_chain_name or doc.get("chain_name"),
            "node_id": display_node_id or doc.get("node_id"),
            "node_name": display_node_name or doc.get("node_name"),
            "layer": display_layer or doc.get("layer"),
            "stage": display_stage or doc.get("stage"),
        })
        label = " · ".join([item for item in [display_chain_name, display_node_name] if item])
        candidate_groups = _apply_candidate_new_high_signals(base_candidate_groups, candidate_new_high_signals)
        display_context = _chain_heat_display_context(display_doc, candidate_groups)
        late_session_signal = _late_session_momentum_label(doc)
        if late_session_signal:
            change_explain = _text(display_context.get("change_explain"))
            if late_session_signal not in change_explain:
                display_context["change_explain"] = "；".join(part for part in [change_explain, late_session_signal] if part)
        concept_overlays = doc.get("source_concept_overlays") if isinstance(doc.get("source_concept_overlays"), list) else []
        source_event_overlays = doc.get("source_event_concept_overlays") if isinstance(doc.get("source_event_concept_overlays"), list) else []
        theme_overlays = doc.get("source_theme_overlays") if isinstance(doc.get("source_theme_overlays"), list) else []
        source_event_theme_overlays = doc.get("source_event_theme_overlays") if isinstance(doc.get("source_event_theme_overlays"), list) else []
        display_context["source_concept_overlays"] = concept_overlays
        display_context["source_event_concept_overlays"] = source_event_overlays
        display_context["source_theme_overlays"] = theme_overlays
        display_context["source_event_theme_overlays"] = source_event_theme_overlays
        display_rank_score = _chain_heat_display_rank_score(display_doc, display_context)
        graph = _chain_graph_doc(doc.get("chain_id"), display_node_id or doc.get("node_id")) if _chain_heat_shell_graph_enabled() else {}
        viewpoint_context = _viewpoint_context_from_graph(graph) if graph else {}
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
                "change_display_kind": display_context.get("change_display_kind"),
                "primary_domain": display_context.get("primary_domain"),
                "reference_domain": display_context.get("reference_domain"),
            },
        )
        technical_linkage = _technical_linkage_from_groups(candidate_groups)
        risk_flags = _chain_risk_flags(doc, data_truth)
        carrier = (candidate_groups.get("leaders") or candidate_groups.get("elastic") or [{}])[0]
        day_change_as_of = doc_trade_day
        day_change_pct = _float(doc.get("change_pct")) if day_change_as_of == _day_change_expected_day() else None
        range_key = (_normalize_board_heat_kind(target_kind), target_label)
        missing_range_status = "missing_target" if range_returns_enabled else "lazy"
        range_returns, range_source, range_meta = range_lookup.get(range_key, ({}, "", {"status": missing_range_status}))
        row = {
            **doc,
            **display_context,
            "taxonomy_node_id": doc.get("node_id"),
            "taxonomy_node_name": doc.get("node_name"),
            "chain_name": display_chain_name or doc.get("chain_name"),
            "node_id": display_node_id or doc.get("node_id"),
            "node_name": display_node_name or doc.get("node_name"),
            "layer": display_layer or doc.get("layer"),
            "stage": display_stage or doc.get("stage"),
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
            "range_returns": range_returns,
            "intraday_momentum_returns": {
                "momentum_5m": doc.get("momentum_5m"),
                "momentum_15m": doc.get("momentum_15m"),
                "momentum_30m": doc.get("momentum_30m"),
            },
            "late_session_signal": late_session_signal,
            "late_session_reason": late_session_signal,
            "range_return_source": range_source or "chain_heat_snapshots",
            "range_return_status": range_meta.get("status") or ("board_heat_price" if range_returns else "board_heat_kline_missing"),
            "range_return_meta": range_meta,
            "lane": "board_lane",
            "second_screen_role": "chain_heat_map",
            "action_status": doc.get("phase"),
            "trader_action": _chain_heat_trader_action(display_context) or doc.get("trader_action"),
            "invalidates_when": doc.get("invalidates_when"),
            "explanation": " · ".join([
                _text(doc.get("range_pattern")),
                f"热度 {doc.get('heat_score')}",
                f"来源 {doc.get('integrated_count')} 个行业/概念",
            ]),
            "rank_reason": display_context.get("change_explain"),
            "trace_summary": display_context.get("change_explain"),
            "display_rank_score": display_rank_score,
            "source": "chain_heat_snapshots",
            "latest_signal": doc.get("trading_signal"),
            "target_kind": target_kind,
            "target_label": target_label,
            "target_symbol": target_label,
            "target_freq": DEFAULT_TERMINAL_FREQ,
            "display_label": label or target_label,
            "chain_key": _text(doc.get("chain_id")),
            "node_key": display_node_id or _text(doc.get("node_id")),
            "chart_mode_default": "chain_heat",
            "heat_target_label": target_label,
            "heat_resolution_status": "chain_primary_domain",
            "carrier": carrier,
            "representatives": {
                "core": candidate_groups.get("leaders", []),
                "upstream": candidate_groups.get("upstream", []),
                "elastic": candidate_groups.get("elastic", []),
                "downstream": candidate_groups.get("downstream", []),
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
                "chain_name": display_chain_name or doc.get("chain_name"),
                "node_id": display_node_id or doc.get("node_id"),
                "node_name": display_node_name or doc.get("node_name"),
                "layer": display_layer or doc.get("layer"),
                "stage": display_stage or doc.get("stage"),
                "taxonomy_node_id": doc.get("node_id"),
                "taxonomy_node_name": doc.get("node_name"),
                "mapping_status": "mapped",
                "evidence_sources": doc.get("evidence_sources") or [],
            },
        }
        rows.append(row)
    if _chain_heat_shell_theme_rows_enabled():
        try:
            rows.extend(_non_chain_theme_sector_rows(db, limit=4))
        except Exception:
            pass
    rows.sort(key=lambda item: (_float(item.get("display_rank_score"), 0) or 0, _float(item.get("heat_score"), 0) or 0), reverse=True)
    return rows[:limit]


def _visible_quote_symbols(rows: list[dict[str, Any]], limit: int) -> list[str]:
    symbols: list[str] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        value = row.get("symbol") or row.get("code") or row.get("raw_code")
        normalized, _ = _normalize_stock_symbol(str(value or ""))
        if not normalized:
            continue
        if normalized.split(".", 1)[0] not in {"SH", "SZ", "BJ"}:
            continue
        if normalized not in symbols:
            symbols.append(normalized)
    return symbols


def _refresh_realtime_quotes_for_rows(
    db,
    rows: list[dict[str, Any]],
    *,
    refresh_key: str,
    limit: int,
) -> dict[str, Any]:
    if _text(os.getenv("TERMINAL_WORKBENCH_VISIBLE_QUOTE_REFRESH")).lower() not in {"1", "true", "yes", "on"}:
        return {"status": "skipped", "reason": "disabled"}
    if _a_day_change_mode() != "quote_intraday":
        return {"status": "skipped", "reason": "not_intraday"}
    try:
        max_symbols = max(1, min(120, int(os.getenv("TERMINAL_WORKBENCH_VISIBLE_QUOTE_LIMIT", "80"))))
    except (TypeError, ValueError):
        max_symbols = 80
    symbols = _visible_quote_symbols(rows, min(limit, max_symbols))
    if not symbols:
        return {"status": "skipped", "reason": "empty_symbols"}
    try:
        min_seconds = max(0.0, float(os.getenv("TERMINAL_WORKBENCH_VISIBLE_QUOTE_MIN_SECONDS", "3")))
    except (TypeError, ValueError):
        min_seconds = 3.0
    now_monotonic = time.monotonic()
    with _VISIBLE_QUOTE_REFRESH_LOCK:
        last = float(_VISIBLE_QUOTE_REFRESH_LAST.get(refresh_key) or 0.0)
        if last and now_monotonic - last < min_seconds:
            return {"status": "throttled", "count": len(symbols)}
        _VISIBLE_QUOTE_REFRESH_LAST[refresh_key] = now_monotonic
    try:
        from signals.sync.modules.quote_snapshots import _fetch_eastmoney_ulist_docs

        now = _sync_now()
        trading_day = _day_change_expected_day("quote_intraday") or _market_today("A").isoformat()
        docs, observations = _fetch_eastmoney_ulist_docs(db, symbols, now, trading_day)
        if docs:
            collection = db["quote_snapshots"]
            for doc in docs.values():
                collection.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        errors = [item for item in observations if isinstance(item, dict) and item.get("error")]
        return {"status": "ok" if docs else "empty", "count": len(symbols), "live": len(docs), "errors": len(errors)}
    except Exception as exc:
        return {"status": "failed", "count": len(symbols), "error": f"{exc.__class__.__name__}: {exc}"}


def _shell_stock_range_return_limit(group: str) -> int:
    defaults = {
        "focus_stocks": 6,
        "risk_stocks": 3,
        "watch_stocks": 4,
        "clue_stocks": 3,
        "manual_clues": 0,
        "scored_stocks": 3,
    }
    env_name = f"TERMINAL_WORKBENCH_{group.upper()}_RANGE_RETURN_LIMIT"
    try:
        return max(0, int(os.getenv(env_name, str(defaults.get(group, 12)))))
    except (TypeError, ValueError):
        return defaults.get(group, 12)


def _batch_stock_range_returns(
    rows: list[dict[str, Any]],
    range_columns: list[dict[str, Any]],
) -> dict[str, tuple[dict[str, Optional[float]], str, str]]:
    if not rows or not _range_return_column_keys(range_columns):
        return {}
    code_to_symbol: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("range_returns"):
            continue
        symbol = _text(row.get("symbol") or row.get("code") or row.get("target_symbol") or row.get("label"))
        normalized, raw_code = _normalize_stock_symbol(symbol)
        raw = raw_code or _text(row.get("raw_code")) or (normalized or symbol).split(".")[-1]
        raw = raw.strip()
        if raw:
            code_to_symbol.setdefault(raw, normalized or symbol)
    if not code_to_symbol:
        return {}
    start_dates = [
        pd.Timestamp(_text(column.get("start_date")))
        for column in range_columns
        if _text(column.get("start_date"))
    ]
    start_dates = [item for item in start_dates if not pd.isna(item)]
    min_start = min(start_dates) if start_dates else None
    query: dict[str, Any] = {
        "meta.symbol": {"$in": sorted(code_to_symbol)},
        "meta.freq": "日线",
    }
    if min_start is not None:
        query["dt"] = {"$gte": min_start.to_pydatetime()}
    try:
        cursor = _mongo_db()["bars"].find(
            query,
            {"_id": 0, "dt": 1, "close": 1, "meta.symbol": 1, "meta.source": 1},
        ).sort([("meta.symbol", 1), ("dt", 1)])
        docs = list(cursor)
    except Exception:
        return {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    sources: dict[str, set[str]] = {}
    for doc in docs:
        meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
        raw = _text(meta.get("symbol"))
        if not raw:
            continue
        grouped.setdefault(raw, []).append(doc)
        source = _text(meta.get("source"))
        if source:
            sources.setdefault(raw, set()).add(source)
    output: dict[str, tuple[dict[str, Optional[float]], str, str]] = {}
    for raw, symbol in code_to_symbol.items():
        records = [
            {
                "dt": pd.to_datetime(doc.get("dt"), errors="coerce"),
                "close": _float(doc.get("close")),
            }
            for doc in grouped.get(raw, [])
        ]
        records = [
            item for item in records
            if not pd.isna(item["dt"]) and item["close"] is not None
        ]
        if not records:
            continue
        df = pd.DataFrame(records).sort_values("dt").drop_duplicates(subset=["dt"], keep="last").set_index("dt")
        returns = _compute_range_returns(df, range_columns, adjust_price_discontinuities=True)
        if not returns:
            continue
        status = _range_return_status_from_returns(returns, range_columns)
        source_label = "+".join(sorted(sources.get(raw) or [])) or "bars"
        output[_text(symbol).upper()] = (returns, f"bars_batch:{source_label}", status)
    return output


def _fill_lazy_stock_range_returns(
    rows: list[dict[str, Any]],
    range_columns: list[dict[str, Any]],
) -> None:
    lookup = _batch_stock_range_returns(rows, range_columns)
    if not lookup and not any(isinstance(row, dict) and _is_all_etf_review_row(row) for row in rows):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        existing_returns = row.get("range_returns") if isinstance(row.get("range_returns"), dict) else {}
        if existing_returns:
            if _text(row.get("range_return_status")) in {"", "lazy"}:
                row["range_return_status"] = _range_return_status_from_returns(existing_returns, range_columns)
            continue
        symbol = _text(row.get("symbol") or row.get("code") or row.get("target_symbol") or row.get("label"))
        normalized, _ = _normalize_stock_symbol(symbol)
        key = _text(normalized or symbol).upper()
        payload = lookup.get(key) if lookup else None
        if not payload:
            if _text(row.get("range_return_status")) in {"", "lazy"}:
                if _is_all_etf_review_row(row):
                    row["range_return_status"] = "spot_only"
                    row["range_return_source"] = row.get("range_return_source") or "strategy_snapshots.etf_analysis.spot"
                elif lookup:
                    row["range_return_status"] = "unsupported_market" if key.startswith(("HK.", "US.")) else "range_returns_kline_empty"
            continue
        returns, source, status = payload
        row["range_returns"] = returns
        row["range_return_source"] = source
        row["range_return_status"] = status


def _shell_stock_group_display_cap(group: str) -> int:
    defaults = {
        "focus_stocks": 36,
        "risk_stocks": 12,
        "watch_stocks": 24,
        "clue_stocks": 24,
    }
    env_name = f"TERMINAL_WORKBENCH_{group.upper()}_SHELL_CAP"
    try:
        return max(1, int(os.getenv(env_name, str(defaults.get(group, 24)))))
    except (TypeError, ValueError):
        return defaults.get(group, 24)


def _raw_shell_stock_row(item: dict[str, Any], *, group: str) -> dict[str, Any]:
    symbol = _text(item.get("symbol") or item.get("code") or item.get("raw_code") or item.get("label"))
    normalized, raw_code = _normalize_stock_symbol(symbol)
    normalized = normalized or symbol
    pool_defaults = {
        "focus_stocks": ("focus", "confirmed_entry", "买点池", "entry_ready"),
        "watch_stocks": ("watch", "watch_pool", "盯盘池", "watch"),
        "clue_stocks": ("clue_pool", "clue_pool", "线索池", "manual_review"),
        "manual_clues": ("clue_pool", "clue_pool", "线索池", "manual_review"),
    }
    pool_type, trade_stage, stage_label, action_status = pool_defaults.get(group, ("watch", "watch_pool", "观察", "watch"))
    row = dict(item)
    row.update({
        "kind": "stock",
        "label": normalized,
        "symbol": normalized,
        "code": normalized,
        "raw_code": raw_code or normalized.split(".")[-1],
        "name": _text(item.get("name")) or _stock_name(normalized, item),
        "pool_type": _text(item.get("pool_type")) or pool_type,
        "trade_stage": _text(item.get("trade_stage")) or trade_stage,
        "stage_label": _text(item.get("stage_label")) or stage_label,
        "action_status": _text(item.get("action_status")) or action_status,
        "latest_signal": _text(item.get("latest_signal") or item.get("reason")) or stage_label,
        "range_returns": {},
        "range_return_status": _text(item.get("range_return_status")) or "lazy",
        "target_kind": "stock",
        "target_label": normalized,
        "target_symbol": normalized,
        "target_freq": DEFAULT_TERMINAL_FREQ,
    })
    return row


def _terminal_stock_pool_raw_group_rows(group: str, limit: int) -> list[dict[str, Any]]:
    try:
        db = _mongo_db()
        doc = db["terminal_stock_pool"].find_one(
            {"pool": "terminal_stock_pool", "market": "A"},
            {
                "stocks": 1,
                "focus_stocks": 1,
                "risk_stocks": 1,
                "watch_stocks": 1,
                "clue_stocks": 1,
            },
            sort=[("updated_at", -1)],
        ) or {}
    except Exception:
        return []
    source_rows = doc.get(group)
    if source_rows is None and group == "focus_stocks":
        source_rows = doc.get("stocks")
    rows: list[dict[str, Any]] = []
    for item in source_rows or []:
        if not isinstance(item, dict):
            continue
        row = _raw_shell_stock_row(item, group=group)
        if not _text(row.get("symbol")):
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _manual_clue_raw_rows(limit: int) -> list[dict[str, Any]]:
    try:
        db = _mongo_db()
        docs = list(db["terminal_manual_clues"].find(
            {"active": {"$ne": False}},
            {"_id": 0, "symbol": 1, "raw_code": 1, "name": 1, "reason": 1, "updated_at": 1},
        ).sort([("updated_at", -1), ("created_at", -1)]).limit(limit))
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for doc in docs:
        if isinstance(doc, dict):
            row = _raw_shell_stock_row(doc, group="manual_clues")
            row.update({
                "source_collection": "terminal_manual_clues",
                "source_tags": ["用户探索", "临时线索"],
                "manual_clue": True,
                "deletable": True,
            })
            rows.append(row)
    return rows


def _hot_rank_clue_rows(limit: int) -> list[dict[str, Any]]:
    try:
        db = _mongo_db()
        docs = list(db["hot_rank_clues"].find(
            {"active": True},
            {
                "_id": 1,
                "raw_code": 1,
                "code": 1,
                "symbol": 1,
                "name": 1,
                "score": 1,
                "tier": 1,
                "sources": 1,
                "ranks": 1,
                "strategy_tags": 1,
                "reason_summary": 1,
                "as_of": 1,
                "updated_at": 1,
            },
        ).sort([("score", -1), ("updated_at", -1)]).limit(limit))
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        raw_code = _text(doc.get("raw_code") or doc.get("code") or doc.get("_id"))
        symbol = _text(doc.get("symbol") or raw_code)
        row = _raw_shell_stock_row({
            "symbol": symbol,
            "raw_code": raw_code,
            "name": doc.get("name"),
            "reason": _text(doc.get("reason_summary")) or "热榜启动/均线攀爬线索",
            "score": doc.get("score"),
            "latest_signal": _text(doc.get("reason_summary")) or "热榜启动/均线攀爬线索",
            "source_collection": "hot_rank_clues",
            "source_collections": ["hot_rank_clues"],
            "source_tags": ["自动热榜", _text(doc.get("tier")) or "线索"],
            "metadata": {
                "source_collection": "hot_rank_clues",
                "auto_clue": True,
                "hot_rank_score": doc.get("score"),
                "hot_rank_tier": doc.get("tier"),
                "hot_rank_sources": list(doc.get("sources") or []),
                "hot_rank_ranks": dict(doc.get("ranks") or {}),
                "strategy_tags": list(doc.get("strategy_tags") or []),
                "as_of": _text(doc.get("as_of")),
            },
        }, group="clue_stocks")
        row.update({
            "source": "hot_rank_clues",
            "source_collection": "hot_rank_clues",
            "source_collections": ["hot_rank_clues"],
            "source_tags": ["自动热榜", _text(doc.get("tier")) or "线索"],
            "trace_summary": "auto_clue:hot_rank_clues",
            "clue_quality_score": doc.get("score"),
            "hot_rank_tier": doc.get("tier"),
            "hot_rank_sources": list(doc.get("sources") or []),
            "hot_rank_ranks": dict(doc.get("ranks") or {}),
            "hot_rank_strategy_tags": list(doc.get("strategy_tags") or []),
            "hot_rank_as_of": _text(doc.get("as_of")),
        })
        rows.append(row)
    return rows


def _shell_manual_clue_limit() -> int:
    try:
        return max(1, int(os.getenv("TERMINAL_WORKBENCH_MANUAL_CLUE_LIMIT", "12")))
    except (TypeError, ValueError):
        return 12


def _shell_manual_clue_decision_limit() -> int:
    try:
        return max(0, int(os.getenv("TERMINAL_WORKBENCH_MANUAL_CLUE_DECISION_LIMIT", "0")))
    except (TypeError, ValueError):
        return 0


def _terminal_stock_pool_group_rows(range_columns: list[dict[str, Any]], group: str = "focus_stocks", limit: Optional[int] = None) -> list[dict[str, Any]]:
    try:
        db = _mongo_db()
        doc = db["terminal_stock_pool"].find_one(
            {"pool": "terminal_stock_pool", "market": "A"},
            {
                "stocks": 1,
                "focus_stocks": 1,
                "risk_stocks": 1,
                "watch_stocks": 1,
                "clue_stocks": 1,
                "stock_limit": 1,
                "risk_limit": 1,
                "watch_limit": 1,
                "clue_limit": 1,
            },
            sort=[("updated_at", -1)],
        ) or {}
    except Exception:
        return []
    if limit is None:
        env_name = {
            "focus_stocks": "TERMINAL_WORKBENCH_FOCUS_STOCK_LIMIT",
            "risk_stocks": "TERMINAL_WORKBENCH_RISK_STOCK_LIMIT",
            "watch_stocks": "TERMINAL_WORKBENCH_WATCH_STOCK_LIMIT",
            "clue_stocks": "TERMINAL_WORKBENCH_CLUE_STOCK_LIMIT",
        }.get(group, "TERMINAL_WORKBENCH_FOCUS_STOCK_LIMIT")
        limit_key = {
            "focus_stocks": "stock_limit",
            "risk_stocks": "risk_limit",
            "watch_stocks": "watch_limit",
            "clue_stocks": "clue_limit",
        }.get(group, "stock_limit")
        fallback_default = 120 if group == "watch_stocks" else (36 if group == "clue_stocks" else 72)
        try:
            limit = max(1, int(os.getenv(env_name) or doc.get(limit_key) or fallback_default))
        except (TypeError, ValueError):
            limit = fallback_default
    rows: list[dict[str, Any]] = []
    source_rows = doc.get(group)
    if source_rows is None and group == "focus_stocks":
        source_rows = doc.get("stocks")
    source_rows = [item for item in source_rows or [] if isinstance(item, dict)]
    if group in {"focus_stocks", "risk_stocks", "watch_stocks", "clue_stocks"}:
        _refresh_realtime_quotes_for_rows(db, source_rows, refresh_key=group, limit=limit)
    range_return_limit = _shell_stock_range_return_limit(group)
    chain_positions = _terminal_stock_chain_position_map(source_rows)
    for item in source_rows:
        reasons = [reason for reason in item.get("inclusion_reasons") or [] if isinstance(reason, dict)]
        has_technical = any(
            reason.get("reason_type") in {"technical_trigger", "technical_signal"}
            or reason.get("source_collection") == "terminal_technical_signals"
            for reason in reasons
        )
        fallback_only = bool(reasons) and all(reason.get("reason_type") == "fallback_watch" for reason in reasons)
        if group == "focus_stocks" and (fallback_only or (item.get("signal_origin") == "fallback_watch" and not has_technical)):
            continue
        row = _enrich_shell_stock_row(
            dict(item),
            range_columns,
            require_range_returns=len(rows) < range_return_limit,
        )
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
        row = _refresh_shell_stock_chain_assignment(row, chain_positions)
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


def _manual_clue_rows(
    range_columns: list[dict[str, Any]],
    limit: Optional[int] = None,
    *,
    decision_enrich_limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    limit = limit or _shell_manual_clue_limit()
    try:
        db = _mongo_db()
        docs = list(db["terminal_manual_clues"].find(
            {"active": {"$ne": False}},
            {"_id": 0},
        ).sort([("updated_at", -1), ("created_at", -1)]).limit(limit))
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    range_return_limit = _shell_stock_range_return_limit("manual_clues")
    for doc in docs:
        symbol = _text(doc.get("symbol"))
        normalized, raw_code = _normalize_stock_symbol(symbol)
        if not normalized:
            continue
        row = _enrich_shell_stock_row(
            {
                "symbol": normalized,
                "raw_code": raw_code,
                "name": doc.get("name") or _stock_name(normalized),
                "reason": "用户临时探索，不影响自动入池",
                "latest_signal": "手动线索",
                "source": "terminal_manual_clues",
            },
            range_columns,
            require_range_returns=len(rows) < range_return_limit,
        )
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
        if decision_enrich_limit is None or len(rows) < decision_enrich_limit:
            row = _enrich_manual_clue_decision(row, normalized)
        rows.append(row)
    return rows


def _manual_clue_attack_focus_rows(
    manual_clues: list[dict[str, Any]],
    existing_focus: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    existing_symbols = {
        _text(row.get("symbol") or row.get("code")).upper()
        for row in existing_focus
        if _text(row.get("symbol") or row.get("code"))
    }
    promoted: list[dict[str, Any]] = []
    for row in manual_clues:
        symbol = _text(row.get("symbol") or row.get("code")).upper()
        if not symbol or symbol in existing_symbols:
            continue
        missing_gates = set(row.get("missing_gates") or row.get("blocked_by") or [])
        if missing_gates & {"risk_clear", "period_conflict", "hard_technical", "upper_context", "right_side"}:
            continue
        upper_side = _text(row.get("upper_timeframe_side"))
        execution_side = _text(row.get("execution_timeframe_side"))
        trade_side = _text(row.get("trade_timeframe_side"))
        if upper_side not in {"right", "mixed"} or execution_side not in {"right", "mixed"}:
            continue
        if trade_side == "none" and "trigger_30m" not in missing_gates:
            continue
        item = dict(row)
        item.update({
            "manual_attack_focus": True,
            "pool_type": "focus",
            "trade_stage": "attack_entry",
            "stage_label": "进攻买点",
            "current_position": "进攻买点",
            "decision_stage": "entry_waiting_confirm",
            "entry_gate_status": "manual_attack_entry",
            "action_status": "manual_attack_entry",
            "actionability": "entry_waiting_confirm",
            "queue_lane": "entry_waiting_confirm",
            "trader_action": "进攻买点复核",
            "recommended_action": "进攻买点复核",
            "next_action": "进攻买点复核",
            "trade_intent": "attack_entry",
            "trade_intent_label": "进攻买点",
            "setup_mode": "right_attack",
            "setup_mode_label": "右侧进攻",
            "setup_side_label": "进攻买点",
            "can_trade_now": True,
            "missing_condition": (
                "30m未补齐，按进攻买点小仓复核"
                if "trigger_30m" in missing_gates
                else "买点路径已走通，手动线索提升为进攻买点"
            ),
            "invalidates_when": "5m/15m转弱、30m迟迟不补或上级周期转冲突",
            "invalidation": "5m/15m转弱、30m迟迟不补或上级周期转冲突",
            "explanation": "手动线索已满足日/周和5m/15m右侧确认，提升到进攻买点复核。",
        })
        promoted.append(item)
        existing_symbols.add(symbol)
    return promoted


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


def _remaining_manual_clues_after_attack_focus(
    manual_clues: list[dict[str, Any]],
    manual_attack_focus: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    attack_symbols = {
        _text(row.get("symbol") or row.get("code")).upper()
        for row in manual_attack_focus
        if _text(row.get("symbol") or row.get("code"))
    }
    if not attack_symbols:
        return list(manual_clues)
    return [
        row for row in manual_clues
        if _text(row.get("symbol") or row.get("code")).upper() not in attack_symbols
    ]


def _enrich_scored_stock_rows(rows: list[dict[str, Any]], range_columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        _refresh_realtime_quotes_for_rows(_mongo_db(), rows, refresh_key="scored_stocks", limit=len(rows) or 1)
    except Exception:
        pass
    range_return_limit = _shell_stock_range_return_limit("scored_stocks")
    return [
        _enrich_shell_stock_row(
            dict(item),
            range_columns,
            require_range_returns=index < range_return_limit,
        ) if item.get("symbol") else dict(item)
        for index, item in enumerate(rows)
    ]


def _focus_stock_pool_meta(focus_count: int) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "label": "买点池",
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
            elif int(doc.get("candidate_count") or 0) > 0 or doc.get("watch_stocks") or doc.get("clue_stocks") or doc.get("risk_stocks"):
                empty_reason = _text(freshness.get("stale_reason"))
                if empty_reason in {"", "terminal_stock_pool_empty", "terminal_focus_stock_pool_empty"}:
                    empty_reason = "当前没有通过买点池闸门；标的已进入盯盘池/线索池等待买点质量与均线确认。"
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


def _kline_cache_coverage_shell_summary() -> dict[str, Any]:
    return {
        "status": "deferred",
        "reason": "full_kline_coverage_is_expensive_for_shell_init",
        "collections": [],
    }


def _build_trader_task_queue(
    *,
    decision_rows: list[dict[str, Any]],
    focus_stocks: list[dict[str, Any]],
    sector_boards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    allowed_lanes = {"entry_ready", "entry_waiting_confirm"}
    lane_titles = {
        "entry_ready": "买点池",
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
            return ""
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
    {"key": "left_attack", "label": "低吸进攻"},
    {"key": "right_attack", "label": "右侧进攻"},
    {"key": "watch", "label": "盯盘观察"},
    {"key": "clue", "label": "线索池"},
]

TRADE_ROLE_DEFINITIONS = {
    "left_attack": {
        "definition": "日/周一买、背驰买或低吸型二买叠加10/20日线承接，进入低吸进攻复核。",
        "source": "terminal_stock_pool.setup_mode + ma_alignment + buy_point_quality",
    },
    "right_attack": {
        "definition": "日/周买点成立后，用30m/15m/5m二买、三买、趋势或突破型信号确认执行。",
        "source": "terminal_stock_pool.setup_mode + ma_alignment + buy_point_quality",
    },
    "watch": {
        "definition": "日/周有买点苗头但还缺30m、5m/15m执行确认；或短周期异动但缺日/周买点。",
        "source": "terminal_stock_pool.watch_stocks",
    },
    "clue": {
        "definition": "只有人工/系统来源或短周期线索，还没有日/周硬买点。",
        "source": "terminal_stock_pool.clue_stocks + terminal_manual_clues",
    },
    "risk_first": {
        "definition": "卖点、冲突或过期信号，只用于从机会池排除；非持仓不推送风险动作。",
        "source": "terminal_stock_pool.risk_stocks",
    },
}


def _trade_role_for_shell_stock(row: dict[str, Any]) -> str:
    setup_mode = _text(row.get("setup_mode"))
    if setup_mode in {"left_attack", "right_attack", "watch", "clue", "risk_first"}:
        return setup_mode
    pool_type = _text(row.get("pool_type"))
    trade_stage = _text(row.get("trade_stage"))
    if pool_type == "risk" or trade_stage == "skip_now":
        return "risk_first"
    if pool_type == "focus" or trade_stage in {"left_attack", "attack_entry", "confirmed_entry"}:
        return "left_attack" if trade_stage == "left_attack" else "right_attack"
    if pool_type == "watch":
        return "watch"
    return "clue"


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
    del sector_boards, risk_stocks
    stock_rows = [*focus_stocks, *watch_stocks, *clue_stocks]
    items: list[dict[str, Any]] = []
    for role, label, fallback in (
        ("left_attack", "低吸进攻", "左侧买点叠加关键均线，先复核位置和失效条件。"),
        ("right_attack", "右侧进攻", "执行周期买点叠加均线确认，复核下单节奏。"),
        ("watch", "盯盘观察", "买点或线索未完全共振，等缺口补齐。"),
        ("clue", "线索池", "只有来源线索，还没有硬技术买点。"),
    ):
        row = _first_stock_for_role(stock_rows, role)
        if row:
            items.append(_stock_role_item(row, role, label, fallback))
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
        "risk_policy": "risk_stocks_excluded_from_opportunity_map",
    }


def _build_ai_alerts(trade_map: dict[str, Any]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for item in trade_map.get("items") or []:
        if not isinstance(item, dict):
            continue
        role = _text(item.get("role"))
        name = _text(item.get("name"))
        if role == "left_attack":
            alerts.append({
                "level": "info",
                "role": role,
                "text": f"{name or '低吸进攻'}按左侧买点和均线承接复核，不推非持仓动作。",
                "command": "只看低吸进攻",
            })
        elif role == "right_attack":
            alerts.append({
                "level": "info",
                "role": role,
                "text": f"{name or '右侧进攻'}按执行周期买点和均线确认复核。",
                "command": "只看右侧进攻",
            })
    return alerts[:3]


def _trade_command_suggestions() -> list[str]:
    return [
        "只看低吸进攻",
        "只看右侧进攻",
        "解释这只票为什么入池",
        "哪些还缺均线确认",
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
        enriched = _enrich_stock_row(dict(row), range_columns, lightweight=True)
        add(enriched, "stock")
    for row in sell_rows:
        enriched = _enrich_stock_row(dict(row), range_columns, lightweight=True)
        add(enriched, "stock")
    for row in decision_rows:
        if row.get("symbol"):
            enriched = _enrich_stock_row(dict(row), range_columns, lightweight=True)
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
    strategy_snapshot = _normalize_strategy_snapshot_for_shell(_safe_strategy_snapshot())
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
    major_indices, industry_etfs = _split_macro_watchlist_rows(macro_indices)
    etf_analysis = _shell_etf_analysis(strategy_snapshot)
    all_etfs = _shell_etf_review_rows(strategy_snapshot)
    all_etfs_total = _shell_etf_universe_total(etf_analysis, len(all_etfs))
    etf_universe = etf_analysis.get("universe") if isinstance(etf_analysis.get("universe"), dict) else {}
    etf_source_counts = etf_universe.get("source_counts") if isinstance(etf_universe.get("source_counts"), dict) else {}
    etf_asset_class_counts = etf_analysis.get("asset_class_counts") if isinstance(etf_analysis.get("asset_class_counts"), dict) else {}
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
    warning_rows = [
        dict(item)
        for item in strategy_snapshot.get("warnings", [])
        if isinstance(item, dict)
    ]
    try:
        _refresh_realtime_quotes_for_rows(_mongo_db(), warning_rows, refresh_key="sell_warnings", limit=len(warning_rows) or 1)
    except Exception:
        pass
    sell_warning_range_return_limit = _shell_stock_range_return_limit("sell_warnings")
    sell_warnings = [
        _enrich_shell_stock_row(
            dict(item),
            range_columns,
            require_range_returns=index < sell_warning_range_return_limit,
        ) if isinstance(item, dict) and item.get("symbol") else dict(item)
        for index, item in enumerate(warning_rows)
    ]
    decision_rows_raw = [
        dict(item)
        for item in strategy_snapshot.get("decision_queue", [])
        if isinstance(item, dict)
    ]
    snapshot_cluster = _cluster_from_strategy_snapshot(strategy_snapshot)
    cluster: dict[str, Any] = {"market_status": {}, "data_warning": ""}
    industry_top = snapshot_cluster.get("industry_top") or _gateway_rank_rows("board", top=8)
    concept_top = snapshot_cluster.get("concept_top") or _gateway_rank_rows("concept", top=8)
    sector_boards = _chain_heat_sector_rows()
    focus_stocks = _terminal_stock_pool_rows(
        range_columns,
        limit=_shell_stock_group_display_cap("focus_stocks"),
    )
    risk_stocks = _terminal_stock_pool_group_rows(
        range_columns,
        "risk_stocks",
        limit=_shell_stock_group_display_cap("risk_stocks"),
    )
    watch_stocks = _terminal_stock_pool_group_rows(
        range_columns,
        "watch_stocks",
        limit=_shell_stock_group_display_cap("watch_stocks"),
    )
    clue_stocks = _terminal_stock_pool_group_rows(
        range_columns,
        "clue_stocks",
        limit=_shell_stock_group_display_cap("clue_stocks"),
    )
    hot_rank_clues = _hot_rank_clue_rows(
        limit=_shell_stock_group_display_cap("clue_stocks"),
    )
    manual_clues = _manual_clue_rows(
        range_columns,
        limit=_shell_manual_clue_limit(),
        decision_enrich_limit=_shell_manual_clue_decision_limit(),
    )
    manual_attack_focus = _manual_clue_attack_focus_rows(manual_clues, focus_stocks)
    focus_stocks = _merge_stock_rows_by_symbol(manual_attack_focus + focus_stocks)
    manual_focus_count = len(manual_attack_focus)
    remaining_manual_clues = _remaining_manual_clues_after_attack_focus(manual_clues, manual_attack_focus)
    scored_raw = _merge_stock_rows_by_symbol(remaining_manual_clues + hot_rank_clues + clue_stocks + strategy_clues)
    scored = _enrich_scored_stock_rows(scored_raw, range_columns)
    for stock_rows in (
        all_etfs,
        focus_stocks,
        risk_stocks,
        watch_stocks,
        clue_stocks,
        hot_rank_clues,
        manual_clues,
        scored,
        sell_warnings,
    ):
        _fill_lazy_stock_range_returns(stock_rows, range_columns)
    focus_stocks_meta = _focus_stock_pool_meta(len(focus_stocks))
    if manual_focus_count:
        focus_stocks_meta["manual_attack_count"] = manual_focus_count
        focus_stocks_meta["source_collection"] = "terminal_stock_pool + terminal_manual_clues.attack_focus"
    for rows, lane in (
        (macro_indices, "quote_lane"),
        (all_etfs, "quote_lane"),
        (sector_boards, "board_lane"),
        (focus_stocks, "signal_lane"),
        (risk_stocks, "signal_lane"),
        (watch_stocks, "signal_lane"),
        (clue_stocks, "signal_lane"),
        (hot_rank_clues, "signal_lane"),
        (manual_clues, "signal_lane"),
    ):
        for row in rows:
            row["lane_status"] = sync_lanes.get(lane, {})
            row["freshness"] = row["lane_status"].get("freshness", "unknown")

    decision_queue = _build_trader_task_queue(
        decision_rows=decision_rows_raw,
        focus_stocks=focus_stocks,
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
            "major_indices": major_indices,
            "industry_etfs": industry_etfs,
            "all_etfs": all_etfs,
            "macro_indices": macro_indices,
            "sector_boards": sector_boards_shell,
            "buy_candidates": scored_shell,
            "focus_stocks": focus_stocks_shell,
            "risk_stocks": risk_stocks_shell,
            "watch_stocks": watch_stocks_shell,
        },
        "watchlist_groups_meta": {
            "major_indices": {
                "label": "大盘指数",
                "source_collection": "index_bars",
                "count": len(major_indices),
            },
            "industry_etfs": {
                "label": "行业ETF",
                "source_collection": "bars + quote_snapshots",
                "count": len(industry_etfs),
            },
            "all_etfs": {
                "label": "全量ETF",
                "source_collection": "strategy_snapshots.etf_analysis",
                "count": all_etfs_total,
                "review_count": len(all_etfs),
                "role": "all_market_etf_review_universe",
                "source": _text(etf_universe.get("source")) or "strategy_snapshot.etf_analysis",
                "source_counts": dict(etf_source_counts),
                "asset_class_counts": dict(etf_asset_class_counts),
                "as_of": _text(etf_universe.get("as_of")),
            },
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
                "source_collection": "terminal_manual_clues + hot_rank_clues + terminal_stock_pool.clue_stocks + strategy_snapshots.strategy_candidate",
                "count": len(scored),
                "role": "source_clue_only_not_entry",
                "manual_clues": len(remaining_manual_clues),
                "auto_hot_rank_clues": len(hot_rank_clues),
                "manual_attack_promoted": manual_focus_count,
                "empty_reason": "" if scored else "当前没有纯线索；已有硬技术的标的会进入盯盘池或买点池。",
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
        "watchlist": [],
        "etf_analysis": etf_analysis,
        "watchlist_range_columns": range_columns,
        "kline_cache_coverage": _kline_cache_coverage_shell_summary(),
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
            "freq": DEFAULT_TERMINAL_FREQ,
        },
        "legacy_url": "/legacy",
        "notices": notices,
    }


def _refresh_shell_cache_once(engine: Any) -> None:
    acquired = _SHELL_CACHE_LOCK.acquire(blocking=False)
    if not acquired:
        return
    try:
        refreshed_now = time.monotonic()
        payload = _build_shell_payload_uncached(engine)
        quote_watermark = _quote_snapshot_watermark()
        ttl_seconds = _shell_cache_ttl_seconds(payload)
        _SHELL_CACHE.update({
            "payload": dict(payload),
            "refreshed_at": refreshed_now,
            "expires_at": refreshed_now + ttl_seconds,
            "quote_watermark": quote_watermark,
        })
    except Exception:
        logger.exception("Workbench shell cache refresh failed")
    finally:
        _SHELL_CACHE_LOCK.release()


def _schedule_shell_cache_refresh(engine: Any) -> None:
    thread = threading.Thread(
        target=_refresh_shell_cache_once,
        args=(engine,),
        name="signals-shell-cache-refresh",
        daemon=True,
    )
    thread.start()


def _build_shell_payload(engine) -> Dict[str, Any]:
    now = time.monotonic()
    cached_payload = _SHELL_CACHE.get("payload")
    cached_quote_watermark = str(_SHELL_CACHE.get("quote_watermark") or "")
    if _shell_cache_usable(cached_payload, engine) and now < float(_SHELL_CACHE.get("expires_at") or 0):
        current_session: Optional[dict[str, Any]] = None
        try:
            current_session = _serialize_session(engine.get_status())
        except Exception:
            current_session = None
        return _payload_from_shell_cache(
            cached_payload,
            "hit",
            now,
            cached_quote_watermark,
            current_session=current_session,
        )

    _schedule_shell_cache_refresh(engine)
    if _shell_cache_usable(cached_payload, engine):
        current_session = None
        try:
            current_session = _serialize_session(engine.get_status())
        except Exception:
            current_session = None
        return _payload_from_shell_cache(
            cached_payload,
            "stale_refreshing",
            now,
            cached_quote_watermark,
            current_session=current_session,
        )
    return _build_shell_placeholder_payload("building", now, cached_quote_watermark)


def _strategy_snapshot_has_etf_analysis(snapshot: dict[str, Any]) -> bool:
    etf_analysis = snapshot.get("etf_analysis")
    if not isinstance(etf_analysis, dict):
        return False
    review_rows = etf_analysis.get("review_universe")
    return isinstance(review_rows, list) and len(review_rows) > 0


def _safe_strategy_snapshot() -> Dict[str, Any]:
    fallback_snapshot: dict[str, Any] = {}
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
                if _strategy_snapshot_has_etf_analysis(snapshot):
                    return snapshot
                fallback_snapshot = snapshot
    except Exception:
        pass
    try:
        snapshot = get_strategy_snapshot()
        return dict(snapshot) if isinstance(snapshot, dict) else fallback_snapshot
    except Exception as exc:
        if fallback_snapshot:
            fallback_snapshot.setdefault("read_model_source", "mongodb.strategy_snapshots_without_etf_analysis")
            return fallback_snapshot
        return {
            "daily_brief": {"summary": f"strategy_snapshot_error:{exc.__class__.__name__}"},
            "candidates": [],
            "warnings": [],
            "themes": [],
            "decision_queue": [],
            "strategy_kpis": {},
            "source_confidence": {"overall": 0, "sources": []},
        }


def _normalize_strategy_snapshot_for_shell(snapshot: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(snapshot) if isinstance(snapshot, dict) else {}
    brief = dict(normalized.get("daily_brief")) if isinstance(normalized.get("daily_brief"), dict) else {}
    summary = _text(brief.get("summary"))
    if summary:
        summary = re.sub(r"，先处理\s*\d+\s*个风险预警", "", summary)
        summary = summary.replace("确认买点复核", "买点池复核")
        brief["summary"] = summary
    if "risk_notes" in brief:
        brief["risk_notes"] = []
    normalized["daily_brief"] = brief
    return normalized


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


def _taxonomy_adjacent_chain_candidates(chain_id: str, node_id: str) -> list[dict[str, Any]]:
    chain = load_industry_chains().get(_text(chain_id)) or {}
    node = (chain.get("nodes_by_id") or {}).get(_text(node_id)) or {}
    if not node:
        return []
    rows: list[dict[str, Any]] = []
    relation_labels = {
        "upstream": "上游",
        "downstream": "下游",
    }
    for relation_type, rep_key in (("upstream", "upstream_representatives"), ("downstream", "downstream_representatives")):
        for rep in node.get(rep_key) or []:
            symbol, raw_code = _stock_symbol_from_code_or_name(rep.get("symbol"), rep.get("name"))
            if not symbol:
                continue
            rows.append({
                "symbol": symbol,
                "raw_code": raw_code or symbol.split(".", 1)[-1],
                "name": _text(rep.get("name")) or _stock_name(symbol),
                "source": "semantic_industry_chain_relation",
                "relation": _text(rep.get("relation")) or relation_labels.get(relation_type, relation_type),
                "representative_type": relation_type,
                "chain_relation_type": relation_type,
                "priority": int(rep.get("priority") or 0),
                "base_priority": int(rep.get("priority") or 0),
                "chain_id": chain.get("chain_id"),
                "chain_name": chain.get("name"),
                "node_id": node.get("node_id"),
                "node_name": node.get("name"),
                "layer": node.get("layer"),
                "stage": node.get("stage"),
                "confidence": 92,
                "source_note": _text(rep.get("source_note")) or f"{node.get('name')} {relation_labels.get(relation_type, relation_type)}代表",
                "evidence_sources": ["industry_chains.yaml", "semantic_industry_chain_relation"],
            })
    return rows


def _chain_rebuild_board_candidates(name: str, kind: str, limit: int = 20) -> list[dict[str, Any]]:
    query_name = _text(name)
    if not query_name:
        return []
    board_kind = "concept" if kind == "concept" else "industry"
    try:
        db = _mongo_db()
        mapping = db["source_board_chain_mappings"].find_one(
            {
                "kind": board_kind,
                "mapping_status": "mapped",
                "$or": [{"canonical_name": query_name}, {"raw_name": query_name}],
            },
            {"_id": 0},
            sort=[("trade_date", -1), ("updated_at", -1), ("confidence", -1)],
        ) or {}
    except Exception:
        return []
    chain_id = _text(mapping.get("chain_id"))
    node_id = _text(mapping.get("node_id"))
    if not chain_id or not node_id:
        return []
    current_chain = load_industry_chains().get(chain_id) or {}
    if node_id not in (current_chain.get("nodes_by_id") or {}):
        return []
    trade_date = _text(mapping.get("trade_date"))
    try:
        db = _mongo_db()
        rollup_query = {"market": "A", "chain_id": chain_id, "node_id": node_id}
        if trade_date:
            rollup_query["trade_date"] = trade_date
        rollup = db["chain_node_security_rollups"].find_one(
            rollup_query,
            {"_id": 0},
            sort=[("trade_date", -1), ("coverage_rank", 1), ("updated_at", -1)],
        ) or {}
        if not rollup and trade_date:
            rollup = db["chain_node_security_rollups"].find_one(
                {"market": "A", "chain_id": chain_id, "node_id": node_id},
                {"_id": 0},
                sort=[("trade_date", -1), ("coverage_rank", 1), ("updated_at", -1)],
            ) or {}
        top_rows = [item for item in (rollup.get("top_securities") or []) if isinstance(item, dict)]
        if not top_rows:
            top_rows = list(db["security_chain_memberships"].find(
                {"market": "A", "chain_id": chain_id, "node_id": node_id},
                {"_id": 0},
            ).sort([("trade_date", -1), ("is_primary_chain", -1), ("exposure_score", -1), ("confidence", -1)]).limit(limit))
    except Exception:
        top_rows = []
        rollup = {}
    rows: list[dict[str, Any]] = []
    seen_relation_keys: set[tuple[str, str]] = set()
    for index, item in enumerate(top_rows[:limit]):
        symbol, raw_code = _stock_symbol_from_code_or_name(item.get("raw_code") or item.get("symbol"), item.get("name"))
        if not symbol:
            continue
        seen_relation_keys.add((symbol.upper(), ""))
        representative_type = _text(item.get("representative_type")) or ("core" if item.get("is_primary_chain") or index < 3 else f"{board_kind}_constituent")
        rows.append({
            "symbol": symbol,
            "raw_code": raw_code or symbol.split(".", 1)[-1],
            "name": _text(item.get("name")) or _stock_name(symbol),
            "source": "chain_rebuild_rollup",
            "relation": " / ".join(part for part in [_text(mapping.get("chain_name")), _text(mapping.get("node_name"))] if part),
            "representative_type": representative_type,
            "priority": round(140 - index + _float(item.get("exposure_score"), 0), 3),
            "chain_id": chain_id,
            "chain_name": mapping.get("chain_name") or rollup.get("chain_name"),
            "node_id": node_id,
            "node_name": mapping.get("node_name") or rollup.get("node_name"),
            "layer": mapping.get("layer") or rollup.get("layer"),
            "stage": mapping.get("stage") or rollup.get("stage"),
            "confidence": item.get("confidence") or mapping.get("confidence"),
            "exposure_score": item.get("exposure_score"),
            "representative_priority": item.get("representative_priority"),
            "representative_relation": item.get("representative_relation"),
            "taxonomy_representative": bool(item.get("taxonomy_representative")),
            "is_primary_chain": item.get("is_primary_chain"),
            "source_note": item.get("source_note") or "盘后全局产业链重塑优先映射",
            "evidence_sources": item.get("evidence_sources") or mapping.get("evidence_sources") or [],
            "mapping_source": "source_board_chain_mappings",
            "trade_date": trade_date or rollup.get("trade_date"),
        })
    for item in _taxonomy_adjacent_chain_candidates(chain_id, node_id):
        symbol = _text(item.get("symbol")).upper()
        relation_type = _text(item.get("chain_relation_type"))
        if not symbol or (symbol, relation_type) in seen_relation_keys:
            continue
        seen_relation_keys.add((symbol, relation_type))
        rows.append({
            **item,
            "mapping_source": "industry_chains.yaml",
            "trade_date": trade_date or rollup.get("trade_date"),
        })
    return rows


def _representative_rank_for_summary(value: Any) -> int:
    return {"core": 4, "elastic": 3, "upstream": 2, "downstream": 1}.get(_text(value), 0)


def _taxonomy_representative_position_summary(symbol: str) -> dict[str, Any]:
    normalized, raw_code = _stock_symbol_from_code_or_name(symbol)
    normalized = _text(normalized).upper()
    raw_code = _text(raw_code or symbol).upper().split(".", 1)[-1]
    if not normalized and not raw_code:
        return {}

    rows: list[dict[str, Any]] = []
    for chain in load_industry_chains().values():
        chain_id = _text(chain.get("chain_id"))
        chain_name = _text(chain.get("name"))
        for node in chain.get("nodes") or []:
            node_id = _text(node.get("node_id"))
            for representative_type, rep_key in (("core", "core_representatives"), ("elastic", "elastic_representatives")):
                for rep in node.get(rep_key) or []:
                    rep_symbol, rep_raw_code = _stock_symbol_from_code_or_name(rep.get("symbol"), rep.get("name"))
                    rep_symbol = _text(rep_symbol).upper()
                    rep_raw_code = _text(rep_raw_code).upper()
                    if not rep_symbol and not rep_raw_code:
                        continue
                    if normalized and rep_symbol != normalized:
                        continue
                    if raw_code and rep_raw_code and rep_raw_code != raw_code:
                        continue
                    priority = int(rep.get("priority") or 0)
                    rows.append({
                        "chain_id": chain_id,
                        "chain": chain_name,
                        "chain_name": chain_name,
                        "node_id": node_id,
                        "node": _text(node.get("name")),
                        "node_name": _text(node.get("name")),
                        "role": _text(rep.get("relation")) or _text(node.get("name")),
                        "layer": _text(node.get("layer")),
                        "stage": _text(node.get("stage")),
                        "source": "industry_chains.yaml",
                        "source_note": _text(rep.get("source_note")) or "代表标的静态映射",
                        "confidence": "taxonomy_representative",
                        "representative_type": representative_type,
                        "representative_priority": priority,
                        "representative_relation": _text(rep.get("relation")),
                        "taxonomy_representative": True,
                        "is_primary_chain": True,
                        "related_chains": [],
                    })
    if not rows:
        return {}

    rows.sort(
        key=lambda row: (
            _representative_rank_for_summary(row.get("representative_type")),
            int(row.get("representative_priority") or 0),
        ),
        reverse=True,
    )
    primary = dict(rows[0])
    primary["related_chains"] = [
        row.get("chain_name")
        for row in rows[1:]
        if row.get("chain_name") and row.get("chain_name") != primary.get("chain_name")
    ][:3]
    return primary


def _membership_should_yield_to_taxonomy_representative(membership: dict[str, Any], taxonomy: dict[str, Any]) -> bool:
    if not membership or not taxonomy:
        return False
    if (
        _text(membership.get("chain_id")) == _text(taxonomy.get("chain_id"))
        and _text(membership.get("node_id")) == _text(taxonomy.get("node_id"))
    ):
        return False
    membership_key = (
        _representative_rank_for_summary(membership.get("representative_type")),
        int(membership.get("representative_priority") or 0),
    )
    taxonomy_key = (
        _representative_rank_for_summary(taxonomy.get("representative_type")),
        int(taxonomy.get("representative_priority") or 0),
    )
    if membership.get("taxonomy_representative"):
        return taxonomy_key >= membership_key
    if not _text(membership.get("representative_type")):
        return taxonomy_key >= (3, 85)
    return taxonomy_key > membership_key


def _stock_chain_membership_rows(symbol: str, limit: int = 12) -> list[dict[str, Any]]:
    raw_symbol = _text(symbol).upper()
    raw_code = raw_symbol.split(".", 1)[-1] if "." in raw_symbol else raw_symbol
    if not raw_code:
        return []
    try:
        db = _mongo_db()
        query = {
            "market": "A",
            "$or": [
                {"symbol": raw_symbol},
                {"raw_code": raw_code},
                {"symbol": symbol},
            ],
        }
        first = db["security_chain_memberships"].find_one(
            query,
            {"_id": 0},
            sort=[("trade_date", -1), ("is_primary_chain", -1), ("exposure_score", -1), ("confidence", -1)],
        ) or {}
    except Exception:
        return []
    if not first:
        return []
    trade_date = _text(first.get("trade_date"))
    if not trade_date:
        return [first]
    try:
        cursor = db["security_chain_memberships"].find(
            {**query, "trade_date": trade_date},
            {"_id": 0},
        ).sort([("is_primary_chain", -1), ("exposure_score", -1), ("confidence", -1)]).limit(limit)
        rows = [row for row in cursor if isinstance(row, dict)]
        return rows or [first]
    except Exception:
        return [first]


def _source_board_names(row: dict[str, Any], limit: int = 6) -> list[str]:
    names: list[str] = []
    for item in row.get("source_boards") or []:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


def _stock_chain_related_positions(rows: list[dict[str, Any]], primary: dict[str, Any]) -> list[dict[str, Any]]:
    primary_key = (_text(primary.get("chain_id")), _text(primary.get("node_id")))
    related: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (_text(row.get("chain_id")), _text(row.get("node_id")))
        if not key[0] or not key[1] or key == primary_key or key in seen:
            continue
        seen.add(key)
        related.append({
            "chain_id": key[0],
            "chain_name": _text(row.get("chain_name")),
            "node_id": key[1],
            "node_name": _text(row.get("node_name")),
            "confidence": row.get("confidence"),
            "exposure_score": row.get("exposure_score"),
            "membership_type": _text(row.get("membership_type")),
            "source_board_names": _source_board_names(row),
        })
    return related[:6]


def _stock_chain_board_driver_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    board_rows: dict[str, dict[str, Any]] = {}
    trade_date = _text((rows[0] if rows else {}).get("trade_date"))
    try:
        db = _mongo_db()
    except Exception:
        db = None
    for row in rows:
        for source_board in row.get("source_boards") or []:
            if not isinstance(source_board, dict):
                continue
            name = _text(source_board.get("name"))
            source_board_id = _text(source_board.get("source_board_id"))
            if not name and not source_board_id:
                continue
            key = source_board_id or name
            candidate = {
                "name": name,
                "source_board_id": source_board_id,
                "kind": _text(source_board.get("kind")),
                "source": _text(source_board.get("source")),
                "chain_id": _text(row.get("chain_id")),
                "chain_name": _text(row.get("chain_name")),
                "node_id": _text(row.get("node_id")),
                "node_name": _text(row.get("node_name")),
                "mapping_confidence": source_board.get("confidence") or row.get("confidence"),
                "membership_type": _text(row.get("membership_type")),
            }
            existing = board_rows.get(key)
            if not existing or _float(candidate.get("mapping_confidence")) > _float(existing.get("mapping_confidence")):
                board_rows[key] = candidate

    for candidate in board_rows.values():
        catalog = {}
        if db is not None:
            try:
                clauses = []
                if candidate.get("source_board_id"):
                    clauses.append({"source_board_id": candidate.get("source_board_id")})
                if candidate.get("name"):
                    clauses.extend([
                        {"canonical_name": candidate.get("name")},
                        {"raw_name": candidate.get("name")},
                    ])
                query = {"$or": clauses} if clauses else {}
                if trade_date:
                    query = {**query, "trade_date": trade_date}
                catalog = db["source_board_catalog"].find_one(
                    query,
                    {"_id": 0, "change_pct": 1, "rank_idx": 1, "source": 1, "kind": 1, "trade_date": 1, "canonical_name": 1},
                    sort=[("trade_date", -1), ("updated_at", -1)],
                ) or {}
            except Exception:
                catalog = {}
        if catalog:
            candidate["change_pct"] = _float(catalog.get("change_pct"))
            candidate["rank_idx"] = catalog.get("rank_idx")
            candidate["catalog_trade_date"] = _text(catalog.get("trade_date"))
            candidate["kind"] = _text(catalog.get("kind")) or candidate.get("kind")
            candidate["source"] = _text(catalog.get("source")) or candidate.get("source")
        else:
            candidate["change_pct"] = None
        candidate["is_non_chain_theme"] = bool(non_chain_reason(candidate.get("name")))

    def sort_key(item: dict[str, Any]) -> tuple[float, float, float, float]:
        change = item.get("change_pct")
        has_change = 1.0 if change is not None else 0.0
        concept_bonus = 0.15 if _text(item.get("kind")) == "concept" else 0.0
        chain_penalty = -0.4 if item.get("is_non_chain_theme") else 0.0
        return (
            has_change,
            _float(change, 0.0) + concept_bonus + chain_penalty,
            _float(item.get("mapping_confidence"), 0.0),
            1.0 if _text(item.get("kind")) == "concept" else 0.0,
        )

    candidates = list(board_rows.values())
    candidates.sort(key=sort_key, reverse=True)
    return candidates[:12]


def _select_chain_driver(candidates: list[dict[str, Any]], chain_id: str) -> dict[str, Any]:
    chain = _text(chain_id)
    if not chain:
        return {}
    for item in candidates:
        if _text(item.get("chain_id")) == chain and item.get("change_pct") is not None:
            return item
    for item in candidates:
        if _text(item.get("chain_id")) == chain:
            return item
    return {}


def _stock_chain_driver_summary(active: dict[str, Any], chain_driver: dict[str, Any]) -> str:
    active_name = _text(active.get("name"))
    chain_name = _text(chain_driver.get("name"))
    if not active_name:
        return ""
    active_change = active.get("change_pct")
    active_text = f"{active_name} {float(active_change):+.2f}%" if active_change is not None else active_name
    if chain_driver and chain_name and chain_name != active_name:
        chain_change = chain_driver.get("change_pct")
        chain_text = f"{chain_name} {float(chain_change):+.2f}%" if chain_change is not None else chain_name
        return f"当日最强板块是 {active_text}；主链相关驱动是 {chain_text}。"
    return f"当日最强板块是 {active_text}。"


def _stock_chain_membership_summary(symbol: str) -> dict[str, Any]:
    rows = _stock_chain_membership_rows(symbol)
    if not rows:
        return {}
    row = rows[0]
    related_positions = _stock_chain_related_positions(rows, row)
    driver_candidates = _stock_chain_board_driver_candidates(rows)
    active_driver = driver_candidates[0] if driver_candidates else {}
    chain_driver = _select_chain_driver(driver_candidates, _text(row.get("chain_id")))
    return {
        "chain_id": _text(row.get("chain_id")),
        "chain": _text(row.get("chain_name")),
        "chain_name": _text(row.get("chain_name")),
        "node_id": _text(row.get("node_id")),
        "node": _text(row.get("node_name")),
        "node_name": _text(row.get("node_name")),
        "role": _text(row.get("role") or row.get("representative_relation") or row.get("node_name")),
        "layer": _text(row.get("layer")),
        "stage": _text(row.get("stage")),
        "source": "security_chain_memberships",
        "source_note": _text(row.get("source_note")) or "盘后全局产业链重塑主归属",
        "confidence": row.get("confidence"),
        "exposure_score": row.get("exposure_score"),
        "membership_type": _text(row.get("membership_type")),
        "reviewed_override": bool(row.get("reviewed_override")),
        "is_primary_chain": bool(row.get("is_primary_chain")),
        "trade_date": _text(row.get("trade_date")),
        "representative_type": _text(row.get("representative_type")),
        "representative_priority": row.get("representative_priority"),
        "representative_relation": _text(row.get("representative_relation")),
        "taxonomy_representative": bool(row.get("taxonomy_representative")),
        "source_board_names": _source_board_names(row),
        "concept_driver_candidates": driver_candidates,
        "active_driver_concept": active_driver,
        "primary_chain_driver": chain_driver,
        "driver_summary": _stock_chain_driver_summary(active_driver, chain_driver),
        "secondary_chain_positions": related_positions,
        "related_chains": [item["chain_name"] for item in related_positions if item.get("chain_name")][:3],
    }


def _stock_chain_candidate_inputs(chain_position: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    chain_id = _text(chain_position.get("chain_id"))
    node_id = _text(chain_position.get("node_id"))
    if not chain_id or not node_id:
        return []
    trade_date = _text(chain_position.get("trade_date"))
    try:
        db = _mongo_db()
        rollup_query = {"market": "A", "chain_id": chain_id, "node_id": node_id}
        if trade_date:
            rollup_query["trade_date"] = trade_date
        rollup = db["chain_node_security_rollups"].find_one(
            rollup_query,
            {"_id": 0},
            sort=[("trade_date", -1), ("coverage_rank", 1), ("updated_at", -1)],
        ) or {}
        if not rollup and trade_date:
            rollup = db["chain_node_security_rollups"].find_one(
                {"market": "A", "chain_id": chain_id, "node_id": node_id},
                {"_id": 0},
                sort=[("trade_date", -1), ("coverage_rank", 1), ("updated_at", -1)],
            ) or {}
        top_rows = [item for item in (rollup.get("top_securities") or []) if isinstance(item, dict)]
        if not top_rows:
            top_rows = list(db["security_chain_memberships"].find(
                {"market": "A", "chain_id": chain_id, "node_id": node_id},
                {"_id": 0},
            ).sort([("trade_date", -1), ("is_primary_chain", -1), ("exposure_score", -1), ("confidence", -1)]).limit(limit))
    except Exception:
        top_rows = []
        rollup = {}
    rows: list[dict[str, Any]] = []
    seen_relation_keys: set[tuple[str, str]] = set()
    chain_name = _text(chain_position.get("chain_name") or chain_position.get("chain") or rollup.get("chain_name"))
    node_name = _text(chain_position.get("node_name") or chain_position.get("node") or rollup.get("node_name"))
    for index, item in enumerate(top_rows[:limit]):
        symbol, raw_code = _stock_symbol_from_code_or_name(item.get("raw_code") or item.get("symbol"), item.get("name"))
        if not symbol:
            continue
        seen_relation_keys.add((symbol.upper(), ""))
        representative_type = _text(item.get("representative_type")) or ("core" if item.get("is_primary_chain") or index < 3 else "chain_constituent")
        rows.append({
            "symbol": symbol,
            "raw_code": raw_code or symbol.split(".", 1)[-1],
            "name": _text(item.get("name")) or _stock_name(symbol),
            "source": "security_chain_memberships",
            "relation": " / ".join(part for part in [chain_name, node_name] if part),
            "representative_type": representative_type,
            "priority": round(140 - index + _float(item.get("exposure_score"), 0), 3),
            "chain_id": chain_id,
            "chain_name": chain_name,
            "node_id": node_id,
            "node_name": node_name,
            "layer": _text(chain_position.get("layer") or rollup.get("layer")),
            "stage": _text(chain_position.get("stage") or rollup.get("stage")),
            "confidence": item.get("confidence") or chain_position.get("confidence"),
            "exposure_score": item.get("exposure_score"),
            "representative_priority": item.get("representative_priority"),
            "representative_relation": item.get("representative_relation"),
            "taxonomy_representative": bool(item.get("taxonomy_representative")),
            "is_primary_chain": item.get("is_primary_chain"),
            "source_note": item.get("source_note") or "盘后全局产业链重塑同节点标的",
            "evidence_sources": item.get("evidence_sources") or [],
            "trade_date": trade_date or _text(rollup.get("trade_date")),
        })
    for item in _taxonomy_adjacent_chain_candidates(chain_id, node_id):
        symbol = _text(item.get("symbol")).upper()
        relation_type = _text(item.get("chain_relation_type"))
        if not symbol or (symbol, relation_type) in seen_relation_keys:
            continue
        seen_relation_keys.add((symbol, relation_type))
        rows.append({
            **item,
            "source": "semantic_industry_chain_relation",
            "trade_date": trade_date or _text(rollup.get("trade_date")),
        })
    return rows


def _stock_chain_context(chain_position: dict[str, Any]) -> dict[str, Any]:
    if not chain_position:
        return {}
    chain_name = _text(chain_position.get("chain_name") or chain_position.get("chain"))
    node_name = _text(chain_position.get("node_name") or chain_position.get("node") or chain_position.get("role"))
    candidates = _stock_chain_candidate_inputs(chain_position)
    candidate_groups = _candidate_groups(candidates, heat_value=chain_position.get("exposure_score")) if candidates else {}
    return {
        **chain_position,
        "chain_name": chain_name,
        "node_name": node_name,
        "mapping_status": _text(chain_position.get("source")) or "security_chain_memberships",
        "mapping_chain": {
            "chain_id": _text(chain_position.get("chain_id")),
            "chain_name": chain_name,
            "node_id": _text(chain_position.get("node_id")),
            "node_name": node_name,
            "layer": _text(chain_position.get("layer")),
            "stage": _text(chain_position.get("stage")),
            "confidence": chain_position.get("confidence"),
            "source_note": chain_position.get("source_note"),
        },
        "candidate_groups": candidate_groups,
        "focus_stocks_preview": _flatten_candidate_groups(candidate_groups, limit=6) if candidate_groups else [],
        "data_truth": {
            "collection": "security_chain_memberships",
            "as_of": _text(chain_position.get("trade_date")),
            "mapping_status": _text(chain_position.get("source")) or "security_chain_memberships",
        },
    }


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
        from signals.sync.modules.stock_daily import _replace_daily_docs_batch, _sync_one_stock

        now = _sync_now()
        docs = _sync_one_stock(
            code,
            (now - timedelta(days=365 * 5)).strftime("%Y%m%d"),
            now.strftime("%Y%m%d"),
        )
        if not docs:
            return False
        db = _mongo_db()
        written = _replace_daily_docs_batch(
            db["bars"],
            db["sync_log"],
            {code: docs},
            source="concept_carrier_preheat",
        )
        return bool(written.get(code))
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
        from signals.sync.modules.minute_change import recalculate_minute_change_pct
        from signals.sync.modules.stock_minute import _sync_one_minute

        db = _mongo_db()
        docs = _sync_one_minute(code, minute_freq, db=db)
        if not docs:
            return False
        docs = recalculate_minute_change_pct(db, code, docs, asset_type="stock")
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
        if ok:
            _clear_symbol_payload_cache(symbol, "stock", canonical)

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


def _has_chain_backed_candidates(candidates: list[dict[str, Any]]) -> bool:
    chain_sources = {"chain_rebuild_rollup", "semantic_industry_chain"}
    for item in candidates:
        if (
            _text(item.get("source")) in chain_sources
            and _text(item.get("chain_id"))
            and _text(item.get("node_id"))
        ):
            return True
    return False


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
        for item in _chain_rebuild_board_candidates(concept_name, "concept"):
            add(
                symbol=_text(item.get("symbol")),
                raw_code=_text(item.get("raw_code")),
                name=_text(item.get("name")),
                source=_text(item.get("source")),
                relation=_text(item.get("relation")),
                extra=item,
            )
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
                    "chain_relation_type": item.get("chain_relation_type"),
                    "source_note": item.get("source_note"),
                    "confidence": item.get("confidence"),
                    "hit_terms": item.get("hit_terms"),
                    "evidence_sources": item.get("evidence_sources"),
                },
            )
        seen_nodes: set[tuple[str, str]] = set()
        for item in list(candidates):
            chain_id = _text(item.get("chain_id"))
            node_id = _text(item.get("node_id"))
            if not chain_id or not node_id or (chain_id, node_id) in seen_nodes:
                continue
            seen_nodes.add((chain_id, node_id))
            for adjacent in _taxonomy_adjacent_chain_candidates(chain_id, node_id):
                add(
                    symbol=_text(adjacent.get("symbol")),
                    raw_code=_text(adjacent.get("raw_code")),
                    name=_text(adjacent.get("name")),
                    source=_text(adjacent.get("source")),
                    relation=_text(adjacent.get("relation")),
                    extra=adjacent,
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

    if not non_chain and not _has_chain_backed_candidates(candidates):
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
        "source_board_name": item.get("source_board_name"),
        "source_board_kind": item.get("source_board_kind"),
        "representative_type": item.get("representative_type"),
        "representative_priority": item.get("representative_priority"),
        "representative_relation": item.get("representative_relation"),
        "chain_relation_type": item.get("chain_relation_type"),
        "priority": item.get("priority"),
        "base_priority": item.get("base_priority"),
        "chain_id": item.get("chain_id"),
        "chain_name": item.get("chain_name"),
        "node_id": item.get("node_id"),
        "node_name": item.get("node_name"),
        "layer": item.get("layer"),
        "stage": item.get("stage"),
        "confidence": item.get("confidence"),
        "amount": item.get("amount"),
        "turnover_pct": item.get("turnover_pct"),
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
        "latest_signal": _signal_or_fallback({**report, **chart_report}, pd.DataFrame()),
        "key_levels": chart_report.get("key_levels") or ma_context.get("key_levels") or [],
        "style_switch": style_switch.suggestion if style_switch else "",
    }
    symbol = str(report.get("symbol") or "")
    day_change_mode = _a_day_change_mode()
    daily_day_change = None
    daily_df = pd.DataFrame()
    if day_change_mode == "daily_close":
        try:
            daily_df, _daily_source = _index_df(symbol, "daily")
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
        if daily_day_change is not None:
            summary.update({
                "day_change_pct": daily_day_change,
                "daily_change_pct": daily_day_change,
                "today_change_pct": daily_day_change,
                "gain_pct": daily_day_change,
                "day_change_source": daily_day_source,
                "day_change_mode": day_change_mode,
                "day_change_as_of": daily_as_of,
                "day_change_freq": "",
            })
        if latest_daily_close is not None:
            summary["latest_price"] = latest_daily_close
    if day_change_mode != "daily_close" or daily_day_change is None:
        summary.update(_shortest_realtime_day_change("index", symbol))
    summary = _apply_quote_overlay(summary, symbol)
    if summary.get("today_change_pct") is not None:
        summary["gain_pct"] = summary.get("today_change_pct")
    if (daily_df is None or daily_df.empty) and symbol:
        try:
            daily_df, _daily_source = _index_df(symbol, "daily")
        except Exception:
            daily_df = pd.DataFrame()
    summary.update(_index_ma_fields_from_daily_df(daily_df))
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
    day_change_mode = _a_day_change_mode()
    daily_day_change = None
    daily_df = pd.DataFrame()
    if day_change_mode == "daily_close":
        try:
            daily_df, _daily_source = _index_df(symbol, "daily")
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
        if daily_day_change is not None:
            summary.update({
                "day_change_pct": daily_day_change,
                "daily_change_pct": daily_day_change,
                "today_change_pct": daily_day_change,
                "gain_pct": daily_day_change,
                "day_change_source": daily_day_source,
                "day_change_mode": day_change_mode,
                "day_change_as_of": daily_as_of,
                "day_change_freq": "",
            })
        if latest_daily_close is not None:
            summary["latest_price"] = latest_daily_close
    if day_change_mode != "daily_close" or daily_day_change is None:
        summary.update(_shortest_realtime_day_change("index", symbol))
    summary = _apply_quote_overlay(summary, symbol)
    if summary.get("today_change_pct") is not None:
        summary["gain_pct"] = summary.get("today_change_pct")
    if (daily_df is None or daily_df.empty) and symbol:
        try:
            daily_df, _daily_source = _index_df(symbol, "daily")
        except Exception:
            daily_df = pd.DataFrame()
    summary.update(_index_ma_fields_from_daily_df(daily_df))
    chain_position = _stock_chain_position_summary(symbol)
    if chain_position:
        trade_role = _trade_role_for_stock_summary(chain_position)
        chain_text = " · ".join(
            value
            for value in [_text(chain_position.get("chain")), _text(chain_position.get("node") or chain_position.get("role"))]
            if value
        )
        trade_role_label = {
            "watch": "盯盘观察",
            "clue": "线索池",
        }.get(trade_role, "线索池")
        summary.update({
            "chain_position": chain_position,
            "chain_context": _stock_chain_context(chain_position),
            "setup_mode": trade_role,
            "setup_mode_label": trade_role_label,
            "trade_role": trade_role,
            "trade_role_label": trade_role_label,
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
    summary = {
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
    minute_change = _shortest_realtime_day_change("industry", name)
    if minute_change:
        summary.update(minute_change)
    return summary


def _stock_chain_position_summary(symbol: str) -> dict[str, Any]:
    membership = _stock_chain_membership_summary(symbol)
    taxonomy = _taxonomy_representative_position_summary(symbol)
    if _membership_should_yield_to_taxonomy_representative(membership, taxonomy):
        secondary_positions = []
        if membership:
            secondary_positions.append({
                "chain_id": membership.get("chain_id"),
                "chain_name": membership.get("chain_name") or membership.get("chain"),
                "node_id": membership.get("node_id"),
                "node_name": membership.get("node_name") or membership.get("node"),
                "confidence": membership.get("confidence"),
                "exposure_score": membership.get("exposure_score"),
                "membership_type": "broad_primary_membership",
                "source_board_names": membership.get("source_board_names") or [],
            })
            secondary_positions.extend(membership.get("secondary_chain_positions") or [])
        related_chains = [item.get("chain_name") for item in secondary_positions if item.get("chain_name")]
        driver_candidates = membership.get("concept_driver_candidates") or []
        active_driver = (driver_candidates[0] if driver_candidates else {}) if isinstance(driver_candidates, list) else {}
        chain_driver = _select_chain_driver(driver_candidates if isinstance(driver_candidates, list) else [], taxonomy.get("chain_id"))
        return {
            **taxonomy,
            "secondary_chain_positions": secondary_positions[:6],
            "related_chains": list(dict.fromkeys(related_chains))[:3],
            "concept_driver_candidates": driver_candidates if isinstance(driver_candidates, list) else [],
            "active_driver_concept": active_driver,
            "primary_chain_driver": chain_driver,
            "driver_summary": _stock_chain_driver_summary(active_driver, chain_driver),
            "source_note": _text(taxonomy.get("source_note")) or "当前静态产业链映射覆盖旧重塑结果",
            "stale_membership_source": "security_chain_memberships",
            "stale_membership_chain_id": membership.get("chain_id"),
            "stale_membership_node_id": membership.get("node_id"),
            "stale_membership_trade_date": membership.get("trade_date"),
        }
    if membership:
        return membership
    if taxonomy:
        return taxonomy
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
    if phase in {"accelerating", "warming"}:
        return "watch"
    return "clue"


def _stock_summary_trade_read(chain_position: dict[str, Any], role: str) -> str:
    chain = " · ".join(
        value
        for value in [_text(chain_position.get("chain")), _text(chain_position.get("node") or chain_position.get("role"))]
        if value
    )
    if role == "watch":
        return f"{chain or '盯盘观察'}：只有产业链/图表背景，不代表真实持仓；等买点质量和均线确认后再进入买点池。"
    return f"{chain or '线索池'}：当前不在买点池，先看图表证据，不作为执行买点。"


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
    minute_change = {}
    daily_day_change = None
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
        if not minute_change:
            summary.update({
                "day_change_pct": daily_day_change,
                "daily_change_pct": daily_day_change,
                "today_change_pct": daily_day_change,
                "day_change_source": daily_day_source,
                "day_change_mode": day_change_mode,
                "day_change_as_of": daily_as_of,
                "day_change_freq": "",
            })
        if latest_daily_close is not None and not minute_change:
            summary["latest_price"] = latest_daily_close
    if day_change_mode != "daily_close" or daily_day_change is None:
        minute_change = _shortest_realtime_day_change("stock", symbol)
    if minute_change:
        summary.update(minute_change)
    summary.update(_latest_daily_trading_values(symbol, chart))
    summary = _apply_quote_overlay(summary, symbol)
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
        trade_role_label = {
            "watch": "盯盘观察",
            "clue": "线索池",
        }.get(trade_role, "线索池")
        summary.update({
            "chain_position": chain_position,
            "chain_context": _stock_chain_context(chain_position),
            "setup_mode": trade_role,
            "setup_mode_label": trade_role_label,
            "trade_role": trade_role,
            "trade_role_label": trade_role_label,
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
    chart = _chart_from_df(df, symbol=str(report.get("symbol") or name), freq=requested_freq, source=source, live_render=True)
    chart = _mark_chart_readiness(chart, kind="index", requested_freq=requested_freq)
    ma_signal = _current_timeframe_ma_signal(chart, requested_freq)
    chart["signals"] = [
        *(chart.get("signals") if isinstance(chart.get("signals"), list) else []),
        *_index_report_chart_signals(report, chart, requested_freq),
        *([ma_signal] if ma_signal else []),
    ]
    chart = _merge_signal_pool_into_chart(chart, str(report.get("symbol") or name), requested_freq, kind="index")
    plan = _plan_for_index(engine, name)
    summary = _summary_from_index(report, chart)
    ma_state = ma_signal.get("ma_state") if isinstance(ma_signal, dict) else {}
    if isinstance(ma_state, dict) and ma_state:
        summary["latest_signal"] = ma_signal.get("signal_type") or ma_state.get("signal_type")
        summary["conclusion"] = ma_state.get("summary") or summary.get("conclusion")
        summary["current_timeframe_ma"] = ma_state

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
        "summary": summary,
        "signals": chart.get("signals", []),
        "plan": plan,
        "review": _review_context(engine, "index", name),
        "trade": _trade_context(None),
        "analysis_target": "",
        "candidate_stocks": [],
        "custom_signal_count": 0,
        "direct_custom_signal_count": 0,
        "visible_custom_signal_count": 0,
        "hidden_custom_signal_count": 0,
        "available_custom_signal_freqs": [],
        "hidden_reasons": ["index_has_no_direct_custom_signal"],
        "related_custom_signals": [],
    }


async def _build_static_index_target(name: str, symbol: str, freq: str) -> Dict[str, Any]:
    requested_freq = _canonical_freq(freq)
    df, source = _index_df(symbol, requested_freq)
    chart = _chart_from_df(df, symbol=symbol, freq=requested_freq, source=source, live_render=True)
    chart = _mark_chart_readiness(chart, kind="index", requested_freq=requested_freq)
    signal_context = _cached_static_index_signal_context(name, symbol)
    ma_signal = _current_timeframe_ma_signal(chart, requested_freq)
    if signal_context:
        chart["signals"] = [
            *(chart.get("signals") if isinstance(chart.get("signals"), list) else []),
            *_index_report_chart_signals(signal_context, chart, requested_freq),
        ]
    if ma_signal:
        chart["signals"] = [
            *(chart.get("signals") if isinstance(chart.get("signals"), list) else []),
            ma_signal,
        ]
    chart = _merge_signal_pool_into_chart(chart, symbol, requested_freq, kind="index")
    summary = _summary_from_static_index(name, symbol, chart)
    chart_signals = chart.get("signals") if isinstance(chart.get("signals"), list) else []
    context_signal = _signal_or_fallback(signal_context, df) if signal_context else ""
    chart_report_signal = next(
        (
            _text(item.get("type") or item.get("signal_type"))
            for item in reversed(chart_signals)
            if isinstance(item, dict) and _text(item.get("source")) == "signals.index_report"
        ),
        "",
    )
    if signal_context:
        for key in ("daily_trend", "f30_trend", "f15_trend"):
            if _text(signal_context.get(key)):
                summary[key] = signal_context[key]
    summary["latest_signal"] = (
        (ma_signal.get("signal_type") if ma_signal else "")
        or context_signal
        or summary.get("latest_signal")
        or chart_report_signal
        or _ma_signal_from_df(df)
    )
    ma_state = ma_signal.get("ma_state") if isinstance(ma_signal, dict) else {}
    if isinstance(ma_state, dict) and ma_state:
        summary["conclusion"] = ma_state.get("summary") or summary.get("conclusion")
        summary["current_timeframe_ma"] = ma_state
    try:
        engine = _ensure_engine()
    except Exception:
        engine = None
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
        "candidate_stocks": [],
        "custom_signal_count": 0,
        "direct_custom_signal_count": 0,
        "visible_custom_signal_count": 0,
        "hidden_custom_signal_count": 0,
        "available_custom_signal_freqs": [],
        "hidden_reasons": ["index_has_no_direct_custom_signal"],
        "related_custom_signals": [],
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
        minute_change = _shortest_realtime_day_change("industry", heat_target_label)
        heat_change = minute_change.get("day_change_pct") if minute_change else latest_heat.get("change_pct")
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
                "latest_signal": _text(latest_heat.get("trading_signal") or latest_heat.get("latest_signal")) or ("行业热度观察" if heat_ready else "行业热度缓存未就绪"),
                "latest_price": minute_change.get("latest_price") if minute_change else (chart.get("ohlcv", [{}])[-1].get("close", 0) if chart.get("ohlcv") else 0),
                "conclusion": "行业图形来自东财板块快照 change_pct 重采样，不是成分股价格K线。" if heat_ready else "行业分钟热度缓存未就绪。",
                "gain_pct": heat_change,
                "day_change_pct": heat_change,
                "daily_change_pct": heat_change,
                "today_change_pct": minute_change.get("today_change_pct") if minute_change else None,
                "day_change_source": minute_change.get("day_change_source") if minute_change else ("board_heat_ticks" if latest_heat.get("change_pct") is not None else ""),
                "day_change_mode": minute_change.get("day_change_mode") if minute_change else _a_day_change_mode(),
                "day_change_as_of": minute_change.get("day_change_as_of") if minute_change else _date_text(latest_heat.get("trade_minute")),
                "day_change_freq": minute_change.get("day_change_freq") if minute_change else requested_freq,
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

    async def fallback_to_concept_carrier(reason: str) -> Optional[Dict[str, Any]]:
        if non_chain:
            return None
        fallback = _preview_carrier(carrier_candidates)
        if not fallback:
            return None
        symbol, raw_code = _candidate_symbol_fields(fallback)
        if not symbol:
            return None
        payload = await _build_stock_target(symbol, raw_code, requested_freq)
        stock_title = payload.get("summary", {}).get("title") or fallback.get("name") or symbol
        payload["target"] = {
            **payload.get("target", {}),
            "kind": "concept",
            "label": name,
            "symbol": symbol,
            **_target_time_fields(symbol=symbol, source=payload.get("chart", {}).get("meta", {}).get("source", "")),
            "requested_freq": requested_freq,
            "carrier_kind": "stock",
            "carrier_symbol": symbol,
            "mapping_status": "mapped",
            "unmapped_reason": "",
            "fallback_reason": reason,
            **_target_diagnostics("stock", symbol, requested_freq),
        }
        payload["summary"] = {
            **payload.get("summary", {}),
            "title": name,
            "subtitle": f"概念承接 -> {stock_title}({symbol})",
            "conclusion": f"{name} 概念热度分钟缓存未就绪，已用产业链代表股 {stock_title} 承接图形复核。",
            "representatives": representatives,
            "candidate_groups": candidate_groups,
            "carrier": _representative_payload(fallback),
            "fallback_reason": reason,
            "mapping_chain": {
                "query": name,
                "concepts": [name],
                "industries": related[:5],
                "mapping_status": "carrier_fallback",
                "candidate_count": len(carrier_candidates),
            },
        }
        payload["candidate_groups"] = candidate_groups
        payload["candidate_stocks"] = ordered_candidates
        payload["analysis_target"] = symbol
        payload["hidden_reasons"] = ["concept_heat_chart_fallback_to_carrier"]
        return payload

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
        minute_change = _shortest_realtime_day_change("concept", heat_target_label)
        heat_change = minute_change.get("day_change_pct") if minute_change else (latest_heat.get("change_pct") or heat_value)
        if not heat_ready:
            carrier_payload = await fallback_to_concept_carrier("concept_heat_not_ready")
            if carrier_payload is not None:
                carrier_payload["data_truth"] = data_truth
                carrier_payload["related_custom_signals"] = related_custom_signals
                return carrier_payload
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
                "latest_signal": _text(latest_heat.get("trading_signal") or latest_heat.get("latest_signal")) or ("概念热度观察" if heat_ready else "概念热度缓存未就绪"),
                "latest_price": minute_change.get("latest_price") if minute_change else (chart.get("ohlcv", [{}])[-1].get("close", 0) if chart.get("ohlcv") else 0),
                "conclusion": "概念图形来自东财概念快照 change_pct 重采样，不是成分股价格K线。" if heat_ready else "概念分钟热度缓存未就绪。",
                "gain_pct": heat_change,
                "day_change_pct": heat_change,
                "daily_change_pct": heat_change,
                "today_change_pct": minute_change.get("today_change_pct") if minute_change else None,
                "day_change_source": minute_change.get("day_change_source") if minute_change else ("board_heat_ticks" if latest_heat.get("change_pct") is not None else ""),
                "day_change_mode": minute_change.get("day_change_mode") if minute_change else _a_day_change_mode(),
                "day_change_as_of": minute_change.get("day_change_as_of") if minute_change else _date_text(latest_heat.get("trade_minute")),
                "day_change_freq": minute_change.get("day_change_freq") if minute_change else requested_freq,
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
            payload["chart"] = _chart_from_df(df, symbol=carrier["symbol"], freq=requested_freq, source=source, live_render=True)
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
        chart = _chart_from_df(df, symbol=symbol, freq=requested_freq, source=source, live_render=True)
    elif requested_freq == "30min":
        df, source = _stock_df(symbol, requested_freq)
        chart = _chart_from_df(df, symbol=symbol, freq=requested_freq, source=source, live_render=True)
    else:
        df, source = _stock_df(symbol, requested_freq)
        chart = _chart_from_df(df, symbol=symbol, freq=requested_freq, source=source, live_render=True)
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
    spot_snapshot_fallback = False
    if requested_freq == "daily" and not _chart_has_ohlcv(chart):
        spot_df, spot_source = _etf_spot_snapshot_df(symbol)
        if not spot_df.empty:
            load_meta = {
                key: value
                for key, value in (chart.get("meta") or {}).items()
                if key.startswith("load_")
            }
            chart = _chart_from_df(spot_df, symbol=symbol, freq="daily", source=spot_source, live_render=False)
            meta = dict(chart.get("meta") or {})
            meta.update(load_meta)
            meta.update({
                "cache_status": "spot_only",
                "fallback_reason": "spot_snapshot_fallback",
                "not_ready_reason": "history_cache_missing",
                "history_cache_status": "missing",
                "spot_snapshot_only": True,
                "requested_freq": requested_freq,
                "effective_freq": "daily",
            })
            chart["meta"] = meta
            spot_snapshot_fallback = True
    chart = _mark_chart_readiness(chart, kind="stock", requested_freq=requested_freq)
    if spot_snapshot_fallback:
        meta = dict(chart.get("meta") or {})
        meta.update({
            "cache_status": "spot_only",
            "fallback_reason": "spot_snapshot_fallback",
            "not_ready_reason": "history_cache_missing",
            "history_cache_status": "missing",
            "spot_snapshot_only": True,
        })
        chart["meta"] = meta
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
    summary = _summary_from_stock(symbol, stock, chart)
    ma_acceptance = _latest_terminal_ma_acceptance(symbol)
    if ma_acceptance:
        summary["ma_acceptance"] = ma_acceptance
    return {
        "target": {
            "kind": "stock",
            "label": stock.get("name") or symbol,
            "symbol": symbol,
            **_target_time_fields(symbol=symbol, source=chart.get("meta", {}).get("source", "")),
            "requested_freq": requested_freq,
            "effective_freq": requested_freq,
            "available_freqs": UI_FREQS,
            "fallback_reason": chart.get("meta", {}).get("fallback_reason", ""),
            "not_ready_reason": chart.get("meta", {}).get("not_ready_reason", ""),
            **_target_diagnostics("stock", symbol, requested_freq),
        },
        "chart": chart,
        "summary": summary,
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
        cache_result = _remove_manual_clue_from_shell_cache({normalized, raw_code, symbol})
        return {
            "status": "ok",
            "symbol": normalized,
            "deleted": int(getattr(result, "deleted_count", 0) or 0),
            **cache_result,
        }

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
    freq: str = Query(DEFAULT_TERMINAL_FREQ, description="5min / 15min / 30min / daily / weekly"),
):
    return await run_in_threadpool(_cached_build_workbench_symbol_payload, symbol, kind, freq)


def _run_workbench_target_builder(builder: Any) -> Dict[str, Any]:
    return asyncio.run(builder)


def _build_workbench_symbol_payload(symbol: str, kind: str, freq: str) -> Dict[str, Any] | JSONResponse:
    engine = _ensure_engine()
    if not engine.is_ready() and kind in {"auto", "index"}:
        static_index = _resolve_static_index(symbol)
        if static_index is not None:
            return _run_workbench_target_builder(_build_static_index_target(static_index[0], static_index[1], freq))
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
        return _run_workbench_target_builder(_build_index_target(engine, resolved["label"], freq))
    if resolved["kind"] == "industry":
        return _run_workbench_target_builder(_build_industry_target(engine, resolved["label"], freq))
    if resolved["kind"] == "concept":
        return _run_workbench_target_builder(_build_concept_target(engine, resolved["label"], freq))
    return _run_workbench_target_builder(_build_stock_target(resolved["label"], resolved["raw_code"], freq))


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
    filtered["terminal"] = build_backtest_terminal(filtered)
    return filtered
