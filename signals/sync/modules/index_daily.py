# -*- coding: utf-8 -*-
"""
指数日线同步 — 宏观观察指数全量覆盖

数据源: AKShare stock_zh_index_daily（A股宏观观察池）
        + yfinance（美股 3 只）
策略: 全量覆盖（数据量小，每次全量更安全）
频率: 工作日 16:30
"""
import logging
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd
from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.core.macro_universe import macro_a_index_codes
from ..proxy import em_proxy
from ..retry import sync_retry

logger = logging.getLogger("signals.sync.index_daily")


def _pure_index_code(symbol: str) -> str:
    return str(symbol).replace("sh", "").replace("sz", "").replace("SH", "").replace("SZ", "")


def _write_index_docs(db: Database, symbol: str, freq: str, docs: list[dict], replace_bars: bool = True) -> int:
    """Write index bars to the dedicated collection and compatibility bars."""
    if not docs:
        return 0
    index_docs = []
    bars_docs = []
    for doc in docs:
        item = dict(doc)
        item["meta"] = {**item.get("meta", {}), "symbol": symbol, "freq": freq, "asset_type": "index"}
        index_docs.append(item)
        bars_docs.append(dict(item))

    index_col = db["index_bars"]
    index_col.delete_many({"meta.symbol": symbol, "meta.freq": freq})
    index_col.insert_many(index_docs, ordered=False)

    if replace_bars:
        bars_col = db["bars"]
        bars_col.delete_many({"meta.symbol": symbol, "meta.freq": freq})
        bars_col.insert_many(bars_docs, ordered=False)
    return len(index_docs)


def _latest_daily_dt(db: Database, symbol: str) -> str:
    doc = db["index_bars"].find_one(
        {"meta.symbol": symbol, "meta.freq": {"$in": ["日线", "daily", "D", "1d"]}},
        {"dt": 1},
        sort=[("dt", -1)],
    ) or {}
    value = doc.get("dt")
    try:
        return pd.to_datetime(value).date().isoformat()
    except Exception:
        return ""


def _expected_trade_day() -> str:
    try:
        from signals.data.mongo_fallback import get_last_trading_day

        return str(get_last_trading_day("A"))[:10]
    except Exception:
        return naive_market_now("A").date().isoformat()


def _minute_docs_for_day(db: Database, symbol: str, day: str) -> tuple[list[dict], str]:
    start = datetime.fromisoformat(day)
    end = start + timedelta(days=1)
    for freq in ("5分钟", "5min", "15分钟", "15min", "30分钟", "30min"):
        docs = list(db["index_bars"].find(
            {
                "meta.symbol": symbol,
                "meta.freq": freq,
                "dt": {"$gte": start, "$lt": end},
            },
            {"_id": 0},
        ).sort("dt", 1))
        if docs:
            return docs, freq
    return [], ""


def _fallback_today_from_minute_bars(db: Database, index_codes: dict[str, str]) -> int:
    """Patch today's index daily bar from verified minute cache when daily provider lags."""
    expected_day = _expected_trade_day()
    index_ops = []
    bars_ops = []
    patched = 0
    for name, symbol in index_codes.items():
        if _latest_daily_dt(db, symbol) == expected_day:
            continue
        minute_docs, source_freq = _minute_docs_for_day(db, symbol, expected_day)
        if not minute_docs:
            logger.warning("  ✗ %s (%s): 日线缺 %s，分钟线也缺", name, symbol, expected_day)
            continue
        frame = pd.DataFrame(minute_docs)
        for column in ("open", "high", "low", "close", "vol", "amount"):
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=["open", "high", "low", "close"], how="any")
        if frame.empty:
            continue
        doc = {
            "dt": datetime.fromisoformat(expected_day),
            "meta": {
                "symbol": symbol,
                "freq": "日线",
                "asset_type": "index",
                "market": "A",
                "source": "index_minute_rollup",
                "derived_from_freq": source_freq,
            },
            "open": float(frame["open"].iloc[0]),
            "high": float(frame["high"].max()),
            "low": float(frame["low"].min()),
            "close": float(frame["close"].iloc[-1]),
            "vol": int(frame["vol"].fillna(0).sum()) if "vol" in frame.columns else 0,
            "amount": float(frame["amount"].fillna(0).sum()) if "amount" in frame.columns else 0,
        }
        selector = {"meta.symbol": symbol, "meta.freq": "日线", "dt": doc["dt"]}
        index_ops.append(UpdateOne(selector, {"$set": doc}, upsert=True))
        bars_ops.append(UpdateOne(selector, {"$set": doc}, upsert=True))
        db["sync_log"].update_one(
            {"_id": f"index_daily:{symbol}"},
            {"$set": {
                "module": "index_daily",
                "symbol": symbol,
                "last_dt": doc["dt"],
                "last_run": naive_market_now("A"),
                "status": "ok",
                "source": "index_minute_rollup",
            }},
            upsert=True,
        )
        patched += 1
        logger.info("  ↳ %s: 用 %s 合成 %s 指数日线", name, source_freq, expected_day)
    if not index_ops and not bars_ops:
        return 0
    if index_ops:
        db["index_bars"].bulk_write(index_ops, ordered=False)
    if bars_ops:
        db["bars"].bulk_write(bars_ops, ordered=False)
    return patched


def _sync_a_index(db: Database, ak_codes: dict, start_date: str,
                  proxy_url: str = None):
    """A 股 7 只指数日线"""
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
                        "meta": {"symbol": symbol, "freq": "日线", "asset_type": "index", "market": "A", "source": "akshare"},
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "vol": int(row["volume"]) if pd.notna(row["volume"]) else 0,
                    "amount": 0,
                })

            if docs:
                written = _write_index_docs(db, symbol, "日线", docs)
                inserted += written

                sync_col.update_one(
                    {"_id": f"index_daily:{symbol}"},
                    {"$set": {
                        "module": "index_daily",
                        "symbol": symbol,
                        "last_dt": docs[-1]["dt"],
                        "last_run": naive_market_now("A"),
                        "status": "ok",
                        "bar_count": written,
                    }},
                    upsert=True,
                )
                logger.info(f"  ✓ {name}: {written} bars")

        except Exception as e:
            logger.error(f"  ✗ {name}: {e}")

    return inserted


def _sync_us_index(db: Database, us_codes: dict):
    """美股 3 只 ETF 日线（yfinance，无需代理）"""
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

    def scalar(value):
        if isinstance(value, pd.Series):
            return value.iloc[0] if not value.empty else None
        return value

    for name, futu_code in us_codes.items():
        ticker = ticker_map.get(futu_code, futu_code.split(".")[-1])
        try:
            data = yf.download(ticker, period="2y", progress=False)
            if data is None or data.empty:
                continue

            if isinstance(data.columns, pd.MultiIndex):
                data.columns = [col[0] for col in data.columns]
            data = data.reset_index()
            docs = []
            for _, row in data.iterrows():
                docs.append({
                    "dt": pd.to_datetime(scalar(row["Date"])),
                    "meta": {"symbol": futu_code, "freq": "日线", "asset_type": "index", "market": "US", "source": "yfinance"},
                    "open": float(scalar(row["Open"])),
                    "high": float(scalar(row["High"])),
                    "low": float(scalar(row["Low"])),
                    "close": float(scalar(row["Close"])),
                    "vol": int(scalar(row["Volume"])) if pd.notna(scalar(row["Volume"])) else 0,
                    "amount": 0,
                })

            if docs:
                written = _write_index_docs(db, futu_code, "日线", docs)
                inserted += written

                sync_col.update_one(
                    {"_id": f"index_daily:{futu_code}"},
                    {"$set": {
                        "module": "index_daily",
                        "symbol": futu_code,
                        "last_dt": docs[-1]["dt"],
                        "last_run": naive_market_now("US"),
                        "status": "ok",
                        "bar_count": written,
                    }},
                    upsert=True,
                )
                logger.info(f"  ✓ {name}: {written} bars")

        except Exception as e:
            logger.error(f"  ✗ {name}: {e}")

    return inserted


def _fallback_from_existing_bars(db: Database, index_codes: dict[str, str]) -> int:
    """Seed index_bars from already cached bars when external providers fail."""
    ops = []
    for name, symbol in index_codes.items():
        pure = _pure_index_code(symbol)
        candidates = [symbol, pure, symbol.upper(), symbol.lower()]
        docs = list(db["bars"].find(
            {
                "meta.symbol": {"$in": candidates},
                "meta.freq": {"$in": ["daily", "日线", "D", "1d"]},
            },
            {"_id": 0},
        ).sort("dt", 1))
        if not docs:
            continue
        for doc in docs:
            item = dict(doc)
            item["meta"] = {**item.get("meta", {}), "symbol": symbol, "freq": item.get("meta", {}).get("freq", "日线"), "asset_type": "index"}
            item["source"] = item.get("source") or "bars_fallback"
            ops.append(UpdateOne(
                {"meta.symbol": symbol, "meta.freq": item["meta"]["freq"], "dt": item["dt"]},
                {"$set": item},
                upsert=True,
            ))
        logger.info("  ↳ %s: copied %d cached bars into index_bars", name, len(docs))
    if not ops:
        return 0
    result = db["index_bars"].bulk_write(ops, ordered=False)
    return int(result.upserted_count + result.modified_count)


@sync_retry(max_attempts=5, min_wait=3)
def sync_index_daily(db: Database, proxy_url: str = None) -> dict:
    """
    指数日线全量同步。

    A 股宏观观察池 + 美股 3 只指数。
    """
    import config

    start_date = (naive_market_now("A") - timedelta(
        days=config.INDEX_MA_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    a_index_codes = macro_a_index_codes()
    logger.info(f"指数日线同步: A股 {len(a_index_codes)} 只指数, 起始 {start_date}")

    a_count = _sync_a_index(db, a_index_codes, start_date, proxy_url)
    minute_rollup_count = _fallback_today_from_minute_bars(db, a_index_codes)

    # 港股恒生科技走 AKShare（同 A 股接口格式不同，这里简单处理）
    # 实际环境可能需要 Futu，此处用 yfinance 兜底
    us_count = _sync_us_index(db, config.INDEX_US_CODES)

    total = a_count + us_count + minute_rollup_count
    if total == 0:
        total = _fallback_from_existing_bars(db, a_index_codes)
    logger.info(f"指数日线完成: {total} bars")
    return {"inserted": total, "minute_rollup_patched": minute_rollup_count}
