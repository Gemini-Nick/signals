# -*- coding: utf-8 -*-
"""HK stock daily bar sync for the postmarket A+H technical scan."""
from __future__ import annotations

import logging
import os
import time
import io
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dt_time, timedelta
from typing import Any

import akshare as ak
import pandas as pd
import requests
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.core.trading_dates import is_trading_day, trading_day_key
from signals.data.fetcher import no_proxy
from signals.sync.task_context import get_task_env
from signals.sync.volume_units import CANONICAL_STOCK_VOLUME_UNIT, normalize_stock_volume

logger = logging.getLogger("signals.sync.hk_stock_daily")
MIN_DAILY_HISTORY_DAYS = 365 * 5

DAILY_FREQ = "日线"
_PROGRESS_META_ID = "hk_stock_daily:progress:_meta"
_HKEX_SECURITIES_XLSX_URL = "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"
_TENCENT_FQKLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_SINA_DAILY_LOCK = threading.Lock()


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(get_task_env(name, os.getenv(name, str(default))) or default)
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(get_task_env(name, os.getenv(name, str(default))) or default))
    except (TypeError, ValueError):
        return max(minimum, default)


def _pure_hk_code(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        raw = raw.split(".", 1)[1]
    raw = raw.replace("HK", "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits.zfill(5) if digits and len(digits) <= 5 else ""


def _hk_symbol(code: Any) -> str:
    pure = _pure_hk_code(code)
    return f"HK.{pure}" if pure else ""


def _shard_count() -> int:
    return _env_int("HK_STOCK_DAILY_SHARD_COUNT", 1, minimum=1, maximum=64)


def _shard_index() -> int:
    count = _shard_count()
    return min(_env_int("HK_STOCK_DAILY_SHARD_INDEX", 0, minimum=0, maximum=63), count - 1)


def _shard_key() -> str:
    return str(get_task_env("HK_STOCK_DAILY_SHARD_KEY", "") or "").strip() or "all"


def _progress_meta_id(shard_key: str | None = None) -> str:
    key = shard_key or _shard_key()
    if key and key != "all":
        return f"hk_stock_daily:progress:{key}"
    return _PROGRESS_META_ID


def _apply_code_shard(codes: list[str]) -> tuple[list[str], dict[str, int | str]]:
    count = _shard_count()
    index = _shard_index()
    key = _shard_key()
    if count <= 1:
        return codes, {"shard_key": key, "shard_index": index, "shard_count": count, "global_total": len(codes)}
    shard_codes = [code for position, code in enumerate(codes) if position % count == index]
    return shard_codes, {"shard_key": key, "shard_index": index, "shard_count": count, "global_total": len(codes)}


def _select_hk_due_codes(
    codes: list[str],
    sync_docs: dict[str, Any],
    short_history_codes: set[str],
    end_date: str,
    max_codes: int,
) -> tuple[list[str], int]:
    """Select a bounded due batch without starving the universe tail."""
    if max_codes <= 0:
        return list(codes), 0
    due_codes: list[str] = []
    for code in codes:
        last_dt = _coerce_last_dt(sync_docs.get(_hk_symbol(code)))
        if code in short_history_codes or last_dt is None or last_dt.strftime("%Y%m%d") < end_date:
            due_codes.append(code)
    return due_codes[:max_codes], max(0, len(due_codes) - max_codes)


def _hk_daily_end_date_key(now: datetime | None = None) -> str:
    local = now or naive_market_now("HK")
    try:
        return trading_day_key("HK", now=local, compact=True, open_time=dt_time(9, 30))
    except Exception:
        if local.weekday() < 5 and local.hour >= 9 and (local.hour > 9 or local.minute >= 30):
            return local.strftime("%Y%m%d")
        d = local - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.strftime("%Y%m%d")


def _coerce_last_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if value:
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d")
        except ValueError:
            try:
                return pd.to_datetime(value).to_pydatetime()
            except Exception:
                return None
    return None


def _daily_doc_trade_date(doc: dict[str, Any]) -> datetime | None:
    try:
        parsed = pd.to_datetime(doc.get("dt"), errors="coerce")
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime().replace(hour=0, minute=0, second=0, microsecond=0)


def _valid_hk_daily_doc(doc: dict[str, Any], *, end_date: str | None = None) -> bool:
    dt = _daily_doc_trade_date(doc)
    if dt is None:
        return False
    if end_date and dt.strftime("%Y%m%d") > str(end_date)[:8]:
        return False
    prices = [doc.get(field) for field in ("open", "high", "low", "close")]
    try:
        if any(float(value) <= 0 for value in prices):
            return False
    except (TypeError, ValueError):
        return False
    if doc.get("low") > doc.get("high"):
        return False
    if doc.get("open") > doc.get("high") or doc.get("open") < doc.get("low"):
        return False
    if doc.get("close") > doc.get("high") or doc.get("close") < doc.get("low"):
        return False
    return is_trading_day("HK", dt.date())


def _pick_column(df: pd.DataFrame, *names: str) -> str | None:
    for name in names:
        if name in df.columns:
            return name
    lower_map = {str(col).strip().lower(): col for col in df.columns}
    for name in names:
        found = lower_map.get(name.lower())
        if found is not None:
            return found
    return None


def _normalize_ohlc_bounds(open_value: Any, high_value: Any, low_value: Any, close_value: Any) -> dict[str, float] | None:
    try:
        values = {
            "open": float(open_value),
            "high": float(high_value),
            "low": float(low_value),
            "close": float(close_value),
        }
    except (TypeError, ValueError):
        return None
    if any(pd.isna(value) for value in values.values()):
        return None
    values["high"] = max(values.values())
    values["low"] = min(values.values())
    return values


def _docs_from_hk_daily_df(code: str, df: pd.DataFrame, source: str, *, end_date: str | None = None) -> list[dict[str, Any]]:
    """Convert AKShare HK daily/hist frames into canonical Mongo bar docs."""
    if df is None or df.empty:
        return []
    columns = {
        "dt": _pick_column(df, "日期", "date", "dt", "时间"),
        "open": _pick_column(df, "开盘", "open"),
        "high": _pick_column(df, "最高", "high"),
        "low": _pick_column(df, "最低", "low"),
        "close": _pick_column(df, "收盘", "close"),
        "vol": _pick_column(df, "成交量", "volume", "vol"),
        "amount": _pick_column(df, "成交额", "amount", "turnover"),
    }
    if any(columns[key] is None for key in ("dt", "open", "high", "low", "close", "vol")):
        return []
    docs: list[dict[str, Any]] = []
    symbol = _hk_symbol(code)
    pure = _pure_hk_code(code)
    for _, row in df.iterrows():
        prices = _normalize_ohlc_bounds(
            row[columns["open"]],
            row[columns["high"]],
            row[columns["low"]],
            row[columns["close"]],
        )
        if prices is None:
            continue
        source_vol = row[columns["vol"]] if pd.notna(row[columns["vol"]]) else 0
        vol, source_volume_unit = normalize_stock_volume(source_vol, source=source, default_source_unit="shares")
        amount_col = columns.get("amount")
        amount_value = row[amount_col] if amount_col and pd.notna(row[amount_col]) else 0
        doc = {
            "dt": pd.to_datetime(row[columns["dt"]]),
            "meta": {
                "symbol": symbol,
                "raw_code": pure,
                "freq": DAILY_FREQ,
                "market": "HK",
                "source": source,
                "volume_unit": CANONICAL_STOCK_VOLUME_UNIT,
                "source_volume_unit": source_volume_unit,
                "source_vol": float(source_vol or 0),
            },
            "open": prices["open"],
            "high": prices["high"],
            "low": prices["low"],
            "close": prices["close"],
            "vol": vol,
            "amount": int(float(amount_value or 0)),
            "source": source,
        }
        if _valid_hk_daily_doc(doc, end_date=end_date):
            docs.append(doc)
    return docs


def _latest_daily_dates_by_symbol(db: Database, symbols: list[str]) -> dict[str, datetime]:
    if not symbols:
        return {}
    try:
        rows = db["bars"].aggregate([
            {"$match": {"meta.freq": DAILY_FREQ, "meta.symbol": {"$in": symbols}}},
            {"$group": {"_id": "$meta.symbol", "latest_dt": {"$max": "$dt"}}},
        ])
        return {
            str(row.get("_id")): row.get("latest_dt")
            for row in rows
            if row.get("_id") and isinstance(row.get("latest_dt"), datetime)
        }
    except Exception as exc:
        logger.debug("读取 HK bars 日线最新日期失败: %s", exc)
        return {}


def _earliest_daily_dates_by_symbol(db: Database, symbols: list[str]) -> dict[str, datetime]:
    if not symbols:
        return {}
    try:
        rows = db["bars"].aggregate([
            {"$match": {"meta.freq": DAILY_FREQ, "meta.symbol": {"$in": symbols}}},
            {"$group": {"_id": "$meta.symbol", "earliest_dt": {"$min": "$dt"}}},
        ])
        return {
            str(row.get("_id")): row.get("earliest_dt")
            for row in rows
            if row.get("_id") and isinstance(row.get("earliest_dt"), datetime)
        }
    except Exception as exc:
        logger.debug("读取 HK bars 日线最早日期失败: %s", exc)
        return {}


def _cached_hk_universe(db: Database | None) -> list[str]:
    if db is None:
        return []
    codes: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        code = _pure_hk_code(value)
        if code and code not in seen:
            seen.add(code)
            codes.append(code)

    try:
        for doc in db["sync_log"].find({"module": "hk_stock_daily", "symbol": {"$exists": True}}, {"symbol": 1}):
            add(doc.get("symbol"))
    except Exception as exc:
        logger.debug("读取 hk_stock_daily sync_log universe 失败: %s", exc)
    try:
        for doc in db["bars"].aggregate([
            {"$match": {"meta.market": "HK", "meta.freq": DAILY_FREQ}},
            {"$sort": {"dt": -1}},
            {"$group": {"_id": "$meta.symbol", "latest_dt": {"$first": "$dt"}}},
            {"$limit": 20000},
        ]):
            add(doc.get("_id"))
    except Exception as exc:
        logger.debug("读取 HK bars universe 失败: %s", exc)
    return codes


def _extract_hk_codes_from_spot(df: pd.DataFrame) -> list[str]:
    code_col = _pick_column(df, "代码", "code", "symbol", "证券代码")
    if not code_col:
        return []
    name_col = _pick_column(df, "名称", "name", "证券简称")
    codes: list[str] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        name = str(row[name_col]) if name_col and pd.notna(row[name_col]) else ""
        if "退" in name:
            continue
        code = _pure_hk_code(row[code_col])
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def _extract_hk_codes_from_hkex_frame(df: pd.DataFrame) -> list[str]:
    code_col = _pick_column(df, "Stock Code", "stock code", "证券代码", "代码", "code", "symbol")
    if not code_col:
        return []
    category_col = _pick_column(df, "Category", "category", "类别")
    codes: list[str] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        if category_col:
            category = str(row[category_col] or "").strip().lower()
            if category and category != "equity":
                continue
        code = _pure_hk_code(row[code_col])
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def _fetch_hkex_security_list_codes() -> list[str]:
    timeout = _env_float("HK_STOCK_DAILY_HKEX_TIMEOUT", 15.0, minimum=1.0)
    headers = {
        "User-Agent": "Mozilla/5.0 Signals HK stock daily sync",
        "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
    }
    response = requests.get(_HKEX_SECURITIES_XLSX_URL, timeout=timeout, headers=headers)
    response.raise_for_status()
    df = pd.read_excel(io.BytesIO(response.content), sheet_name=0, dtype=str, header=2)
    codes = _extract_hk_codes_from_hkex_frame(df)
    if not codes:
        raise RuntimeError("hkex_security_list_empty")
    return codes


def _fetch_akshare_hk_spot_codes() -> list[str]:
    with no_proxy():
        df = ak.stock_hk_spot_em()
    return _extract_hk_codes_from_spot(df)


def _hk_universe_sources() -> list[str]:
    raw = get_task_env("HK_STOCK_DAILY_UNIVERSE_SOURCES", os.getenv("HK_STOCK_DAILY_UNIVERSE_SOURCES", ""))
    single = get_task_env("HK_STOCK_DAILY_UNIVERSE_SOURCE", os.getenv("HK_STOCK_DAILY_UNIVERSE_SOURCE", ""))
    if single:
        raw = single
    if not raw or str(raw).strip().lower() == "auto":
        raw = "hkex,akshare,cache"
    sources: list[str] = []
    seen: set[str] = set()
    aliases = {"official": "hkex", "hkex_xlsx": "hkex", "eastmoney": "akshare", "em": "akshare", "mongo": "cache"}
    for item in str(raw).split(","):
        source = aliases.get(item.strip().lower(), item.strip().lower())
        if source in {"hkex", "akshare", "cache"} and source not in seen:
            sources.append(source)
            seen.add(source)
    return sources or ["hkex", "akshare", "cache"]


def _hk_history_sources() -> list[str]:
    raw = get_task_env("HK_STOCK_DAILY_HISTORY_SOURCES", os.getenv("HK_STOCK_DAILY_HISTORY_SOURCES", ""))
    single = get_task_env("HK_STOCK_DAILY_HISTORY_SOURCE", os.getenv("HK_STOCK_DAILY_HISTORY_SOURCE", ""))
    if single:
        raw = single
    if not raw or str(raw).strip().lower() == "auto":
        # Tencent is a direct HTTP endpoint with an explicit timeout.  The
        # AKShare/Sina path uses an embedded JS runtime and has no reliable
        # request deadline; putting it first can leave a whole optional
        # postmarket shard in ``running`` while its progress is already
        # stale.  Keep the slower providers as fallbacks for symbols Tencent
        # does not cover.
        raw = "tencent,daily,hist"
    sources: list[str] = []
    seen: set[str] = set()
    aliases = {
        "sina": "daily",
        "stock_hk_daily": "daily",
        "eastmoney": "hist",
        "em": "hist",
        "stock_hk_hist": "hist",
        "qq": "tencent",
        "tencent_hk": "tencent",
    }
    for item in str(raw).split(","):
        source = aliases.get(item.strip().lower(), item.strip().lower())
        if source in {"daily", "tencent", "hist"} and source not in seen:
            sources.append(source)
            seen.add(source)
    return sources or ["daily", "tencent", "hist"]


def _get_all_hk_codes(db: Database | None = None) -> list[str]:
    for source in _hk_universe_sources():
        if source == "hkex":
            try:
                codes = _fetch_hkex_security_list_codes()
                logger.info("使用 HKEX 官方证券清单作为港股 universe: %d 只", len(codes))
                return codes
            except Exception as exc:
                logger.warning("获取 HKEX 港股列表失败: %s", exc)
        elif source == "akshare":
            try:
                codes = _fetch_akshare_hk_spot_codes()
                if codes:
                    return codes
            except Exception as exc:
                logger.warning("获取 AKShare 港股列表失败: %s", exc)
        elif source == "cache":
            cached = _cached_hk_universe(db)
            if cached:
                logger.warning("使用 Mongo cached HK universe: %d 只", len(cached))
                return cached
    raise RuntimeError("hk_universe_empty")


def _parse_only_hk_codes(raw: str) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for item in str(raw or "").replace(";", ",").split(","):
        code = _pure_hk_code(item)
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def _fetch_tencent_hk_daily_df(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    website_symbol = f"hk{_pure_hk_code(code)}"
    count = _env_int("HK_STOCK_DAILY_TENCENT_COUNT", 900, minimum=30, maximum=2000)
    timeout = _env_float("HK_STOCK_DAILY_TENCENT_TIMEOUT", 10.0, minimum=1.0)
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            _TENCENT_FQKLINE_URL,
            params={"param": f"{website_symbol},day,,,{count},qfq"},
            headers={
                "User-Agent": "Mozilla/5.0 Signals HK stock daily sync",
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://gu.qq.com/",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        session.close()

    data = payload.get("data")
    symbol_payload = data.get(website_symbol) if isinstance(data, dict) else {}
    if not isinstance(symbol_payload, dict):
        symbol_payload = {}
    rows = symbol_payload.get("qfqday") or symbol_payload.get("day") or []
    parsed: list[dict[str, Any]] = []
    start_dt = datetime.strptime(start_date[:8], "%Y%m%d")
    end_dt = datetime.strptime(end_date[:8], "%Y%m%d")
    for row in rows:
        if len(row) < 6:
            continue
        dt = pd.to_datetime(row[0], errors="coerce")
        if pd.isna(dt):
            continue
        py_dt = dt.to_pydatetime().replace(hour=0, minute=0, second=0, microsecond=0)
        if py_dt < start_dt or py_dt > end_dt:
            continue
        parsed.append({
            "日期": py_dt,
            "开盘": float(row[1]),
            "收盘": float(row[2]),
            "最高": float(row[3]),
            "最低": float(row[4]),
            "成交量": float(row[5]),
            "成交额": 0,
        })
    return pd.DataFrame(parsed)


def _fetch_one_hk_daily(code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    adjust = str(get_task_env("HK_STOCK_DAILY_ADJUST", os.getenv("HK_STOCK_DAILY_ADJUST", "")) or "")
    source_errors: list[str] = []
    start_dt = datetime.strptime(start_date[:8], "%Y%m%d")

    def filter_start(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [doc for doc in docs if _daily_doc_trade_date(doc) and _daily_doc_trade_date(doc) >= start_dt]

    for source in _hk_history_sources():
        if source == "hist":
            try:
                with no_proxy():
                    df = ak.stock_hk_hist(
                        symbol=_pure_hk_code(code),
                        period="daily",
                        start_date=start_date,
                        end_date=end_date,
                        adjust=adjust,
                    )
                docs = _docs_from_hk_daily_df(code, df, "akshare_stock_hk_hist", end_date=end_date)
                if docs:
                    return filter_start(docs)
            except Exception as exc:
                source_errors.append(f"stock_hk_hist: {exc}")
        elif source == "daily":
            try:
                # AKShare's Sina HK daily path uses py_mini_racer internally; running
                # several MiniRacer contexts in parallel can abort the whole process.
                with _SINA_DAILY_LOCK:
                    with no_proxy():
                        df = ak.stock_hk_daily(symbol=_pure_hk_code(code), adjust=adjust)
                docs = _docs_from_hk_daily_df(code, df, "akshare_stock_hk_daily", end_date=end_date)
                if docs:
                    return filter_start(docs)
            except Exception as exc:
                source_errors.append(f"stock_hk_daily: {exc}")
        elif source == "tencent":
            try:
                df = _fetch_tencent_hk_daily_df(code, start_date, end_date)
                docs = _docs_from_hk_daily_df(code, df, "website_tencent_hk", end_date=end_date)
                if docs:
                    return filter_start(docs)
            except Exception as exc:
                source_errors.append(f"tencent_hk_daily: {exc}")
    if source_errors:
        raise RuntimeError("; ".join(source_errors))
    return []


def _write_daily_docs_batch(bars_col, sync_col, docs_by_code: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    """Insert new HK daily bars without UpdateOne because bars is a time-series collection."""
    docs_by_code = {
        _pure_hk_code(code): [doc for doc in docs if _valid_hk_daily_doc(doc)]
        for code, docs in docs_by_code.items()
        if docs
    }
    docs_by_code = {code: docs for code, docs in docs_by_code.items() if code and docs}
    if not docs_by_code:
        return {}
    all_docs = [doc for docs in docs_by_code.values() for doc in docs]
    symbols = [_hk_symbol(code) for code in docs_by_code]
    dts = [doc["dt"] for doc in all_docs]
    existing_by_key: dict[tuple[str, datetime], dict[str, Any]] = {}
    try:
        for item in bars_col.find(
            {
                "meta.symbol": {"$in": symbols},
                "meta.freq": DAILY_FREQ,
                "dt": {"$in": dts},
            },
            {"dt": 1, "meta": 1, "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1},
        ):
            symbol = str((item.get("meta") or {}).get("symbol") or "")
            dt = item.get("dt")
            if symbol and isinstance(dt, datetime):
                existing_by_key[(symbol, dt)] = item
    except Exception as exc:
        logger.debug("批量查询已有 HK 日线失败，继续尝试写入: %s", exc)

    def changed(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
        for field in ("open", "high", "low", "close", "vol"):
            if existing.get(field) != candidate.get(field):
                return True
        candidate_amount = candidate.get("amount")
        if candidate_amount not in (None, 0) and existing.get("amount") != candidate_amount:
            return True
        return False

    new_docs: list[dict[str, Any]] = []
    refresh_docs: list[dict[str, Any]] = []
    refresh_ids: list[Any] = []
    for doc in all_docs:
        key = (str((doc.get("meta") or {}).get("symbol") or ""), doc.get("dt"))
        existing = existing_by_key.get(key)
        if existing is None:
            new_docs.append(doc)
        elif changed(existing, doc):
            refresh_doc = dict(doc)
            if not refresh_doc.get("amount") and existing.get("amount"):
                refresh_doc["amount"] = existing.get("amount")
            refresh_docs.append(refresh_doc)
            refresh_ids.append(existing["_id"])

    written_by_code = {code: 0 for code in docs_by_code}
    refreshed_count = 0
    if refresh_docs:
        deleted_docs: list[dict[str, Any]] = []
        for old_id, refresh_doc in zip(refresh_ids, refresh_docs):
            delete_result = bars_col.delete_many({"_id": {"$in": [old_id]}})
            deleted_count = int(getattr(delete_result, "deleted_count", 0) or 0)
            if deleted_count == 1:
                deleted_docs.append(refresh_doc)
            elif deleted_count:
                logger.warning("HK 日线刷新删除数量异常 id=%s deleted=%d", old_id, deleted_count)
        if deleted_docs:
            result = bars_col.insert_many(deleted_docs, ordered=False)
            refreshed_count = len(getattr(result, "inserted_ids", []) or [])
            for doc in deleted_docs[:refreshed_count]:
                raw_code = _pure_hk_code((doc.get("meta") or {}).get("raw_code") or (doc.get("meta") or {}).get("symbol"))
                if raw_code in written_by_code:
                    written_by_code[raw_code] += 1
    if new_docs:
        result = bars_col.insert_many(new_docs, ordered=False)
        inserted_count = len(getattr(result, "inserted_ids", []) or [])
        for doc in new_docs[:inserted_count]:
            raw_code = _pure_hk_code((doc.get("meta") or {}).get("raw_code") or (doc.get("meta") or {}).get("symbol"))
            if raw_code in written_by_code:
                written_by_code[raw_code] += 1
    if refreshed_count:
        logger.info("HK 日线刷新已存在 bar: %d", refreshed_count)
    now = naive_market_now("HK")
    for code, docs in docs_by_code.items():
        latest = max(doc["dt"] for doc in docs)
        symbol = _hk_symbol(code)
        sync_col.update_one(
            {"_id": f"hk_stock_daily:{symbol}"},
            {"$set": {
                "module": "hk_stock_daily",
                "market": "HK",
                "symbol": symbol,
                "raw_code": code,
                "status": "ok",
                "last_dt": latest,
                "last_run": now,
                "bar_count": len(docs),
                "written": written_by_code.get(code, 0),
            }},
            upsert=True,
        )
    return written_by_code


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
    started_at: datetime,
    shard_key: str,
    shard_index: int,
    shard_count: int,
    global_total: int,
    deferred_count: int = 0,
    latest_symbol: str = "",
    latest_status: str = "",
    remaining_count: int = 0,
) -> None:
    now = naive_market_now("HK")
    coverage_pct = round((processed / total * 100), 2) if total else 0.0
    sync_col.update_one(
        {"_id": _progress_meta_id(shard_key)},
        {"$set": {
            "module": "hk_stock_daily",
            "market": "HK",
            "status": status,
            "scope": scope,
            "last_run": now,
            "started_at": started_at,
            "processed": processed,
            "total": total,
            "inserted": inserted,
            "skipped": skipped,
            "errors": errors_count,
            "deferred": deferred_count,
            "remaining": remaining_count,
            "latest_symbol": latest_symbol,
            "latest_status": latest_status,
            "coverage_pct": coverage_pct,
            "shard_key": shard_key,
            "shard_index": shard_index,
            "shard_count": shard_count,
            "global_total": global_total,
            "elapsed_seconds": (now - started_at).total_seconds(),
        }},
        upsert=True,
    )


def sync_hk_stock_daily(db: Database, proxy_url: str = None) -> dict:
    """Incrementally sync full HK stock daily bars into the shared bars collection."""
    del proxy_url
    bars_col = db["bars"]
    sync_col = db["sync_log"]
    now = naive_market_now("HK")
    end_date = _hk_daily_end_date_key(now)
    only_codes = _parse_only_hk_codes(get_task_env("HK_STOCK_DAILY_ONLY_CODES", os.getenv("HK_STOCK_DAILY_ONLY_CODES", "")))
    all_codes = only_codes if only_codes else _get_all_hk_codes(db)
    codes, shard_meta = _apply_code_shard(all_codes)
    shard_total = len(codes)
    shard_key = str(shard_meta["shard_key"])
    shard_index = int(shard_meta["shard_index"])
    shard_count = int(shard_meta["shard_count"])
    global_total = int(shard_meta["global_total"])
    scope = str(get_task_env("HK_STOCK_DAILY_SCOPE", os.getenv("HK_STOCK_DAILY_SCOPE", "all")) or "all")
    logger.info("港股日线同步: %d/%d 只股票, scope=%s shard=%s", len(codes), global_total, scope, shard_key)

    sync_docs = {
        str(doc.get("symbol")): doc.get("last_dt")
        for doc in sync_col.find(
            {"module": "hk_stock_daily", "symbol": {"$exists": True}},
            {"symbol": 1, "last_dt": 1},
        )
        if doc.get("symbol")
    }
    hk_symbols = [_hk_symbol(code) for code in codes]
    bars_latest = _latest_daily_dates_by_symbol(db, hk_symbols)
    sync_docs.update(bars_latest)

    run_started_at = now
    total_inserted = 0
    total_skipped = 0
    processed_count = 0
    errors: list[tuple[str, str]] = []
    deferred: list[tuple[str, str]] = []
    progress_interval = _env_int("HK_STOCK_DAILY_PROGRESS_INTERVAL", 25, minimum=1)
    workers = _env_int("HK_STOCK_DAILY_WORKERS", 3, minimum=1, maximum=8)
    call_interval = _env_float("HK_STOCK_DAILY_CALL_INTERVAL", 0.12, minimum=0.0)
    write_batch_symbols = _env_int("HK_STOCK_DAILY_WRITE_BATCH_SYMBOLS", 40, minimum=1)
    lookback_days = _env_int("HK_STOCK_DAILY_LOOKBACK_DAYS", MIN_DAILY_HISTORY_DAYS, minimum=30)
    refresh_lookback_days = _env_int("HK_STOCK_DAILY_REFRESH_LOOKBACK_DAYS", 5, minimum=0, maximum=30)
    history_cutoff = (now - timedelta(days=max(lookback_days, MIN_DAILY_HISTORY_DAYS))).replace(
        hour=0, minute=0, second=0, microsecond=0)
    bars_earliest = _earliest_daily_dates_by_symbol(db, hk_symbols)
    short_history_codes = {
        code
        for code in codes
        if _coerce_last_dt(bars_earliest.get(_hk_symbol(code))) is None
        or _coerce_last_dt(bars_earliest.get(_hk_symbol(code))) > history_cutoff
    }
    # Apply the batch cap after identifying due symbols. Truncating the
    # universe before this point starves the later codes forever because every
    # retry keeps seeing the same already-fresh prefix.
    max_codes = _env_int("HK_STOCK_DAILY_MAX_CODES", 0)
    codes, remaining_due = _select_hk_due_codes(
        codes,
        sync_docs,
        short_history_codes,
        end_date,
        max_codes,
    )
    pending_docs: dict[str, list[dict[str, Any]]] = {}

    _write_progress(
        sync_col,
        status="running",
        scope=scope,
        total=shard_total,
        processed=0,
        inserted=0,
        skipped=0,
        errors_count=0,
        started_at=run_started_at,
        shard_key=shard_key,
        shard_index=shard_index,
        shard_count=shard_count,
        global_total=global_total,
    )

    def _flush_pending() -> int:
        nonlocal pending_docs
        if not pending_docs:
            return 0
        batch = pending_docs
        pending_docs = {}
        written_by_code = _write_daily_docs_batch(bars_col, sync_col, batch)
        return sum(int(value or 0) for value in written_by_code.values())

    def _process(code: str) -> tuple[str, list[dict[str, Any]], str]:
        symbol = _hk_symbol(code)
        last_dt = _coerce_last_dt(sync_docs.get(symbol))
        short_history = code in short_history_codes
        if last_dt:
            inc_start = (last_dt + timedelta(days=1)).strftime("%Y%m%d")
            if refresh_lookback_days:
                inc_start = min(inc_start, (last_dt - timedelta(days=refresh_lookback_days)).strftime("%Y%m%d"))
            if inc_start > end_date:
                if not short_history:
                    return code, [], "skip"
                inc_start = history_cutoff.strftime("%Y%m%d")
        else:
            inc_start = history_cutoff.strftime("%Y%m%d")
        try:
            docs = _fetch_one_hk_daily(code, inc_start, end_date)
            if call_interval:
                time.sleep(call_interval)
            return code, docs, "history_backfill" if short_history and docs else ("ok" if docs else "empty_docs")
        except Exception as exc:
            return code, [], str(exc)[:240]

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hk-stock-daily") as executor:
        futures = {executor.submit(_process, code): code for code in codes}
        for future in as_completed(futures):
            code = futures[future]
            latest_status = "error"
            try:
                code, docs, latest_status = future.result()
                if latest_status in {"skip", "empty_docs"}:
                    total_skipped += 1
                elif latest_status in {"ok", "history_backfill"}:
                    pending_docs[code] = docs
                    if len(pending_docs) >= write_batch_symbols:
                        total_inserted += _flush_pending()
                elif str(latest_status).startswith("deferred/"):
                    deferred.append((code, latest_status))
                else:
                    errors.append((code, str(latest_status)[:240]))
            except Exception as exc:
                latest_status = str(exc)[:240]
                errors.append((code, latest_status))
            finally:
                processed_count += 1
                if processed_count % progress_interval == 0 or processed_count == len(codes):
                    final_partial = bool(errors or deferred or remaining_due)
                    _write_progress(
                        sync_col,
                        status="running" if processed_count < len(codes) else ("partial" if final_partial else "ok"),
                        scope=scope,
                        total=shard_total,
                        processed=processed_count,
                        inserted=total_inserted,
                        skipped=total_skipped,
                        errors_count=len(errors),
                        deferred_count=len(deferred),
                        latest_symbol=_hk_symbol(code),
                        latest_status=latest_status,
                        started_at=run_started_at,
                        shard_key=shard_key,
                        shard_index=shard_index,
                        shard_count=shard_count,
                        global_total=global_total,
                        remaining_count=remaining_due,
                    )

    total_inserted += _flush_pending()
    final_status = "partial" if errors or deferred or remaining_due else "ok"
    coverage_pct = round((processed_count / shard_total * 100), 2) if shard_total else 0.0
    _write_progress(
        sync_col,
        status=final_status,
        scope=scope,
        total=shard_total,
        processed=processed_count,
        inserted=total_inserted,
        skipped=total_skipped,
        errors_count=len(errors),
        deferred_count=len(deferred),
        started_at=run_started_at,
        shard_key=shard_key,
        shard_index=shard_index,
        shard_count=shard_count,
        global_total=global_total,
        remaining_count=remaining_due,
    )
    db["data_freshness"].update_one(
        {"domain": "kline", "market": "HK", "mode": "historical", "collection": "bars", "freq": DAILY_FREQ},
        {"$set": {
            "domain": "kline",
            "market": "HK",
            "mode": "historical",
            "lane": "workbench_lane",
            "collection": "bars",
            "freq": DAILY_FREQ,
            "freshness": "fresh" if total_inserted or total_skipped or (shard_total and not remaining_due) else "empty",
            "latest_dt": now.date().isoformat(),
            "as_of": now.date().isoformat(),
            "updated_at": now,
            "count": total_inserted,
            "scanned_symbols": processed_count,
            "coverage_pct": coverage_pct,
            "stale_reason": "" if processed_count else "hk_stock_daily_universe_empty",
        }},
        upsert=True,
    )
    return {
        "status": final_status,
        "inserted": total_inserted,
        "symbols": processed_count,
        "processed": processed_count,
        "total": shard_total,
        "selected_codes": len(codes),
        "remaining_due": remaining_due,
        "skipped": total_skipped,
        "errors": len(errors),
        "deferred": len(deferred),
        "coverage_pct": coverage_pct,
        "scope": scope,
        "market": "HK",
        "shard_key": shard_key,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "global_total": global_total,
        "sample_errors": errors[:8],
        "sample_deferred": deferred[:8],
        "short_history_backfill_codes": len(short_history_codes),
    }
