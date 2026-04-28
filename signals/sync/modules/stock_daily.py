# -*- coding: utf-8 -*-
"""
A股日线同步 — ~5000 只股票增量同步

数据源: AKShare stock_zh_a_hist（东财接口）
策略: 增量同步，从 sync_log.last_dt 开始拉增量
频率: 工作日 16:30
"""
import logging
import os
import atexit
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout, as_completed
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from ..proxy import em_proxy
from ..retry import sync_retry
from .daily_sources import fetch_tencent_daily

logger = logging.getLogger("signals.sync.stock_daily")

# Keep the default serial. AKShare's Eastmoney daily path can native-crash when
# multiple MiniRacer initializations happen in parallel on macOS.
_BATCH_WORKERS = max(1, int(os.getenv("STOCK_DAILY_WORKERS", "1")))
# 每只股票间隔（秒），避免被东财限速
_CALL_INTERVAL = 0.3
_PROVIDER_TIMEOUT_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="stock-daily-provider")
atexit.register(_PROVIDER_TIMEOUT_POOL.shutdown, wait=False, cancel_futures=True)
_DEFAULT_PRIORITY_CODES = "688802,300575"
_PROGRESS_META_ID = "stock_daily:progress:_meta"


def _provider_timeout() -> float:
    return float(os.getenv("STOCK_DAILY_PROVIDER_TIMEOUT", "12"))


def _progress_interval() -> int:
    return max(1, int(os.getenv("STOCK_DAILY_PROGRESS_INTERVAL", "25")))


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
    latest_symbol: str = "",
    latest_status: str = "",
    latest_written: int = 0,
) -> None:
    now = naive_market_now("A")
    progress_pct = round(processed / total * 100, 2) if total else 0
    sync_col.update_one(
        {"_id": _PROGRESS_META_ID},
        {"$set": {
            "module": "stock_daily",
            "status": status,
            "scope": scope,
            "total": total,
            "processed": processed,
            "remaining": max(0, total - processed),
            "inserted": inserted,
            "skipped": skipped,
            "errors": errors_count,
            "latest_symbol": latest_symbol,
            "latest_status": latest_status,
            "latest_written": latest_written,
            "progress_pct": progress_pct,
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


def _get_all_stock_codes() -> list:
    """获取全量 A 股代码列表"""
    try:
        df = ak.stock_info_a_code_name()
        return df["code"].tolist()
    except Exception as e:
        logger.warning(f"获取股票列表失败: {e}，使用 stock_zh_a_spot_em 兜底")
        df = ak.stock_zh_a_spot_em()
        return df["代码"].tolist()


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
    full_sync = os.getenv("SIGNALS_SYNC_FULL_STOCK_DAILY", "false").lower() == "true"
    scope = os.getenv("STOCK_DAILY_SCOPE", "active").lower()
    if full_sync or scope == "all":
        return _get_all_stock_codes(), "all"

    codes = _get_active_stock_codes(db)
    if codes:
        if os.getenv("STOCK_DAILY_ONLY_CODES", "").strip():
            return codes, "manual_only_codes"
        return codes, "active"

    return _get_all_stock_codes(), "all_fallback"


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


def _sync_one_stock(code: str, last_dt: str, end_date: str,
                    proxy_url: str = None) -> list:
    """同步单只股票日线，返回文档列表"""
    start = last_dt or "19900101"
    primary_source = os.getenv("STOCK_DAILY_PRIMARY_SOURCE", "tencent").lower()
    if primary_source == "tencent":
        df = fetch_tencent_daily(
            code,
            start_date=start,
            end_date=end_date,
            timeout=float(os.getenv("STOCK_DAILY_TENCENT_TIMEOUT", "8")),
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

    source = "eastmoney"
    try:
        with em_proxy(proxy_url):
            df = _call_provider(
                lambda: ak.stock_zh_a_hist(
                    symbol=code, period="daily",
                    start_date=start, end_date=end_date,
                    adjust="qfq",
                )
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
        source = "sina"
        prefix = "sh" if code.startswith(("5", "6", "9")) else ("bj" if code.startswith(("4", "8")) else "sz")
        try:
            df = _call_provider(
                lambda: ak.stock_zh_a_daily(
                    symbol=f"{prefix}{code}",
                    start_date=start,
                    end_date=end_date,
                    adjust="qfq",
                )
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
            source = "tencent"
            try:
                df = fetch_tencent_daily(
                    code,
                    start_date=start,
                    end_date=end_date,
                    timeout=float(os.getenv("STOCK_DAILY_TENCENT_TIMEOUT", "8")),
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
            except Exception as tencent_exc:
                raise RuntimeError(
                    f"eastmoney={str(em_exc)[:160]}; sina={str(sina_exc)[:160]}; "
                    f"tencent={str(tencent_exc)[:160]}"
                ) from tencent_exc
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
    codes, scope = _get_stock_codes(db)
    logger.info(f"A股日线同步: {len(codes)} 只股票, scope={scope}")

    # 批量查询 sync_log
    sync_docs = {
        doc["symbol"]: doc.get("last_dt")
        for doc in sync_col.find(
            {"module": "stock_daily", "symbol": {"$exists": True}},
            {"symbol": 1, "last_dt": 1}
        )
        if doc.get("symbol")
    }

    total_inserted = 0
    total_skipped = 0
    processed_count = 0
    errors = []
    progress_interval = _progress_interval()
    _write_progress(
        sync_col,
        status="running",
        scope=scope,
        total=len(codes),
        processed=0,
        inserted=0,
        skipped=0,
        errors_count=0,
    )

    def _process(code):
        last_dt_raw = sync_docs.get(code)
        if last_dt_raw:
            # 增量：从 last_dt 下一天开始
            if isinstance(last_dt_raw, datetime):
                inc_start = (last_dt_raw + timedelta(days=1)).strftime("%Y%m%d")
            else:
                inc_start = (datetime.strptime(str(last_dt_raw)[:10], "%Y-%m-%d")
                             + timedelta(days=1)).strftime("%Y%m%d")
            if inc_start > end_date:
                return code, [], "skip"
        else:
            # 全量：近 2 年
            inc_start = (now - timedelta(days=730)).strftime("%Y%m%d")

        try:
            docs = _sync_one_stock(code, inc_start, end_date, proxy_url)
            time.sleep(_CALL_INTERVAL)
            return code, docs, "ok"
        except Exception as e:
            return code, [], str(e)

    # 并行拉取
    with ThreadPoolExecutor(max_workers=_BATCH_WORKERS) as executor:
        futures = {executor.submit(_process, c): c for c in codes}
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
                    # 写入 MongoDB
                    existing_dts = {
                        item.get("dt")
                        for item in bars_col.find(
                            {
                                "meta.symbol": code,
                                "meta.freq": "日线",
                                "dt": {"$in": [doc["dt"] for doc in docs]},
                            },
                            {"dt": 1},
                        )
                    }
                    new_docs = [doc for doc in docs if doc["dt"] not in existing_dts]
                    written = 0
                    if new_docs:
                        result = bars_col.insert_many(new_docs, ordered=False)
                        written = len(result.inserted_ids)
                    latest_written = written
                    total_inserted += written
                    # 更新 sync_log
                    last = docs[-1]["dt"]
                    sync_col.update_one(
                        {"_id": f"stock_daily:{code}"},
                        {"$set": {
                            "module": "stock_daily",
                            "symbol": code,
                            "last_dt": last,
                            "last_run": naive_market_now("A"),
                            "status": "ok",
                            "bar_count": len(docs),
                            "written": written,
                        }},
                        upsert=True,
                    )
                elif status != "ok":
                    errors.append((code, str(status)[:240]))
            except Exception as e:
                errors.append((code, str(e)[:240]))
            finally:
                processed_count += 1
                if processed_count % progress_interval == 0 or processed_count == len(codes):
                    _write_progress(
                        sync_col,
                        status="running" if processed_count < len(codes) else ("partial" if errors else "ok"),
                        scope=scope,
                        total=len(codes),
                        processed=processed_count,
                        inserted=total_inserted,
                        skipped=total_skipped,
                        errors_count=len(errors),
                        latest_symbol=latest_symbol,
                        latest_status=latest_status,
                        latest_written=latest_written,
                    )

    logger.info(f"A股日线完成: +{total_inserted} bars, "
                f"{total_skipped} 已最新, {len(errors)} 失败")
    if errors[:5]:
        logger.warning(f"前 5 个错误: {errors[:5]}")

    return {
        "inserted": total_inserted,
        "skipped": total_skipped,
        "errors": len(errors),
        "scope": scope,
        "codes": len(codes),
        "processed": processed_count,
        "total": len(codes),
        "expected_codes": len(codes),
        "covered_codes": max(0, len(codes) - len(errors)),
        "coverage_pct": round((max(0, len(codes) - len(errors)) / len(codes) * 100), 2) if codes else 0,
        "progress_pct": round((processed_count / len(codes) * 100), 2) if codes else 0,
    }
