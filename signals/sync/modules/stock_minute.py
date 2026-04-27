# -*- coding: utf-8 -*-
"""
A股分钟线同步 — 活跃标的 5M/15M/30M 增量同步

数据源: Sina/Tencent 公共分钟线；东财分钟线可显式开启为最后兜底
策略: 增量同步，仅白名单 + 最近入池标的（~200只上限）
频率: 工作日 16:00
注意: 公共分钟线返回滚动窗口数据，直接全量覆盖
"""
import logging
import os
import time

import akshare as ak
import pandas as pd
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from ..proxy import em_proxy
from ..retry import sync_retry
from .minute_sources import fetch_public_minute, stock_to_market_symbol

logger = logging.getLogger("signals.sync.stock_minute")

_CALL_INTERVAL = float(os.getenv("STOCK_MINUTE_CALL_INTERVAL", "0.5"))
_PUBLIC_TIMEOUT = float(os.getenv("STOCK_MINUTE_TIMEOUT", "5"))
_MINUTE_FREQS = ["5分钟", "15分钟", "30分钟"]
_ENABLE_EASTMONEY_FALLBACK = os.getenv("STOCK_MINUTE_EASTMONEY_FALLBACK", "false").lower() == "true"
_DEFAULT_PRIORITY_CODES = "688802,300575"


def _index_codes() -> set[str]:
    import config

    return {
        str(symbol).lower().replace("sh", "").replace("sz", "")
        for symbol in getattr(config, "INDEX_AK_CODES", {}).values()
    }


def _pure_a_code(symbol: object) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    pure = raw.split(".", 1)[-1] if "." in raw else raw
    pure = pure.replace("SH", "").replace("SZ", "").replace("BJ", "")
    if pure.isdigit() and len(pure) == 6:
        return pure
    return ""


def _iter_strategy_snapshot_symbols() -> list[str]:
    symbols: list[str] = []
    try:
        from signals.strategy.snapshot import get_strategy_snapshot

        snapshot = get_strategy_snapshot()
    except Exception:
        return symbols

    for key in ("candidates", "warnings", "decision_queue", "buy_candidates", "sell_warnings"):
        rows = snapshot.get(key) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for field in ("symbol", "code", "raw_code"):
                value = row.get(field)
                if value:
                    symbols.append(str(value))
            metadata = row.get("metadata")
            if isinstance(metadata, dict):
                for field in ("symbol", "code", "raw_code"):
                    value = metadata.get(field)
                    if value:
                        symbols.append(str(value))

    rows = snapshot.get("themes") or []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            for field in ("leader_symbol", "leader_code", "representative_symbol", "representative_code"):
                value = row.get(field)
                if value:
                    symbols.append(str(value))
    return symbols


def _iter_configured_extra_symbols() -> list[str]:
    symbols: list[str] = []
    raw = os.getenv("STOCK_MINUTE_EXTRA_CODES", "")
    for value in raw.replace(";", ",").split(","):
        value = value.strip()
        if value:
            symbols.append(value)
    try:
        from signals.core.concept_carriers import preferred_carrier_symbols

        symbols.extend(preferred_carrier_symbols())
    except Exception:
        pass
    return symbols


def _env_symbol_values(*names: str, default: str = "") -> list[str]:
    values: list[str] = []
    raw = default
    for name in names:
        configured = os.getenv(name, "")
        if configured.strip():
            raw = configured
            break
    for value in raw.replace(";", ",").split(","):
        value = value.strip()
        if value:
            values.append(value)
    return values


def _selection_cap() -> int:
    lane = os.getenv("SIGNALS_CURRENT_SYNC_LANE", "")
    if lane == "signal_lane":
        return int(os.getenv("STOCK_MINUTE_SIGNAL_MAX_CODES", "24"))
    return int(os.getenv("STOCK_MINUTE_MAX_CODES", "200"))


def _select_symbols_with_priority(
    ordered: list[str],
    priority: set[str],
    max_symbols: int,
) -> tuple[list[str], list[dict[str, str]]]:
    if max_symbols <= 0 or len(ordered) <= max_symbols:
        return ordered, []
    priority_ordered = [code for code in ordered if code in priority]
    normal_ordered = [code for code in ordered if code not in priority]
    remaining = max(0, max_symbols - len(priority_ordered))
    selected = [*priority_ordered, *normal_ordered[:remaining]]
    selected_set = set(selected)
    skipped = [
        {"symbol": code, "reason": "cap_exceeded", "next_due_hint": "next signal_lane cycle"}
        for code in ordered
        if code not in selected_set
    ]
    return selected, skipped


def _get_active_symbols_with_meta(db: Database) -> tuple[list[str], dict]:
    """获取需要同步分钟线的活跃标的列表"""
    import config

    symbols: list[str] = []
    priority_symbols: set[str] = set()
    source_counts: dict[str, int] = {}
    index_codes = _index_codes()

    def add(value: object, source: str, *, priority: bool = False) -> None:
        code = _pure_a_code(value)
        if code in index_codes:
            return
        if priority and code:
            priority_symbols.add(code)
        if code and code not in symbols:
            symbols.append(code)
            source_counts[source] = source_counts.get(source, 0) + 1

    only_codes = os.getenv("STOCK_MINUTE_ONLY_CODES", "")
    if only_codes.strip():
        for symbol in only_codes.replace(";", ",").split(","):
            add(symbol, "only_codes", priority=True)
        return symbols, {
            "priority_symbols": symbols,
            "skipped_symbols": [],
            "source_counts": {"only_codes": len(symbols)},
            "max_symbols": len(symbols),
        }

    for symbol in _env_symbol_values(
        "STOCK_MINUTE_PRIORITY_CODES",
        "SIGNALS_PRIORITY_STOCK_CODES",
        default=os.getenv("STOCK_MINUTE_DEFAULT_PRIORITY_CODES", _DEFAULT_PRIORITY_CODES),
    ):
        add(symbol, "priority_codes", priority=True)

    for symbol in getattr(config, "WHITELIST", []):
        add(symbol, "whitelist", priority=True)

    for symbol in _iter_strategy_snapshot_symbols():
        add(symbol, "strategy_snapshot", priority=True)

    for symbol in _iter_configured_extra_symbols():
        add(symbol, "configured_extra", priority=True)

    pool = db["market_pools"].find_one(
        {"pool": "active"},
        {"symbols": 1, "items": 1},
        sort=[("dt", -1), ("updated_at", -1)],
    ) or {}
    for symbol in pool.get("symbols") or []:
        add(symbol, "active_pool")
    for item in pool.get("items") or []:
        if isinstance(item, dict):
            add(item.get("symbol") or item.get("code"), "active_pool")

    for doc in db["signals"].find({}, {"symbol": 1}).sort("signal_date", -1).limit(300):
        add(doc.get("symbol"), "signals")

    # Keep recently requested/synced symbols warm so UI-visible names do not fall
    # out of the signal lane just because the active pool rotated.
    recent = db["sync_log"].find(
        {"module": {"$in": ["stock_minute", "stock_daily"]}, "status": "ok"},
        {"symbol": 1, "module": 1},
    ).sort("last_run", -1).limit(200)

    for doc in recent:
        add(doc.get("symbol"), f"recent_{doc.get('module') or 'sync'}", priority=doc.get("module") == "stock_minute")

    max_symbols = _selection_cap()
    selected, skipped = _select_symbols_with_priority(symbols, priority_symbols, max_symbols)
    return selected, {
        "priority_symbols": [code for code in selected if code in priority_symbols],
        "skipped_symbols": skipped,
        "source_counts": source_counts,
        "max_symbols": max_symbols,
        "candidate_count": len(symbols),
    }


def _get_active_symbols(db: Database) -> list:
    return _get_active_symbols_with_meta(db)[0]


def _sync_one_minute(code: str, freq: str, proxy_url: str = None) -> list:
    """同步单只股票分钟线"""
    period_map = {"5分钟": "5", "15分钟": "15", "30分钟": "30"}
    period = period_map.get(freq, "30")
    source = "eastmoney"

    try:
        df, source = fetch_public_minute(stock_to_market_symbol(code), period, timeout=_PUBLIC_TIMEOUT)
    except Exception as public_error:
        if not _ENABLE_EASTMONEY_FALLBACK:
            logger.warning("公共分钟线失败，跳过东财兜底 %s %s: %s", code, freq, public_error)
            return []
        logger.warning("公共分钟线失败，显式尝试东财兜底 %s %s: %s", code, freq, public_error)
        with em_proxy(proxy_url):
            df = ak.stock_zh_a_hist_min_em(
                symbol=code, period=period, adjust="qfq")

    if df is None or df.empty:
        return []

    docs = []
    for _, row in df.iterrows():
        docs.append({
            "dt": pd.to_datetime(row["时间"]),
            "meta": {"symbol": code, "freq": freq, "source": source, "market": "A"},
            "open": float(row["开盘"]),
            "high": float(row["最高"]),
            "low": float(row["最低"]),
            "close": float(row["收盘"]),
            "vol": int(row["成交量"]) if pd.notna(row["成交量"]) else 0,
            "amount": int(float(row["成交额"])) if pd.notna(row["成交额"]) else 0,
        })
    return docs


@sync_retry
def sync_stock_minute(db: Database, proxy_url: str = None) -> dict:
    """
    A 股分钟线增量同步。

    仅同步白名单 + 最近活跃标的的 5M、15M 和 30M 数据。
    公共分钟线返回滚动窗口，直接全量写入（小数据量）。
    """
    bars_col = db["bars"]
    sync_col = db["sync_log"]

    symbols, selection_meta = _get_active_symbols_with_meta(db)
    logger.info(f"分钟线同步: {len(symbols)} 只活跃标的")

    total_inserted = 0
    errors = []

    for code in symbols:
        for freq in _MINUTE_FREQS:
            try:
                docs = _sync_one_minute(code, freq, proxy_url)
                time.sleep(_CALL_INTERVAL)

                if not docs:
                    continue

                # 删除该标的该频率的旧数据，重新插入（滚动窗口，全量覆盖更安全）
                bars_col.delete_many({
                    "meta.symbol": code,
                    "meta.freq": freq,
                })
                bars_col.insert_many(docs, ordered=False)
                total_inserted += len(docs)

                sync_col.update_one(
                    {"_id": f"stock_minute:{code}:{freq}"},
                    {"$set": {
                        "module": "stock_minute",
                        "symbol": code,
                        "last_dt": docs[-1]["dt"],
                        "last_run": naive_market_now("A"),
                        "status": "ok",
                        "bar_count": len(docs),
                        "source": docs[-1].get("meta", {}).get("source"),
                    }},
                    upsert=True,
                )

            except Exception as e:
                errors.append((code, freq, str(e)))

    skipped_symbols = selection_meta.get("skipped_symbols") or []
    sync_col.update_one(
        {"_id": "stock_minute:selection:_meta"},
        {"$set": {
            "module": "stock_minute",
            "status": "ok" if not skipped_symbols else "partial",
            "last_run": naive_market_now("A"),
            "selected_symbols": symbols,
            "priority_symbols": selection_meta.get("priority_symbols") or [],
            "skipped_symbols": skipped_symbols[:80],
            "skipped_count": len(skipped_symbols),
            "source_counts": selection_meta.get("source_counts") or {},
            "max_symbols": selection_meta.get("max_symbols"),
            "candidate_count": selection_meta.get("candidate_count"),
        }},
        upsert=True,
    )

    logger.info(f"分钟线完成: +{total_inserted} bars, {len(errors)} 失败, {len(skipped_symbols)} cap跳过")
    return {
        "inserted": total_inserted,
        "errors": len(errors),
        "selected": len(symbols),
        "priority": len(selection_meta.get("priority_symbols") or []),
        "skipped": len(skipped_symbols),
        "skipped_symbols": skipped_symbols[:20],
    }
