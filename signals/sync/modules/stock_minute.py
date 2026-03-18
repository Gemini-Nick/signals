# -*- coding: utf-8 -*-
"""
A股分钟线同步 — 活跃标的 30M/15M 增量同步

数据源: AKShare stock_zh_a_hist_min_em（东财分钟线接口）
策略: 增量同步，仅白名单 + 最近入池标的（~200只上限）
频率: 工作日 16:00
注意: 东财分钟线 API 仅返回最近 5 个交易日数据
"""
import logging
import time
from datetime import datetime

import akshare as ak
import pandas as pd
from pymongo.database import Database

from ..proxy import em_proxy
from ..retry import sync_retry

logger = logging.getLogger("signals.sync.stock_minute")

_CALL_INTERVAL = 0.5  # 分钟线限速更严格


def _get_active_symbols(db: Database) -> list:
    """获取需要同步分钟线的活跃标的列表"""
    import config

    symbols = list(config.WHITELIST)

    # 从 sync_log 获取最近有日线同步的标的（取前200）
    recent = db["sync_log"].find(
        {"module": "stock_daily", "status": "ok"},
        {"symbol": 1},
    ).sort("last_run", -1).limit(200)

    for doc in recent:
        sym = doc.get("symbol", "")
        if sym and sym not in symbols:
            symbols.append(sym)

    return symbols[:200]


def _sync_one_minute(code: str, freq: str, proxy_url: str = None) -> list:
    """同步单只股票分钟线"""
    period_map = {"30分钟": "30", "15分钟": "15"}
    period = period_map.get(freq, "30")

    with em_proxy(proxy_url):
        df = ak.stock_zh_a_hist_min_em(
            symbol=code, period=period, adjust="qfq")

    if df is None or df.empty:
        return []

    docs = []
    for _, row in df.iterrows():
        docs.append({
            "dt": pd.to_datetime(row["时间"]),
            "meta": {"symbol": code, "freq": freq},
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

    仅同步白名单 + 最近活跃标的的 30M 和 15M 数据。
    东财分钟线 API 仅返回最近 5 天，直接全量写入（小数据量）。
    """
    bars_col = db["bars"]
    sync_col = db["sync_log"]

    symbols = _get_active_symbols(db)
    logger.info(f"分钟线同步: {len(symbols)} 只活跃标的")

    total_inserted = 0
    errors = []

    for code in symbols:
        for freq in ["30分钟", "15分钟"]:
            try:
                docs = _sync_one_minute(code, freq, proxy_url)
                time.sleep(_CALL_INTERVAL)

                if not docs:
                    continue

                # 删除该标的该频率的旧数据，重新插入（5天窗口，全量覆盖更安全）
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
                    }},
                    upsert=True,
                )

            except Exception as e:
                errors.append((code, freq, str(e)))

    logger.info(f"分钟线完成: +{total_inserted} bars, {len(errors)} 失败")
    return {"inserted": total_inserted, "errors": len(errors)}
