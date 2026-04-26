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

from ..proxy import em_proxy
from ..retry import sync_retry

logger = logging.getLogger("signals.sync.stock_daily")

# 每批次并行拉取股票数
_BATCH_WORKERS = 8
# 每只股票间隔（秒），避免被东财限速
_CALL_INTERVAL = 0.3
_PROVIDER_TIMEOUT_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="stock-daily-provider")
atexit.register(_PROVIDER_TIMEOUT_POOL.shutdown, wait=False, cancel_futures=True)


def _provider_timeout() -> float:
    return float(os.getenv("STOCK_DAILY_PROVIDER_TIMEOUT", "12"))


def _call_provider(fn):
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
    index_codes = {_pure_a_code(symbol) for symbol in getattr(config, "INDEX_AK_CODES", {}).values()}
    index_codes.discard("")

    def add(value: object) -> None:
        code = _pure_a_code(value)
        if code in index_codes:
            return
        if code and code not in codes:
            codes.append(code)

    for symbol in getattr(config, "WHITELIST", []):
        add(symbol)

    pool = db["market_pools"].find_one(
        {"pool": "active"},
        {"symbols": 1},
        sort=[("dt", -1), ("updated_at", -1)],
    ) or {}
    for symbol in pool.get("symbols") or []:
        add(symbol)

    for doc in db["signals"].find({}, {"symbol": 1}).sort("signal_date", -1).limit(200):
        add(doc.get("symbol"))

    max_codes = int(os.getenv("STOCK_DAILY_MAX_CODES", str(getattr(config, "MAX_POOL_SIZE", 50) or 50)))
    if max_codes > 0:
        codes = codes[:max_codes]
    return codes


def _get_stock_codes(db: Database) -> tuple[list[str], str]:
    full_sync = os.getenv("SIGNALS_SYNC_FULL_STOCK_DAILY", "false").lower() == "true"
    scope = os.getenv("STOCK_DAILY_SCOPE", "active").lower()
    if full_sync or scope == "all":
        return _get_all_stock_codes(), "all"

    codes = _get_active_stock_codes(db)
    if codes:
        return codes, "active"

    return _get_all_stock_codes(), "all_fallback"


def _sync_one_stock(code: str, last_dt: str, end_date: str,
                    proxy_url: str = None) -> list:
    """同步单只股票日线，返回文档列表"""
    start = last_dt or "19900101"
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
            raise RuntimeError(f"eastmoney={str(em_exc)[:160]}; sina={str(sina_exc)[:160]}") from sina_exc
    if df is None or df.empty:
        return []

    docs = []
    for _, row in df.iterrows():
        dt = row[column_map["dt"]]
        docs.append({
            "dt": pd.to_datetime(dt),
            "meta": {"symbol": code, "freq": "日线"},
            "open": float(row[column_map["open"]]),
            "high": float(row[column_map["high"]]),
            "low": float(row[column_map["low"]]),
            "close": float(row[column_map["close"]]),
            "vol": int(row[column_map["vol"]]) if pd.notna(row[column_map["vol"]]) else 0,
            "amount": int(float(row[column_map["amount"]])) if pd.notna(row[column_map["amount"]]) else 0,
            "source": source,
        })
    return docs


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
    end_date = datetime.now().strftime("%Y%m%d")

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
    errors = []

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
            inc_start = (datetime.now() - timedelta(days=730)).strftime("%Y%m%d")

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
            try:
                code, docs, status = future.result()
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
                    total_inserted += written
                    # 更新 sync_log
                    last = docs[-1]["dt"]
                    sync_col.update_one(
                        {"_id": f"stock_daily:{code}"},
                        {"$set": {
                            "module": "stock_daily",
                            "symbol": code,
                            "last_dt": last,
                            "last_run": datetime.now(),
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
    }
