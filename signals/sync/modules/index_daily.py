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
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.core.macro_universe import macro_a_index_codes
from signals.data.bar_quality import validate_ohlcv_bar
from ..proxy import em_proxy
from ..retry import sync_retry

logger = logging.getLogger("signals.sync.index_daily")


def _pure_index_code(symbol: str) -> str:
    return str(symbol).replace("sh", "").replace("sz", "").replace("SH", "").replace("SZ", "")


def _number(value, default: float | None = None) -> float | None:
    if value in (None, "", "-"):
        return default
    parsed = pd.to_numeric(value, errors="coerce")
    if pd.isna(parsed):
        return default
    return float(parsed)


def _quote_symbol_for_index(symbol: str) -> str:
    pure = _pure_index_code(symbol)
    prefix = str(symbol or "")[:2].upper()
    if prefix in {"SH", "SZ", "BJ"}:
        return f"{prefix}.{pure}"
    if pure.startswith(("5", "6", "9", "0")):
        return f"SH.{pure}"
    return f"SZ.{pure}"


def _quote_candidates_for_index(symbol: str) -> list[str]:
    dotted = _quote_symbol_for_index(symbol)
    compact = dotted.replace(".", "")
    return list(dict.fromkeys([
        dotted,
        compact,
        compact.lower(),
        symbol,
        str(symbol).upper(),
        str(symbol).lower(),
    ]))


def _write_index_docs(db: Database, symbol: str, freq: str, docs: list[dict], replace_bars: bool = True) -> int:
    """Write index bars to the dedicated collection and compatibility bars."""
    if not docs:
        return 0
    index_docs = []
    bars_docs = []
    for doc in docs:
        item = dict(doc)
        item["meta"] = {**item.get("meta", {}), "symbol": symbol, "freq": freq, "asset_type": "index"}
        if all(field in item for field in ("open", "high", "low", "close", "vol", "dt")):
            accepted, reason = validate_ohlcv_bar(item, allow_zero_volume=False)
            if not accepted:
                logger.warning("reject index bar %s %s: %s", symbol, item.get("dt"), reason)
                continue
        index_docs.append(item)
        bars_docs.append(dict(item))
    if not index_docs:
        return 0

    index_col = db["index_bars"]
    index_col.delete_many({"meta.symbol": symbol, "meta.freq": freq})
    index_col.insert_many(index_docs, ordered=False)

    if replace_bars:
        bars_col = db["bars"]
        bars_col.delete_many({"meta.symbol": symbol, "meta.freq": freq})
        bars_col.insert_many(bars_docs, ordered=False)
    return len(index_docs)


def _merge_existing_older_index_docs(db: Database, symbol: str, freq: str, docs: list[dict]) -> list[dict]:
    """Keep older cached index history when the current provider returns a shorter window."""
    if not docs:
        return []
    first_dt = min(doc["dt"] for doc in docs if doc.get("dt") is not None)
    cached: list[dict] = []
    for collection in ("index_bars", "bars"):
        try:
            cached.extend(list(db[collection].find(
                {"meta.symbol": symbol, "meta.freq": freq, "dt": {"$lt": first_dt}},
                {"_id": 0},
            )))
        except Exception as exc:
            logger.debug("读取旧指数缓存失败 %s %s: %s", collection, symbol, exc)

    deduped: dict[object, dict] = {}
    for doc in cached + docs:
        dt = doc.get("dt")
        if dt is None:
            continue
        item = dict(doc)
        item.pop("_id", None)
        item["meta"] = {**item.get("meta", {}), "symbol": symbol, "freq": freq, "asset_type": "index"}
        deduped[dt] = item
    return [deduped[dt] for dt in sorted(deduped)]


def _fetch_a_index_daily_frame(symbol: str, start_date: str, end_date: str, proxy_url: str | None = None) -> tuple[pd.DataFrame, str]:
    start_compact = pd.to_datetime(start_date).strftime("%Y%m%d")
    end_compact = pd.to_datetime(end_date).strftime("%Y%m%d")
    source_errors: list[str] = []
    with em_proxy(proxy_url):
        try:
            df = ak.stock_zh_index_daily_em(symbol=symbol, start_date=start_compact, end_date=end_compact)
            if df is not None and not df.empty:
                return df, "akshare_em"
        except Exception as exc:
            source_errors.append(f"stock_zh_index_daily_em:{exc}")
        try:
            df = ak.stock_zh_index_daily(symbol=symbol)
            if df is not None and not df.empty:
                return df, "akshare_sina"
        except Exception as exc:
            source_errors.append(f"stock_zh_index_daily:{exc}")
    logger.warning("  ✗ %s: 指数历史源失败 %s", symbol, "; ".join(source_errors)[:240])
    return pd.DataFrame(), ""


def _replace_exact_bar_docs(col, docs: list[dict]) -> int:
    """Replace exact bar measurements without UpdateOne; Mongo time-series rejects non-multi updates."""
    if not docs:
        return 0

    deduped: dict[tuple[str, str, object], dict] = {}
    for doc in docs:
        item = dict(doc)
        item.pop("_id", None)
        meta = item.get("meta") or {}
        symbol = meta.get("symbol")
        freq = meta.get("freq")
        dt = item.get("dt")
        if not symbol or not freq or dt is None:
            continue
        if all(field in item for field in ("open", "high", "low", "close", "vol", "dt")):
            accepted, reason = validate_ohlcv_bar(item, allow_zero_volume=False)
            if not accepted:
                logger.warning("reject exact index bar %s %s: %s", symbol, dt, reason)
                continue
        deduped[(str(symbol), str(freq), dt)] = item

    prepared = list(deduped.values())
    if not prepared:
        return 0

    grouped: dict[tuple[str, str], list[object]] = {}
    for symbol, freq, dt in deduped:
        grouped.setdefault((symbol, freq), []).append(dt)

    for (symbol, freq), dts in grouped.items():
        col.delete_many({"meta.symbol": symbol, "meta.freq": freq, "dt": {"$in": dts}})
    col.insert_many(prepared, ordered=False)
    return len(prepared)


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


def _previous_daily_close(db: Database, symbol: str, day: str) -> float | None:
    try:
        dt = datetime.fromisoformat(day)
        doc = db["index_bars"].find_one(
            {"meta.symbol": symbol, "meta.freq": {"$in": ["日线", "daily", "D", "1d"]}, "dt": {"$lt": dt}},
            {"close": 1},
            sort=[("dt", -1)],
        ) or {}
        return _number(doc.get("close"))
    except Exception:
        return None


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


def _quote_day_text(doc: dict) -> str:
    value = doc.get("dt") or doc.get("trade_date") or doc.get("snapshot_at")
    if hasattr(value, "date"):
        return value.date().isoformat()
    return str(value or "")[:10]


def _daily_doc_from_quote_snapshot(symbol: str, expected_day: str, quote_doc: dict) -> dict | None:
    if not quote_doc:
        return None
    if quote_doc.get("is_stale") or quote_doc.get("freshness") == "stale":
        return None
    quote_day = _quote_day_text(quote_doc)
    if quote_day != expected_day:
        return None

    open_price = _number(quote_doc.get("open"))
    high = _number(quote_doc.get("high"))
    low = _number(quote_doc.get("low"))
    close = _number(quote_doc.get("close"), _number(quote_doc.get("price")))
    if not all(value is not None and value > 0 for value in (open_price, high, low, close)):
        return None

    change_pct = _number(quote_doc.get("change_pct"))
    change = _number(quote_doc.get("change"))
    prev_close = _number(quote_doc.get("prev_close"))
    source = quote_doc.get("source") or "quote_snapshots"
    meta = {
        "symbol": symbol,
        "freq": "日线",
        "asset_type": "index",
        "market": "A",
        "source": source,
        "source_type": "direct_quote_ohlcv",
        "quality": "provisional_close",
        "trade_date": expected_day,
        "quote_symbol": quote_doc.get("symbol") or _quote_symbol_for_index(symbol),
        "quote_snapshot_at": quote_doc.get("snapshot_at"),
        "fallback_reason": "historical_daily_lagged_expected_trade_day",
    }
    doc = {
        "dt": datetime.fromisoformat(expected_day),
        "meta": meta,
        "open": float(open_price),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "vol": int(_number(quote_doc.get("vol"), 0) or 0),
        "amount": float(_number(quote_doc.get("amount"), 0) or 0),
        "source": source,
    }
    if prev_close is not None:
        doc["prev_close"] = float(prev_close)
    if change is not None:
        doc["change"] = float(change)
    if change_pct is not None:
        doc["change_pct"] = float(change_pct)
        doc["pct_chg"] = float(change_pct)
    return doc


def _fallback_today_from_quote_snapshots(db: Database, index_codes: dict[str, str]) -> int:
    """Patch today's index daily bar from post-close quote snapshots before minute rollup."""
    expected_day = _expected_trade_day()
    index_docs = []
    bars_docs = []
    patched = 0
    for name, symbol in index_codes.items():
        if _latest_daily_dt(db, symbol) == expected_day:
            continue
        candidates = _quote_candidates_for_index(symbol)
        try:
            quote_doc = db["quote_snapshots"].find_one(
                {"symbol": {"$in": candidates}},
                {"_id": 0},
                sort=[("snapshot_at", -1), ("dt", -1)],
            ) or {}
        except Exception as exc:
            logger.debug("  ✗ %s (%s): quote snapshot lookup failed: %s", name, symbol, exc)
            quote_doc = {}
        doc = _daily_doc_from_quote_snapshot(symbol, expected_day, quote_doc)
        if not doc:
            continue
        index_docs.append(doc)
        bars_docs.append(dict(doc))
        db["sync_log"].update_one(
            {"_id": f"index_daily:{symbol}"},
            {"$set": {
                "module": "index_daily",
                "symbol": symbol,
                "last_dt": doc["dt"],
                "last_run": naive_market_now("A"),
                "status": "ok",
                "source": doc["meta"]["source"],
                "source_type": "direct_quote_ohlcv",
                "fallback_source": doc["meta"]["source"],
                "fallback_reason": doc["meta"]["fallback_reason"],
                "quality": doc["meta"]["quality"],
            }},
            upsert=True,
        )
        patched += 1
        logger.info("  ↳ %s: 用 quote 收盘快照补 %s 指数日线 close=%.3f", name, expected_day, doc["close"])
    if index_docs:
        _replace_exact_bar_docs(db["index_bars"], index_docs)
    if bars_docs:
        _replace_exact_bar_docs(db["bars"], bars_docs)
    return patched


def _fallback_today_from_minute_bars(db: Database, index_codes: dict[str, str]) -> int:
    """Patch today's index daily bar from verified minute cache when daily provider lags."""
    expected_day = _expected_trade_day()
    index_docs = []
    bars_docs = []
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
                "source_type": "derived",
                "quality": "estimated_close",
                "derived_from_freq": source_freq,
                "rollup_reason": "direct_daily_and_quote_missing",
            },
            "open": float(frame["open"].iloc[0]),
            "high": float(frame["high"].max()),
            "low": float(frame["low"].min()),
            "close": float(frame["close"].iloc[-1]),
            "vol": int(frame["vol"].fillna(0).sum()) if "vol" in frame.columns else 0,
            "amount": float(frame["amount"].fillna(0).sum()) if "amount" in frame.columns else 0,
        }
        previous_close = _previous_daily_close(db, symbol, expected_day)
        if previous_close:
            change_pct = round((doc["close"] - previous_close) / previous_close * 100.0, 4)
            doc["prev_close"] = float(previous_close)
            doc["change"] = round(doc["close"] - previous_close, 4)
            doc["change_pct"] = change_pct
            doc["pct_chg"] = change_pct
        index_docs.append(doc)
        bars_docs.append(dict(doc))
        db["sync_log"].update_one(
            {"_id": f"index_daily:{symbol}"},
            {"$set": {
                "module": "index_daily",
                "symbol": symbol,
                "last_dt": doc["dt"],
                "last_run": naive_market_now("A"),
                "status": "ok",
                "source": "index_minute_rollup",
                "source_type": "derived",
                "quality": "estimated_close",
            }},
            upsert=True,
        )
        patched += 1
        logger.info("  ↳ %s: 用 %s 合成 %s 指数日线", name, source_freq, expected_day)
    if not index_docs and not bars_docs:
        return 0
    if index_docs:
        _replace_exact_bar_docs(db["index_bars"], index_docs)
    if bars_docs:
        _replace_exact_bar_docs(db["bars"], bars_docs)
    return patched


def _sync_a_index(db: Database, ak_codes: dict, start_date: str,
                  proxy_url: str = None):
    """A 股 7 只指数日线"""
    sync_col = db["sync_log"]
    inserted = 0
    end_date = naive_market_now("A").strftime("%Y-%m-%d")

    for name, symbol in ak_codes.items():
        try:
            df, source = _fetch_a_index_daily_frame(symbol, start_date, end_date, proxy_url)
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
                    "meta": {"symbol": symbol, "freq": "日线", "asset_type": "index", "market": "A", "source": source or "akshare"},
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "vol": int(row["volume"]) if pd.notna(row["volume"]) else 0,
                    "amount": float(row.get("amount", 0) or 0),
                })

            if docs:
                docs = _merge_existing_older_index_docs(db, symbol, "日线", docs)
                history_days = (docs[-1]["dt"] - docs[0]["dt"]).days if len(docs) >= 2 else 0
                history_short = docs[0]["dt"] > cutoff
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
                        "history_start": docs[0]["dt"],
                        "history_days": history_days,
                        "history_short": history_short,
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
            now = naive_market_now("US")
            data = yf.download(
                ticker,
                start=(now - timedelta(days=365 * 5)).strftime("%Y-%m-%d"),
                end=(now + timedelta(days=1)).strftime("%Y-%m-%d"),
                interval="1d",
                auto_adjust=False,
                progress=False,
            )
            if data is None or data.empty:
                continue

            if isinstance(data.columns, pd.MultiIndex):
                data.columns = [col[0] for col in data.columns]
            data = data.reset_index()
            docs = []
            for _, row in data.iterrows():
                doc = {
                    "dt": pd.to_datetime(scalar(row["Date"])),
                    "meta": {"symbol": futu_code, "freq": "日线", "asset_type": "index", "market": "US", "source": "yfinance"},
                    "open": float(scalar(row["Open"])),
                    "high": float(scalar(row["High"])),
                    "low": float(scalar(row["Low"])),
                    "close": float(scalar(row["Close"])),
                    "vol": int(scalar(row["Volume"])) if pd.notna(scalar(row["Volume"])) else 0,
                    "amount": 0,
                }
                accepted, reason = validate_ohlcv_bar(doc, allow_zero_volume=False)
                if accepted:
                    docs.append(doc)
                else:
                    logger.warning("  ✗ %s %s rejected: %s", name, doc.get("dt"), reason)

            if docs:
                cutoff = naive_market_now("US") - timedelta(days=365 * 5)
                docs = _merge_existing_older_index_docs(db, futu_code, "日线", docs)
                history_days = (docs[-1]["dt"] - docs[0]["dt"]).days if len(docs) >= 2 else 0
                history_short = docs[0]["dt"].to_pydatetime() > cutoff if hasattr(docs[0]["dt"], "to_pydatetime") else docs[0]["dt"] > cutoff
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
                        "history_start": docs[0]["dt"],
                        "history_days": history_days,
                        "history_short": history_short,
                    }},
                    upsert=True,
                )
                logger.info(f"  ✓ {name}: {written} bars")
            else:
                sync_col.update_one(
                    {"_id": f"index_daily:{futu_code}"},
                    {"$set": {
                        "module": "index_daily",
                        "symbol": futu_code,
                        "last_run": naive_market_now("US"),
                        "status": "unavailable",
                        "bar_count": 0,
                        "reason": "provider_rows_failed_ohlcv_validation",
                    }},
                    upsert=True,
                )

        except Exception as e:
            logger.error(f"  ✗ {name}: {e}")

    return inserted


def _fallback_from_existing_bars(db: Database, index_codes: dict[str, str]) -> int:
    """Seed index_bars from already cached bars when external providers fail."""
    copied_docs = []
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
            copied_docs.append(item)
        logger.info("  ↳ %s: copied %d cached bars into index_bars", name, len(docs))
    if not copied_docs:
        return 0
    return _replace_exact_bar_docs(db["index_bars"], copied_docs)


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
    quote_close_count = _fallback_today_from_quote_snapshots(db, a_index_codes)
    minute_rollup_count = _fallback_today_from_minute_bars(db, a_index_codes)

    # 港股恒生科技走 AKShare（同 A 股接口格式不同，这里简单处理）
    # 实际环境可能需要 Futu，此处用 yfinance 兜底
    us_count = _sync_us_index(db, config.INDEX_US_CODES)

    total = a_count + us_count + quote_close_count + minute_rollup_count
    if total == 0:
        total = _fallback_from_existing_bars(db, a_index_codes)
    logger.info(f"指数日线完成: {total} bars")
    return {
        "inserted": total,
        "quote_close_patched": quote_close_count,
        "minute_rollup_patched": minute_rollup_count,
    }
