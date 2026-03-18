# -*- coding: utf-8 -*-
"""
指数分钟线同步 — 11 只指数 30M/15M

数据源: AKShare stock_zh_index_hist_min_em（东财指数分钟线）
策略: 全量覆盖（数据量小，近 5 天窗口）
频率: 工作日 16:00
"""
import logging
from datetime import datetime

import akshare as ak
import pandas as pd
from pymongo.database import Database

from ..proxy import em_proxy
from ..retry import sync_retry

logger = logging.getLogger("signals.sync.index_minute")


def _sync_a_index_minute(db: Database, ak_codes: dict,
                         proxy_url: str = None) -> int:
    """A 股指数分钟线"""
    bars_col = db["bars"]
    sync_col = db["sync_log"]
    inserted = 0

    period_map = {"30分钟": "30", "15分钟": "15"}

    for name, symbol in ak_codes.items():
        # AKShare 指数分钟线需要纯代码（去掉 sh/sz 前缀）
        pure_code = symbol.replace("sh", "").replace("sz", "")

        for freq, period in period_map.items():
            try:
                with em_proxy(proxy_url):
                    df = ak.stock_zh_index_hist_min_em(
                        symbol=pure_code, period=period)

                if df is None or df.empty:
                    logger.warning(f"  ✗ {name} {freq}: 无数据")
                    continue

                docs = []
                # 东财指数分钟线列名
                dt_col = "时间" if "时间" in df.columns else "datetime"
                for _, row in df.iterrows():
                    docs.append({
                        "dt": pd.to_datetime(row[dt_col]),
                        "meta": {"symbol": symbol, "freq": freq},
                        "open": float(row["开盘"]),
                        "high": float(row["最高"]),
                        "low": float(row["最低"]),
                        "close": float(row["收盘"]),
                        "vol": int(row["成交量"]) if pd.notna(row["成交量"]) else 0,
                        "amount": int(float(row["成交额"])) if pd.notna(row.get("成交额", 0)) else 0,
                    })

                if docs:
                    bars_col.delete_many({
                        "meta.symbol": symbol,
                        "meta.freq": freq,
                    })
                    bars_col.insert_many(docs, ordered=False)
                    inserted += len(docs)

                    sync_col.update_one(
                        {"_id": f"index_minute:{symbol}:{freq}"},
                        {"$set": {
                            "module": "index_minute",
                            "symbol": symbol,
                            "last_dt": docs[-1]["dt"],
                            "last_run": datetime.now(),
                            "status": "ok",
                            "bar_count": len(docs),
                        }},
                        upsert=True,
                    )
                    logger.info(f"  ✓ {name} {freq}: {len(docs)} bars")

            except Exception as e:
                logger.error(f"  ✗ {name} {freq}: {e}")

    return inserted


@sync_retry(max_attempts=5, min_wait=3)
def sync_index_minute(db: Database, proxy_url: str = None) -> dict:
    """指数分钟线全量同步"""
    import config

    logger.info("指数分钟线同步: A股指数 30M/15M")
    total = _sync_a_index_minute(db, config.INDEX_AK_CODES, proxy_url)

    logger.info(f"指数分钟线完成: {total} bars")
    return {"inserted": total}
