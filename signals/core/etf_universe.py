# -*- coding: utf-8 -*-
"""A-share ETF universe helpers for strategy review and batch backtests."""
from __future__ import annotations

import math
import os
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any, Mapping

import requests

from signals.core.market_time import naive_market_now


ALL_ETF_UNIVERSE_TOKENS = {
    "all_etf",
    "all-etf",
    "allmarket_etf",
    "all_market_etf",
    "market_etf",
    "etf_all",
    "etf:all",
    "__all_etf__",
    "全量etf",
    "全部etf",
    "市场etf",
    "全市场etf",
}

_ETF_CODE_PREFIXES = (
    "159",
    "510",
    "511",
    "512",
    "513",
    "515",
    "516",
    "517",
    "518",
    "561",
    "562",
    "563",
    "588",
    "589",
)
_EASTMONEY_ETF_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
_EASTMONEY_ETF_FS = "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827"
_EASTMONEY_ETF_FIELDS = "f2,f3,f5,f6,f7,f8,f12,f13,f14,f15,f16,f17,f18,f20,f21"
_EASTMONEY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Connection": "close",
}


def is_all_etf_universe_token(value: Any) -> bool:
    raw = str(value or "").strip().lower().replace(" ", "").replace("＿", "_")
    return raw in ALL_ETF_UNIVERSE_TOKENS


def normalize_etf_code(value: Any) -> str:
    raw = str(value or "").strip().upper()
    for prefix in ("SH.", "SZ.", "BJ."):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    raw = raw.replace(".", "")
    return raw if len(raw) == 6 and raw.isdigit() else ""


def etf_symbol_for_code(code: Any, market_id: Any = None) -> str:
    normalized = normalize_etf_code(code)
    if not normalized:
        return ""
    if market_id in (0, "0"):
        return f"SZ.{normalized}"
    if market_id in (1, "1"):
        return f"SH.{normalized}"
    if normalized.startswith(("5", "6", "9")):
        return f"SH.{normalized}"
    return f"SZ.{normalized}"


def is_etf_like(code: Any, name: Any = "", source: Any = "") -> bool:
    normalized = normalize_etf_code(code)
    display_name = str(name or "").strip().lower()
    source_text = str(source or "").strip().lower()
    if "etf" in display_name or "交易型开放式指数" in display_name:
        return bool(normalized)
    if source_text in {"sina_etf", "sina_etf_qfq_factor", "eastmoney_etf_spot"}:
        return bool(normalized)
    return bool(normalized and normalized.startswith(_ETF_CODE_PREFIXES))


def fetch_eastmoney_etf_spot_rows(*, timeout: float = 8.0) -> list[dict[str, Any]]:
    """Fetch the all-market ETF spot universe from Eastmoney's native clist API."""
    page_size = 100

    def params(page: int) -> dict[str, str]:
        return {
            "pn": str(page),
            "pz": str(page_size),
            "po": "1",
            "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2",
            "invt": "2",
            "fid": "f12",
            "fs": _EASTMONEY_ETF_FS,
            "fields": _EASTMONEY_ETF_FIELDS,
        }

    rows: list[dict[str, Any]] = []
    with requests.Session() as session:
        session.trust_env = False
        first = session.get(_EASTMONEY_ETF_URL, params=params(1), headers=_EASTMONEY_HEADERS, timeout=timeout)
        first.raise_for_status()
        payload = first.json()
        data = payload.get("data") or {}
        total = int(data.get("total") or 0)
        rows.extend(data.get("diff") or [])
        page_count = max(1, math.ceil(total / page_size))
        for page in range(2, page_count + 1):
            response = session.get(_EASTMONEY_ETF_URL, params=params(page), headers=_EASTMONEY_HEADERS, timeout=timeout)
            response.raise_for_status()
            rows.extend((response.json().get("data") or {}).get("diff") or [])
    return rows


def all_market_etf_universe(
    *,
    db: Any = None,
    include_live: bool | None = None,
    limit: int = 0,
    require_daily_bars: bool = False,
    attach_daily_bars: bool | None = None,
) -> dict[str, Any]:
    """Return the merged ETF universe from live Eastmoney, Mongo, and static watchlists."""
    started = time.monotonic()
    rows: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    warnings: list[str] = []
    source_counts: dict[str, int] = {}
    as_of = naive_market_now("A").date().isoformat()

    def add(row: Mapping[str, Any], source: str) -> None:
        code = normalize_etf_code(row.get("code") or row.get("symbol") or row.get("f12"))
        name = str(row.get("name") or row.get("f14") or "").strip()
        if not is_etf_like(code, name, row.get("source") or source):
            return
        symbol = str(row.get("futu_symbol") or row.get("symbol") or "").strip().upper()
        if not symbol or "." not in symbol:
            symbol = etf_symbol_for_code(code, row.get("market_id") if "market_id" in row else row.get("f13"))
        item = rows.setdefault(code, {"code": code, "symbol": symbol, "name": name or code, "sources": []})
        if name and (not item.get("name") or item.get("name") == code or source == "eastmoney_etf_spot"):
            item["name"] = name
        if symbol and (not item.get("symbol") or "." not in str(item.get("symbol"))):
            item["symbol"] = symbol
        for key, src_key in (
            ("price", "f2"),
            ("latest", "f2"),
            ("change_pct", "f3"),
            ("vol", "f5"),
            ("amount", "f6"),
            ("amplitude_pct", "f7"),
            ("turnover_pct", "f8"),
            ("high", "f15"),
            ("low", "f16"),
            ("open", "f17"),
            ("prev_close", "f18"),
            ("market_cap", "f20"),
            ("float_market_cap", "f21"),
            ("close", "close"),
            ("latest_dt", "latest_dt"),
        ):
            value = row.get(key, row.get(src_key))
            if value not in (None, ""):
                item[key] = value
        item["asset_class"] = _asset_class(item.get("name"), code)
        item["category"] = _category(item.get("name"), code)
        if source not in item["sources"]:
            item["sources"].append(source)
        source_counts[source] = source_counts.get(source, 0) + 1

    cached_count = 0
    if db is not None:
        cached_count = _add_cached_etf_spot_rows(db, add, warnings)

    if include_live is None:
        include_live = _truthy_env("SIGNALS_ETF_UNIVERSE_LIVE", default=db is None)
        if db is not None and cached_count <= 0 and _truthy_env("SIGNALS_ETF_UNIVERSE_LIVE_FALLBACK", default=True):
            include_live = True
    if include_live:
        try:
            for row in fetch_eastmoney_etf_spot_rows(timeout=_live_timeout()):
                add(row, "eastmoney_etf_spot")
        except Exception as exc:
            warnings.append(f"eastmoney_etf_spot:{exc.__class__.__name__}:{str(exc)[:120]}")

    if db is not None:
        _add_stock_name_rows(db, add, warnings)

    _add_static_macro_etfs(add, warnings)

    if attach_daily_bars is None:
        attach_daily_bars = require_daily_bars or not include_live
    if db is not None and attach_daily_bars:
        _add_latest_bar_rows(db, rows, warnings)

    all_rows = list(rows.values())
    all_rows.sort(key=lambda item: (_number(item.get("amount")), _number(item.get("vol")), item.get("code") or ""), reverse=True)
    source_counts = _merged_source_counts(all_rows)
    total = len(all_rows)
    backtest_ready_rows = [row for row in all_rows if row.get("latest_dt")]
    candidate_rows = backtest_ready_rows if require_daily_bars else all_rows
    selected = candidate_rows[:limit] if limit and limit > 0 else candidate_rows
    return {
        "type": "all_etf",
        "as_of": as_of,
        "source": "+".join(sorted(source_counts)) or "empty",
        "source_counts": source_counts,
        "total": total,
        "backtest_ready_total": len(backtest_ready_rows),
        "require_daily_bars": bool(require_daily_bars),
        "limit": int(limit or 0),
        "codes": [row["code"] for row in selected],
        "rows": selected,
        "warnings": warnings,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def _add_cached_etf_spot_rows(db: Any, add, warnings: list[str]) -> int:
    try:
        latest = db["etf_spot_snapshots"].find_one(
            {},
            {"_id": 0, "date_key": 1},
            sort=[("date_key", -1), ("snapshot_at", -1)],
        ) or {}
        date_key = str(latest.get("date_key") or "")
        if not date_key:
            return 0
        cursor = db["etf_spot_snapshots"].find(
            {"date_key": date_key},
            {
                "_id": 0,
                "code": 1,
                "symbol": 1,
                "name": 1,
                "price": 1,
                "latest": 1,
                "change_pct": 1,
                "vol": 1,
                "amount": 1,
                "amplitude_pct": 1,
                "turnover_pct": 1,
                "high": 1,
                "low": 1,
                "open": 1,
                "prev_close": 1,
                "market_cap": 1,
                "float_market_cap": 1,
                "market_id": 1,
                "trade_date": 1,
                "source": 1,
            },
        )
        count = 0
        for row in cursor:
            add(row, "etf_spot_snapshots")
            count += 1
        return count
    except Exception as exc:
        warnings.append(f"etf_spot_snapshots:{exc.__class__.__name__}:{str(exc)[:120]}")
        return 0


def build_etf_strategy_analysis(*, db: Any = None, include_live: bool | None = None) -> dict[str, Any]:
    universe = all_market_etf_universe(db=db, include_live=include_live)
    rows = list(universe.get("rows") or [])
    by_asset: dict[str, int] = {}
    for row in rows:
        key = str(row.get("asset_class") or "other")
        by_asset[key] = by_asset.get(key, 0) + 1
    return {
        "universe": {
            "type": universe.get("type"),
            "as_of": universe.get("as_of"),
            "source": universe.get("source"),
            "source_counts": universe.get("source_counts"),
            "total": universe.get("total", 0),
            "warnings": universe.get("warnings", []),
        },
        "asset_class_counts": by_asset,
        "top_turnover": _project_rows(sorted(rows, key=lambda item: _number(item.get("amount")), reverse=True), limit=20),
        "top_gainers": _project_rows(
            [row for row in sorted(rows, key=lambda item: _number(item.get("change_pct"), -999), reverse=True) if row.get("change_pct") not in (None, "")],
            limit=20,
        ),
        "top_losers": _project_rows(
            [row for row in sorted(rows, key=lambda item: _number(item.get("change_pct"), 999)) if row.get("change_pct") not in (None, "")],
            limit=20,
        ),
        "review_universe": _project_rows(rows, limit=80),
    }


def _add_stock_name_rows(db: Any, add, warnings: list[str]) -> None:
    try:
        cursor = db["stock_names"].find(
            {"name": {"$regex": "ETF", "$options": "i"}},
            {"_id": 0, "code": 1, "symbol": 1, "futu_symbol": 1, "name": 1},
        )
        for row in cursor:
            add(row, "stock_names")
    except Exception as exc:
        warnings.append(f"stock_names:{exc.__class__.__name__}:{str(exc)[:120]}")


def _add_latest_bar_rows(db: Any, rows: "OrderedDict[str, dict[str, Any]]", warnings: list[str]) -> None:
    for code, item in list(rows.items()):
        try:
            doc = db["bars"].find_one(
                {"meta.symbol": code, "meta.freq": {"$in": ["daily", "日线", "D"]}},
                {"_id": 0, "dt": 1, "close": 1, "vol": 1, "amount": 1, "meta": 1},
                sort=[("dt", -1)],
            ) or {}
        except Exception as exc:
            warnings.append(f"bars:{code}:{exc.__class__.__name__}:{str(exc)[:80]}")
            continue
        if not doc:
            continue
        dt = doc.get("dt")
        if isinstance(dt, datetime):
            item["latest_dt"] = dt.date().isoformat()
        elif dt:
            item["latest_dt"] = str(dt)[:10]
        if doc.get("close") not in (None, ""):
            item["close"] = doc.get("close")
        if doc.get("vol") not in (None, ""):
            item["vol"] = doc.get("vol")
        if doc.get("amount") not in (None, ""):
            item["amount"] = doc.get("amount")
        if "bars" not in item["sources"]:
            item["sources"].append("bars")


def _add_static_macro_etfs(add, warnings: list[str]) -> None:
    try:
        from signals.core.macro_universe import macro_industry_etfs_by_code

        for code, payload in macro_industry_etfs_by_code().items():
            add({"code": code, **payload}, "macro_industry_etfs")
    except Exception as exc:
        warnings.append(f"macro_industry_etfs:{exc.__class__.__name__}:{str(exc)[:120]}")


def _project_rows(rows: list[Mapping[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    keys = ("code", "symbol", "name", "asset_class", "category", "price", "close", "change_pct", "amount", "vol", "latest_dt", "sources")
    projected = []
    for row in rows[:limit]:
        projected.append({key: row.get(key) for key in keys if row.get(key) not in (None, "")})
    return projected


def _merged_source_counts(rows: list[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for source in row.get("sources") or []:
            key = str(source or "").strip()
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


def _asset_class(name: Any, code: str) -> str:
    text = str(name or "")
    if any(token in text for token in ("国债", "债", "可转债", "信用债", "城投")):
        return "bond"
    if any(token in text for token in ("黄金", "白银", "豆粕", "能源", "有色", "商品")):
        return "commodity"
    if any(token in text for token in ("恒生", "港股", "H股", "纳指", "标普", "德国", "日经", "海外")):
        return "overseas"
    if "货币" in text:
        return "money_market"
    if code.startswith("588") or any(token in text for token in ("半导体", "芯片", "科创", "机器人", "通信", "医药", "AI", "人工智能")):
        return "theme_equity"
    return "broad_or_sector_equity"


def _category(name: Any, code: str) -> str:
    text = str(name or "")
    for token in ("半导体", "芯片", "科创", "机器人", "通信", "医药", "新能源", "证券", "银行", "军工", "红利", "黄金", "国债", "恒生", "纳指"):
        if token in text:
            return token
    if code.startswith("588"):
        return "科创"
    return "ETF"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _truthy_env(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _live_timeout() -> float:
    try:
        return max(2.0, float(os.getenv("SIGNALS_ETF_UNIVERSE_TIMEOUT", "8")))
    except (TypeError, ValueError):
        return 8.0
