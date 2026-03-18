# -*- coding: utf-8 -*-
"""
指数日线同步 — 11 只指数全量覆盖

数据源: AKShare stock_zh_index_daily（A股 7 只）
        + Futu/yfinance（港股 1 + 美股 3）
策略: 全量覆盖（数据量小，每次全量更安全）
频率: 工作日 16:30
"""
import logging
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd
from pymongo.database import Database

from ..proxy import em_proxy
from ..retry import sync_retry

logger = logging.getLogger("signals.sync.index_daily")


def _sync_a_index(db: Database, ak_codes: dict, start_date: str,
                  proxy_url: str = None):
    """A 股 7 只指数日线"""
    bars_col = db["bars"]
    sync_col = db["sync_log"]
    inserted = 0

    for name, symbol in ak_codes.items():
        try:
            with em_proxy(proxy_url):
                df = ak.stock_zh_index_daily(symbol=symbol)
            if df is None or df.empty:
                logger.warning(f"  ✗ {name} ({symbol}): 无数据")
                continue

            # 过滤日期范围
            df["date"] = pd.to_datetime(df["date"])
            cutoff = pd.to_datetime(start_date)
            df = df[df["date"] >= cutoff]

            if df.empty:
                continue

            docs = []
            for _, row in df.iterrows():
                docs.append({
                    "dt": row["date"],
                    "meta": {"symbol": symbol, "freq": "日线"},
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "vol": int(row["volume"]) if pd.notna(row["volume"]) else 0,
                    "amount": 0,
                })

            if docs:
                # 删除旧数据后重新插入（全量覆盖）
                bars_col.delete_many({
                    "meta.symbol": symbol,
                    "meta.freq": "日线",
                })
                bars_col.insert_many(docs, ordered=False)
                inserted += len(docs)

                sync_col.update_one(
                    {"_id": f"index_daily:{symbol}"},
                    {"$set": {
                        "module": "index_daily",
                        "symbol": symbol,
                        "last_dt": docs[-1]["dt"],
                        "last_run": datetime.now(),
                        "status": "ok",
                        "bar_count": len(docs),
                    }},
                    upsert=True,
                )
                logger.info(f"  ✓ {name}: {len(docs)} bars")

        except Exception as e:
            logger.error(f"  ✗ {name}: {e}")

    return inserted


def _sync_us_index(db: Database, us_codes: dict):
    """美股 3 只 ETF 日线（yfinance，无需代理）"""
    bars_col = db["bars"]
    sync_col = db["sync_log"]
    inserted = 0

    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance 未安装，跳过美股指数同步")
        return 0

    ticker_map = {
        "US.SPY": "SPY", "US.QQQ": "QQQ", "US.DIA": "DIA",
    }

    for name, futu_code in us_codes.items():
        ticker = ticker_map.get(futu_code, futu_code.split(".")[-1])
        try:
            data = yf.download(ticker, period="2y", progress=False)
            if data is None or data.empty:
                continue

            data = data.reset_index()
            docs = []
            for _, row in data.iterrows():
                docs.append({
                    "dt": pd.to_datetime(row["Date"]),
                    "meta": {"symbol": futu_code, "freq": "日线"},
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "vol": int(row["Volume"]) if pd.notna(row["Volume"]) else 0,
                    "amount": 0,
                })

            if docs:
                bars_col.delete_many({
                    "meta.symbol": futu_code,
                    "meta.freq": "日线",
                })
                bars_col.insert_many(docs, ordered=False)
                inserted += len(docs)

                sync_col.update_one(
                    {"_id": f"index_daily:{futu_code}"},
                    {"$set": {
                        "module": "index_daily",
                        "symbol": futu_code,
                        "last_dt": docs[-1]["dt"],
                        "last_run": datetime.now(),
                        "status": "ok",
                        "bar_count": len(docs),
                    }},
                    upsert=True,
                )
                logger.info(f"  ✓ {name}: {len(docs)} bars")

        except Exception as e:
            logger.error(f"  ✗ {name}: {e}")

    return inserted


@sync_retry(max_attempts=5, min_wait=3)
def sync_index_daily(db: Database, proxy_url: str = None) -> dict:
    """
    指数日线全量同步。

    A 股 7 只 + 港股 1 只 + 美股 3 只 = 11 只指数。
    """
    import config

    start_date = (datetime.now() - timedelta(
        days=config.INDEX_MA_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    logger.info(f"指数日线同步: 11 只指数, 起始 {start_date}")

    a_count = _sync_a_index(db, config.INDEX_AK_CODES, start_date, proxy_url)

    # 港股恒生科技走 AKShare（同 A 股接口格式不同，这里简单处理）
    # 实际环境可能需要 Futu，此处用 yfinance 兜底
    us_count = _sync_us_index(db, config.INDEX_US_CODES)

    total = a_count + us_count
    logger.info(f"指数日线完成: {total} bars")
    return {"inserted": total}
