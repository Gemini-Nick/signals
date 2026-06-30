# -*- coding: utf-8 -*-
"""Sync Eastmoney limit-up/failed-board pools into Mongo for replay ranking."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.core.trading_dates import a_share_realtime_day_key
from signals.sync.proxy import em_proxy

from ..retry import sync_retry


POOL_SPECS = {
    "limit_up": {
        "akshare_fn": "stock_zt_pool_em",
        "source": "eastmoney_zt_pool",
        "label": "涨停股池",
    },
    "failed_limit": {
        "akshare_fn": "stock_zt_pool_zbgc_em",
        "source": "eastmoney_zt_pool_zbgc",
        "label": "炸板股池",
    },
    "limit_down": {
        "akshare_fn": "stock_zt_pool_dtgc_em",
        "source": "eastmoney_zt_pool_dtgc",
        "label": "跌停股池",
    },
    "strong": {
        "akshare_fn": "stock_zt_pool_strong_em",
        "source": "eastmoney_zt_pool_strong",
        "label": "强势股池",
    },
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _date_key(trade_date: str | None) -> tuple[str, str]:
    day = trade_date or a_share_realtime_day_key(now=naive_market_now("A"))
    compact = day.replace("-", "")
    dashed = f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}" if len(compact) == 8 else day
    return compact, dashed


def _snapshot_at_for_trade_date(day: str, now: datetime, *, explicit_trade_date: bool = False) -> datetime:
    """Use a stable close timestamp for historical pool backfills."""
    if explicit_trade_date:
        try:
            parsed = datetime.fromisoformat(day)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.date() != now.date():
            return parsed.replace(hour=15, minute=0, second=0, microsecond=0)
    realtime_day = a_share_realtime_day_key(now=now)
    if day == realtime_day:
        return now
    try:
        return datetime.fromisoformat(day).replace(hour=15, minute=0, second=0, microsecond=0)
    except ValueError:
        return now


def _prefixed_symbol(code: str) -> str:
    value = _text(code).zfill(6)
    if value.startswith(("6", "9")):
        return f"SH.{value}"
    if value.startswith(("8", "4")):
        return f"BJ.{value}"
    return f"SZ.{value}"


def _normalize_row(
    row: dict[str, Any],
    *,
    pool: str,
    trade_date: str,
    source: str,
    snapshot_at: datetime | None = None,
) -> dict[str, Any] | None:
    code = _text(row.get("代码") or row.get("code")).zfill(6)
    if not code or code == "000000":
        return None
    observed_at = snapshot_at or naive_market_now("A")
    return {
        "trade_date": trade_date,
        "date_key": trade_date,
        "snapshot_at": observed_at,
        "snapshot_minute": observed_at.strftime("%H:%M"),
        "pool": pool,
        "source": source,
        "code": code,
        "symbol": _prefixed_symbol(code),
        "name": _text(row.get("名称") or row.get("name")),
        "change_pct": _float(_first_present(row.get("涨跌幅"), row.get("change_pct"))),
        "latest_price": _float(_first_present(row.get("最新价"), row.get("price"))),
        "limit_price": _float(_first_present(row.get("涨停价"), row.get("跌停价"))),
        "amount": _float(_first_present(row.get("成交额"), row.get("amount"))),
        "float_market_cap": _float(row.get("流通市值")),
        "market_cap": _float(row.get("总市值")),
        "turnover_pct": _float(_first_present(row.get("换手率"), row.get("turnover_pct"))),
        "seal_amount": _float(_first_present(row.get("封板资金"), row.get("封单资金"))),
        "first_limit_up_time": _text(row.get("首次封板时间")),
        "last_limit_up_time": _text(row.get("最后封板时间")),
        "open_count": _int(_first_present(row.get("炸板次数"), row.get("开板次数"))),
        "board_amount": _float(row.get("板上成交额")),
        "limit_up_stat": _text(row.get("涨停统计")),
        "consecutive_limit_count": _int(row.get("连板数") or row.get("连续跌停")),
        "speed_pct": _float(row.get("涨速")),
        "amplitude_pct": _float(row.get("振幅")),
        "volume_ratio": _float(row.get("量比")),
        "is_new_high": _text(row.get("是否新高")),
        "selected_reason": _text(row.get("入选理由")),
        "industry": _text(row.get("所属行业")),
        "updated_at": observed_at,
    }


def _fetch_pool(ak: Any, fn_name: str, date_compact: str) -> list[dict[str, Any]]:
    fn: Callable[..., Any] | None = getattr(ak, fn_name, None)
    if fn is None:
        raise AttributeError(f"akshare has no {fn_name}")
    frame = fn(date=date_compact)
    if frame is None or getattr(frame, "empty", True):
        return []
    return frame.to_dict("records")


@sync_retry(max_attempts=2, min_wait=1)
def sync_market_limit_pools(db: Database, trade_date: str | None = None, proxy_url: str | None = None) -> dict[str, Any]:
    """Fetch AkShare/Eastmoney market pools and upsert normalized rows."""
    import akshare as ak

    date_compact, day = _date_key(trade_date)
    now = naive_market_now("A")
    snapshot_at = _snapshot_at_for_trade_date(day, now, explicit_trade_date=trade_date is not None)
    docs: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    with em_proxy(proxy_url):
        for pool, spec in POOL_SPECS.items():
            try:
                raw_rows = _fetch_pool(ak, str(spec["akshare_fn"]), date_compact)
            except Exception as exc:
                errors[pool] = f"{exc.__class__.__name__}: {str(exc)[:160]}"
                continue
            for row in raw_rows:
                doc = _normalize_row(
                    row,
                    pool=pool,
                    trade_date=day,
                    source=str(spec["source"]),
                    snapshot_at=snapshot_at,
                )
                if doc:
                    docs.append(doc)

    if not docs:
        db["data_freshness"].update_one(
            {"domain": "market_limit_pools", "market": "A", "trade_date": day},
            {
                "$set": {
                    "domain": "market_limit_pools",
                    "market": "A",
                    "trade_date": day,
                    "snapshot_at": snapshot_at,
                    "snapshot_minute": snapshot_at.strftime("%H:%M"),
                    "freshness": "empty",
                    "updated_at": snapshot_at,
                    "count": 0,
                    "errors": errors,
                }
            },
            upsert=True,
        )
        return {"status": "empty", "trade_date": day, "upserted": 0, "errors": errors}

    ops = [
        UpdateOne(
            {
                "trade_date": doc["trade_date"],
                "snapshot_minute": doc["snapshot_minute"],
                "pool": doc["pool"],
                "code": doc["code"],
            },
            {"$set": doc},
            upsert=True,
        )
        for doc in docs
    ]
    result = db["market_limit_pools"].bulk_write(ops, ordered=False)
    db["data_freshness"].update_one(
        {"domain": "market_limit_pools", "market": "A", "trade_date": day},
        {
            "$set": {
                "domain": "market_limit_pools",
                "market": "A",
                "trade_date": day,
                "snapshot_at": snapshot_at,
                "snapshot_minute": snapshot_at.strftime("%H:%M"),
                "freshness": "fresh",
                "updated_at": snapshot_at,
                "count": len(docs),
                "pools": sorted({doc["pool"] for doc in docs}),
                "errors": errors,
            }
        },
        upsert=True,
    )
    return {
        "status": "ok",
        "trade_date": day,
        "matched": result.matched_count,
        "modified": result.modified_count,
        "upserted": len(result.upserted_ids),
        "count": len(docs),
        "errors": errors,
    }


if __name__ == "__main__":
    from signals.sync.db import get_db

    print(sync_market_limit_pools(get_db()))
