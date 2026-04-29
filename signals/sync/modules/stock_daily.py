# -*- coding: utf-8 -*-
"""
A股日线同步 — ~5000 只股票增量同步

数据源: AKShare stock_zh_a_hist（东财接口）
策略: 增量同步，从 sync_log.last_dt 开始拉增量
频率: 工作日 16:30
"""
import logging
import math
import os
import atexit
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout, as_completed
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd
import requests
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from ..proxy import em_proxy
from ..provider_limits import ProviderCoolingDown, provider_call, providers_all_cooling_down
from ..retry import sync_retry
from ..task_context import get_task_env
from .daily_sources import fetch_tencent_daily

logger = logging.getLogger("signals.sync.stock_daily")

# Keep the default serial. AKShare's Eastmoney daily path can native-crash when
# multiple MiniRacer initializations happen in parallel on macOS.
_BATCH_WORKERS = max(1, int(os.getenv("STOCK_DAILY_WORKERS", "1")))
# 每只股票间隔（秒），避免被东财限速
_CALL_INTERVAL = float(os.getenv("STOCK_DAILY_CALL_INTERVAL", "0.3"))
_PROVIDER_TIMEOUT_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="stock-daily-provider")
atexit.register(_PROVIDER_TIMEOUT_POOL.shutdown, wait=False, cancel_futures=True)
_DEFAULT_PRIORITY_CODES = "688802,300575"
_PROGRESS_META_ID = "stock_daily:progress:_meta"
_STOCK_DAILY_PROVIDER_ENDPOINTS = (
    ("tencent", "stock_daily"),
    ("eastmoney", "stock_daily_hist"),
    ("sina", "stock_daily"),
)
_EM_SPOT_CLIST_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
_EM_SPOT_CLIST_FIELDS = "f2,f5,f6,f12,f15,f16,f17,f18"
_EM_SPOT_CLIST_FS = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"
_EM_SPOT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Connection": "close",
}
_SPOT_BATCH_LOCK = threading.Lock()
_SPOT_BATCH_CACHE: dict[str, pd.DataFrame] = {}


def _shard_count() -> int:
    try:
        return max(1, int(get_task_env("STOCK_DAILY_SHARD_COUNT", "1") or "1"))
    except (TypeError, ValueError):
        return 1


def _shard_index() -> int:
    try:
        return max(0, int(get_task_env("STOCK_DAILY_SHARD_INDEX", "0") or "0"))
    except (TypeError, ValueError):
        return 0


def _shard_key() -> str:
    return (get_task_env("STOCK_DAILY_SHARD_KEY", "") or "").strip() or "all"


def _progress_meta_id(shard_key: str | None = None) -> str:
    key = shard_key or _shard_key()
    if key and key != "all":
        return f"stock_daily:progress:{key}"
    return _PROGRESS_META_ID


def _apply_code_shard(codes: list[str]) -> tuple[list[str], dict[str, int | str]]:
    count = _shard_count()
    index = min(_shard_index(), count - 1)
    key = _shard_key()
    if count <= 1:
        return codes, {"shard_key": key, "shard_index": index, "shard_count": count, "global_total": len(codes)}
    shard_codes = [code for position, code in enumerate(codes) if position % count == index]
    return shard_codes, {"shard_key": key, "shard_index": index, "shard_count": count, "global_total": len(codes)}


def _provider_timeout() -> float:
    return float(os.getenv("STOCK_DAILY_PROVIDER_TIMEOUT", "12"))


def _progress_interval() -> int:
    return max(1, int(os.getenv("STOCK_DAILY_PROGRESS_INTERVAL", "25")))


def _write_batch_symbols() -> int:
    return max(1, int(os.getenv("STOCK_DAILY_WRITE_BATCH_SYMBOLS", "20")))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _batch_today_min_hour() -> int:
    try:
        return max(0, min(23, int(os.getenv("STOCK_DAILY_BATCH_TODAY_MIN_HOUR", "15"))))
    except (TypeError, ValueError):
        return 15


def _batch_prev_close_tolerance() -> float:
    try:
        return max(0.0, float(os.getenv("STOCK_DAILY_BATCH_PREV_CLOSE_TOLERANCE", "0.02")))
    except (TypeError, ValueError):
        return 0.02


def _spot_batch_page_size() -> int:
    try:
        # Eastmoney accepts larger pz values but still returns at most 100 rows.
        return max(50, min(100, int(os.getenv("STOCK_DAILY_SPOT_BATCH_PAGE_SIZE", "100"))))
    except (TypeError, ValueError):
        return 100


def _spot_batch_timeout() -> float:
    try:
        return max(3.0, float(os.getenv("STOCK_DAILY_SPOT_BATCH_TIMEOUT", "10")))
    except (TypeError, ValueError):
        return 10.0


def _coerce_last_dt(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value:
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d")
        except ValueError:
            try:
                return pd.to_datetime(value).to_pydatetime()
            except Exception:
                return None
    return None


def _latest_daily_dates_by_symbol(db: Database, codes: list[str]) -> dict[str, datetime]:
    if not codes:
        return {}
    try:
        rows = db["bars"].aggregate([
            {"$match": {"meta.freq": "日线", "meta.symbol": {"$in": codes}}},
            {"$group": {"_id": "$meta.symbol", "latest_dt": {"$max": "$dt"}}},
        ])
        return {
            str(row.get("_id")): row.get("latest_dt")
            for row in rows
            if row.get("_id") and isinstance(row.get("latest_dt"), datetime)
        }
    except Exception as exc:
        logger.debug("读取 bars 日线最新日期失败: %s", exc)
        return {}


def _stock_daily_providers_all_cooling(db: Database | None) -> bool:
    try:
        return providers_all_cooling_down(db, _STOCK_DAILY_PROVIDER_ENDPOINTS)
    except Exception:
        return False


def _write_daily_docs_batch(bars_col, sync_col, docs_by_code: dict[str, list]) -> dict[str, int]:
    """Write several symbols in one Mongo round trip and update per-symbol cursors."""
    docs_by_code = {code: docs for code, docs in docs_by_code.items() if docs}
    if not docs_by_code:
        return {}
    all_docs = [doc for docs in docs_by_code.values() for doc in docs]
    symbols = list(docs_by_code.keys())
    dts = [doc["dt"] for doc in all_docs]
    existing_keys: set[tuple[str, datetime]] = set()
    try:
        for item in bars_col.find(
            {
                "meta.symbol": {"$in": symbols},
                "meta.freq": "日线",
                "dt": {"$in": dts},
            },
            {"dt": 1, "meta.symbol": 1},
        ):
            symbol = str((item.get("meta") or {}).get("symbol") or "")
            dt = item.get("dt")
            if symbol and isinstance(dt, datetime):
                existing_keys.add((symbol, dt))
    except Exception as exc:
        logger.debug("批量查询已有日线失败，继续尝试写入: %s", exc)

    new_docs = [
        doc for doc in all_docs
        if (str((doc.get("meta") or {}).get("symbol") or ""), doc.get("dt")) not in existing_keys
    ]
    written_by_code = {code: 0 for code in docs_by_code}
    if new_docs:
        result = bars_col.insert_many(new_docs, ordered=False)
        inserted_count = len(getattr(result, "inserted_ids", []) or [])
        for doc in new_docs[:inserted_count]:
            symbol = str((doc.get("meta") or {}).get("symbol") or "")
            if symbol in written_by_code:
                written_by_code[symbol] += 1

    now = naive_market_now("A")
    for code, docs in docs_by_code.items():
        last = docs[-1]["dt"]
        sync_col.update_one(
            {"_id": f"stock_daily:{code}"},
            {"$set": {
                "module": "stock_daily",
                "symbol": code,
                "last_dt": last,
                "last_run": now,
                "status": "ok",
                "bar_count": len(docs),
                "written": written_by_code.get(code, 0),
            }},
            upsert=True,
        )
    return written_by_code


def _write_progress(
    sync_col,
    *,
    status: str,
    scope: str,
    total: int,
    processed: int,
    inserted: int,
    skipped: int,
    errors_count: int,
    deferred_count: int = 0,
    latest_symbol: str = "",
    latest_status: str = "",
    latest_written: int = 0,
    started_at: datetime | None = None,
    shard_key: str | None = None,
    shard_index: int = 0,
    shard_count: int = 1,
    global_total: int | None = None,
) -> None:
    now = naive_market_now("A")
    start = started_at or now
    elapsed_seconds = max(0.0, (now - start).total_seconds())
    inserted_per_min = round(inserted / (elapsed_seconds / 60), 2) if elapsed_seconds > 0 else 0.0
    processed_per_min = round(processed / (elapsed_seconds / 60), 2) if elapsed_seconds > 0 else 0.0
    progress_pct = round(processed / total * 100, 2) if total else 0
    key = shard_key or _shard_key()
    sync_col.update_one(
        {"_id": _progress_meta_id(key)},
        {"$set": {
            "module": "stock_daily",
            "status": status,
            "scope": scope,
            "shard_key": key,
            "shard_index": shard_index,
            "shard_count": shard_count,
            "global_total": global_total if global_total is not None else total,
            "total": total,
            "processed": processed,
            "remaining": max(0, total - processed),
            "inserted": inserted,
            "skipped": skipped,
            "errors": errors_count,
            "deferred": deferred_count,
            "latest_symbol": latest_symbol,
            "latest_status": latest_status,
            "latest_written": latest_written,
            "started_at": start,
            "elapsed_seconds": round(elapsed_seconds, 2),
            "inserted_per_min": inserted_per_min,
            "processed_per_min": processed_per_min,
            "landing_rate": inserted_per_min,
            "missing_symbols": max(0, total - skipped - processed),
            "deferred_symbols": deferred_count,
            "progress_pct": progress_pct,
            "heartbeat_at": now,
            "updated_at": now,
            "last_run": now,
        }},
        upsert=True,
    )
    if key != "all":
        _write_aggregate_progress(sync_col, scope=scope)


def _write_aggregate_progress(sync_col, *, scope: str) -> None:
    try:
        rows = list(sync_col.find(
            {"module": "stock_daily"},
            {
                "status": 1,
                "shard_key": 1,
                "total": 1,
                "processed": 1,
                "inserted": 1,
                "skipped": 1,
                "errors": 1,
                "deferred": 1,
                "latest_symbol": 1,
                "latest_status": 1,
                "latest_written": 1,
                "heartbeat_at": 1,
                "started_at": 1,
                "global_total": 1,
            },
        ))
    except Exception:
        return
    rows = [row for row in rows if row.get("shard_key") and row.get("_id") != _PROGRESS_META_ID]
    if not rows:
        return
    total = sum(int(row.get("total") or 0) for row in rows)
    processed = sum(int(row.get("processed") or 0) for row in rows)
    inserted = sum(int(row.get("inserted") or 0) for row in rows)
    skipped = sum(int(row.get("skipped") or 0) for row in rows)
    errors_count = sum(int(row.get("errors") or 0) for row in rows)
    deferred_count = sum(int(row.get("deferred") or 0) for row in rows)
    now = naive_market_now("A")
    latest = max(rows, key=lambda row: row.get("heartbeat_at") or datetime.min)
    started_values = [row.get("started_at") for row in rows if isinstance(row.get("started_at"), datetime)]
    started_at = min(started_values) if started_values else now
    elapsed_seconds = max(0.0, (now - started_at).total_seconds())
    inserted_per_min = round(inserted / (elapsed_seconds / 60), 2) if elapsed_seconds > 0 else 0.0
    processed_per_min = round(processed / (elapsed_seconds / 60), 2) if elapsed_seconds > 0 else 0.0
    statuses = {str(row.get("status") or "") for row in rows}
    status = "ok" if statuses == {"ok"} else "partial" if "partial" in statuses else "running"
    sync_col.update_one(
        {"_id": _PROGRESS_META_ID},
        {"$set": {
            "module": "stock_daily",
            "status": status,
            "scope": scope,
            "shard_key": "aggregate",
            "shard_count": len(rows),
            "total": total,
            "processed": processed,
            "remaining": max(0, total - processed),
            "inserted": inserted,
            "skipped": skipped,
            "errors": errors_count,
            "deferred": deferred_count,
            "latest_symbol": latest.get("latest_symbol", ""),
            "latest_status": latest.get("latest_status", ""),
            "latest_written": latest.get("latest_written", 0),
            "started_at": started_at,
            "elapsed_seconds": round(elapsed_seconds, 2),
            "inserted_per_min": inserted_per_min,
            "processed_per_min": processed_per_min,
            "landing_rate": inserted_per_min,
            "missing_symbols": max(0, total - skipped - processed),
            "deferred_symbols": deferred_count,
            "progress_pct": round(processed / total * 100, 2) if total else 0,
            "heartbeat_at": now,
            "updated_at": now,
            "last_run": now,
        }},
        upsert=True,
    )


def _call_provider(fn):
    # AKShare's Eastmoney path can initialize libmini_racer. On macOS it has
    # crashed the interpreter when called from worker timeout threads, so the
    # safe default is a direct provider call; use thread mode only when needed.
    if os.getenv("STOCK_DAILY_PROVIDER_TIMEOUT_MODE", "direct").lower() != "thread":
        return fn()
    future = _PROVIDER_TIMEOUT_POOL.submit(fn)
    try:
        return future.result(timeout=_provider_timeout())
    except FutureTimeout as exc:
        future.cancel()
        raise TimeoutError(f"provider_timeout>{_provider_timeout()}s") from exc


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
    raw = os.getenv("STOCK_DAILY_EXTRA_CODES", "")
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


def _select_codes_with_priority(codes: list[str], priority: set[str], max_codes: int) -> tuple[list[str], list[str]]:
    if max_codes <= 0 or len(codes) <= max_codes:
        return codes, []
    priority_codes = [code for code in codes if code in priority]
    normal_codes = [code for code in codes if code not in priority]
    selected = [*priority_codes, *normal_codes[:max(0, max_codes - len(priority_codes))]]
    selected_set = set(selected)
    skipped = [code for code in codes if code not in selected_set]
    return selected, skipped


def _cached_stock_universe(db: Database | None) -> list[str]:
    """Return the last known A-share universe when live stock-list sources fail."""
    if db is None:
        return []

    codes: list[str] = []

    def add(value: object) -> None:
        code = _pure_a_code(value)
        if code and code not in codes:
            codes.append(code)

    try:
        cursor = db["sync_log"].find(
            {"module": "stock_daily", "symbol": {"$exists": True}},
            {"symbol": 1},
        )
        for doc in cursor:
            add(doc.get("symbol"))
    except Exception as exc:
        logger.debug("读取 stock_daily sync_log universe 失败: %s", exc)

    try:
        pool = db["market_pools"].find_one(
            {"pool": "active"},
            {"symbols": 1, "items": 1},
            sort=[("dt", -1), ("updated_at", -1)],
        ) or {}
        for symbol in pool.get("symbols") or []:
            add(symbol)
        for item in pool.get("items") or []:
            if isinstance(item, dict):
                add(item.get("symbol") or item.get("code") or item.get("raw_code"))
            else:
                add(item)
    except Exception as exc:
        logger.debug("读取 market_pools universe 失败: %s", exc)

    try:
        for doc in db["bars"].aggregate([
            {"$match": {"meta.market": "A", "meta.freq": "日线"}},
            {"$sort": {"dt": -1}},
            {"$group": {"_id": "$meta.symbol", "latest_dt": {"$first": "$dt"}}},
            {"$limit": 6000},
        ]):
            add(doc.get("_id"))
    except Exception as exc:
        logger.debug("读取 bars universe 失败: %s", exc)

    return codes


def _get_all_stock_codes(db: Database | None = None) -> list:
    """获取全量 A 股代码列表；网络源失败时复用 Mongo 里的既有 universe。"""
    try:
        with em_proxy(None):
            df = ak.stock_info_a_code_name()
        return df["code"].tolist()
    except Exception as e:
        logger.warning(f"获取股票列表失败: {e}，使用 stock_zh_a_spot_em 兜底")
        try:
            with em_proxy(None):
                df = ak.stock_zh_a_spot_em()
            return df["代码"].tolist()
        except Exception as spot_exc:
            cached = _cached_stock_universe(db)
            if cached:
                logger.warning(
                    "股票列表网络兜底失败: %s；使用 Mongo cached universe: %d 只",
                    spot_exc,
                    len(cached),
                )
                return cached
            raise


def _get_active_stock_codes(db: Database) -> list[str]:
    """Return the bounded active-pool A-share universe for routine preheat."""
    import config

    codes: list[str] = []
    priority_codes: set[str] = set()
    index_codes = {_pure_a_code(symbol) for symbol in getattr(config, "INDEX_AK_CODES", {}).values()}
    index_codes.discard("")

    def add(value: object, *, priority: bool = False) -> None:
        code = _pure_a_code(value)
        if code in index_codes:
            return
        if priority and code:
            priority_codes.add(code)
        if code and code not in codes:
            codes.append(code)

    only_codes = os.getenv("STOCK_DAILY_ONLY_CODES", "")
    if only_codes.strip():
        for symbol in only_codes.replace(";", ",").split(","):
            add(symbol, priority=True)
        return codes

    for symbol in _env_symbol_values(
        "STOCK_DAILY_PRIORITY_CODES",
        "SIGNALS_PRIORITY_STOCK_CODES",
        default=os.getenv("STOCK_DAILY_DEFAULT_PRIORITY_CODES", _DEFAULT_PRIORITY_CODES),
    ):
        add(symbol, priority=True)

    terminal_pool = db["terminal_stock_pool"].find_one(
        {"pool": "terminal_stock_pool", "market": "A"},
        {"stocks": 1},
        sort=[("updated_at", -1)],
    ) or {}
    for item in terminal_pool.get("stocks") or []:
        symbol = item.get("raw_code") or item.get("symbol") if isinstance(item, dict) else item
        add(symbol, priority=True)

    for symbol in getattr(config, "WHITELIST", []):
        add(symbol, priority=True)

    for symbol in _iter_strategy_snapshot_symbols():
        add(symbol, priority=True)

    for symbol in _iter_configured_extra_symbols():
        add(symbol, priority=True)

    pool = db["market_pools"].find_one(
        {"pool": "active"},
        {"symbols": 1},
        sort=[("dt", -1), ("updated_at", -1)],
    ) or {}
    for symbol in pool.get("symbols") or []:
        add(symbol)

    for doc in db["signals"].find({}, {"symbol": 1}).sort("signal_date", -1).limit(200):
        add(doc.get("symbol"))

    default_max_codes = max(
        int(getattr(config, "MAX_POOL_SIZE", 50) or 50),
        int(os.getenv("TERMINAL_REALTIME_STOCK_LIMIT", "72")),
        int(os.getenv("STOCK_MINUTE_SIGNAL_MAX_CODES", "72")) * 4,
    )
    max_codes = int(os.getenv("STOCK_DAILY_MAX_CODES", str(default_max_codes)))
    if max_codes > 0:
        codes, skipped = _select_codes_with_priority(codes, priority_codes, max_codes)
        if skipped:
            db["sync_log"].update_one(
                {"_id": "stock_daily:selection:_meta"},
                {"$set": {
                    "module": "stock_daily",
                    "status": "partial",
                    "last_run": naive_market_now("A"),
                    "selected_symbols": codes,
                    "priority_symbols": [code for code in codes if code in priority_codes],
                    "skipped_symbols": skipped[:80],
                    "skipped_count": len(skipped),
                    "max_codes": max_codes,
                    "candidate_count": len(codes) + len(skipped),
                }},
                upsert=True,
            )
    return codes


def _get_stock_codes(db: Database) -> tuple[list[str], str]:
    full_sync = str(get_task_env("SIGNALS_SYNC_FULL_STOCK_DAILY", "false") or "false").lower() == "true"
    scope = str(get_task_env("STOCK_DAILY_SCOPE", "active") or "active").lower()
    if full_sync or scope == "all":
        return _get_all_stock_codes(db), "all"

    codes = _get_active_stock_codes(db)
    if codes:
        if os.getenv("STOCK_DAILY_ONLY_CODES", "").strip():
            return codes, "manual_only_codes"
        return codes, "active"

    return _get_all_stock_codes(db), "all_fallback"


def _docs_from_daily_df(code: str, df: pd.DataFrame, column_map: dict[str, str], source: str) -> list:
    if df is None or df.empty:
        return []
    docs = []
    for _, row in df.iterrows():
        dt = row[column_map["dt"]]
        docs.append({
            "dt": pd.to_datetime(dt),
            "meta": {"symbol": code, "freq": "日线", "market": "A", "source": source},
            "open": float(row[column_map["open"]]),
            "high": float(row[column_map["high"]]),
            "low": float(row[column_map["low"]]),
            "close": float(row[column_map["close"]]),
            "vol": int(row[column_map["vol"]]) if pd.notna(row[column_map["vol"]]) else 0,
            "amount": int(float(row[column_map["amount"]])) if pd.notna(row[column_map["amount"]]) else 0,
            "source": source,
        })
    return docs


def _safe_number(value, default: float | None = None) -> float | None:
    parsed = pd.to_numeric(value, errors="coerce")
    if pd.isna(parsed):
        return default
    return float(parsed)


def _em_spot_clist_params(page: int, page_size: int) -> dict[str, str]:
    return {
        "pn": str(page),
        "pz": str(page_size),
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": _EM_SPOT_CLIST_FS,
        "fields": _EM_SPOT_CLIST_FIELDS,
    }


def _fetch_eastmoney_spot_batch_df(db: Database | None, end_date: str) -> pd.DataFrame:
    """Fetch one full-market Eastmoney spot snapshot and cache it per process/date."""
    cache_key = f"{end_date}:{_EM_SPOT_CLIST_FIELDS}"
    with _SPOT_BATCH_LOCK:
        cached = _SPOT_BATCH_CACHE.get(cache_key)
        if cached is not None:
            return cached.copy()

        page_size = _spot_batch_page_size()
        timeout = _spot_batch_timeout()
        def _fetch_pages() -> list[dict]:
            rows: list[dict] = []
            with requests.Session() as session:
                session.trust_env = False

                def _request(page: int) -> dict:
                    response = session.get(
                        _EM_SPOT_CLIST_URL,
                        params=_em_spot_clist_params(page, page_size),
                        headers=_EM_SPOT_HEADERS,
                        timeout=timeout,
                    )
                    response.raise_for_status()
                    return response.json()

                first_payload = _request(1)
                first_data = first_payload.get("data") or {}
                total = int(first_data.get("total") or 0)
                rows.extend(first_data.get("diff") or [])
                page_count = max(1, math.ceil(total / page_size))
                for page in range(2, page_count + 1):
                    payload = _request(page)
                    data = payload.get("data") or {}
                    rows.extend(data.get("diff") or [])
            return rows

        rows = provider_call(
            "eastmoney",
            "stock_daily_spot_batch",
            _fetch_pages,
            db=db,
        )

        docs = []
        for row in rows:
            docs.append({
                "代码": row.get("f12"),
                "最新价": row.get("f2"),
                "成交量": row.get("f5"),
                "成交额": row.get("f6"),
                "最高": row.get("f15"),
                "最低": row.get("f16"),
                "今开": row.get("f17"),
                "昨收": row.get("f18"),
            })
        df = pd.DataFrame(docs)
        if not df.empty:
            df["_pure_code"] = df["代码"].map(_pure_a_code)
        _SPOT_BATCH_CACHE[cache_key] = df
        return df.copy()


def _previous_daily_close_by_symbol(db: Database, codes: list[str], end_date: str) -> dict[str, float]:
    if not codes:
        return {}
    try:
        cutoff = pd.to_datetime(end_date).to_pydatetime()
        rows = db["bars"].aggregate([
            {
                "$match": {
                    "meta.freq": "日线",
                    "meta.symbol": {"$in": codes},
                    "dt": {"$lt": cutoff},
                }
            },
            {"$sort": {"meta.symbol": 1, "dt": -1}},
            {"$group": {"_id": "$meta.symbol", "close": {"$first": "$close"}}},
        ])
        result: dict[str, float] = {}
        for row in rows:
            code = _pure_a_code(row.get("_id"))
            close = _safe_number(row.get("close"))
            if code and close and close > 0:
                result[code] = close
        return result
    except Exception as exc:
        logger.debug("读取昨日日线 close 失败，今日批量快照跳过复权校验: %s", exc)
        return {}


def _snapshot_daily_doc(
    code: str,
    row: pd.Series,
    end_date: str,
    *,
    previous_close: float | None = None,
) -> dict | None:
    """Build today's unadjusted daily bar from the all-market spot snapshot.

    Eastmoney's daily qfq series keeps the latest trading day at raw price scale
    in normal cases. Historical gaps and failed snapshot rows still fall back to
    per-symbol kline fetch below.
    """
    open_price = _safe_number(row.get("今开"))
    high = _safe_number(row.get("最高"))
    low = _safe_number(row.get("最低"))
    close = _safe_number(row.get("最新价"))
    vol = _safe_number(row.get("成交量"), 0.0)
    amount = _safe_number(row.get("成交额"), 0.0)
    if not all(value is not None and value > 0 for value in (open_price, high, low, close)):
        return None
    snapshot_prev_close = _safe_number(row.get("昨收"))
    if previous_close and previous_close > 0 and snapshot_prev_close and snapshot_prev_close > 0:
        prev_gap = abs(snapshot_prev_close - previous_close) / previous_close
        if prev_gap > _batch_prev_close_tolerance():
            return None
    return {
        "dt": pd.to_datetime(end_date),
        "meta": {
            "symbol": code,
            "freq": "日线",
            "market": "A",
            "source": "eastmoney_spot_clist_batch",
            "batch_semantics": "today_spot_ohlcv",
            "prev_close": snapshot_prev_close,
        },
        "open": float(open_price),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "vol": int(float(vol or 0)),
        "amount": int(float(amount or 0)),
        "source": "eastmoney_spot_clist_batch",
    }


def _batch_today_candidates(codes: list[str], sync_docs: dict[str, object], end_date: str) -> list[str]:
    candidates: list[str] = []
    for code in codes:
        last_dt = _coerce_last_dt(sync_docs.get(code))
        if not last_dt:
            continue
        inc_start = (last_dt + timedelta(days=1)).strftime("%Y%m%d")
        if inc_start == end_date:
            candidates.append(code)
    return candidates


def _sync_today_from_spot_batch(
    db: Database,
    codes: list[str],
    sync_docs: dict[str, object],
    end_date: str,
) -> tuple[dict[str, list], str]:
    if not _env_bool("STOCK_DAILY_BATCH_TODAY_ENABLED", True):
        return {}, "disabled"
    if naive_market_now("A").hour < _batch_today_min_hour():
        return {}, "before_batch_today_window"
    candidates = _batch_today_candidates(codes, sync_docs, end_date)
    if not candidates:
        return {}, "no_today_candidates"
    try:
        df = _fetch_eastmoney_spot_batch_df(db, end_date)
    except Exception as exc:
        logger.warning("全市场今日日线批量快照失败，回退单股历史 K 线: %s", str(exc)[:200])
        return {}, f"batch_fetch_failed:{exc.__class__.__name__}"
    if df is None or df.empty or "_pure_code" not in df.columns:
        return {}, "batch_snapshot_empty"

    prev_close_by_code = _previous_daily_close_by_symbol(db, candidates, end_date)
    snapshot_rows = {
        str(row.get("_pure_code")): row
        for _, row in df.iterrows()
        if row.get("_pure_code")
    }
    docs_by_code: dict[str, list] = {}
    for code in candidates:
        row = snapshot_rows.get(code)
        if row is None:
            continue
        doc = _snapshot_daily_doc(
            code,
            row,
            end_date,
            previous_close=prev_close_by_code.get(code),
        )
        if doc:
            docs_by_code[code] = [doc]
    fallback_count = max(0, len(candidates) - len(docs_by_code))
    return docs_by_code, f"batch_today_candidates={len(candidates)},batch_docs={len(docs_by_code)},fallback={fallback_count}"


def _provider_failure(failures: list[tuple[str, BaseException]]) -> BaseException:
    message = "; ".join(f"{name}={str(exc)[:160]}" for name, exc in failures)
    if failures and all(isinstance(exc, ProviderCoolingDown) for _, exc in failures):
        return ProviderCoolingDown(message)
    return RuntimeError(message)


def _sync_one_stock(code: str, last_dt: str, end_date: str,
                    proxy_url: str = None, db: Database | None = None) -> list:
    """同步单只股票日线，返回文档列表"""
    start = last_dt or "19900101"
    primary_source = os.getenv("STOCK_DAILY_PRIMARY_SOURCE", "tencent").lower()
    failures: list[tuple[str, BaseException]] = []
    attempted: set[str] = set()
    if primary_source == "tencent":
        attempted.add("tencent")
        try:
            df = provider_call(
                "tencent",
                "stock_daily",
                lambda: fetch_tencent_daily(
                    code,
                    start_date=start,
                    end_date=end_date,
                    timeout=float(os.getenv("STOCK_DAILY_TENCENT_TIMEOUT", "8")),
                ),
                db=db,
            )
            return _docs_from_daily_df(code, df, {
                "dt": "日期",
                "open": "开盘",
                "high": "最高",
                "low": "最低",
                "close": "收盘",
                "vol": "成交量",
                "amount": "成交额",
            }, "tencent")
        except Exception as tencent_primary_exc:
            failures.append(("tencent", tencent_primary_exc))
            logger.debug("tencent daily primary failed %s: %s", code, tencent_primary_exc)

    source = "eastmoney"
    attempted.add("eastmoney")
    try:
        with em_proxy(proxy_url):
            df = provider_call(
                "eastmoney",
                "stock_daily_hist",
                lambda: _call_provider(
                    lambda: ak.stock_zh_a_hist(
                        symbol=code, period="daily",
                        start_date=start, end_date=end_date,
                        adjust="qfq",
                    )
                ),
                db=db,
            )
        column_map = {
            "dt": "日期",
            "open": "开盘",
            "high": "最高",
            "low": "最低",
            "close": "收盘",
            "vol": "成交量",
            "amount": "成交额",
        }
    except Exception as em_exc:
        failures.append(("eastmoney", em_exc))
        source = "sina"
        prefix = "sh" if code.startswith(("5", "6", "9")) else ("bj" if code.startswith(("4", "8")) else "sz")
        attempted.add("sina")
        try:
            df = provider_call(
                "sina",
                "stock_daily",
                lambda: _call_provider(
                    lambda: ak.stock_zh_a_daily(
                        symbol=f"{prefix}{code}",
                        start_date=start,
                        end_date=end_date,
                        adjust="qfq",
                    )
                ),
                db=db,
            )
            column_map = {
                "dt": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "vol": "volume",
                "amount": "amount",
            }
        except Exception as sina_exc:
            failures.append(("sina", sina_exc))
            if "tencent" not in attempted:
                source = "tencent"
                attempted.add("tencent")
                try:
                    df = provider_call(
                        "tencent",
                        "stock_daily",
                        lambda: fetch_tencent_daily(
                            code,
                            start_date=start,
                            end_date=end_date,
                            timeout=float(os.getenv("STOCK_DAILY_TENCENT_TIMEOUT", "8")),
                        ),
                        db=db,
                    )
                    column_map = {
                        "dt": "日期",
                        "open": "开盘",
                        "high": "最高",
                        "low": "最低",
                        "close": "收盘",
                        "vol": "成交量",
                        "amount": "成交额",
                    }
                    return _docs_from_daily_df(code, df, column_map, source)
                except Exception as tencent_exc:
                    failures.append(("tencent", tencent_exc))
            raise _provider_failure(failures)
    return _docs_from_daily_df(code, df, column_map, source)


@sync_retry
def sync_stock_daily(db: Database, proxy_url: str = None) -> dict:
    """
    A 股日线全量增量同步。

    1. 获取全量股票列表
    2. 查 sync_log 获取每只股票的 last_dt
    3. 并行拉取增量数据
    4. bulk_write 到 bars collection
    5. 更新 sync_log
    """
    bars_col = db["bars"]
    sync_col = db["sync_log"]
    now = naive_market_now("A")
    end_date = now.strftime("%Y%m%d")

    # 默认只补活跃池；全市场补仓库需要显式设置 STOCK_DAILY_SCOPE=all。
    all_codes, scope = _get_stock_codes(db)
    codes, shard_meta = _apply_code_shard(all_codes)
    shard_key = str(shard_meta["shard_key"])
    shard_index = int(shard_meta["shard_index"])
    shard_count = int(shard_meta["shard_count"])
    global_total = int(shard_meta["global_total"])
    logger.info(
        "A股日线同步: %d/%d 只股票, scope=%s shard=%s",
        len(codes), global_total, scope, shard_key,
    )

    # 批量查询 sync_log，并用 bars 最新日期补齐，避免 sync_log 旧值导致重复打外部源。
    sync_docs = {
        doc["symbol"]: doc.get("last_dt")
        for doc in sync_col.find(
            {"module": "stock_daily", "symbol": {"$exists": True}},
            {"symbol": 1, "last_dt": 1}
        )
        if doc.get("symbol")
    }
    bars_latest = _latest_daily_dates_by_symbol(db, codes)
    for code, latest_dt in bars_latest.items():
        current = _coerce_last_dt(sync_docs.get(code))
        if current is None or latest_dt > current:
            sync_docs[code] = latest_dt

    total_inserted = 0
    total_skipped = 0
    processed_count = 0
    errors = []
    deferred = []
    progress_interval = _progress_interval()
    run_started_at = now
    _write_progress(
        sync_col,
        status="running",
        scope=scope,
        total=len(codes),
        processed=0,
        inserted=0,
        skipped=0,
        errors_count=0,
        started_at=run_started_at,
        shard_key=shard_key,
        shard_index=shard_index,
        shard_count=shard_count,
        global_total=global_total,
    )

    batch_docs_by_code, batch_reason = _sync_today_from_spot_batch(db, codes, sync_docs, end_date)
    batch_codes = set(batch_docs_by_code)
    batch_inserted = 0
    if batch_docs_by_code:
        written_by_code = _write_daily_docs_batch(bars_col, sync_col, batch_docs_by_code)
        batch_inserted = sum(int(value or 0) for value in written_by_code.values())
        total_inserted += batch_inserted
        processed_count += len(batch_codes)
        _write_progress(
            sync_col,
            status="running" if processed_count < len(codes) else "ok",
            scope=scope,
            total=len(codes),
            processed=processed_count,
            inserted=total_inserted,
            skipped=total_skipped,
            errors_count=0,
            deferred_count=0,
            latest_symbol=next(iter(batch_codes), ""),
            latest_status=f"batch_today:{batch_inserted}/{len(batch_codes)}",
            latest_written=batch_inserted,
            started_at=run_started_at,
            shard_key=shard_key,
            shard_index=shard_index,
            shard_count=shard_count,
            global_total=global_total,
        )
        logger.info(
            "A股日线今日批量快照: %d codes, +%d bars, shard=%s reason=%s",
            len(batch_codes),
            batch_inserted,
            shard_key,
            batch_reason,
        )

    def _process(code):
        last_dt_raw = sync_docs.get(code)
        last_dt = _coerce_last_dt(last_dt_raw)
        if last_dt:
            # 增量：从 last_dt 下一天开始
            inc_start = (last_dt + timedelta(days=1)).strftime("%Y%m%d")
            if inc_start > end_date:
                return code, [], "skip"
        else:
            # 全量：近 2 年
            inc_start = (now - timedelta(days=730)).strftime("%Y%m%d")

        if _stock_daily_providers_all_cooling(db):
            return code, [], "deferred/cooling_down:all_stock_daily_providers"

        try:
            try:
                docs = _sync_one_stock(code, inc_start, end_date, proxy_url, db=db)
            except TypeError as exc:
                if "unexpected keyword argument 'db'" not in str(exc):
                    raise
                docs = _sync_one_stock(code, inc_start, end_date, proxy_url)
            time.sleep(_CALL_INTERVAL)
            return code, docs, "ok"
        except Exception as e:
            if isinstance(e, ProviderCoolingDown):
                return code, [], f"deferred/cooling_down:{e}"
            return code, [], str(e)

    pending_docs: dict[str, list] = {}
    write_batch_symbols = _write_batch_symbols()

    def _flush_pending() -> int:
        nonlocal pending_docs
        if not pending_docs:
            return 0
        batch = pending_docs
        pending_docs = {}
        written_by_code = _write_daily_docs_batch(bars_col, sync_col, batch)
        return sum(int(value or 0) for value in written_by_code.values())

    # 并行拉取
    remaining_codes = [code for code in codes if code not in batch_codes]
    with ThreadPoolExecutor(max_workers=_BATCH_WORKERS) as executor:
        futures = {executor.submit(_process, c): c for c in remaining_codes}
        for future in as_completed(futures):
            code = futures[future]
            latest_symbol = code
            latest_status = "error"
            latest_written = 0
            try:
                code, docs, status = future.result()
                latest_symbol = code
                latest_status = status
                if status == "skip":
                    total_skipped += 1
                elif status == "ok" and docs:
                    pending_docs[code] = docs
                    if len(pending_docs) >= write_batch_symbols:
                        latest_written = _flush_pending()
                        total_inserted += latest_written
                elif status != "ok":
                    if str(status).startswith("deferred/"):
                        deferred.append((code, str(status)[:240]))
                    else:
                        errors.append((code, str(status)[:240]))
            except Exception as e:
                if isinstance(e, ProviderCoolingDown):
                    deferred.append((code, str(e)[:240]))
                    latest_status = f"deferred/cooling_down:{str(e)[:200]}"
                else:
                    errors.append((code, str(e)[:240]))
            finally:
                processed_count += 1
                if processed_count % progress_interval == 0 or processed_count == len(codes):
                    final_partial = bool(errors or deferred)
                    _write_progress(
                        sync_col,
                        status="running" if processed_count < len(codes) else ("partial" if final_partial else "ok"),
                        scope=scope,
                        total=len(codes),
                        processed=processed_count,
                        inserted=total_inserted,
                        skipped=total_skipped,
                        errors_count=len(errors),
                        deferred_count=len(deferred),
                        latest_symbol=latest_symbol,
                        latest_status=latest_status,
                        latest_written=latest_written,
                        started_at=run_started_at,
                        shard_key=shard_key,
                        shard_index=shard_index,
                        shard_count=shard_count,
                        global_total=global_total,
                    )

    total_inserted += _flush_pending()
    _write_progress(
        sync_col,
        status="partial" if errors or deferred else "ok",
        scope=scope,
        total=len(codes),
        processed=processed_count,
        inserted=total_inserted,
        skipped=total_skipped,
        errors_count=len(errors),
        deferred_count=len(deferred),
        latest_symbol=(codes[-1] if codes else ""),
        latest_status="final_flush",
        latest_written=0,
        started_at=run_started_at,
        shard_key=shard_key,
        shard_index=shard_index,
        shard_count=shard_count,
        global_total=global_total,
    )

    logger.info(f"A股日线完成: +{total_inserted} bars, "
                f"{total_skipped} 已最新, {len(errors)} 失败, {len(deferred)} deferred")
    if errors[:5]:
        logger.warning(f"前 5 个错误: {errors[:5]}")
    if deferred[:5]:
        logger.info(f"前 5 个 deferred/cooling_down: {deferred[:5]}")

    return {
        "status": "partial" if errors or deferred else "ok",
        "inserted": total_inserted,
        "skipped": total_skipped,
        "errors": len(errors),
        "deferred": len(deferred),
        "cooling_down": len(deferred),
        "scope": scope,
        "shard_key": shard_key,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "global_total": global_total,
        "codes": len(codes),
        "processed": processed_count,
        "total": len(codes),
        "expected_codes": len(codes),
        "covered_codes": max(0, len(codes) - len(errors) - len(deferred)),
        "coverage_pct": round((max(0, len(codes) - len(errors) - len(deferred)) / len(codes) * 100), 2) if codes else 0,
        "progress_pct": round((processed_count / len(codes) * 100), 2) if codes else 0,
        "sample_errors": errors[:5],
        "sample_deferred": deferred[:5],
        "batch_today": len(batch_codes),
        "batch_today_inserted": batch_inserted,
        "batch_today_reason": batch_reason,
    }
