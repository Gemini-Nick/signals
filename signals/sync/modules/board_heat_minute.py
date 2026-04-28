# -*- coding: utf-8 -*-
"""Industry/concept minute heat ticks for the trading terminal.

This module writes board/concept heat snapshots into Mongo. Workbench minute
charts must read these cached ticks; API requests should not fetch providers.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd
from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.market_time import naive_market_now

from ..retry import sync_retry
from .board_ranking import _fetch_em_board_names_resilient, _health

logger = logging.getLogger("signals.sync.board_heat_minute")


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def _int(value: Any, default: int = 0) -> int:
    parsed = _float(value)
    if parsed is None:
        return default
    return int(parsed)


def _tick_docs(df: pd.DataFrame, *, kind: str, now) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    docs: list[dict[str, Any]] = []
    for rank_idx, row in df.reset_index(drop=True).iterrows():
        name = str(row.get("板块名称") or row.get("board_name") or "").strip()
        if not name:
            continue
        docs.append({
            "kind": kind,
            "name": name,
            "board_name": name,
            "code": str(row.get("板块代码") or row.get("code") or "").strip(),
            "source": "eastmoney_push2delay",
            "dt": now.replace(hour=0, minute=0, second=0, microsecond=0),
            "trade_minute": now.replace(second=0, microsecond=0),
            "snapshot_at": now,
            "rank_idx": int(rank_idx),
            "price": _float(row.get("最新价")),
            "change_pct": _float(row.get("涨跌幅"), 0.0),
            "change_amount": _float(row.get("涨跌额")),
            "market_value": _float(row.get("总市值")),
            "turnover_pct": _float(row.get("换手率")),
            "up_count": _int(row.get("上涨家数")),
            "down_count": _int(row.get("下跌家数")),
            "leader_name": str(row.get("领涨股票") or row.get("leader_name") or "").strip(),
            "leader_change_pct": _float(row.get("领涨股票-涨跌幅") or row.get("leader_change_pct")),
        })
    return docs


def _sync_heat_kind(db: Database, *, kind: str, proxy_url: str | None = None) -> dict:
    now = naive_market_now("A")
    source_kind = "concept" if kind == "concept" else "industry"
    domain = "concept" if kind == "concept" else "board"
    endpoint = f"push2delay_clist_{source_kind}"
    try:
        df = _fetch_em_board_names_resilient(source_kind)
        docs = _tick_docs(df, kind=kind, now=now)
        if not docs:
            _health(db, "em", endpoint, domain, False, "empty")
            return {"status": "degraded", "inserted": 0, "kind": kind, "reason": "board_heat_empty"}
        ops = [
            UpdateOne(
                {
                    "kind": doc["kind"],
                    "name": doc["name"],
                    "source": doc["source"],
                    "trade_minute": doc["trade_minute"],
                },
                {"$set": doc},
                upsert=True,
            )
            for doc in docs
        ]
        result = db["board_heat_ticks"].bulk_write(ops, ordered=False)
        written = int(result.upserted_count + result.modified_count)
        db["data_freshness"].update_one(
            {"domain": domain, "market": "A", "mode": "realtime", "collection": "board_heat_ticks", "scope": kind},
            {"$set": {
                "domain": domain,
                "market": "A",
                "mode": "realtime",
                "lane": "board_lane",
                "collection": "board_heat_ticks",
                "scope": kind,
                "freshness": "fresh",
                "latest_dt": now.isoformat(timespec="minutes"),
                "as_of": now.date().isoformat(),
                "updated_at": now,
                "stale_reason": "",
                "count": len(docs),
            }},
            upsert=True,
        )
        _health(db, "em", endpoint, domain, True)
        logger.info("%s minute heat: %d ticks", kind, len(docs))
        return {"status": "ok", "inserted": written, "ticks": len(docs), "kind": kind}
    except Exception as exc:
        _health(db, "em", endpoint, domain, False, str(exc))
        logger.warning("%s minute heat failed: %s", kind, exc)
        return {
            "status": "degraded",
            "inserted": 0,
            "kind": kind,
            "reason": "provider_route_error",
            "error_msg": str(exc)[:240],
        }


@sync_retry(max_attempts=2, min_wait=2)
def sync_board_heat_minute(db: Database, proxy_url: str = None) -> dict:
    return _sync_heat_kind(db, kind="industry", proxy_url=proxy_url)


@sync_retry(max_attempts=2, min_wait=2)
def sync_concept_heat_minute(db: Database, proxy_url: str = None) -> dict:
    return _sync_heat_kind(db, kind="concept", proxy_url=proxy_url)
