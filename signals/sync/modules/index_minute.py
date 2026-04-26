# -*- coding: utf-8 -*-
"""
指数分钟线同步 — 11 只指数 5M/15M/30M

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


def _write_index_docs(db: Database, symbol: str, freq: str, docs: list[dict]) -> int:
    if not docs:
        return 0
    prepared = []
    for doc in docs:
        item = dict(doc)
        item["meta"] = {**item.get("meta", {}), "symbol": symbol, "freq": freq, "asset_type": "index"}
        prepared.append(item)
    for collection in ("index_bars", "bars"):
        col = db[collection]
        col.delete_many({"meta.symbol": symbol, "meta.freq": freq})
        col.insert_many([dict(item) for item in prepared], ordered=False)
    return len(prepared)


def _sync_a_index_minute(db: Database, ak_codes: dict,
                         proxy_url: str = None) -> int:
    """A 股指数分钟线"""
    sync_col = db["sync_log"]
    inserted = 0

    period_map = {"5分钟": "5", "15分钟": "15", "30分钟": "30"}

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
                        "meta": {"symbol": symbol, "freq": freq, "asset_type": "index"},
                        "open": float(row["开盘"]),
                        "high": float(row["最高"]),
                        "low": float(row["最低"]),
                        "close": float(row["收盘"]),
                        "vol": int(row["成交量"]) if pd.notna(row["成交量"]) else 0,
                        "amount": int(float(row["成交额"])) if pd.notna(row.get("成交额", 0)) else 0,
                    })

                if docs:
                    written = _write_index_docs(db, symbol, freq, docs)
                    inserted += written

                    sync_col.update_one(
                        {"_id": f"index_minute:{symbol}:{freq}"},
                        {"$set": {
                            "module": "index_minute",
                            "symbol": symbol,
                            "last_dt": docs[-1]["dt"],
                            "last_run": datetime.now(),
                            "status": "ok",
                            "bar_count": written,
                        }},
                        upsert=True,
                    )
                    logger.info(f"  ✓ {name} {freq}: {written} bars")

            except Exception as e:
                logger.error(f"  ✗ {name} {freq}: {e}")

    return inserted


@sync_retry(max_attempts=5, min_wait=3)
def sync_index_minute(db: Database, proxy_url: str = None) -> dict:
    """指数分钟线全量同步"""
    import config

    logger.info("指数分钟线同步: A股指数 5M/15M/30M")
    total = _sync_a_index_minute(db, config.INDEX_AK_CODES, proxy_url)

    logger.info(f"指数分钟线完成: {total} bars")
    return {"inserted": total}
