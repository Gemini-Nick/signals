# -*- coding: utf-8 -*-
"""Sample-audit Mongo market bars against online quote/bar providers.

This is intentionally an audit tool first. It does not mutate MongoDB.
Supported comparisons:
- A-share daily bars vs Tencent daily qfq bars.
- A-share 5/15/30 minute bars vs public Sina/Tencent minute bars.
- HK daily bars vs Tencent HK website, Yahoo chart, and AKShare HK hist/daily online sources.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd
import requests
from pymongo import MongoClient

from signals.sync.modules.daily_sources import fetch_tencent_daily
from signals.sync.modules.hk_stock_daily import _docs_from_hk_daily_df, _pure_hk_code
from signals.sync.modules.minute_sources import fetch_public_minute, stock_to_market_symbol


MONGO_URL = "mongodb://127.0.0.1:27017/signals"
DB_NAME = "signals"
DAILY_FREQ = "日线"
MINUTE_FREQS = ("5分钟", "15分钟", "30分钟")
TENCENT_DAILY_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_MINUTE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
SINA_MINUTE_URL = "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData"
YAHOO_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
WEBSITE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return None
        return result
    except Exception:
        return None


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = pd.to_datetime(value, errors="coerce")
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime().replace(tzinfo=None)


def _date_key(value: Any) -> str:
    parsed = _dt(value)
    return parsed.strftime("%Y-%m-%d") if parsed else ""


def _minute_period(freq: str) -> str:
    return "".join(ch for ch in str(freq) if ch.isdigit()) or "5"


def _direct_get(url: str, *, params: dict[str, str], timeout: float, referer: str = "") -> requests.Response:
    session = requests.Session()
    session.trust_env = False
    headers = dict(WEBSITE_HEADERS)
    if referer:
        headers["Referer"] = referer
    try:
        response = session.get(url, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response
    finally:
        session.close()


def _pct_change(close: float | None, prev_close: float | None) -> float | None:
    if close is None or prev_close in (None, 0):
        return None
    return round((close - prev_close) / prev_close * 100.0, 4)


def _row_by_date(df: pd.DataFrame, column: str, target: datetime) -> pd.Series | None:
    if df is None or df.empty or column not in df.columns:
        return None
    target_date = target.date()
    dates = pd.to_datetime(df[column], errors="coerce")
    matched = df[dates.dt.date == target_date]
    if matched.empty:
        return None
    return matched.iloc[-1]


def _row_by_dt(df: pd.DataFrame, column: str, target: datetime) -> pd.Series | None:
    if df is None or df.empty or column not in df.columns:
        return None
    dates = pd.to_datetime(df[column], errors="coerce")
    matched = df[dates == target]
    if matched.empty:
        return None
    return matched.iloc[-1]


def _stock_to_website_symbol(code: str) -> str:
    return stock_to_market_symbol(code)


def _website_tencent_daily(code: str, *, count: int, timeout: float) -> tuple[pd.DataFrame, str]:
    symbol = _stock_to_website_symbol(code)
    response = _direct_get(
        TENCENT_DAILY_URL,
        params={"param": f"{symbol},day,,,{count},qfq"},
        timeout=timeout,
        referer="https://gu.qq.com/",
    )
    payload = response.json()
    rows = payload.get("data", {}).get(symbol, {}).get("qfqday") or payload.get("data", {}).get(symbol, {}).get("day") or []
    parsed = []
    for row in rows:
        if len(row) < 6:
            continue
        parsed.append(
            {
                "时间": pd.to_datetime(row[0], errors="coerce"),
                "开盘": _num(row[1]),
                "收盘": _num(row[2]),
                "最高": _num(row[3]),
                "最低": _num(row[4]),
                # Tencent daily website volume is in hands; Mongo canonical stock daily vol is shares.
                "成交量": (_num(row[5]) or 0.0) * 100,
                "成交额": 0.0,
            }
        )
    return pd.DataFrame(parsed), response.url


def _normalize_website_minute_rows(rows: list[list[Any]], *, source: str) -> pd.DataFrame:
    parsed = []
    for row in rows:
        if len(row) < 6:
            continue
        amount = 0.0
        if source == "tencent" and len(row) > 7 and not isinstance(row[7], dict):
            amount = (_num(row[7]) or 0.0) * 1_000_000
        elif source == "sina" and len(row) > 6:
            amount = _num(row[6]) or 0.0
        dt_value = row[0]
        if source == "tencent":
            dt = pd.to_datetime(dt_value, format="%Y%m%d%H%M", errors="coerce")
        else:
            dt = pd.to_datetime(dt_value, errors="coerce")
        parsed.append(
            {
                "时间": dt,
                "开盘": _num(row[1]),
                "收盘": _num(row[2]),
                "最高": _num(row[3]),
                "最低": _num(row[4]),
                "成交量": _num(row[5]),
                "成交额": amount,
            }
        )
    return pd.DataFrame(parsed).dropna(subset=["时间"]).reset_index(drop=True)


def _website_tencent_minute(code: str, freq: str, *, count: int, timeout: float) -> tuple[pd.DataFrame, str]:
    symbol = _stock_to_website_symbol(code)
    period = _minute_period(freq)
    key = f"m{period}"
    response = _direct_get(
        TENCENT_MINUTE_URL,
        params={"param": f"{symbol},{key},,{count}"},
        timeout=timeout,
        referer="https://gu.qq.com/",
    )
    payload = response.json()
    rows = payload.get("data", {}).get(symbol, {}).get(key, [])
    return _normalize_website_minute_rows(rows, source="tencent"), response.url


def _website_sina_minute(code: str, freq: str, *, count: int, timeout: float) -> tuple[pd.DataFrame, str]:
    symbol = _stock_to_website_symbol(code)
    response = _direct_get(
        SINA_MINUTE_URL,
        params={"symbol": symbol, "scale": _minute_period(freq), "ma": "no", "datalen": str(count)},
        timeout=timeout,
        referer="https://finance.sina.com.cn/",
    )
    text = response.text
    payload = text.split("=(", 1)[1].rsplit(");", 1)[0]
    raw_rows = json.loads(payload)
    rows = [
        [
            row.get("day"),
            row.get("open"),
            row.get("close"),
            row.get("high"),
            row.get("low"),
            row.get("volume"),
            row.get("amount"),
        ]
        for row in raw_rows
    ]
    return _normalize_website_minute_rows(rows, source="sina"), response.url


def _website_yahoo_hk_daily(symbol: str, target_dt: datetime, *, timeout: float) -> tuple[pd.DataFrame, str]:
    code = _pure_hk_code(symbol)
    yahoo_symbol = f"{int(code)}.HK" if code else symbol.replace("HK.", "") + ".HK"
    response = _direct_get(
        YAHOO_CHART_URL.format(symbol=yahoo_symbol),
        params={
            "range": "1mo",
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "true",
        },
        timeout=timeout,
        referer="https://finance.yahoo.com/",
    )
    payload = response.json()
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        return pd.DataFrame(), response.url
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    rows = []
    for idx, ts in enumerate(timestamps):
        dt = pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Hong_Kong").tz_localize(None)
        rows.append(
            {
                "时间": pd.Timestamp(dt.date()),
                "开盘": _num((quote.get("open") or [None])[idx]),
                "最高": _num((quote.get("high") or [None])[idx]),
                "最低": _num((quote.get("low") or [None])[idx]),
                "收盘": _num((quote.get("close") or [None])[idx]),
                "成交量": _num((quote.get("volume") or [None])[idx]),
                "成交额": 0.0,
            }
        )
    return pd.DataFrame(rows).dropna(subset=["时间"]), response.url


def _website_tencent_hk_daily(symbol: str, *, count: int, timeout: float) -> tuple[pd.DataFrame, str]:
    code = _pure_hk_code(symbol)
    website_symbol = f"hk{code}" if code else symbol.lower().replace(".", "")
    response = _direct_get(
        TENCENT_DAILY_URL,
        params={"param": f"{website_symbol},day,,,{count},qfq"},
        timeout=timeout,
        referer="https://gu.qq.com/",
    )
    payload = response.json()
    rows = payload.get("data", {}).get(website_symbol, {}).get("qfqday") or payload.get("data", {}).get(website_symbol, {}).get("day") or []
    parsed = []
    for row in rows:
        if len(row) < 6:
            continue
        parsed.append(
            {
                "时间": pd.to_datetime(row[0], errors="coerce"),
                "开盘": _num(row[1]),
                "收盘": _num(row[2]),
                "最高": _num(row[3]),
                "最低": _num(row[4]),
                "成交量": _num(row[5]) or 0.0,
                "成交额": 0.0,
            }
        )
    return pd.DataFrame(parsed).dropna(subset=["时间"]).reset_index(drop=True), response.url


def _doc_compact(doc: dict[str, Any] | None) -> dict[str, Any]:
    if not doc:
        return {}
    meta = doc.get("meta") or {}
    return {
        "symbol": meta.get("symbol"),
        "market": meta.get("market"),
        "freq": meta.get("freq"),
        "dt": doc.get("dt"),
        "open": doc.get("open"),
        "high": doc.get("high"),
        "low": doc.get("low"),
        "close": doc.get("close"),
        "vol": doc.get("vol"),
        "amount": doc.get("amount"),
        "change_pct": doc.get("change_pct"),
        "pct_chg": doc.get("pct_chg"),
        "prev_close": doc.get("prev_close"),
        "source": doc.get("source") or meta.get("source"),
    }


def _compare_numbers(
    *,
    field: str,
    local: float | None,
    online: float | None,
    abs_tol: float,
    rel_tol: float = 0.0,
) -> dict[str, Any] | None:
    if local is None or online is None:
        if local is None and online is None:
            return None
        return {"field": field, "local": local, "online": online, "reason": "missing_value"}
    diff = round(local - online, 6)
    rel = abs(diff) / abs(online) if online else 0.0
    if abs(diff) <= abs_tol or rel <= rel_tol:
        return None
    return {"field": field, "local": local, "online": online, "diff": diff, "rel_diff": round(rel, 6)}


def _classify(
    *,
    mismatches: list[dict[str, Any]],
    local_doc: dict[str, Any] | None,
    online_found: bool,
    provider_notes: list[str],
) -> str:
    if not local_doc:
        return "local_missing"
    if not online_found:
        return "online_missing_or_provider_window"
    if any("local_missing_latest_online_date" in note for note in provider_notes):
        if mismatches:
            return "local_stale_and_price_mismatch"
        return "local_stale_missing_latest"
    fields = {item["field"] for item in mismatches}
    if "close" in fields or {"open", "high", "low"} & fields:
        if any("hk_provider_conflict" in note for note in provider_notes):
            return "provider_conflict_price_mismatch"
        if any("latest_insert_new_maybe_stale" in note for note in provider_notes):
            return "stale_in_progress_bar"
        return "price_mismatch"
    if "change_pct" in fields or "pct_chg" in fields:
        return "change_pct_mismatch"
    if "vol" in fields or "amount" in fields:
        if fields <= {"vol", "amount"} and any("latest_insert_new_maybe_stale" in note for note in provider_notes):
            return "stale_tail_volume_mismatch"
        return "volume_or_amount_mismatch"
    return "ok"


def _sample_latest_docs(db, *, market: str, freq: str, limit: int, symbols: list[str] | None = None) -> list[dict[str, Any]]:
    match: dict[str, Any] = {"meta.market": market, "meta.freq": freq}
    if symbols:
        match["meta.symbol"] = {"$in": symbols}
    rows = list(
        db["bars"].aggregate(
            [
                {"$match": match},
                {"$sort": {"dt": -1}},
                {"$group": {"_id": "$meta.symbol", "doc": {"$first": "$$ROOT"}}},
                {"$sort": {"_id": 1}},
            ],
            allowDiskUse=True,
        )
    )
    docs = [row["doc"] for row in rows if row.get("doc")]
    rng = random.Random(20260529)
    rng.shuffle(docs)
    return docs[:limit]


def _previous_local_close(db, symbol: str, market: str, dt: datetime) -> float | None:
    prev = db["bars"].find_one(
        {
            "meta.symbol": symbol,
            "meta.market": market,
            "meta.freq": DAILY_FREQ,
            "dt": {"$lt": dt},
        },
        sort=[("dt", -1)],
    )
    return _num(prev.get("close")) if prev else None


def _audit_a_daily(db, doc: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    meta = doc.get("meta") or {}
    symbol = str(meta.get("symbol") or "")
    target_dt = _dt(doc.get("dt"))
    result = {"market": "A", "freq": DAILY_FREQ, "symbol": symbol, "dt": target_dt, "local": _doc_compact(doc)}
    if not symbol or not target_dt:
        result.update({"status": "bad_local_key", "mismatches": []})
        return result
    try:
        df, source_url = _website_tencent_daily(symbol, count=args.daily_count, timeout=args.provider_timeout)
        row = _row_by_date(df, "时间", target_dt)
        if row is None:
            result.update({"status": "online_missing_or_provider_window", "provider": "website:tencent", "source_url": source_url, "mismatches": []})
            return result
        online = {
            "open": _num(row.get("开盘")),
            "high": _num(row.get("最高")),
            "low": _num(row.get("最低")),
            "close": _num(row.get("收盘")),
            "vol": _num(row.get("成交量")),
            "amount": _num(row.get("成交额")),
            "provider": "website:tencent",
            "source_url": source_url,
        }
        row_pos = list(pd.to_datetime(df["时间"], errors="coerce").dt.date).index(target_dt.date())
        prev_close = _num(df.iloc[row_pos - 1].get("收盘")) if row_pos > 0 else _previous_local_close(db, symbol, "A", target_dt)
        online["prev_close"] = prev_close
        online["change_pct"] = _pct_change(online["close"], prev_close)
        mismatches = []
        for field in ("open", "high", "low", "close"):
            item = _compare_numbers(field=field, local=_num(doc.get(field)), online=online[field], abs_tol=args.price_tol)
            if item:
                mismatches.append(item)
        local_change = _num(doc.get("change_pct", doc.get("pct_chg")))
        item = _compare_numbers(field="change_pct", local=local_change, online=online["change_pct"], abs_tol=args.pct_tol)
        if item and local_change is not None:
            mismatches.append(item)
        result.update(
            {
                "status": _classify(mismatches=mismatches, local_doc=doc, online_found=True, provider_notes=[]),
                "provider": "website:tencent",
                "source_url": source_url,
                "online": online,
                "mismatches": mismatches,
            }
        )
    except Exception as exc:
        result.update({"status": "provider_error", "provider": "tencent", "error": f"{type(exc).__name__}: {str(exc)[:240]}", "mismatches": []})
    return result


def _audit_a_minute(db, doc: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    meta = doc.get("meta") or {}
    symbol = str(meta.get("symbol") or "")
    freq = str(meta.get("freq") or "")
    target_dt = _dt(doc.get("dt"))
    result = {"market": "A", "freq": freq, "symbol": symbol, "dt": target_dt, "local": _doc_compact(doc)}
    if not symbol or not target_dt:
        result.update({"status": "bad_local_key", "mismatches": []})
        return result
    provider_docs: dict[str, dict[str, Any]] = {}
    provider_latest_dates: dict[str, datetime] = {}
    errors: list[str] = []
    for provider, fetcher in (("website:tencent", _website_tencent_minute), ("website:sina", _website_sina_minute)):
        try:
            df, source_url = fetcher(symbol, freq, count=args.minute_count, timeout=args.provider_timeout)
            if df is not None and not df.empty:
                latest_dt = pd.to_datetime(df["时间"], errors="coerce").max()
                if pd.notna(latest_dt):
                    provider_latest_dates[provider] = latest_dt.to_pydatetime()
            row = _row_by_dt(df, "时间", target_dt)
            if row is not None:
                provider_docs[provider] = {
                    "open": _num(row.get("开盘")),
                    "high": _num(row.get("最高")),
                    "low": _num(row.get("最低")),
                    "close": _num(row.get("收盘")),
                    "vol": _num(row.get("成交量")),
                    "amount": _num(row.get("成交额")),
                    "provider": provider,
                    "source_url": source_url,
                }
        except Exception as exc:
            errors.append(f"{provider}: {type(exc).__name__}: {str(exc)[:200]}")
    if not provider_docs:
        provider_notes = []
        for provider, latest_dt in provider_latest_dates.items():
            if latest_dt and latest_dt > target_dt:
                provider_notes.append(f"local_missing_latest_online_date:{provider}:{latest_dt.isoformat()}")
        result.update(
            {
                "status": _classify(mismatches=[], local_doc=doc, online_found=bool(provider_notes), provider_notes=provider_notes),
                "provider_notes": provider_notes,
                "errors": errors,
                "mismatches": [],
            }
        )
        return result

    local_source = str(meta.get("source") or "").lower()
    if "tencent" in local_source and "website:tencent" in provider_docs:
        preferred = "website:tencent"
    elif "sina" in local_source and "website:sina" in provider_docs:
        preferred = "website:sina"
    else:
        preferred = "website:tencent" if "website:tencent" in provider_docs else next(iter(provider_docs))
    online = provider_docs[preferred]
    provider_notes: list[str] = []
    for provider, latest_dt in provider_latest_dates.items():
        if latest_dt and latest_dt > target_dt:
            provider_notes.append(f"local_missing_latest_online_date:{provider}:{latest_dt.isoformat()}")
    if len(provider_docs) > 1:
        closes = {name: item.get("close") for name, item in provider_docs.items()}
        if len({value for value in closes.values() if value is not None}) > 1:
            provider_notes.append(f"website_provider_conflict_close:{closes}")
    latest_doc = db["bars"].find_one({"meta.symbol": symbol, "meta.market": "A", "meta.freq": freq}, {"dt": 1}, sort=[("dt", -1)])
    if latest_doc and latest_doc.get("dt") == target_dt:
        provider_notes.append("latest_insert_new_maybe_stale")

    mismatches = []
    for field in ("open", "high", "low", "close"):
        item = _compare_numbers(field=field, local=_num(doc.get(field)), online=online[field], abs_tol=args.price_tol)
        if item:
            mismatches.append(item)
    item = _compare_numbers(
        field="vol",
        local=_num(doc.get("vol")),
        online=online["vol"],
        abs_tol=args.volume_abs_tol,
        rel_tol=args.volume_rel_tol,
    )
    if item:
        mismatches.append(item)
    try:
        result.update(
            {
                "status": _classify(mismatches=mismatches, local_doc=doc, online_found=True, provider_notes=provider_notes),
                "provider": preferred,
                "source_url": online.get("source_url"),
                "provider_notes": provider_notes,
                "errors": errors,
                "online": online,
                "provider_docs": provider_docs,
                "mismatches": mismatches,
            }
        )
    except Exception as exc:
        result.update({"status": "provider_error", "error": f"{type(exc).__name__}: {str(exc)[:240]}", "mismatches": []})
    return result


def _fetch_hk_hist_doc(code: str, target_dt: datetime, args: argparse.Namespace) -> dict[str, Any] | None:
    start = target_dt.strftime("%Y%m%d")
    end = target_dt.strftime("%Y%m%d")
    df = ak.stock_hk_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust=args.hk_adjust)
    docs = _docs_from_hk_daily_df(code, df, "akshare_stock_hk_hist", end_date=end)
    return docs[-1] if docs else None


def _fetch_hk_daily_doc(code: str, target_dt: datetime, args: argparse.Namespace) -> dict[str, Any] | None:
    df = ak.stock_hk_daily(symbol=code, adjust=args.hk_adjust)
    docs = _docs_from_hk_daily_df(code, df, "akshare_stock_hk_daily", end_date=target_dt.strftime("%Y%m%d"))
    matched = [doc for doc in docs if _dt(doc.get("dt")) and _dt(doc.get("dt")).date() == target_dt.date()]
    return matched[-1] if matched else None


def _fetch_hk_yahoo_doc(code: str, target_dt: datetime, args: argparse.Namespace) -> tuple[dict[str, Any] | None, str, datetime | None]:
    df, source_url = _website_yahoo_hk_daily(code, target_dt, timeout=args.provider_timeout)
    latest_online_dt = None
    if df is not None and not df.empty:
        latest_online_dt = pd.to_datetime(df["时间"], errors="coerce").max().to_pydatetime()
    row = _row_by_date(df, "时间", target_dt)
    if row is None:
        return None, source_url, latest_online_dt
    symbol = f"HK.{_pure_hk_code(code)}"
    return (
        {
            "dt": pd.to_datetime(row["时间"]).to_pydatetime(),
            "meta": {"symbol": symbol, "raw_code": _pure_hk_code(code), "freq": DAILY_FREQ, "market": "HK", "source": "website_yahoo"},
            "open": _num(row.get("开盘")),
            "high": _num(row.get("最高")),
            "low": _num(row.get("最低")),
            "close": _num(row.get("收盘")),
            "vol": int(_num(row.get("成交量")) or 0),
            "amount": int(_num(row.get("成交额")) or 0),
            "source": "website_yahoo",
        },
        source_url,
        latest_online_dt,
    )


def _fetch_hk_tencent_doc(code: str, target_dt: datetime, args: argparse.Namespace) -> tuple[dict[str, Any] | None, str, datetime | None]:
    df, source_url = _website_tencent_hk_daily(code, count=args.daily_count, timeout=args.provider_timeout)
    latest_online_dt = None
    if df is not None and not df.empty:
        latest_online_dt = pd.to_datetime(df["时间"], errors="coerce").max().to_pydatetime()
    row = _row_by_date(df, "时间", target_dt)
    if row is None:
        return None, source_url, latest_online_dt
    symbol = f"HK.{_pure_hk_code(code)}"
    return (
        {
            "dt": pd.to_datetime(row["时间"]).to_pydatetime(),
            "meta": {"symbol": symbol, "raw_code": _pure_hk_code(code), "freq": DAILY_FREQ, "market": "HK", "source": "website_tencent_hk"},
            "open": _num(row.get("开盘")),
            "high": _num(row.get("最高")),
            "low": _num(row.get("最低")),
            "close": _num(row.get("收盘")),
            "vol": int(_num(row.get("成交量")) or 0),
            "amount": int(_num(row.get("成交额")) or 0),
            "source": "website_tencent_hk",
        },
        source_url,
        latest_online_dt,
    )


def _audit_hk_daily(db, doc: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    meta = doc.get("meta") or {}
    symbol = str(meta.get("symbol") or "")
    code = _pure_hk_code(symbol)
    target_dt = _dt(doc.get("dt"))
    result = {"market": "HK", "freq": DAILY_FREQ, "symbol": symbol, "dt": target_dt, "local": _doc_compact(doc)}
    if not code or not target_dt:
        result.update({"status": "bad_local_key", "mismatches": []})
        return result
    provider_docs: dict[str, dict[str, Any]] = {}
    source_urls: dict[str, str] = {}
    latest_online_dates: dict[str, datetime] = {}
    errors: list[str] = []
    try:
        fetched, source_url, latest_online_dt = _fetch_hk_tencent_doc(code, target_dt, args)
        if fetched:
            provider_docs["website:tencent_hk"] = fetched
            source_urls["website:tencent_hk"] = source_url
        if latest_online_dt:
            latest_online_dates["website:tencent_hk"] = latest_online_dt
    except Exception as exc:
        errors.append(f"website:tencent_hk: {type(exc).__name__}: {str(exc)[:180]}")
    try:
        fetched, source_url, latest_online_dt = _fetch_hk_yahoo_doc(code, target_dt, args)
        if fetched:
            provider_docs["website:yahoo"] = fetched
            source_urls["website:yahoo"] = source_url
        if latest_online_dt:
            latest_online_dates["website:yahoo"] = latest_online_dt
    except Exception as exc:
        errors.append(f"website:yahoo: {type(exc).__name__}: {str(exc)[:180]}")
    for provider, fetcher in (("akshare_stock_hk_hist", _fetch_hk_hist_doc), ("akshare_stock_hk_daily", _fetch_hk_daily_doc)):
        try:
            fetched = fetcher(code, target_dt, args)
            if fetched:
                provider_docs[provider] = fetched
        except Exception as exc:
            errors.append(f"{provider}: {type(exc).__name__}: {str(exc)[:180]}")
    if not provider_docs:
        result.update({"status": "online_missing_or_provider_window", "errors": errors, "mismatches": []})
        return result

    preferred_name = (
        "website:tencent_hk"
        if "website:tencent_hk" in provider_docs
        else ("website:yahoo" if "website:yahoo" in provider_docs else ("akshare_stock_hk_hist" if "akshare_stock_hk_hist" in provider_docs else next(iter(provider_docs))))
    )
    online_doc = provider_docs[preferred_name]
    provider_notes: list[str] = []
    latest_local = db["bars"].find_one({"meta.symbol": symbol, "meta.market": "HK", "meta.freq": DAILY_FREQ}, {"dt": 1}, sort=[("dt", -1)])
    local_latest_dt = _dt(latest_local.get("dt")) if latest_local else None
    for provider, latest_online_dt in latest_online_dates.items():
        if local_latest_dt and latest_online_dt and latest_online_dt.date() > local_latest_dt.date():
            provider_notes.append(f"local_missing_latest_online_date:{provider}:{latest_online_dt.date().isoformat()}")
    if len(provider_docs) > 1:
        closes = {name: _num(item.get("close")) for name, item in provider_docs.items()}
        if len({value for value in closes.values() if value is not None}) > 1:
            provider_notes.append(f"hk_provider_conflict_close:{closes}")
    online = _doc_compact(online_doc)
    online["provider_docs"] = {name: _doc_compact(item) for name, item in provider_docs.items()}
    mismatches = []
    for field in ("open", "high", "low", "close"):
        item = _compare_numbers(field=field, local=_num(doc.get(field)), online=_num(online_doc.get(field)), abs_tol=args.price_tol)
        if item:
            mismatches.append(item)
    result.update(
        {
            "status": _classify(mismatches=mismatches, local_doc=doc, online_found=True, provider_notes=provider_notes),
            "provider": preferred_name,
            "source_url": source_urls.get(preferred_name),
            "provider_notes": provider_notes,
            "errors": errors,
            "online": online,
            "mismatches": mismatches,
        }
    )
    return result


def _explicit_symbols(raw: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    for item in str(raw or "").replace(";", ",").split(","):
        value = item.strip().upper()
        if not value:
            continue
        if value.startswith("HK."):
            result["HK"].append(value)
        elif value.startswith(("SH.", "SZ.", "BJ.")):
            result["A"].append(value.split(".", 1)[1])
        elif value.isdigit() and len(value) == 6:
            result["A"].append(value)
        elif value.isdigit() and len(value) <= 5:
            result["HK"].append(f"HK.{value.zfill(5)}")
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    client = MongoClient(args.mongo_url, serverSelectionTimeoutMS=3000)
    db = client[args.db_name]
    requested = [item.strip() for item in args.groups.split(",") if item.strip()]
    explicit = _explicit_symbols(args.symbols)
    results: list[dict[str, Any]] = []

    for group in requested:
        if group == "A:daily":
            docs = _sample_latest_docs(db, market="A", freq=DAILY_FREQ, limit=args.samples_per_group, symbols=explicit.get("A"))
            for doc in docs:
                results.append(_audit_a_daily(db, doc, args))
                time.sleep(args.call_interval)
        elif group == "HK:daily":
            docs = _sample_latest_docs(db, market="HK", freq=DAILY_FREQ, limit=args.samples_per_group, symbols=explicit.get("HK"))
            for doc in docs:
                results.append(_audit_hk_daily(db, doc, args))
                time.sleep(args.call_interval)
        elif group.startswith("A:") and group.split(":", 1)[1] in MINUTE_FREQS:
            freq = group.split(":", 1)[1]
            docs = _sample_latest_docs(db, market="A", freq=freq, limit=args.samples_per_group, symbols=explicit.get("A"))
            for doc in docs:
                results.append(_audit_a_minute(db, doc, args))
                time.sleep(args.call_interval)
        else:
            results.append({"group": group, "status": "unsupported_group", "mismatches": []})

    status_counts = Counter(str(item.get("status")) for item in results)
    mismatch_field_counts = Counter()
    for item in results:
        for mismatch in item.get("mismatches") or []:
            mismatch_field_counts[str(mismatch.get("field"))] += 1
    summary = {
        "generated_at": datetime.now(),
        "groups": requested,
        "samples": len(results),
        "status_counts": dict(status_counts),
        "mismatch_field_counts": dict(mismatch_field_counts),
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, ensure_ascii=False, default=_json_default))
    print(f"wrote {output}")
    client.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo-url", default=MONGO_URL)
    parser.add_argument("--db-name", default=DB_NAME)
    parser.add_argument("--groups", default="A:daily,A:5分钟,A:15分钟,A:30分钟,HK:daily")
    parser.add_argument("--symbols", default="", help="Optional comma list such as 300750,HK.03750.")
    parser.add_argument("--samples-per-group", type=int, default=8)
    parser.add_argument("--daily-count", type=int, default=900)
    parser.add_argument("--minute-count", type=int, default=400)
    parser.add_argument("--price-tol", type=float, default=0.02)
    parser.add_argument("--pct-tol", type=float, default=0.05)
    parser.add_argument("--volume-abs-tol", type=float, default=100.0)
    parser.add_argument("--volume-rel-tol", type=float, default=0.02)
    parser.add_argument("--provider-timeout", type=float, default=10.0)
    parser.add_argument("--call-interval", type=float, default=0.2)
    parser.add_argument("--hk-adjust", default="qfq")
    parser.add_argument("--output", default="/tmp/signals_market_data_online_audit.json")
    return parser


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
