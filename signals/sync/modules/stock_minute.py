# -*- coding: utf-8 -*-
"""
A股分钟线同步 — 活跃标的 5M/15M/30M 增量同步

数据源: Sina/Tencent 公共分钟线；东财分钟线可显式开启为最后兜底
策略: 增量同步，仅白名单 + 最近入池标的（~200只上限）
频率: 工作日 16:00
注意: 公共分钟线返回滚动窗口数据；盘中只取尾窗并写入新 bar
"""
import logging
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import akshare as ak
import pandas as pd
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from ..proxy import em_proxy
from ..retry import sync_retry
from ..task_context import get_task_env
from .minute_sources import fetch_public_minute, stock_to_market_symbol

logger = logging.getLogger("signals.sync.stock_minute")

_CALL_INTERVAL = float(os.getenv("STOCK_MINUTE_CALL_INTERVAL", "0.5"))
_PUBLIC_TIMEOUT = float(os.getenv("STOCK_MINUTE_TIMEOUT", "5"))
_MINUTE_FREQS = ["5分钟", "15分钟", "30分钟"]
_ENABLE_EASTMONEY_FALLBACK = os.getenv("STOCK_MINUTE_EASTMONEY_FALLBACK", "false").lower() == "true"
_DEFAULT_PRIORITY_CODES = "688802,300575"
_DEFAULT_TAIL_COUNTS = {"5分钟": 240, "15分钟": 160, "30分钟": 120}


def _int_env(name: str, default: int, *, min_value: int = 1, max_value: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _worker_count() -> int:
    return _int_env("STOCK_MINUTE_WORKERS", 3, min_value=1, max_value=6)


def _tail_count_for_freq(freq: str) -> int:
    suffix_map = {"5分钟": "5", "15分钟": "15", "30分钟": "30"}
    default = _DEFAULT_TAIL_COUNTS.get(freq, 120)
    generic = _int_env("STOCK_MINUTE_TAIL_COUNT", default, min_value=40, max_value=500)
    suffix = suffix_map.get(freq)
    if not suffix:
        return generic
    return _int_env(f"STOCK_MINUTE_TAIL_COUNT_{suffix}", generic, min_value=40, max_value=500)


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
    if _postmarket_minute_scope():
        return _int_env("STOCK_MINUTE_POSTMARKET_MAX_CODES", 360, min_value=1, max_value=1000)
    lane = os.getenv("SIGNALS_CURRENT_SYNC_LANE", "")
    market = os.getenv("SIGNALS_CURRENT_SYNC_MARKET", "")
    if lane == "signal_lane":
        if market == "A":
            return int(os.getenv("STOCK_MINUTE_SIGNAL_MAX_CODES", "72"))
        close_default = os.getenv("TERMINAL_REALTIME_STOCK_LIMIT", "72")
        return int(os.getenv("STOCK_MINUTE_CLOSE_MAX_CODES", close_default))
    if market == "A":
        return int(os.getenv("STOCK_MINUTE_SIGNAL_MAX_CODES", "72"))
    return int(os.getenv("STOCK_MINUTE_MAX_CODES", "200"))


def _postmarket_minute_scope() -> bool:
    scope = str(get_task_env("STOCK_MINUTE_SCOPE", "") or "").strip().lower()
    if scope in {"postmarket", "postmarket_candidates", "expanded_postmarket"}:
        return True
    return str(get_task_env("SIGNALS_POSTMARKET_MINUTE_PREHEAT", "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


def _add_candidate(
    symbols: list[str],
    source_counts: dict[str, int],
    priority_symbols: set[str],
    pinned_symbols: set[str],
    index_codes: set[str],
    value: object,
    source: str,
    *,
    priority: bool = True,
    pinned: bool = False,
    symbol_sources: dict[str, set[str]] | None = None,
) -> str:
    code = _pure_a_code(value)
    if not code or code in index_codes:
        return ""
    if code not in symbols:
        symbols.append(code)
    if priority:
        priority_symbols.add(code)
    if pinned:
        pinned_symbols.add(code)
    if source:
        source_counts[source] = source_counts.get(source, 0) + 1
        if symbol_sources is not None:
            symbol_sources.setdefault(code, set()).add(source)
    return code


def _reason_types_from_row(row: dict) -> set[str]:
    reasons = row.get("inclusion_reasons") if isinstance(row.get("inclusion_reasons"), list) else []
    reason_types = {str(reason.get("reason_type") or "") for reason in reasons if isinstance(reason, dict)}
    signal_origin = str(row.get("signal_origin") or "")
    if signal_origin:
        reason_types.add(signal_origin)
    return {reason_type for reason_type in reason_types if reason_type}


def _add_postmarket_expanded_candidates(
    db: Database,
    symbols: list[str],
    source_counts: dict[str, int],
    priority_symbols: set[str],
    pinned_symbols: set[str],
    index_codes: set[str],
    symbol_sources: dict[str, set[str]] | None = None,
) -> None:
    """Add wider next-day minute preheat candidates without scanning all A shares."""
    tech_limit = _int_env("STOCK_MINUTE_POSTMARKET_TECHNICAL_LIMIT", 500, min_value=1, max_value=2000)
    knowledge_limit = _int_env("STOCK_MINUTE_POSTMARKET_KNOWLEDGE_LIMIT", 300, min_value=1, max_value=1000)
    chain_limit = _int_env("STOCK_MINUTE_POSTMARKET_CHAIN_LIMIT", 120, min_value=1, max_value=500)

    try:
        cursor = db["terminal_technical_signals"].find(
            {"market": "A"},
            {"symbol": 1, "raw_code": 1, "signal_side": 1, "total_score": 1, "score": 1, "updated_at": 1, "as_of": 1},
        ).sort([("as_of", -1), ("updated_at", -1), ("total_score", -1), ("score", -1)]).limit(tech_limit)
        for doc in cursor:
            _add_candidate(symbols, source_counts, priority_symbols, pinned_symbols, index_codes, doc.get("raw_code") or doc.get("symbol"), "terminal_technical_signals", symbol_sources=symbol_sources)
    except Exception as exc:
        logger.debug("postmarket technical minute candidates skipped: %s", exc)

    try:
        cursor = db["knowledge_market_views"].find(
            {"market": "A", "target_type": "stock"},
            {"symbol": 1, "raw_code": 1, "updated_at": 1, "as_of": 1},
        ).sort([("as_of", -1), ("updated_at", -1)]).limit(knowledge_limit)
        for doc in cursor:
            _add_candidate(symbols, source_counts, priority_symbols, pinned_symbols, index_codes, doc.get("raw_code") or doc.get("symbol"), "knowledge_market_views", symbol_sources=symbol_sources)
    except Exception as exc:
        logger.debug("postmarket knowledge minute candidates skipped: %s", exc)

    try:
        latest = db["chain_heat_snapshots"].find_one({"market": "A"}, {"trade_minute": 1}, sort=[("trade_minute", -1)]) or {}
        trade_minute = latest.get("trade_minute")
        if trade_minute is not None:
            cursor = db["chain_heat_snapshots"].find(
                {"market": "A", "trade_minute": trade_minute},
                {"representatives": 1, "integrated_domains": 1},
            ).sort("rank", 1).limit(chain_limit)
            for chain in cursor:
                for rep in chain.get("representatives") or []:
                    if isinstance(rep, dict):
                        _add_candidate(symbols, source_counts, priority_symbols, pinned_symbols, index_codes, rep.get("symbol"), "chain_representatives", symbol_sources=symbol_sources)
                for domain in chain.get("integrated_domains") or []:
                    if isinstance(domain, dict):
                        _add_candidate(symbols, source_counts, priority_symbols, pinned_symbols, index_codes, domain.get("leader_symbol"), "chain_domain_leaders", symbol_sources=symbol_sources)
    except Exception as exc:
        logger.debug("postmarket chain minute candidates skipped: %s", exc)


def _select_symbols_with_priority(
    ordered: list[str],
    priority: set[str],
    max_symbols: int,
    *,
    pinned: set[str] | None = None,
    last_runs: dict[str, object] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    if max_symbols <= 0 or len(ordered) <= max_symbols:
        return ordered, []
    pinned = pinned or set()
    last_runs = last_runs or {}
    original_idx = {code: idx for idx, code in enumerate(ordered)}

    def stale_rank(code: str) -> float:
        value = last_runs.get(code)
        if value is None:
            return float("-inf")
        if hasattr(value, "timestamp"):
            return float(value.timestamp())
        try:
            parsed = pd.to_datetime(value, errors="coerce")
            if pd.isna(parsed):
                return float("-inf")
            return float(parsed.timestamp())
        except Exception:
            return float("-inf")

    pinned_ordered = [code for code in ordered if code in pinned]
    pinned_selected = pinned_ordered[:max_symbols]
    remaining = max_symbols - len(pinned_selected)
    rest = [code for code in ordered if code not in set(pinned_selected)]
    rest_sorted = sorted(
        rest,
        key=lambda code: (
            0 if code in priority else 1,
            stale_rank(code),
            original_idx.get(code, 0),
        ),
    )
    selected = [*pinned_selected, *rest_sorted[:remaining]]
    selected_set = set(selected)
    skipped = [
        {
            "symbol": code,
            "reason": "rotation_pending_priority" if code in priority else "rotation_pending",
            "next_due_hint": "stale-first signal_lane rotation",
        }
        for code in ordered
        if code not in selected_set
    ]
    return selected, skipped


def _latest_stock_minute_runs(db: Database, symbols: list[str]) -> dict[str, object]:
    if not symbols:
        return {}
    last_runs: dict[str, object] = {}
    try:
        docs = db["sync_log"].find(
            {"module": "stock_minute", "status": "ok", "symbol": {"$in": symbols}},
            {"symbol": 1, "last_run": 1},
        ).sort("last_run", -1)
    except Exception:
        return {}
    for doc in docs:
        symbol = _pure_a_code(doc.get("symbol"))
        if symbol and symbol not in last_runs:
            last_runs[symbol] = doc.get("last_run")
    return last_runs


def _minute_trade_date() -> str:
    return naive_market_now("A").date().isoformat()


def _minute_universe_statuses(db: Database, symbols: list[str], trade_date: str) -> dict[str, dict]:
    if not symbols:
        return {}
    try:
        cursor = db["minute_preheat_universe"].find(
            {"trade_date": trade_date, "symbol": {"$in": symbols}},
            {"symbol": 1, "status": 1, "updated_at": 1, "cached_at": 1, "last_attempt_at": 1},
        )
    except Exception:
        return {}
    result: dict[str, dict] = {}
    for doc in cursor:
        code = _pure_a_code(doc.get("symbol"))
        if code:
            result[code] = dict(doc)
    return result


def _upsert_minute_preheat_universe(
    db: Database,
    *,
    symbols: list[str],
    symbol_sources: dict[str, set[str]],
    priority_symbols: set[str],
    pinned_symbols: set[str],
    source_counts: dict[str, int],
    max_symbols: int,
) -> dict[str, int]:
    trade_date = _minute_trade_date()
    now = naive_market_now("A")
    col = db["minute_preheat_universe"]
    written = 0
    try:
        for idx, code in enumerate(symbols):
            sources = sorted(symbol_sources.get(code) or [])
            col.update_one(
                {"_id": f"{trade_date}:{code}"},
                {
                    "$set": {
                        "trade_date": trade_date,
                        "market": "A",
                        "symbol": code,
                        "order": idx,
                        "sources": sources,
                        "source_counts": source_counts,
                        "priority": code in priority_symbols,
                        "pinned": code in pinned_symbols,
                        "max_per_run": max_symbols,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "status": "pending",
                        "freq_status": {},
                        "skipped_reason": "",
                        "created_at": now,
                    },
                },
                upsert=True,
            )
            written += 1
    except Exception:
        return {"written": written, "total": len(symbols)}
    return {"written": written, "total": len(symbols)}


def _select_postmarket_minute_symbols(
    ordered: list[str],
    priority: set[str],
    max_symbols: int,
    *,
    pinned: set[str],
    last_runs: dict[str, object],
    universe_states: dict[str, dict],
) -> tuple[list[str], list[dict[str, str]]]:
    if max_symbols <= 0 or len(ordered) <= max_symbols:
        return ordered, []
    original_idx = {code: idx for idx, code in enumerate(ordered)}

    def stale_rank(code: str) -> float:
        value = last_runs.get(code)
        if value is None:
            return float("-inf")
        try:
            parsed = pd.to_datetime(value, errors="coerce")
            if pd.isna(parsed):
                return float("-inf")
            return float(parsed.timestamp())
        except Exception:
            return float("-inf")

    def status_rank(code: str) -> int:
        if code in pinned:
            return 0
        status = str((universe_states.get(code) or {}).get("status") or "pending")
        if status in {"pending", "error", "stale"}:
            return 1
        if status == "running":
            return 2
        if status == "cached":
            return 4
        return 3

    sorted_codes = sorted(
        ordered,
        key=lambda code: (
            status_rank(code),
            0 if code in priority else 1,
            stale_rank(code),
            original_idx.get(code, 0),
        ),
    )
    selected = sorted_codes[:max_symbols]
    selected_set = set(selected)
    skipped = [
        {
            "symbol": code,
            "reason": "postmarket_universe_pending",
            "next_due_hint": "next postmarket minute preheat rotation",
        }
        for code in ordered
        if code not in selected_set
    ]
    return selected, skipped


def _mark_minute_universe_selected(db: Database, symbols: list[str]) -> None:
    if not symbols:
        return
    trade_date = _minute_trade_date()
    now = naive_market_now("A")
    try:
        for code in symbols:
            db["minute_preheat_universe"].update_one(
                {"_id": f"{trade_date}:{code}"},
                {"$set": {"status": "running", "selected_current_run": True, "last_attempt_at": now, "updated_at": now}},
                upsert=True,
            )
    except Exception:
        return


def _mark_minute_universe_results(db: Database, per_symbol: dict[str, dict]) -> dict[str, int]:
    if not per_symbol:
        return {"cached": 0, "error": 0}
    trade_date = _minute_trade_date()
    now = naive_market_now("A")
    cached = 0
    failed = 0
    try:
        for code, result in per_symbol.items():
            freq_status = result.get("freq_status") or {}
            errors = int(result.get("errors") or 0)
            ok_calls = sum(1 for status in freq_status.values() if status in {"ok", "empty"})
            status = "cached" if errors == 0 and ok_calls == len(_MINUTE_FREQS) else "error"
            if status == "cached":
                cached += 1
            else:
                failed += 1
            db["minute_preheat_universe"].update_one(
                {"_id": f"{trade_date}:{code}"},
                {"$set": {
                    "status": status,
                    "freq_status": freq_status,
                    "written": int(result.get("written") or 0),
                    "errors": errors,
                    "skipped_reason": "" if status == "cached" else "minute_fetch_failed",
                    "cached_at": now if status == "cached" else None,
                    "selected_current_run": False,
                    "updated_at": now,
                }},
                upsert=True,
            )
    except Exception:
        return {"cached": cached, "error": failed}
    return {"cached": cached, "error": failed}


def _minute_universe_summary(db: Database, trade_date: str | None = None) -> dict[str, int]:
    trade_date = trade_date or _minute_trade_date()
    try:
        rows = list(db["minute_preheat_universe"].find({"trade_date": trade_date}, {"status": 1}))
    except Exception:
        return {}
    counts = defaultdict(int)
    for row in rows:
        counts[str(row.get("status") or "pending")] += 1
    total = sum(counts.values())
    return {
        "total": total,
        "cached": counts["cached"],
        "pending": counts["pending"],
        "running": counts["running"],
        "error": counts["error"],
    }


def _get_active_symbols_with_meta(db: Database) -> tuple[list[str], dict]:
    """获取需要同步分钟线的活跃标的列表。

    The trading terminal's whitebox pool is the single source of truth for
    intraday stock minute refreshes. Other sources feed terminal_stock_pool in
    the post-market builder instead of being read ad hoc here.
    """
    priority_symbols: set[str] = set()
    pinned_symbols: set[str] = set()
    symbol_sources: dict[str, set[str]] = {}
    index_codes = _index_codes()

    only_codes = os.getenv("STOCK_MINUTE_ONLY_CODES", "")
    if only_codes.strip():
        symbols: list[str] = []
        for symbol in only_codes.replace(";", ",").split(","):
            code = _pure_a_code(symbol)
            if code and code not in index_codes and code not in symbols:
                symbols.append(code)
        return symbols, {
            "priority_symbols": symbols,
            "pinned_symbols": symbols,
            "skipped_symbols": [],
            "source_counts": {"only_codes": len(symbols)},
            "max_symbols": len(symbols),
            "rotation_enabled": False,
        }

    terminal_pool = db["terminal_stock_pool"].find_one(
        {"pool": "terminal_stock_pool", "market": "A"},
        {"stocks": 1, "skipped_stocks": 1, "candidate_count": 1, "reason_counts": 1, "stock_limit": 1},
        sort=[("updated_at", -1)],
    ) or {}
    symbols = []
    source_counts: dict[str, int] = {}
    postmarket_scope = _postmarket_minute_scope()
    expanded_candidates_added = False
    for row in terminal_pool.get("stocks") or []:
        if isinstance(row, dict):
            reason_types = _reason_types_from_row(row)
            code = _add_candidate(
                symbols,
                source_counts,
                priority_symbols,
                pinned_symbols,
                index_codes,
                row.get("raw_code") or row.get("symbol") or row.get("code"),
                "terminal_stock_pool",
                priority=bool(reason_types),
                pinned="user_pinned" in reason_types,
                symbol_sources=symbol_sources,
            )
            for reason_type in reason_types:
                if reason_type:
                    source_counts[reason_type] = source_counts.get(reason_type, 0) + 1
        else:
            _add_candidate(symbols, source_counts, priority_symbols, pinned_symbols, index_codes, row, "terminal_stock_pool", symbol_sources=symbol_sources)

    if not symbols and postmarket_scope:
        _add_postmarket_expanded_candidates(
            db,
            symbols,
            source_counts,
            priority_symbols,
            pinned_symbols,
            index_codes,
            symbol_sources,
        )
        expanded_candidates_added = True

    if not symbols:
        return [], {
            "priority_symbols": [],
            "pinned_symbols": [],
            "skipped_symbols": [],
            "source_counts": {},
            "max_symbols": 0,
            "candidate_count": 0,
            "rotation_enabled": False,
            "rotation_policy": "terminal_stock_pool_required",
            "not_ready_reason": "terminal_stock_pool_empty",
        }

    skipped_pool = []
    for row in terminal_pool.get("skipped_stocks") or []:
        if not isinstance(row, dict):
            continue
        code = _pure_a_code(row.get("raw_code") or row.get("symbol") or row.get("code"))
        if code and postmarket_scope:
            reason_types = _reason_types_from_row(row)
            _add_candidate(
                symbols,
                source_counts,
                priority_symbols,
                pinned_symbols,
                index_codes,
                code,
                "terminal_stock_pool_skipped",
                priority=bool(reason_types),
                pinned="user_pinned" in reason_types,
                symbol_sources=symbol_sources,
            )
            for reason_type in reason_types:
                source_counts[reason_type] = source_counts.get(reason_type, 0) + 1
        elif code:
            skipped_pool.append({
                "symbol": code,
                "reason": "terminal_stock_pool_cap",
                "next_due_hint": row.get("signal_origin") or "whitebox pool rank rotation",
            })
    if postmarket_scope and not expanded_candidates_added:
        _add_postmarket_expanded_candidates(
            db,
            symbols,
            source_counts,
            priority_symbols,
            pinned_symbols,
            index_codes,
            symbol_sources,
        )
    max_symbols = _selection_cap()
    last_runs = _latest_stock_minute_runs(db, symbols)
    universe_meta: dict[str, int] = {}
    if postmarket_scope:
        universe_meta = _upsert_minute_preheat_universe(
            db,
            symbols=symbols,
            symbol_sources=symbol_sources,
            priority_symbols=priority_symbols,
            pinned_symbols=pinned_symbols,
            source_counts=source_counts,
            max_symbols=max_symbols,
        )
        universe_states = _minute_universe_statuses(db, symbols, _minute_trade_date())
        selected, skipped = _select_postmarket_minute_symbols(
            symbols,
            priority_symbols,
            max_symbols,
            pinned=pinned_symbols,
            last_runs=last_runs,
            universe_states=universe_states,
        )
    else:
        selected, skipped = _select_symbols_with_priority(
            symbols,
            priority_symbols,
            max_symbols,
            pinned=pinned_symbols,
            last_runs=last_runs,
        )
    universe_summary = _minute_universe_summary(db) if postmarket_scope else {}
    return selected, {
        "priority_symbols": [code for code in selected if code in priority_symbols],
        "pinned_symbols": [code for code in selected if code in pinned_symbols],
        "skipped_symbols": skipped + skipped_pool,
        "source_counts": source_counts,
        "max_symbols": max_symbols,
        "candidate_count": int(terminal_pool.get("candidate_count") or len(symbols)),
        "rotation_enabled": True,
        "rotation_policy": "postmarket_expanded_candidate_preheat" if postmarket_scope else "terminal_stock_pool_rank_then_stale_first",
        "minute_scope": "postmarket_candidates" if postmarket_scope else "terminal_stock_pool",
        "tier_counts": {
            "selected_pinned": sum(1 for code in selected if code in pinned_symbols),
            "selected_priority": sum(1 for code in selected if code in priority_symbols and code not in pinned_symbols),
            "selected_normal": sum(1 for code in selected if code not in priority_symbols),
            "candidate_priority": len(priority_symbols),
            "candidate_pinned": len(pinned_symbols),
            "pool_skipped": len(skipped_pool),
        },
        "universe_total": universe_summary.get("total") or universe_meta.get("total") or len(symbols) if postmarket_scope else len(symbols),
        "universe_cached": universe_summary.get("cached", 0),
        "universe_pending": universe_summary.get("pending", 0),
        "universe_error": universe_summary.get("error", 0),
    }


def _get_active_symbols(db: Database) -> list:
    return _get_active_symbols_with_meta(db)[0]


def _sync_one_minute(code: str, freq: str, proxy_url: str = None, *, tail_count: int | None = None) -> list:
    """同步单只股票分钟线"""
    period_map = {"5分钟": "5", "15分钟": "15", "30分钟": "30"}
    period = period_map.get(freq, "30")
    source = "eastmoney"
    tail_count = tail_count or _tail_count_for_freq(freq)

    try:
        df, source = fetch_public_minute(
            stock_to_market_symbol(code),
            period,
            timeout=_PUBLIC_TIMEOUT,
            datalen=tail_count,
            count=tail_count,
        )
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


def _insert_new_minute_docs(bars_col, code: str, freq: str, docs: list[dict]) -> dict:
    if not docs:
        return {"written": 0, "bar_count": 0, "last_dt": None}

    latest_before = bars_col.find_one(
        {"meta.symbol": code, "meta.freq": freq},
        {"dt": 1},
        sort=[("dt", -1)],
    )
    latest_dt = latest_before.get("dt") if latest_before else None
    new_docs = [doc for doc in docs if latest_dt is None or doc["dt"] > latest_dt]
    written = 0
    if new_docs:
        result = bars_col.insert_many(new_docs, ordered=False)
        written = len(result.inserted_ids)
    return {
        "written": written,
        "inserted": written,
        "skipped_existing": len(docs) - len(new_docs),
        "bar_count": len(docs),
        "last_dt": docs[-1]["dt"],
        "latest_before": latest_dt,
    }


@sync_retry
def sync_stock_minute(db: Database, proxy_url: str = None) -> dict:
    """
    A 股分钟线增量同步。

    仅同步白名单 + 最近活跃标的的 5M、15M 和 30M 数据。
    公共分钟线返回滚动窗口；盘中只拉尾窗并插入新 bar，避免删整段重插。
    """
    bars_col = db["bars"]
    sync_col = db["sync_log"]

    symbols, selection_meta = _get_active_symbols_with_meta(db)
    logger.info(f"分钟线同步: {len(symbols)} 只活跃标的")
    postmarket_scope = _postmarket_minute_scope()
    if postmarket_scope:
        _mark_minute_universe_selected(db, symbols)

    workers = _worker_count()
    tail_counts = {freq: _tail_count_for_freq(freq) for freq in _MINUTE_FREQS}
    total_written = 0
    total_skipped_existing = 0
    empty = 0
    errors = []
    per_symbol: dict[str, dict] = defaultdict(lambda: {"written": 0, "errors": 0, "freq_status": {}})
    tasks = [(code, freq) for code in symbols for freq in _MINUTE_FREQS]

    def sync_one(code: str, freq: str) -> dict:
        started = time.monotonic()
        docs = _sync_one_minute(code, freq, proxy_url, tail_count=tail_counts[freq])
        time.sleep(_CALL_INTERVAL)
        if not docs:
            sync_col.update_one(
                {"_id": f"stock_minute:{code}:{freq}"},
                {"$set": {
                    "module": "stock_minute",
                    "symbol": code,
                    "freq": freq,
                    "last_run": naive_market_now("A"),
                    "status": "empty",
                    "incremental": True,
                    "write_mode": "insert_new",
                    "tail_count": tail_counts[freq],
                    "elapsed": round(time.monotonic() - started, 3),
                }},
                upsert=True,
            )
            return {"code": code, "freq": freq, "status": "empty", "written": 0, "skipped_existing": 0}

        write_result = _insert_new_minute_docs(bars_col, code, freq, docs)
        sync_col.update_one(
            {"_id": f"stock_minute:{code}:{freq}"},
            {"$set": {
                "module": "stock_minute",
                "symbol": code,
                "freq": freq,
                "last_dt": write_result["last_dt"],
                "last_run": naive_market_now("A"),
                "status": "ok",
                "bar_count": write_result["bar_count"],
                "written": write_result["written"],
                "inserted": write_result["inserted"],
                "skipped_existing": write_result["skipped_existing"],
                "incremental": True,
                "write_mode": "insert_new",
                "tail_count": tail_counts[freq],
                "latest_before": write_result.get("latest_before"),
                "source": docs[-1].get("meta", {}).get("source"),
                "elapsed": round(time.monotonic() - started, 3),
            }},
            upsert=True,
        )
        return {"code": code, "freq": freq, "status": "ok", **write_result}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(sync_one, code, freq): (code, freq) for code, freq in tasks}
        for future in as_completed(future_map):
            code, freq = future_map[future]
            try:
                result = future.result()
                per_symbol[code]["freq_status"][freq] = str(result.get("status") or "ok")
                per_symbol[code]["written"] += int(result.get("written") or 0)
                if result.get("status") == "empty":
                    empty += 1
                total_written += int(result.get("written") or 0)
                total_skipped_existing += int(result.get("skipped_existing") or 0)
            except Exception as e:
                per_symbol[code]["freq_status"][freq] = "error"
                per_symbol[code]["errors"] += 1
                errors.append((code, freq, str(e)))
                sync_col.update_one(
                    {"_id": f"stock_minute:{code}:{freq}"},
                    {"$set": {
                        "module": "stock_minute",
                        "symbol": code,
                        "freq": freq,
                        "last_run": naive_market_now("A"),
                        "status": "error",
                        "error": str(e)[:300],
                        "incremental": True,
                        "write_mode": "insert_new",
                        "tail_count": tail_counts.get(freq),
                    }},
                    upsert=True,
                )

    universe_result = _mark_minute_universe_results(db, per_symbol) if postmarket_scope else {}
    universe_summary = _minute_universe_summary(db) if postmarket_scope else {}
    skipped_symbols = selection_meta.get("skipped_symbols") or []
    sync_col.update_one(
        {"_id": "stock_minute:selection:_meta"},
        {"$set": {
            "module": "stock_minute",
            "status": "ok" if not skipped_symbols else "partial",
            "last_run": naive_market_now("A"),
            "selected_symbols": symbols,
            "priority_symbols": selection_meta.get("priority_symbols") or [],
            "pinned_symbols": selection_meta.get("pinned_symbols") or [],
            "skipped_symbols": skipped_symbols[:80],
            "skipped_count": len(skipped_symbols),
            "source_counts": selection_meta.get("source_counts") or {},
            "max_symbols": selection_meta.get("max_symbols"),
            "candidate_count": selection_meta.get("candidate_count"),
            "rotation_enabled": selection_meta.get("rotation_enabled", False),
            "rotation_policy": selection_meta.get("rotation_policy", ""),
            "minute_scope": selection_meta.get("minute_scope", ""),
            "tier_counts": selection_meta.get("tier_counts") or {},
            "universe_total": universe_summary.get("total") or selection_meta.get("universe_total"),
            "universe_cached": universe_summary.get("cached", 0),
            "universe_pending": universe_summary.get("pending", 0),
            "universe_running": universe_summary.get("running", 0),
            "universe_error": universe_summary.get("error", 0),
            "workers": workers,
            "tail_counts": tail_counts,
            "incremental": True,
            "write_mode": "insert_new",
            "planned_calls": len(tasks),
            "empty_calls": empty,
            "failed_calls": len(errors),
            "written": total_written,
            "skipped_existing": total_skipped_existing,
        }},
        upsert=True,
    )

    logger.info(
        "分钟线完成: %d calls, inserted=%d skipped_existing=%d empty=%d failed=%d, %d cap跳过",
        len(tasks), total_written, total_skipped_existing, empty, len(errors), len(skipped_symbols),
    )
    return {
        "inserted": total_written,
        "written": total_written,
        "skipped_existing": total_skipped_existing,
        "errors": len(errors),
        "empty": empty,
        "selected": len(symbols),
        "priority": len(selection_meta.get("priority_symbols") or []),
        "skipped": len(skipped_symbols),
        "skipped_symbols": skipped_symbols[:20],
        "minute_scope": selection_meta.get("minute_scope", ""),
        "source_counts": selection_meta.get("source_counts") or {},
        "universe_total": universe_summary.get("total") or selection_meta.get("universe_total"),
        "universe_cached": universe_summary.get("cached", 0),
        "universe_pending": universe_summary.get("pending", 0),
        "universe_error": universe_summary.get("error", 0),
        "universe_result": universe_result,
        "workers": workers,
        "tail_counts": tail_counts,
        "planned_calls": len(tasks),
        "incremental": True,
        "write_mode": "insert_new",
    }
