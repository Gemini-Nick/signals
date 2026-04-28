# -*- coding: utf-8 -*-
"""
A股分钟线同步 — 活跃标的 5M/15M/30M 增量同步

数据源: Sina/Tencent 公共分钟线；东财分钟线可显式开启为最后兜底
策略: 增量同步，仅白名单 + 最近入池标的（~200只上限）
频率: 工作日 16:00
注意: 公共分钟线返回滚动窗口数据；盘中只取尾窗并写入新 bar
"""
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import akshare as ak
import pandas as pd
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from ..proxy import em_proxy
from ..retry import sync_retry
from .minute_sources import fetch_public_minute, stock_to_market_symbol

logger = logging.getLogger("signals.sync.stock_minute")

_CALL_INTERVAL = float(os.getenv("STOCK_MINUTE_CALL_INTERVAL", "0.5"))
_PUBLIC_TIMEOUT = float(os.getenv("STOCK_MINUTE_TIMEOUT", "5"))
_MINUTE_FREQS = ["5分钟", "15分钟", "30分钟"]
_ENABLE_EASTMONEY_FALLBACK = os.getenv("STOCK_MINUTE_EASTMONEY_FALLBACK", "false").lower() == "true"
_DEFAULT_PRIORITY_CODES = "688802,300575"
_DEFAULT_TAIL_COUNTS = {"5分钟": 240, "15分钟": 160, "30分钟": 120}


def _int_env(name: str, default: int, *, min_value: int = 1, max_value: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _worker_count() -> int:
    return _int_env("STOCK_MINUTE_WORKERS", 3, min_value=1, max_value=6)


def _tail_count_for_freq(freq: str) -> int:
    suffix_map = {"5分钟": "5", "15分钟": "15", "30分钟": "30"}
    default = _DEFAULT_TAIL_COUNTS.get(freq, 120)
    generic = _int_env("STOCK_MINUTE_TAIL_COUNT", default, min_value=40, max_value=500)
    suffix = suffix_map.get(freq)
    if not suffix:
        return generic
    return _int_env(f"STOCK_MINUTE_TAIL_COUNT_{suffix}", generic, min_value=40, max_value=500)


def _index_codes() -> set[str]:
    import config

    return {
        str(symbol).lower().replace("sh", "").replace("sz", "")
        for symbol in getattr(config, "INDEX_AK_CODES", {}).values()
    }


def _pure_a_code(symbol: object) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    pure = raw.split(".", 1)[-1] if "." in raw else raw
    pure = pure.replace("SH", "").replace("SZ", "").replace("BJ", "")
    if pure.isdigit() and len(pure) == 6:
        return pure
    return ""


def _iter_strategy_snapshot_symbols() -> list[str]:
    symbols: list[str] = []
    try:
        from signals.strategy.snapshot import get_strategy_snapshot

        snapshot = get_strategy_snapshot()
    except Exception:
        return symbols

    for key in ("candidates", "warnings", "decision_queue", "buy_candidates", "sell_warnings"):
        rows = snapshot.get(key) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for field in ("symbol", "code", "raw_code"):
                value = row.get(field)
                if value:
                    symbols.append(str(value))
            metadata = row.get("metadata")
            if isinstance(metadata, dict):
                for field in ("symbol", "code", "raw_code"):
                    value = metadata.get(field)
                    if value:
                        symbols.append(str(value))

    rows = snapshot.get("themes") or []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            for field in ("leader_symbol", "leader_code", "representative_symbol", "representative_code"):
                value = row.get(field)
                if value:
                    symbols.append(str(value))
    return symbols


def _iter_configured_extra_symbols() -> list[str]:
    symbols: list[str] = []
    raw = os.getenv("STOCK_MINUTE_EXTRA_CODES", "")
    for value in raw.replace(";", ",").split(","):
        value = value.strip()
        if value:
            symbols.append(value)
    try:
        from signals.core.concept_carriers import preferred_carrier_symbols

        symbols.extend(preferred_carrier_symbols())
    except Exception:
        pass
    return symbols


def _env_symbol_values(*names: str, default: str = "") -> list[str]:
    values: list[str] = []
    raw = default
    for name in names:
        configured = os.getenv(name, "")
        if configured.strip():
            raw = configured
            break
    for value in raw.replace(";", ",").split(","):
        value = value.strip()
        if value:
            values.append(value)
    return values


def _selection_cap() -> int:
    lane = os.getenv("SIGNALS_CURRENT_SYNC_LANE", "")
    market = os.getenv("SIGNALS_CURRENT_SYNC_MARKET", "")
    if lane == "signal_lane":
        if market == "A":
            return int(os.getenv("STOCK_MINUTE_SIGNAL_MAX_CODES", "72"))
        close_default = os.getenv("TERMINAL_REALTIME_STOCK_LIMIT", "72")
        return int(os.getenv("STOCK_MINUTE_CLOSE_MAX_CODES", close_default))
    if market == "A":
        return int(os.getenv("STOCK_MINUTE_SIGNAL_MAX_CODES", "72"))
    return int(os.getenv("STOCK_MINUTE_MAX_CODES", "200"))


def _select_symbols_with_priority(
    ordered: list[str],
    priority: set[str],
    max_symbols: int,
    *,
    pinned: set[str] | None = None,
    last_runs: dict[str, object] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    if max_symbols <= 0 or len(ordered) <= max_symbols:
        return ordered, []
    pinned = pinned or set()
    last_runs = last_runs or {}
    original_idx = {code: idx for idx, code in enumerate(ordered)}

    def stale_rank(code: str) -> float:
        value = last_runs.get(code)
        if value is None:
            return float("-inf")
        if hasattr(value, "timestamp"):
            return float(value.timestamp())
        try:
            parsed = pd.to_datetime(value, errors="coerce")
            if pd.isna(parsed):
                return float("-inf")
            return float(parsed.timestamp())
        except Exception:
            return float("-inf")

    pinned_ordered = [code for code in ordered if code in pinned]
    pinned_selected = pinned_ordered[:max_symbols]
    remaining = max_symbols - len(pinned_selected)
    rest = [code for code in ordered if code not in set(pinned_selected)]
    rest_sorted = sorted(
        rest,
        key=lambda code: (
            0 if code in priority else 1,
            stale_rank(code),
            original_idx.get(code, 0),
        ),
    )
    selected = [*pinned_selected, *rest_sorted[:remaining]]
    selected_set = set(selected)
    skipped = [
        {
            "symbol": code,
            "reason": "rotation_pending_priority" if code in priority else "rotation_pending",
            "next_due_hint": "stale-first signal_lane rotation",
        }
        for code in ordered
        if code not in selected_set
    ]
    return selected, skipped


def _latest_stock_minute_runs(db: Database, symbols: list[str]) -> dict[str, object]:
    if not symbols:
        return {}
    last_runs: dict[str, object] = {}
    try:
        docs = db["sync_log"].find(
            {"module": "stock_minute", "status": "ok", "symbol": {"$in": symbols}},
            {"symbol": 1, "last_run": 1},
        ).sort("last_run", -1)
    except Exception:
        return {}
    for doc in docs:
        symbol = _pure_a_code(doc.get("symbol"))
        if symbol and symbol not in last_runs:
            last_runs[symbol] = doc.get("last_run")
    return last_runs


def _get_active_symbols_with_meta(db: Database) -> tuple[list[str], dict]:
    """获取需要同步分钟线的活跃标的列表"""
    import config

    symbols: list[str] = []
    priority_symbols: set[str] = set()
    pinned_symbols: set[str] = set()
    source_counts: dict[str, int] = {}
    index_codes = _index_codes()

    def add(value: object, source: str, *, priority: bool = False, pinned: bool = False) -> None:
        code = _pure_a_code(value)
        if code in index_codes:
            return
        if priority and code:
            priority_symbols.add(code)
        if pinned and code:
            pinned_symbols.add(code)
            priority_symbols.add(code)
        if code and code not in symbols:
            symbols.append(code)
            source_counts[source] = source_counts.get(source, 0) + 1

    only_codes = os.getenv("STOCK_MINUTE_ONLY_CODES", "")
    if only_codes.strip():
        for symbol in only_codes.replace(";", ",").split(","):
            add(symbol, "only_codes", priority=True, pinned=True)
        return symbols, {
            "priority_symbols": symbols,
            "pinned_symbols": symbols,
            "skipped_symbols": [],
            "source_counts": {"only_codes": len(symbols)},
            "max_symbols": len(symbols),
            "rotation_enabled": False,
        }

    for symbol in _env_symbol_values(
        "STOCK_MINUTE_PRIORITY_CODES",
        "SIGNALS_PRIORITY_STOCK_CODES",
        default=os.getenv("STOCK_MINUTE_DEFAULT_PRIORITY_CODES", _DEFAULT_PRIORITY_CODES),
    ):
        add(symbol, "priority_codes", priority=True, pinned=True)

    terminal_pool = db["terminal_realtime_pool"].find_one(
        {"pool": "terminal_realtime", "market": "A"},
        {"stocks": 1},
        sort=[("updated_at", -1)],
    ) or {}
    for symbol in terminal_pool.get("stocks") or []:
        add(symbol, "terminal_realtime_pool", priority=True)

    for symbol in getattr(config, "WHITELIST", []):
        add(symbol, "whitelist", priority=True, pinned=True)

    for symbol in _iter_strategy_snapshot_symbols():
        add(symbol, "strategy_snapshot", priority=True)

    for symbol in _iter_configured_extra_symbols():
        add(symbol, "configured_extra")

    pool = db["market_pools"].find_one(
        {"pool": "active"},
        {"symbols": 1, "items": 1},
        sort=[("dt", -1), ("updated_at", -1)],
    ) or {}
    for symbol in pool.get("symbols") or []:
        add(symbol, "active_pool")
    for item in pool.get("items") or []:
        if isinstance(item, dict):
            add(item.get("symbol") or item.get("code"), "active_pool")

    for doc in db["signals"].find({}, {"symbol": 1}).sort("signal_date", -1).limit(300):
        add(doc.get("symbol"), "signals")

    # Keep recently requested/synced symbols warm so UI-visible names do not fall
    # out of the signal lane just because the active pool rotated.
    recent = db["sync_log"].find(
        {"module": {"$in": ["stock_minute", "stock_daily"]}, "status": "ok"},
        {"symbol": 1, "module": 1},
    ).sort("last_run", -1).limit(200)

    for doc in recent:
        add(doc.get("symbol"), f"recent_{doc.get('module') or 'sync'}")

    max_symbols = _selection_cap()
    last_runs = _latest_stock_minute_runs(db, symbols)
    selected, skipped = _select_symbols_with_priority(
        symbols,
        priority_symbols,
        max_symbols,
        pinned=pinned_symbols,
        last_runs=last_runs,
    )
    return selected, {
        "priority_symbols": [code for code in selected if code in priority_symbols],
        "pinned_symbols": [code for code in selected if code in pinned_symbols],
        "skipped_symbols": skipped,
        "source_counts": source_counts,
        "max_symbols": max_symbols,
        "candidate_count": len(symbols),
        "rotation_enabled": True,
        "rotation_policy": "pinned_then_priority_stale_first",
        "tier_counts": {
            "selected_pinned": sum(1 for code in selected if code in pinned_symbols),
            "selected_priority": sum(1 for code in selected if code in priority_symbols and code not in pinned_symbols),
            "selected_normal": sum(1 for code in selected if code not in priority_symbols),
            "candidate_priority": len(priority_symbols),
            "candidate_pinned": len(pinned_symbols),
        },
    }


def _get_active_symbols(db: Database) -> list:
    return _get_active_symbols_with_meta(db)[0]


def _sync_one_minute(code: str, freq: str, proxy_url: str = None, *, tail_count: int | None = None) -> list:
    """同步单只股票分钟线"""
    period_map = {"5分钟": "5", "15分钟": "15", "30分钟": "30"}
    period = period_map.get(freq, "30")
    source = "eastmoney"
    tail_count = tail_count or _tail_count_for_freq(freq)

    try:
        df, source = fetch_public_minute(
            stock_to_market_symbol(code),
            period,
            timeout=_PUBLIC_TIMEOUT,
            datalen=tail_count,
            count=tail_count,
        )
    except Exception as public_error:
        if not _ENABLE_EASTMONEY_FALLBACK:
            logger.warning("公共分钟线失败，跳过东财兜底 %s %s: %s", code, freq, public_error)
            return []
        logger.warning("公共分钟线失败，显式尝试东财兜底 %s %s: %s", code, freq, public_error)
        with em_proxy(proxy_url):
            df = ak.stock_zh_a_hist_min_em(
                symbol=code, period=period, adjust="qfq")

    if df is None or df.empty:
        return []

    docs = []
    for _, row in df.iterrows():
        docs.append({
            "dt": pd.to_datetime(row["时间"]),
            "meta": {"symbol": code, "freq": freq, "source": source, "market": "A"},
            "open": float(row["开盘"]),
            "high": float(row["最高"]),
            "low": float(row["最低"]),
            "close": float(row["收盘"]),
            "vol": int(row["成交量"]) if pd.notna(row["成交量"]) else 0,
            "amount": int(float(row["成交额"])) if pd.notna(row["成交额"]) else 0,
        })
    return docs


def _insert_new_minute_docs(bars_col, code: str, freq: str, docs: list[dict]) -> dict:
    if not docs:
        return {"written": 0, "bar_count": 0, "last_dt": None}

    latest_before = bars_col.find_one(
        {"meta.symbol": code, "meta.freq": freq},
        {"dt": 1},
        sort=[("dt", -1)],
    )
    latest_dt = latest_before.get("dt") if latest_before else None
    new_docs = [doc for doc in docs if latest_dt is None or doc["dt"] > latest_dt]
    written = 0
    if new_docs:
        result = bars_col.insert_many(new_docs, ordered=False)
        written = len(result.inserted_ids)
    return {
        "written": written,
        "inserted": written,
        "skipped_existing": len(docs) - len(new_docs),
        "bar_count": len(docs),
        "last_dt": docs[-1]["dt"],
        "latest_before": latest_dt,
    }


@sync_retry
def sync_stock_minute(db: Database, proxy_url: str = None) -> dict:
    """
    A 股分钟线增量同步。

    仅同步白名单 + 最近活跃标的的 5M、15M 和 30M 数据。
    公共分钟线返回滚动窗口；盘中只拉尾窗并插入新 bar，避免删整段重插。
    """
    bars_col = db["bars"]
    sync_col = db["sync_log"]

    symbols, selection_meta = _get_active_symbols_with_meta(db)
    logger.info(f"分钟线同步: {len(symbols)} 只活跃标的")

    workers = _worker_count()
    tail_counts = {freq: _tail_count_for_freq(freq) for freq in _MINUTE_FREQS}
    total_written = 0
    total_skipped_existing = 0
    empty = 0
    errors = []
    tasks = [(code, freq) for code in symbols for freq in _MINUTE_FREQS]

    def sync_one(code: str, freq: str) -> dict:
        started = time.monotonic()
        docs = _sync_one_minute(code, freq, proxy_url, tail_count=tail_counts[freq])
        time.sleep(_CALL_INTERVAL)
        if not docs:
            sync_col.update_one(
                {"_id": f"stock_minute:{code}:{freq}"},
                {"$set": {
                    "module": "stock_minute",
                    "symbol": code,
                    "freq": freq,
                    "last_run": naive_market_now("A"),
                    "status": "empty",
                    "incremental": True,
                    "write_mode": "insert_new",
                    "tail_count": tail_counts[freq],
                    "elapsed": round(time.monotonic() - started, 3),
                }},
                upsert=True,
            )
            return {"code": code, "freq": freq, "status": "empty", "written": 0, "skipped_existing": 0}

        write_result = _insert_new_minute_docs(bars_col, code, freq, docs)
        sync_col.update_one(
            {"_id": f"stock_minute:{code}:{freq}"},
            {"$set": {
                "module": "stock_minute",
                "symbol": code,
                "freq": freq,
                "last_dt": write_result["last_dt"],
                "last_run": naive_market_now("A"),
                "status": "ok",
                "bar_count": write_result["bar_count"],
                "written": write_result["written"],
                "inserted": write_result["inserted"],
                "skipped_existing": write_result["skipped_existing"],
                "incremental": True,
                "write_mode": "insert_new",
                "tail_count": tail_counts[freq],
                "latest_before": write_result.get("latest_before"),
                "source": docs[-1].get("meta", {}).get("source"),
                "elapsed": round(time.monotonic() - started, 3),
            }},
            upsert=True,
        )
        return {"code": code, "freq": freq, "status": "ok", **write_result}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(sync_one, code, freq): (code, freq) for code, freq in tasks}
        for future in as_completed(future_map):
            code, freq = future_map[future]
            try:
                result = future.result()
                if result.get("status") == "empty":
                    empty += 1
                total_written += int(result.get("written") or 0)
                total_skipped_existing += int(result.get("skipped_existing") or 0)
            except Exception as e:
                errors.append((code, freq, str(e)))
                sync_col.update_one(
                    {"_id": f"stock_minute:{code}:{freq}"},
                    {"$set": {
                        "module": "stock_minute",
                        "symbol": code,
                        "freq": freq,
                        "last_run": naive_market_now("A"),
                        "status": "error",
                        "error": str(e)[:300],
                        "incremental": True,
                        "write_mode": "insert_new",
                        "tail_count": tail_counts.get(freq),
                    }},
                    upsert=True,
                )

    skipped_symbols = selection_meta.get("skipped_symbols") or []
    sync_col.update_one(
        {"_id": "stock_minute:selection:_meta"},
        {"$set": {
            "module": "stock_minute",
            "status": "ok" if not skipped_symbols else "partial",
            "last_run": naive_market_now("A"),
            "selected_symbols": symbols,
            "priority_symbols": selection_meta.get("priority_symbols") or [],
            "pinned_symbols": selection_meta.get("pinned_symbols") or [],
            "skipped_symbols": skipped_symbols[:80],
            "skipped_count": len(skipped_symbols),
            "source_counts": selection_meta.get("source_counts") or {},
            "max_symbols": selection_meta.get("max_symbols"),
            "candidate_count": selection_meta.get("candidate_count"),
            "rotation_enabled": selection_meta.get("rotation_enabled", False),
            "rotation_policy": selection_meta.get("rotation_policy", ""),
            "tier_counts": selection_meta.get("tier_counts") or {},
            "workers": workers,
            "tail_counts": tail_counts,
            "incremental": True,
            "write_mode": "insert_new",
            "planned_calls": len(tasks),
            "empty_calls": empty,
            "failed_calls": len(errors),
            "written": total_written,
            "skipped_existing": total_skipped_existing,
        }},
        upsert=True,
    )

    logger.info(
        "分钟线完成: %d calls, inserted=%d skipped_existing=%d empty=%d failed=%d, %d cap跳过",
        len(tasks), total_written, total_skipped_existing, empty, len(errors), len(skipped_symbols),
    )
    return {
        "inserted": total_written,
        "written": total_written,
        "skipped_existing": total_skipped_existing,
        "errors": len(errors),
        "empty": empty,
        "selected": len(symbols),
        "priority": len(selection_meta.get("priority_symbols") or []),
        "skipped": len(skipped_symbols),
        "skipped_symbols": skipped_symbols[:20],
        "workers": workers,
        "tail_counts": tail_counts,
        "planned_calls": len(tasks),
        "incremental": True,
        "write_mode": "insert_new",
    }
