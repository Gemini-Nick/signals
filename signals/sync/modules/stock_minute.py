# -*- coding: utf-8 -*-
"""
A股分钟线同步 — 活跃标的 5M/15M/30M 增量同步

数据源: Sina/Tencent 公共分钟线；东财分钟线可显式开启为最后兜底
策略: 增量同步，仅白名单 + 最近入池标的（~200只上限）
频率: 工作日 16:00
注意: 公共分钟线返回滚动窗口数据，直接全量覆盖
"""
import logging
import os
import time
from datetime import datetime

import akshare as ak
import pandas as pd
from pymongo.database import Database

from ..proxy import em_proxy
from ..retry import sync_retry
from .minute_sources import fetch_public_minute, stock_to_market_symbol

logger = logging.getLogger("signals.sync.stock_minute")

_CALL_INTERVAL = float(os.getenv("STOCK_MINUTE_CALL_INTERVAL", "0.5"))
_PUBLIC_TIMEOUT = float(os.getenv("STOCK_MINUTE_TIMEOUT", "5"))
_MINUTE_FREQS = ["5分钟", "15分钟", "30分钟"]
_ENABLE_EASTMONEY_FALLBACK = os.getenv("STOCK_MINUTE_EASTMONEY_FALLBACK", "false").lower() == "true"


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

    for key in ("buy_candidates", "sell_warnings", "decision_queue"):
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


def _get_active_symbols(db: Database) -> list:
    """获取需要同步分钟线的活跃标的列表"""
    import config

    symbols: list[str] = []
    index_codes = _index_codes()

    def add(value: object) -> None:
        code = _pure_a_code(value)
        if code in index_codes:
            return
        if code and code not in symbols:
            symbols.append(code)

    only_codes = os.getenv("STOCK_MINUTE_ONLY_CODES", "")
    if only_codes.strip():
        for symbol in only_codes.replace(";", ",").split(","):
            add(symbol)
        return symbols

    for symbol in getattr(config, "WHITELIST", []):
        add(symbol)

    for symbol in _iter_strategy_snapshot_symbols():
        add(symbol)

    for symbol in _iter_configured_extra_symbols():
        add(symbol)

    pool = db["market_pools"].find_one(
        {"pool": "active"},
        {"symbols": 1, "items": 1},
        sort=[("dt", -1), ("updated_at", -1)],
    ) or {}
    for symbol in pool.get("symbols") or []:
        add(symbol)
    for item in pool.get("items") or []:
        if isinstance(item, dict):
            add(item.get("symbol") or item.get("code"))

    for doc in db["signals"].find({}, {"symbol": 1}).sort("signal_date", -1).limit(300):
        add(doc.get("symbol"))

    # 从 sync_log 获取最近有日线同步的标的（取前200）
    recent = db["sync_log"].find(
        {"module": "stock_daily", "status": "ok"},
        {"symbol": 1},
    ).sort("last_run", -1).limit(200)

    for doc in recent:
        add(doc.get("symbol"))

    max_symbols = int(os.getenv("STOCK_MINUTE_MAX_CODES", "200"))
    return symbols[:max_symbols] if max_symbols > 0 else symbols


def _sync_one_minute(code: str, freq: str, proxy_url: str = None) -> list:
    """同步单只股票分钟线"""
    period_map = {"5分钟": "5", "15分钟": "15", "30分钟": "30"}
    period = period_map.get(freq, "30")
    source = "eastmoney"

    try:
        df, source = fetch_public_minute(stock_to_market_symbol(code), period, timeout=_PUBLIC_TIMEOUT)
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
            "meta": {"symbol": code, "freq": freq, "source": source},
            "open": float(row["开盘"]),
            "high": float(row["最高"]),
            "low": float(row["最低"]),
            "close": float(row["收盘"]),
            "vol": int(row["成交量"]) if pd.notna(row["成交量"]) else 0,
            "amount": int(float(row["成交额"])) if pd.notna(row["成交额"]) else 0,
        })
    return docs


@sync_retry
def sync_stock_minute(db: Database, proxy_url: str = None) -> dict:
    """
    A 股分钟线增量同步。

    仅同步白名单 + 最近活跃标的的 5M、15M 和 30M 数据。
    公共分钟线返回滚动窗口，直接全量写入（小数据量）。
    """
    bars_col = db["bars"]
    sync_col = db["sync_log"]

    symbols = _get_active_symbols(db)
    logger.info(f"分钟线同步: {len(symbols)} 只活跃标的")

    total_inserted = 0
    errors = []

    for code in symbols:
        for freq in _MINUTE_FREQS:
            try:
                docs = _sync_one_minute(code, freq, proxy_url)
                time.sleep(_CALL_INTERVAL)

                if not docs:
                    continue

                # 删除该标的该频率的旧数据，重新插入（滚动窗口，全量覆盖更安全）
                bars_col.delete_many({
                    "meta.symbol": code,
                    "meta.freq": freq,
                })
                bars_col.insert_many(docs, ordered=False)
                total_inserted += len(docs)

                sync_col.update_one(
                    {"_id": f"stock_minute:{code}:{freq}"},
                    {"$set": {
                        "module": "stock_minute",
                        "symbol": code,
                        "last_dt": docs[-1]["dt"],
                        "last_run": datetime.now(),
                        "status": "ok",
                        "bar_count": len(docs),
                        "source": docs[-1].get("meta", {}).get("source"),
                    }},
                    upsert=True,
                )

            except Exception as e:
                errors.append((code, freq, str(e)))

    logger.info(f"分钟线完成: +{total_inserted} bars, {len(errors)} 失败")
    return {"inserted": total_inserted, "errors": len(errors)}
