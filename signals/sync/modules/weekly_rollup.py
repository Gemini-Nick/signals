# -*- coding: utf-8 -*-
"""Build cached weekly/monthly bars from verified daily bars."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pandas as pd
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.core.trading_dates import is_trading_day
from signals.sync.task_context import get_task_env
from signals.sync.volume_units import CANONICAL_STOCK_VOLUME_UNIT

logger = logging.getLogger("signals.sync.weekly_rollup")

DAILY_FREQS = ["日线", "daily", "D", "1d"]
WEEKLY_FREQ = "周线"
MONTHLY_FREQ = "月线"
ROLLUP_SPECS = {
    WEEKLY_FREQ: ("W-FRI", "weekly_rollup"),
    MONTHLY_FREQ: ("ME", "monthly_rollup"),
}
ROLLUP_INSERT_BATCH_SIZE = 1000
ROLLUP_MAX_SYMBOLS = 6000
ROLLUP_POSTMARKET_CANDIDATE_MAX_SYMBOLS = 300


def _daily_freq_priority(meta: object) -> int:
    if not isinstance(meta, dict):
        return 10
    freq = str(meta.get("freq") or "").strip()
    if freq == "日线":
        return 0
    if freq in {"daily", "D", "1d"}:
        return 1
    return 5


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(str(get_task_env(name, str(default)) or default).strip())
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _raw_a_code(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        raw = raw.split(".", 1)[1]
    if len(raw) >= 8 and raw[:2] in {"SH", "SZ", "BJ"}:
        raw = raw[2:]
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits if len(digits) == 6 else ""


def _prefixed_a_symbol(code: str) -> str:
    pure = _raw_a_code(code)
    if not pure:
        return str(code or "").strip()
    if pure.startswith(("5", "6", "9")):
        return f"SH.{pure}"
    if pure.startswith(("4", "8")):
        return f"BJ.{pure}"
    return f"SZ.{pure}"


def _append_candidate_symbol(out: list[str], value: Any) -> None:
    code = _raw_a_code(value)
    if code and code not in out:
        out.append(code)


def _symbol_query_values(symbols: list[str]) -> list[str]:
    values: list[str] = []
    for symbol in symbols:
        code = _raw_a_code(symbol)
        if not code:
            continue
        for value in (code, _prefixed_a_symbol(code)):
            if value and value not in values:
                values.append(value)
    return values


def _postmarket_rollup_scope() -> str:
    configured = str(get_task_env("WEEKLY_ROLLUP_SCOPE", "") or "").strip().lower()
    if configured:
        return configured
    if get_task_env("SIGNALS_POSTMARKET_RUN_ID") or get_task_env("SIGNALS_POSTMARKET_TRADE_DATE"):
        return "postmarket_candidates"
    return "all"


def _rollup_symbol_limit(scope: str) -> int:
    default = ROLLUP_POSTMARKET_CANDIDATE_MAX_SYMBOLS if scope == "postmarket_candidates" else ROLLUP_MAX_SYMBOLS
    return _env_int("WEEKLY_ROLLUP_MAX_SYMBOLS", default, minimum=1, maximum=ROLLUP_MAX_SYMBOLS)


def _symbols_with_daily(db: Database, collection: str) -> list[str]:
    try:
        return [
            symbol for symbol in db[collection].distinct("meta.symbol", {"meta.freq": {"$in": DAILY_FREQS}})
            if symbol
        ]
    except Exception:
        return []


def _daily_docs(db: Database, collection: str, symbol: str) -> list[dict[str, Any]]:
    return list(db[collection].find(
        {"meta.symbol": symbol, "meta.freq": {"$in": DAILY_FREQS}},
        {"_id": 0},
    ).sort("dt", 1))


def _symbols_from_stock_minute_selection(db: Database) -> list[str]:
    configured = str(get_task_env("WEEKLY_ROLLUP_SELECTION_META_ID", "") or "").strip()
    meta_ids = [configured] if configured else []
    if not meta_ids and (_postmarket_rollup_scope() == "postmarket_candidates" or get_task_env("SIGNALS_POSTMARKET_RUN_ID")):
        meta_ids.append("stock_minute:postmarket_selection:_meta")
    meta_ids.append("stock_minute:selection:_meta")
    doc: dict[str, Any] = {}
    for meta_id in dict.fromkeys(meta_ids):
        try:
            doc = db["sync_log"].find_one(
                {"_id": meta_id},
                {"selected_symbols": 1, "priority_symbols": 1, "pinned_symbols": 1},
            ) or {}
        except Exception:
            doc = {}
        if doc.get("selected_symbols") or doc.get("priority_symbols") or doc.get("pinned_symbols"):
            break
    symbols: list[str] = []
    for key in ("pinned_symbols", "priority_symbols", "selected_symbols"):
        for value in doc.get(key) or []:
            _append_candidate_symbol(symbols, value)
    return symbols


def _symbols_from_terminal_pool(db: Database) -> list[str]:
    try:
        doc = db["terminal_stock_pool"].find_one(
            {"pool": "terminal_stock_pool", "market": "A"},
            {"focus_stocks": 1, "stocks": 1, "watch_stocks": 1, "clue_stocks": 1},
            sort=[("updated_at", -1)],
        ) or {}
    except Exception:
        return []
    symbols: list[str] = []
    for key in ("focus_stocks", "stocks", "watch_stocks", "clue_stocks"):
        for row in doc.get(key) or []:
            if isinstance(row, dict):
                _append_candidate_symbol(symbols, row.get("raw_code") or row.get("symbol") or row.get("code"))
            else:
                _append_candidate_symbol(symbols, row)
    return symbols


def _postmarket_candidate_symbols(db: Database, *, limit: int) -> list[str]:
    symbols: list[str] = []
    for value in _symbols_from_stock_minute_selection(db):
        _append_candidate_symbol(symbols, value)
    for value in _symbols_from_terminal_pool(db):
        _append_candidate_symbol(symbols, value)
    return symbols[:limit]


def _postmarket_stock_symbols(db: Database) -> list[str]:
    trade_date = str(get_task_env("SIGNALS_POSTMARKET_TRADE_DATE", "") or "").strip()[:10]
    if not trade_date:
        return []
    limit = _rollup_symbol_limit("all")
    symbols: list[str] = []
    seen: set[str] = set()
    try:
        cursor = db["fullmarket_spot_snapshots"].find(
            {"trade_date": trade_date},
            {"_id": 0, "code": 1},
        )
    except Exception:
        return []
    for row in cursor:
        code = str(row.get("code") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        symbols.append(code)
        if len(symbols) >= limit:
            break
    return symbols


def _daily_docs_by_symbol(db: Database, collection: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    query: dict[str, Any] = {"meta.freq": {"$in": DAILY_FREQS}}
    if collection == "bars":
        scope = _postmarket_rollup_scope()
        limit = _rollup_symbol_limit(scope)
        symbols = _postmarket_candidate_symbols(db, limit=limit) if scope == "postmarket_candidates" else []
        if not symbols and (scope == "postmarket_candidates" or get_task_env("SIGNALS_POSTMARKET_TRADE_DATE")):
            symbols = _postmarket_stock_symbols(db)
        if symbols:
            query["meta.symbol"] = {"$in": _symbol_query_values(symbols)}
        elif scope == "postmarket_candidates" or get_task_env("SIGNALS_POSTMARKET_RUN_ID"):
            return grouped
    cursor = db[collection].find(
        query,
        {"_id": 0},
    )
    for doc in cursor:
        meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
        symbol = str(meta.get("symbol") or "").strip()
        if not symbol:
            continue
        grouped.setdefault(symbol, []).append(doc)
    return grouped


def _prepare_daily_frame(docs: list[dict[str, Any]], *, symbol: str = "") -> tuple[pd.DataFrame, str]:
    if not docs:
        return pd.DataFrame(), "A"
    source_meta = next((doc.get("meta") for doc in docs if isinstance(doc.get("meta"), dict)), {}) or {}
    market = str(source_meta.get("market") or ("HK" if str(symbol).upper().startswith("HK.") else "A")).upper()
    df = pd.DataFrame(docs)
    if df.empty or "dt" not in df.columns:
        return pd.DataFrame(), market
    df["dt"] = pd.to_datetime(df["dt"], errors="coerce")
    df = df.dropna(subset=["dt"])
    if df.empty:
        return pd.DataFrame(), market
    df["_freq_priority"] = df["meta"].map(_daily_freq_priority) if "meta" in df.columns else 10
    df["_row_order"] = range(len(df))
    df = (
        df.sort_values(["dt", "_freq_priority", "_row_order"])
        .drop_duplicates(subset=["dt"], keep="first")
        .sort_values("dt")
        .set_index("dt")
    )
    for column in ("open", "high", "low", "close", "vol", "amount"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    df["_quality"] = df["meta"].map(
        lambda meta: str(meta.get("quality") or "") if isinstance(meta, dict) else ""
    ) if "meta" in df.columns else ""
    required = ["open", "high", "low", "close"]
    if any(column not in df.columns for column in required):
        return pd.DataFrame(), market
    return df, market


def _next_trading_day_after(day, *, market: str) -> Any:
    cur = pd.to_datetime(day).date() + timedelta(days=1)
    for _ in range(20):
        if is_trading_day(market, cur):
            return cur
        cur += timedelta(days=1)
    return None


def _same_rollup_period(left, right, *, freq: str) -> bool:
    if not left or not right:
        return False
    left_ts = pd.Timestamp(left)
    right_ts = pd.Timestamp(right)
    if freq == WEEKLY_FREQ:
        return left_ts.to_period("W-FRI") == right_ts.to_period("W-FRI")
    if freq == MONTHLY_FREQ:
        return left_ts.to_period("M") == right_ts.to_period("M")
    return False


def _is_partial_period(data_as_of, *, period_end, freq: str, market: str) -> bool:
    data_day = pd.to_datetime(data_as_of).date()
    period_day = pd.to_datetime(period_end).date()
    if data_day >= period_day:
        return False
    next_day = _next_trading_day_after(data_day, market=market)
    return bool(next_day and _same_rollup_period(data_day, next_day, freq=freq))


def _period_docs(
    symbol: str,
    df: pd.DataFrame,
    *,
    collection: str,
    market: str,
    freq: str,
) -> list[dict[str, Any]]:
    if df.empty:
        return []
    resample_rule, source_name = ROLLUP_SPECS[freq]
    required = ["open", "high", "low", "close"]
    df["_source_dt"] = df.index
    period = df.resample(resample_rule).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "vol": "sum" if "vol" in df.columns else "first",
        "amount": "sum" if "amount" in df.columns else "first",
        "_source_dt": "last",
        "_quality": "last",
    })
    period = period.dropna(subset=required, how="any")
    source = source_name
    if collection == "index_bars":
        source = f"index_{source_name}"
    out: list[dict[str, Any]] = []
    for dt_idx, row in period.iterrows():
        period_end = pd.to_datetime(dt_idx)
        data_as_of = pd.to_datetime(row.get("_source_dt") or dt_idx)
        is_partial_period = _is_partial_period(data_as_of, period_end=period_end, freq=freq, market=market)
        display_dt = data_as_of if is_partial_period else period_end
        quality = str(row.get("_quality") or "").strip()
        out.append({
            "dt": display_dt,
            "meta": {
                "symbol": symbol,
                "freq": freq,
                "source": source,
                "market": market,
                "volume_unit": CANONICAL_STOCK_VOLUME_UNIT,
                "source_volume_unit": "daily_shares_rollup",
                "period_end": period_end.date().isoformat(),
                "data_as_of": data_as_of.date().isoformat(),
                "time_semantics": "period_data_as_of" if is_partial_period else "period_end",
                "is_partial_period": is_partial_period,
                **({"quality": quality, "source_quality": quality} if quality else {}),
                **({"asset_type": "index"} if collection == "index_bars" else {}),
            },
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "vol": int(row.get("vol") or 0),
            "amount": int(row.get("amount") or 0),
            "source": source,
        })
    return out


def _rollup_collection(db: Database, collection: str) -> dict[str, Any]:
    grouped = _daily_docs_by_symbol(db, collection)
    symbols = list(grouped)
    if not symbols:
        return {
            "symbols": 0,
            "daily_latest_dt": "",
            "daily_count": 0,
            "covered_symbols": 0,
            "missing_symbols": 0,
            "quality": "empty",
            "inserted_by_freq": {WEEKLY_FREQ: 0, MONTHLY_FREQ: 0},
            "latest_data_as_of": {},
        }
    docs_by_freq: dict[str, list[dict[str, Any]]] = {WEEKLY_FREQ: [], MONTHLY_FREQ: []}
    latest_daily_dt = ""
    daily_count = 0
    covered_symbols = 0
    has_provisional = False
    for symbol, docs in grouped.items():
        frame, market = _prepare_daily_frame(docs, symbol=symbol)
        if not frame.empty:
            covered_symbols += 1
            daily_count += len(frame)
            latest_daily_dt = max(latest_daily_dt, frame.index.max().date().isoformat())
            latest_quality = str(frame.iloc[-1].get("_quality") or "")
            has_provisional = bool(has_provisional or latest_quality == "provisional_close")
        for freq in (WEEKLY_FREQ, MONTHLY_FREQ):
            docs_by_freq[freq].extend(_period_docs(symbol, frame, collection=collection, market=market, freq=freq))
    db[collection].delete_many({"meta.symbol": {"$in": symbols}, "meta.freq": {"$in": [WEEKLY_FREQ, MONTHLY_FREQ]}})
    inserted_by_freq: dict[str, int] = {}
    latest_data_as_of: dict[str, str] = {}
    partial_by_freq: dict[str, bool] = {}
    for freq, docs in docs_by_freq.items():
        inserted_by_freq[freq] = len(docs)
        for doc in docs:
            meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
            data_as_of = str(meta.get("data_as_of") or "")
            if data_as_of:
                latest_data_as_of[freq] = max(latest_data_as_of.get(freq, ""), data_as_of)
            partial_by_freq[freq] = bool(partial_by_freq.get(freq) or meta.get("is_partial_period"))
        for start in range(0, len(docs), ROLLUP_INSERT_BATCH_SIZE):
            batch = docs[start:start + ROLLUP_INSERT_BATCH_SIZE]
            if batch:
                db[collection].insert_many(batch, ordered=False)
    return {
        "symbols": len(symbols),
        "daily_latest_dt": latest_daily_dt,
        "daily_count": daily_count,
        "covered_symbols": covered_symbols,
        "missing_symbols": max(0, len(symbols) - covered_symbols),
        "quality": "provisional_close" if has_provisional else "official",
        "inserted_by_freq": inserted_by_freq,
        "latest_data_as_of": latest_data_as_of,
        "partial_by_freq": partial_by_freq,
    }


def _write_rollup_freshness(
    db: Database,
    *,
    collection: str,
    freq: str,
    count: int,
    symbol_count: int,
    latest_dt: str,
    is_partial: bool,
    coverage_pct: float,
    missing_symbols: int,
    quality: str,
    now,
) -> None:
    db["data_freshness"].update_one(
        {"domain": "kline", "market": "A", "mode": "historical", "collection": collection, "freq": freq},
        {"$set": {
            "domain": "kline",
            "market": "A",
            "mode": "historical",
            "lane": "workbench_lane",
            "collection": collection,
            "freq": freq,
            "freshness": "fresh" if count else "empty",
            "latest_dt": latest_dt,
            "as_of": latest_dt,
            "data_as_of": latest_dt,
            "is_partial_period": is_partial,
            "coverage_pct": coverage_pct,
            "missing_symbols": missing_symbols,
            "quality": quality,
            "updated_at": now,
            "stale_reason": "" if count else f"{collection}_missing",
            "count": count,
            "symbol_count": symbol_count,
        }},
        upsert=True,
    )


def sync_weekly_rollup(db: Database, proxy_url: str = None) -> dict:
    """Generate cached weekly/monthly bars for stocks and indices from daily cache."""
    now = naive_market_now("A")
    stock_scope = _postmarket_rollup_scope()
    stock_stats = _rollup_collection(db, "bars")
    index_stats = _rollup_collection(db, "index_bars")
    stock_symbols = int(stock_stats.get("symbols") or 0)
    index_symbols = int(index_stats.get("symbols") or 0)
    stock_weekly = int((stock_stats.get("inserted_by_freq") or {}).get(WEEKLY_FREQ) or 0)
    index_weekly = int((index_stats.get("inserted_by_freq") or {}).get(WEEKLY_FREQ) or 0)
    stock_monthly = int((stock_stats.get("inserted_by_freq") or {}).get(MONTHLY_FREQ) or 0)
    index_monthly = int((index_stats.get("inserted_by_freq") or {}).get(MONTHLY_FREQ) or 0)
    daily_latest = max(str(stock_stats.get("daily_latest_dt") or ""), str(index_stats.get("daily_latest_dt") or ""))
    total_symbols = stock_symbols + index_symbols
    covered_symbols = int(stock_stats.get("covered_symbols") or 0) + int(index_stats.get("covered_symbols") or 0)
    missing_symbols = int(stock_stats.get("missing_symbols") or 0) + int(index_stats.get("missing_symbols") or 0)
    coverage_pct = round((covered_symbols / total_symbols * 100), 2) if total_symbols else 0.0
    quality = "provisional_close" if "provisional_close" in {str(stock_stats.get("quality") or ""), str(index_stats.get("quality") or "")} else ("empty" if not covered_symbols else "official")
    _write_rollup_freshness(
        db,
        collection="daily_bars",
        freq="日线",
        count=int(stock_stats.get("daily_count") or 0) + int(index_stats.get("daily_count") or 0),
        symbol_count=total_symbols,
        latest_dt=daily_latest,
        is_partial=False,
        coverage_pct=coverage_pct,
        missing_symbols=missing_symbols,
        quality=quality,
        now=now,
    )
    for freq, collection in ((WEEKLY_FREQ, "weekly_rollup"), (MONTHLY_FREQ, "monthly_rollup")):
        latest_data_as_of = max(
            str((stock_stats.get("latest_data_as_of") or {}).get(freq) or ""),
            str((index_stats.get("latest_data_as_of") or {}).get(freq) or ""),
        )
        _write_rollup_freshness(
            db,
            collection=collection,
            freq=freq,
            count=int((stock_stats.get("inserted_by_freq") or {}).get(freq) or 0) + int((index_stats.get("inserted_by_freq") or {}).get(freq) or 0),
            symbol_count=total_symbols,
            latest_dt=latest_data_as_of,
            is_partial=bool((stock_stats.get("partial_by_freq") or {}).get(freq) or (index_stats.get("partial_by_freq") or {}).get(freq)),
            coverage_pct=coverage_pct,
            missing_symbols=missing_symbols,
            quality=quality,
            now=now,
        )
    inserted = stock_weekly + index_weekly + stock_monthly + index_monthly
    logger.info("weekly/monthly rollup: +%d bars stocks=%d indices=%d scope=%s", inserted, stock_symbols, index_symbols, stock_scope)
    return {
        "inserted": inserted,
        "stock_symbols": stock_symbols,
        "index_symbols": index_symbols,
        "stock_inserted": stock_weekly,
        "index_inserted": index_weekly,
        "stock_weekly_inserted": stock_weekly,
        "index_weekly_inserted": index_weekly,
        "stock_monthly_inserted": stock_monthly,
        "index_monthly_inserted": index_monthly,
        "daily_latest_dt": daily_latest,
        "weekly_latest_data_as_of": max(
            str((stock_stats.get("latest_data_as_of") or {}).get(WEEKLY_FREQ) or ""),
            str((index_stats.get("latest_data_as_of") or {}).get(WEEKLY_FREQ) or ""),
        ),
        "monthly_latest_data_as_of": max(
            str((stock_stats.get("latest_data_as_of") or {}).get(MONTHLY_FREQ) or ""),
            str((index_stats.get("latest_data_as_of") or {}).get(MONTHLY_FREQ) or ""),
        ),
        "coverage_pct": coverage_pct,
        "missing_symbols": missing_symbols,
        "quality": quality,
        "stock_scope": stock_scope,
        "stock_symbol_limit": _rollup_symbol_limit(stock_scope),
    }
