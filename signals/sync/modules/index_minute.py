# -*- coding: utf-8 -*-
"""
指数分钟线同步 — 宏观观察指数 5M/15M/30M

数据源: Sina 公共分钟线；Tencent 指数单位不一致，仅显式开启时兜底；东财可显式开启为最后兜底
策略: 尾部 upsert，刷新实时未收盘 K 线并覆盖旧的错误来源缓存
频率: 工作日 16:00
"""
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import akshare as ak
import pandas as pd
from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.macro_universe import macro_a_index_codes
from signals.core.market_time import naive_market_now, to_market_naive
from ..proxy import em_proxy
from ..retry import sync_retry
from .minute_sources import fetch_public_minute

logger = logging.getLogger("signals.sync.index_minute")
_ENABLE_EASTMONEY_FALLBACK = os.getenv("INDEX_MINUTE_EASTMONEY_FALLBACK", "false").lower() == "true"
_PUBLIC_TIMEOUT = float(os.getenv("INDEX_MINUTE_TIMEOUT", "5"))
_CALL_INTERVAL = float(os.getenv("INDEX_MINUTE_CALL_INTERVAL", "0.2"))
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
    return _int_env("INDEX_MINUTE_WORKERS", 3, min_value=1, max_value=6)


def _tail_count_for_freq(freq: str) -> int:
    suffix_map = {"5分钟": "5", "15分钟": "15", "30分钟": "30"}
    default = _DEFAULT_TAIL_COUNTS.get(freq, 120)
    generic = _int_env("INDEX_MINUTE_TAIL_COUNT", default, min_value=40, max_value=500)
    suffix = suffix_map.get(freq)
    if not suffix:
        return generic
    return _int_env(f"INDEX_MINUTE_TAIL_COUNT_{suffix}", generic, min_value=40, max_value=500)


def _minute_providers() -> tuple[str, ...]:
    configured = os.getenv("INDEX_MINUTE_PROVIDERS", "").strip()
    if configured:
        providers = tuple(item.strip().lower() for item in configured.replace(";", ",").split(",") if item.strip())
        if providers:
            return providers
    if os.getenv("INDEX_MINUTE_TENCENT_FALLBACK", "false").lower() in {"1", "true", "yes", "on"}:
        return ("sina", "tencent")
    return ("sina",)


def _upsert_tail_docs(col, symbol: str, freq: str, docs: list[dict]) -> dict:
    if not docs:
        return {"written": 0, "inserted": 0, "modified": 0, "matched": 0, "upserted": 0, "skipped_existing": 0}
    ops = []
    for doc in docs:
        selector = {"meta.symbol": symbol, "meta.freq": freq, "dt": doc["dt"]}
        ops.append(UpdateOne(selector, {"$set": dict(doc)}, upsert=True))
    result = col.bulk_write(ops, ordered=False)
    upserted = len(getattr(result, "upserted_ids", {}) or {})
    modified = int(getattr(result, "modified_count", 0) or 0)
    matched = int(getattr(result, "matched_count", 0) or 0)
    return {
        "written": upserted + modified,
        "inserted": upserted,
        "modified": modified,
        "matched": matched,
        "upserted": upserted,
        "skipped_existing": max(0, matched - modified),
    }


def _write_index_docs(db: Database, symbol: str, freq: str, docs: list[dict]) -> dict:
    if not docs:
        return {
            "inserted": 0,
            "modified": 0,
            "written": 0,
            "compat_inserted": 0,
            "compat_modified": 0,
            "compat_written": 0,
            "skipped_existing": 0,
            "compat_skipped_existing": 0,
            "bar_count": 0,
        }
    prepared = []
    for doc in docs:
        item = dict(doc)
        item["meta"] = {**item.get("meta", {}), "symbol": symbol, "freq": freq, "asset_type": "index", "market": "A"}
        prepared.append(item)
    primary = _upsert_tail_docs(db["index_bars"], symbol, freq, prepared)
    compat = _upsert_tail_docs(db["bars"], symbol, freq, prepared)
    return {
        "inserted": primary["inserted"],
        "modified": primary["modified"],
        "written": primary["written"],
        "compat_inserted": compat["inserted"],
        "compat_modified": compat["modified"],
        "compat_written": compat["written"],
        "skipped_existing": primary["skipped_existing"],
        "compat_skipped_existing": compat["skipped_existing"],
        "bar_count": len(prepared),
    }


def _fetch_index_docs(
    db: Database,
    name: str,
    symbol: str,
    freq: str,
    period: str,
    proxy_url: str | None,
    tail_count: int,
) -> tuple[list[dict], str]:
    pure_code = symbol.replace("sh", "").replace("sz", "")
    try:
        df, source = fetch_public_minute(
            symbol,
            period,
            providers=_minute_providers(),
            timeout=_PUBLIC_TIMEOUT,
            datalen=tail_count,
            count=tail_count,
            db=db,
            endpoint="index_minute",
        )
    except Exception as public_error:
        if not _ENABLE_EASTMONEY_FALLBACK:
            logger.warning("公共指数分钟线失败，跳过东财兜底 %s %s: %s", symbol, freq, public_error)
            return [], ""
        logger.warning("公共指数分钟线失败，显式尝试东财兜底 %s %s: %s", symbol, freq, public_error)
        source = "eastmoney"
        with em_proxy(proxy_url):
            fetch_index_minute = getattr(ak, "index_zh_a_hist_min_em", None)
            if fetch_index_minute is None:
                fetch_index_minute = getattr(ak, "stock_zh_index_hist_min_em", None)
            if fetch_index_minute is None:
                raise AttributeError("akshare has no index minute kline API")
            df = fetch_index_minute(symbol=pure_code, period=period)

    if df is None or df.empty:
        logger.warning("  ✗ %s %s: 无数据", name, freq)
        return [], source

    docs = []
    dt_col = "时间" if "时间" in df.columns else "datetime"
    for _, row in df.iterrows():
        dt = to_market_naive(row[dt_col], market="A", symbol=symbol, source=source)
        if dt is None:
            continue
        docs.append({
            "dt": dt,
            "meta": {"symbol": symbol, "freq": freq, "asset_type": "index", "source": source, "market": "A"},
            "open": float(row["开盘"]),
            "high": float(row["最高"]),
            "low": float(row["最低"]),
            "close": float(row["收盘"]),
            "vol": int(row["成交量"]) if pd.notna(row["成交量"]) else 0,
            "amount": int(float(row["成交额"])) if pd.notna(row.get("成交额", 0)) else 0,
        })
    return docs, source


def _sync_a_index_minute(db: Database, ak_codes: dict,
                         proxy_url: str = None) -> dict:
    """A 股指数分钟线"""
    sync_col = db["sync_log"]
    inserted = 0
    compat_inserted = 0
    modified = 0
    compat_modified = 0
    written_total = 0
    compat_written_total = 0
    skipped_existing = 0
    errors = []
    empty = 0

    period_map = {"5分钟": "5", "15分钟": "15", "30分钟": "30"}
    tail_counts = {freq: _tail_count_for_freq(freq) for freq in period_map}
    workers = _worker_count()
    tasks = [(name, symbol, freq, period) for name, symbol in ak_codes.items() for freq, period in period_map.items()]

    def sync_one(name: str, symbol: str, freq: str, period: str) -> dict:
        started = time.monotonic()
        docs, source = _fetch_index_docs(db, name, symbol, freq, period, proxy_url, tail_counts[freq])
        time.sleep(_CALL_INTERVAL)
        if not docs:
            sync_col.update_one(
                {"_id": f"index_minute:{symbol}:{freq}"},
                {"$set": {
                    "module": "index_minute",
                    "symbol": symbol,
                    "freq": freq,
                    "last_run": naive_market_now("A"),
                    "status": "empty",
                    "incremental": True,
                    "write_mode": "upsert_tail",
                    "providers": list(_minute_providers()),
                    "tail_count": tail_counts[freq],
                    "elapsed": round(time.monotonic() - started, 3),
                }},
                upsert=True,
            )
            return {
                "status": "empty",
                "inserted": 0,
                "modified": 0,
                "written": 0,
                "compat_inserted": 0,
                "compat_modified": 0,
                "compat_written": 0,
                "skipped_existing": 0,
            }

        written = _write_index_docs(db, symbol, freq, docs)
        sync_col.update_one(
            {"_id": f"index_minute:{symbol}:{freq}"},
            {"$set": {
                "module": "index_minute",
                "symbol": symbol,
                "freq": freq,
                "last_dt": docs[-1]["dt"],
                "last_run": naive_market_now("A"),
                "status": "ok",
                "bar_count": written["bar_count"],
                "inserted": written["inserted"],
                "modified": written["modified"],
                "written": written["written"],
                "compat_inserted": written["compat_inserted"],
                "compat_modified": written["compat_modified"],
                "compat_written": written["compat_written"],
                "skipped_existing": written["skipped_existing"],
                "source": source,
                "incremental": True,
                "write_mode": "upsert_tail",
                "providers": list(_minute_providers()),
                "tail_count": tail_counts[freq],
                "elapsed": round(time.monotonic() - started, 3),
            }},
            upsert=True,
        )
        logger.info("  ✓ %s %s: written=%d inserted=%d modified=%d skipped_existing=%d", name, freq, written["written"], written["inserted"], written["modified"], written["skipped_existing"])
        return {"status": "ok", **written}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(sync_one, *task): task for task in tasks}
        for future in as_completed(future_map):
            name, symbol, freq, _period = future_map[future]
            try:
                result = future.result()
                if result.get("status") == "empty":
                    empty += 1
                inserted += int(result.get("inserted") or 0)
                compat_inserted += int(result.get("compat_inserted") or 0)
                modified += int(result.get("modified") or 0)
                compat_modified += int(result.get("compat_modified") or 0)
                written_total += int(result.get("written") or 0)
                compat_written_total += int(result.get("compat_written") or 0)
                skipped_existing += int(result.get("skipped_existing") or 0)
            except Exception as exc:
                errors.append({"name": name, "symbol": symbol, "freq": freq, "error": str(exc)[:240]})
                logger.error("  ✗ %s %s: %s", name, freq, exc)

    return {
        "inserted": inserted,
        "modified": modified,
        "written": written_total,
        "compat_inserted": compat_inserted,
        "compat_modified": compat_modified,
        "compat_written": compat_written_total,
        "skipped_existing": skipped_existing,
        "errors": len(errors),
        "empty": empty,
        "workers": workers,
        "tail_counts": tail_counts,
        "planned_calls": len(tasks),
        "incremental": True,
        "write_mode": "upsert_tail",
        "providers": list(_minute_providers()),
    }


@sync_retry(max_attempts=5, min_wait=3)
def sync_index_minute(db: Database, proxy_url: str = None) -> dict:
    """指数分钟线全量同步"""
    logger.info("指数分钟线同步: A股指数 5M/15M/30M")
    ak_codes = macro_a_index_codes()
    result = _sync_a_index_minute(db, ak_codes, proxy_url)
    status = "ok" if result["errors"] == 0 and result["empty"] == 0 else "partial"
    now = naive_market_now("A")
    db["sync_log"].update_one(
        {"_id": "index_minute:A:_meta"},
        {"$set": {
            "module": "index_minute",
            "market": "A",
            "last_run": now,
            "updated_at": now,
            "status": status,
            "planned_calls": result.get("planned_calls", 0),
            "failed_calls": result.get("errors", 0),
            "empty_calls": result.get("empty", 0),
            "inserted": result.get("inserted", 0),
            "modified": result.get("modified", 0),
            "written": result.get("written", 0),
            "skipped_existing": result.get("skipped_existing", 0),
            "tail_counts": result.get("tail_counts", {}),
            "result": result,
            "error_msg": "" if status == "ok" else "index_minute_partial",
            "degraded_reason": "" if status == "ok" else "empty_or_failed_index_minute_calls",
            "write_mode": "upsert_tail",
            "providers": list(_minute_providers()),
        }},
        upsert=True,
    )

    logger.info(
        "指数分钟线完成: written=%d inserted=%d modified=%d skipped_existing=%d failed=%d",
        result["written"],
        result["inserted"],
        result["modified"],
        result["skipped_existing"],
        result["errors"],
    )
    return result
