# -*- coding: utf-8 -*-
"""Collect company business facts used as industry-chain evidence.

The first source is Eastmoney F10: company info and 主营构成.  The module is
deliberately incremental; it refreshes the highest-risk / most active names
first, then the chain rebuild consumes the collected facts as a separate
evidence layer.
"""
from __future__ import annotations

import logging
import math
import os
import time
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Any

import akshare as ak
import pandas as pd
from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.core.trading_dates import trading_day_key
from ..proxy import em_proxy
from ..retry import sync_retry
from ..task_context import get_task_env

logger = logging.getLogger("signals.sync.security_business_facts")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, float) and math.isnan(value):
            return default
        return float(value)
    except Exception:
        return default


def _pure_a_code(value: Any) -> str:
    raw = _text(value).upper()
    if not raw:
        return ""
    pure = raw.split(".", 1)[-1] if "." in raw else raw
    pure = pure.replace("SH", "").replace("SZ", "").replace("BJ", "")
    return pure if pure.isdigit() and len(pure) == 6 else ""


def _prefixed_a_symbol(code: str) -> str:
    if code.startswith(("6", "9")):
        return f"SH.{code}"
    if code.startswith(("4", "8")):
        return f"BJ.{code}"
    return f"SZ.{code}"


def _ak_symbol(code: str) -> str:
    prefix = "SH" if code.startswith(("6", "9")) else "BJ" if code.startswith(("4", "8")) else "SZ"
    return f"{prefix}{code}"


def _unique(values: list[Any]) -> list[str]:
    output: list[str] = []
    for value in values:
        item = _text(value)
        if item and item not in output:
            output.append(item)
    return output


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(get_task_env(name, str(default)) or str(default)))
    except (TypeError, ValueError):
        return max(minimum, default)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(get_task_env(name, str(default)) or str(default)))
    except (TypeError, ValueError):
        return max(minimum, default)


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if not value:
        return None
    try:
        parsed = pd.to_datetime(value).to_pydatetime()
    except Exception:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _latest_trade_date(db: Database, collection: str, field: str = "trade_date") -> str:
    doc = db[collection].find_one(
        {field: {"$exists": True}},
        {field: 1},
        sort=[(field, -1)],
    ) or {}
    return _text(doc.get(field))


def _add_code(output: list[str], value: Any) -> None:
    code = _pure_a_code(value)
    if code and code not in output:
        output.append(code)


def _requested_codes() -> list[str]:
    raw = _text(get_task_env("SECURITY_BUSINESS_FACT_CODES", ""))
    if not raw:
        return []
    codes: list[str] = []
    for item in raw.replace(";", ",").split(","):
        _add_code(codes, item)
    return codes


def _candidate_codes(db: Database, limit: int) -> list[str]:
    requested = _requested_codes()
    if requested:
        return requested[:limit]
    codes: list[str] = []
    latest_evidence_date = _latest_trade_date(db, "security_concept_evidence")
    if latest_evidence_date:
        cursor = db["security_concept_evidence"].find(
            {
                "trade_date": latest_evidence_date,
                "evidence_layer": {"$in": ["weak_source", "market_theme", "candidate_theme"]},
            },
            {"raw_code": 1},
        ).sort([("volume_driver_score", -1), ("evidence_strength", -1)]).limit(limit * 6)
        for row in cursor:
            _add_code(codes, row.get("raw_code"))
            if len(codes) >= limit:
                return codes

    latest_membership_date = _latest_trade_date(db, "security_chain_memberships")
    if latest_membership_date:
        cursor = db["security_chain_memberships"].find(
            {"trade_date": latest_membership_date, "is_primary_chain": True, "membership_type": {"$in": ["theme", "weak_related"]}},
            {"raw_code": 1},
        ).sort([("exposure_score", -1), ("confidence", -1)]).limit(limit * 3)
        for row in cursor:
            _add_code(codes, row.get("raw_code"))
            if len(codes) >= limit:
                return codes

    latest_quote_date = _latest_trade_date(db, "quote_snapshots")
    if latest_quote_date:
        cursor = db["quote_snapshots"].find(
            {"trade_date": latest_quote_date},
            {"code": 1, "symbol": 1},
        ).sort([("turnover_pct", -1), ("change_pct", -1)]).limit(limit * 2)
        for row in cursor:
            _add_code(codes, row.get("code") or row.get("symbol"))
            if len(codes) >= limit:
                return codes

    latest_spot = db["fullmarket_spot_snapshots"].find_one(
        {"date_key": {"$exists": True}},
        {"date_key": 1},
        sort=[("date_key", -1), ("snapshot_at", -1)],
    ) or {}
    date_key = _text(latest_spot.get("date_key"))
    if date_key:
        for row in db["fullmarket_spot_snapshots"].find({"date_key": date_key}, {"code": 1}).limit(limit * 2):
            _add_code(codes, row.get("code"))
            if len(codes) >= limit:
                return codes
    return codes[:limit]


def _needs_refresh(db: Database, code: str, *, now: datetime, ok_days: int, retry_days: int) -> bool:
    doc = db["security_business_facts"].find_one({"raw_code": code}, {"status": 1, "updated_at": 1})
    if not doc:
        return True
    updated_at = _coerce_datetime(doc.get("updated_at"))
    if not updated_at:
        return True
    days = ok_days if _text(doc.get("status")) == "ok" else retry_days
    return updated_at < now - timedelta(days=days)


def _info_items(df: pd.DataFrame | None) -> dict[str, str]:
    if df is None or df.empty or "item" not in df.columns or "value" not in df.columns:
        return {}
    return {
        _text(item): _text(value)
        for item, value in zip(df["item"].tolist(), df["value"].tolist())
        if _text(item)
    }


def _business_rows(df: pd.DataFrame | None, *, min_revenue_ratio: float) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    rows: list[dict[str, Any]] = []
    latest_report = ""
    if "报告日期" in df.columns:
        latest_report = max((_text(value) for value in df["报告日期"].tolist()), default="")
    scoped = df
    if latest_report and "报告日期" in df.columns:
        scoped = df[df["报告日期"].astype(str) == latest_report]
    for _, row in scoped.iterrows():
        term = _text(row.get("主营构成"))
        if not term:
            continue
        revenue_ratio = _float(row.get("收入比例"))
        if revenue_ratio < min_revenue_ratio:
            continue
        rows.append({
            "report_date": _text(row.get("报告日期")),
            "category": _text(row.get("分类类型")),
            "term": term,
            "revenue": _float(row.get("主营收入")),
            "revenue_ratio": revenue_ratio,
            "cost": _float(row.get("主营成本")),
            "cost_ratio": _float(row.get("成本比例")),
            "profit": _float(row.get("主营利润")),
            "profit_ratio": _float(row.get("利润比例")),
            "gross_margin": _float(row.get("毛利率")),
        })
    rows.sort(key=lambda item: (_float(item.get("revenue_ratio")), _float(item.get("revenue"))), reverse=True)
    return rows[:12]


def _fetch_one(code: str, *, timeout: float, min_revenue_ratio: float, proxy_url: str | None = None) -> dict[str, Any]:
    now = naive_market_now("A")
    symbol = _prefixed_a_symbol(code)
    direct = str(get_task_env("SECURITY_BUSINESS_FACT_DIRECT", "false") or "false").lower() in {"1", "true", "yes", "on"}
    context = em_proxy(proxy_url) if direct else nullcontext()
    with context:
        info_df = ak.stock_individual_info_em(symbol=code, timeout=timeout)
        business_df = ak.stock_zygc_em(symbol=_ak_symbol(code))
    info = _info_items(info_df)
    business_rows = _business_rows(business_df, min_revenue_ratio=min_revenue_ratio)
    terms = _unique([
        info.get("行业"),
        *(row.get("term") for row in business_rows),
    ])
    name = _text(info.get("股票简称"))
    return {
        "_id": f"A:{symbol.replace('.', ':')}",
        "security_id": f"A:{symbol.replace('.', ':')}",
        "market": "A",
        "symbol": symbol,
        "raw_code": code,
        "name": name,
        "industry": _text(info.get("行业")),
        "listed_at": _text(info.get("上市时间")),
        "business_terms": terms[:16],
        "business_rows": business_rows,
        "source": "eastmoney_f10",
        "source_functions": ["stock_individual_info_em", "stock_zygc_em"],
        "status": "ok" if terms else "empty",
        "as_of": trading_day_key("A", now=now),
        "updated_at": now,
    }


@sync_retry(max_attempts=2, min_wait=5)
def sync_security_business_facts(db: Database, proxy_url: str = None) -> dict[str, Any]:
    started = time.monotonic()
    now = naive_market_now("A")
    limit = _env_int("SECURITY_BUSINESS_FACT_MAX_CODES", 80, minimum=1)
    ok_days = _env_int("SECURITY_BUSINESS_FACT_OK_REFRESH_DAYS", 30, minimum=1)
    retry_days = _env_int("SECURITY_BUSINESS_FACT_RETRY_DAYS", 3, minimum=0)
    timeout = _env_float("SECURITY_BUSINESS_FACT_PROVIDER_TIMEOUT", 8.0, minimum=1.0)
    call_interval = _env_float("SECURITY_BUSINESS_FACT_CALL_INTERVAL", 0.8, minimum=0.0)
    min_revenue_ratio = _env_float("SECURITY_BUSINESS_FACT_MIN_REVENUE_RATIO", 0.03, minimum=0.0)
    codes = [
        code for code in _candidate_codes(db, limit * 2)
        if _needs_refresh(db, code, now=now, ok_days=ok_days, retry_days=retry_days)
    ][:limit]

    ops: list[UpdateOne] = []
    errors: list[dict[str, str]] = []
    for code in codes:
        try:
            doc = _fetch_one(code, timeout=timeout, min_revenue_ratio=min_revenue_ratio, proxy_url=proxy_url)
            ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True))
        except Exception as exc:
            logger.warning("security business fact failed %s: %s", code, exc)
            symbol = _prefixed_a_symbol(code)
            errors.append({"code": code, "error": str(exc)[:180]})
            ops.append(UpdateOne(
                {"_id": f"A:{symbol.replace('.', ':')}"},
                {"$set": {
                    "security_id": f"A:{symbol.replace('.', ':')}",
                    "market": "A",
                    "symbol": symbol,
                    "raw_code": code,
                    "source": "eastmoney_f10",
                    "status": "error",
                    "error_msg": str(exc)[:300],
                    "updated_at": now,
                }},
                upsert=True,
            ))
        if call_interval:
            time.sleep(call_interval)

    inserted = modified = 0
    if ops:
        result = db["security_business_facts"].bulk_write(ops, ordered=False)
        inserted = int(result.upserted_count)
        modified = int(result.modified_count)

    status = "ok" if ops and not errors else "partial" if ops else "skipped"
    db["sync_log"].update_one(
        {"_id": "security_business_facts:_meta"},
        {"$set": {
            "module": "security_business_facts",
            "status": status,
            "last_run": now,
            "heartbeat_at": naive_market_now("A"),
            "bar_count": len(ops),
            "candidate_count": len(codes),
            "error_msg": f"{len(errors)} errors" if errors else "",
            "sample_errors": errors[:10],
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }},
        upsert=True,
    )
    db["data_freshness"].update_one(
        {"domain": "business_facts", "market": "A", "mode": "postmarket", "collection": "security_business_facts"},
        {"$set": {
            "domain": "business_facts",
            "market": "A",
            "mode": "postmarket",
            "collection": "security_business_facts",
            "freshness": "fresh" if ops else "stale",
            "latest_dt": trading_day_key("A", now=now),
            "as_of": trading_day_key("A", now=now),
            "updated_at": naive_market_now("A"),
            "count": db["security_business_facts"].count_documents({"status": "ok"}),
            "last_run_count": len(ops),
            "stale_reason": "" if ops else "no_due_business_fact_candidates",
        }},
        upsert=True,
    )
    return {
        "module": "security_business_facts",
        "status": status,
        "inserted": inserted,
        "modified": modified,
        "candidate_count": len(codes),
        "error_count": len(errors),
        "elapsed": round(time.monotonic() - started, 3),
    }
