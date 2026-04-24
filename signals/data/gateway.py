# -*- coding: utf-8 -*-
"""Signals data gateway.

The gateway separates historical close data from realtime quote snapshots.
It is intentionally small in v1: board/concept/constituent routing is owned
here, while legacy K-line fetchers are called through a historical wrapper until
they are migrated fully.
"""
from __future__ import annotations

import logging
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from .models import DataRequest, DataResponse, normalize_as_of, resolve_mode

logger = logging.getLogger("signals.data.gateway")

SOURCE_COLLECTIONS = {
    "board": [("em", "board_em"), ("ths", "board_ths"), ("sina", "board_sina")],
    "concept": [("sina", "concept_sina"), ("em", "concept_em"), ("ths", "concept_ths")],
}

CANONICAL_COLLECTIONS = {
    "board": "board_ranking",
    "concept": "concept_ranking",
}

FREQ_ALIASES = {
    "daily": ["daily", "日线", "D", "1d"],
    "weekly": ["weekly", "周线", "W", "1w"],
    "monthly": ["monthly", "月线", "M", "1m"],
    "15m": ["15m", "15分钟", "15"],
    "30m": ["30m", "30分钟", "30"],
    "quote": ["quote"],
}


def _elapsed_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000


def _latest_df(collection: str, query: Optional[dict] = None) -> Optional[pd.DataFrame]:
    from signals.data.mongo_fallback import get_latest_df

    df = get_latest_df(collection, query=query)
    if df is None or df.empty:
        return None
    return df


def _latest_as_of(df: pd.DataFrame) -> Optional[str]:
    if df is None or df.empty or "dt" not in df.columns:
        return None
    try:
        return str(pd.to_datetime(df["dt"].max()).date())
    except Exception:
        return str(df["dt"].iloc[0])[:10]


def _normalize_freq(freq: Optional[str]) -> str:
    value = (freq or "daily").strip()
    for canonical, aliases in FREQ_ALIASES.items():
        if value in aliases:
            return canonical
    return value


def _freq_candidates(freq: Optional[str]) -> list[str]:
    canonical = _normalize_freq(freq)
    aliases = FREQ_ALIASES.get(canonical, [canonical])
    return list(dict.fromkeys([canonical, *aliases]))


def _symbol_candidates(symbol: Optional[str]) -> list[str]:
    if not symbol:
        return []
    raw = str(symbol).strip()
    pure = raw.split(".")[-1] if "." in raw else raw
    candidates = [
        raw,
        pure,
        raw.upper(),
        raw.lower(),
        f"SH.{pure}" if pure.startswith(("5", "6", "9")) else f"SZ.{pure}",
        f"sh{pure}" if pure.startswith(("5", "6", "9")) else f"sz{pure}",
    ]
    return list(dict.fromkeys(candidates))


def _bars_df_from_docs(docs: list[dict], source: str) -> pd.DataFrame:
    if not docs:
        return pd.DataFrame()
    df = pd.DataFrame(docs)
    if "dt" not in df.columns:
        return pd.DataFrame()
    df["dt"] = pd.to_datetime(df["dt"], errors="coerce")
    df = df.dropna(subset=["dt"]).sort_values("dt").set_index("dt")
    for col in ["open", "high", "low", "close", "vol", "amount"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    keep = [c for c in ["open", "high", "low", "close", "vol", "amount"] if c in df.columns]
    df = df[keep] if keep else df
    df.attrs["data_source"] = source
    if not df.empty:
        df.attrs["as_of"] = str(df.index.max().date())
    return df


def _load_bars_from_mongo(symbol: Optional[str], freq: Optional[str]) -> tuple[pd.DataFrame, str]:
    if not symbol:
        return pd.DataFrame(), ""
    try:
        from signals.data.mongo_fallback import get_db, get_kline_docs

        db = get_db()
        if db is None:
            return pd.DataFrame(), ""

        symbols = _symbol_candidates(symbol)
        freqs = _freq_candidates(freq)
        docs = list(db["bars"].find(
            {"meta.symbol": {"$in": symbols}, "meta.freq": {"$in": freqs}},
            {"_id": 0},
        ).sort("dt", 1))
        df = _bars_df_from_docs(docs, "bars")
        if not df.empty:
            return df, "bars"

        for code in symbols:
            for freq_candidate in freqs:
                docs = get_kline_docs("kline_cache", code, freq_candidate)
                df = _bars_df_from_docs(docs, "kline_cache")
                if not df.empty:
                    return df, "kline_cache"
    except Exception:
        logger.debug("kline mongo read failed", exc_info=True)
    return pd.DataFrame(), ""


def _load_bars_from_disk(symbol: Optional[str], freq: Optional[str]) -> tuple[pd.DataFrame, str]:
    if not symbol:
        return pd.DataFrame(), ""
    root = Path(__file__).resolve().parents[2]
    for code in _symbol_candidates(symbol):
        for freq_candidate in _freq_candidates(freq):
            path = root / ".data" / "cache" / f"kline_{code}_{freq_candidate}.json"
            if not path.exists():
                continue
            try:
                records = json.loads(path.read_text(encoding="utf-8"))
                df = _bars_df_from_docs(records, "disk_kline_cache")
                if not df.empty:
                    return df, "disk_kline_cache"
            except Exception:
                logger.debug("disk kline read failed: %s", path, exc_info=True)
    return pd.DataFrame(), ""


def _standardize_rank_df(df: pd.DataFrame, domain: str) -> pd.DataFrame:
    """Normalize old canonical fields into gateway fields."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    rename = {
        "板块名称": "board_name",
        "板块": "board_name",
        "name": "board_name",
        "concept": "board_name",
        "gain_pct": "change_pct",
        "涨跌幅": "change_pct",
        "领涨股票": "leader_name",
        "leading_stock": "leader_name",
        "领涨股": "leader_name",
        "领涨股票-涨跌幅": "leader_change_pct",
        "leading_gain": "leader_change_pct",
        "换手率": "turnover_pct",
        "上涨家数": "up_count",
        "下跌家数": "down_count",
    }
    out = out.rename(columns={k: v for k, v in rename.items() if k in out.columns})
    if out.columns.duplicated().any():
        for col in out.columns[out.columns.duplicated()].unique():
            dup = out.loc[:, out.columns == col]
            out[col] = dup.bfill(axis=1).iloc[:, 0]
        out = out.loc[:, ~out.columns.duplicated()]
    if "board_name" not in out.columns and domain == "concept":
        out["board_name"] = out.get("concept_name", "")
    if "source" not in out.columns:
        out["source"] = "canonical"
    for col in ["change_pct", "leader_change_pct", "turnover_pct", "up_count", "down_count"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _merge_sources(domain: str, dfs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    from signals.data.board_normalizer import merge_industry_sources

    if domain == "board":
        return merge_industry_sources(dfs)

    # Concepts use the same unified columns; a dedicated merge keeps source
    # priority suited to concepts without changing the industry helper.
    priority = ["sina", "em", "ths"]
    merged = None
    used = []
    for src in priority:
        df = dfs.get(src)
        if df is None or df.empty:
            continue
        used.append(src)
        if merged is None:
            merged = df.copy()
            continue
        for _, row in df.iterrows():
            name = row.get("board_name")
            if not name:
                continue
            mask = merged["board_name"] == name
            if mask.any():
                idx = merged.loc[mask].index[0]
                for col in df.columns:
                    if col in ("board_name", "source", "dt"):
                        continue
                    if col in merged.columns and pd.isna(merged.at[idx, col]) and pd.notna(row[col]):
                        merged.at[idx, col] = row[col]
            else:
                merged = pd.concat([merged, row.to_frame().T], ignore_index=True)
    if merged is None:
        return pd.DataFrame()
    merged["source"] = "+".join(used)
    return merged


def _read_source_snapshots(domain: str) -> tuple[pd.DataFrame, str, Optional[str]]:
    dfs: dict[str, pd.DataFrame] = {}
    source_labels = []
    dates = []
    for src, collection in SOURCE_COLLECTIONS[domain]:
        df = _latest_df(collection)
        if df is None:
            continue
        dfs[src] = _standardize_rank_df(df, domain)
        source_labels.append(collection)
        as_of = _latest_as_of(df)
        if as_of:
            dates.append(as_of)
    if not dfs:
        return pd.DataFrame(), "", None
    merged = _merge_sources(domain, dfs)
    return merged, "+".join(source_labels), max(dates) if dates else None


def _write_provider_health(provider: str, endpoint: str, domain: str,
                           ok: bool, latency_ms: float, error: str = ""):
    try:
        from signals.data.mongo_fallback import get_db

        db = get_db()
        if db is None:
            return
        now = datetime.now()
        update = {
            "provider": provider,
            "endpoint": endpoint,
            "domain": domain,
            "avg_latency_ms": latency_ms,
            "status": "ok" if ok else "degraded",
        }
        if ok:
            update["last_success_at"] = now
            update["last_error_type"] = None
        else:
            update["last_error_at"] = now
            update["last_error_type"] = error[:200]
        db.provider_health.update_one(
            {"provider": provider, "endpoint": endpoint, "domain": domain},
            {"$set": update},
            upsert=True,
        )
    except Exception:
        logger.debug("provider health write failed", exc_info=True)


def _write_data_freshness(domain: str, mode: str, collection: str,
                          latest_dt: Optional[str], stale_reason: str = ""):
    try:
        from signals.data.mongo_fallback import get_db

        db = get_db()
        if db is None:
            return
        db.data_freshness.update_one(
            {"domain": domain, "market": "A", "mode": mode, "collection": collection},
            {"$set": {
                "domain": domain,
                "market": "A",
                "mode": mode,
                "as_of": latest_dt,
                "collection": collection,
                "latest_dt": latest_dt,
                "stale_reason": stale_reason,
                "updated_at": datetime.now(),
            }},
            upsert=True,
        )
    except Exception:
        logger.debug("data freshness write failed", exc_info=True)


def _fetch_realtime_sources(domain: str) -> list[str]:
    """Compatibility stub.

    Runtime gateway reads snapshots only. External provider refresh belongs to
    signals.sync/backfill modules, so this function intentionally does nothing.
    """
    return [f"{domain}_runtime_provider_fetch_disabled"]


def _get_rank(request: DataRequest, domain: str) -> DataResponse:
    start = time.monotonic()
    mode = resolve_mode(request)
    errors: list[str] = []
    target = normalize_as_of(request.as_of)

    if mode == "historical":
        collection = CANONICAL_COLLECTIONS[domain]
        df = _latest_df(collection, {"source": "canonical"})
        if df is not None:
            df = _standardize_rank_df(df, domain)
            as_of = _latest_as_of(df)
            stale = bool(as_of and as_of < target)
            _write_data_freshness(domain, mode, collection, as_of, "older_than_request" if stale else "")
            return DataResponse(
                data=df,
                mode_used=mode,
                source=collection,
                as_of=as_of,
                freshness="stale" if stale else "fresh",
                is_stale=stale,
                latency_ms=_elapsed_ms(start),
            )

        snapshot, source, as_of = _read_source_snapshots(domain)
        if not snapshot.empty:
            stale = True
            return DataResponse(
                data=snapshot,
                mode_used=mode,
                source=source or "source_snapshot",
                as_of=as_of,
                freshness="stale",
                is_stale=stale,
                latency_ms=_elapsed_ms(start),
                errors=["canonical_miss_source_snapshot_fallback"],
            )

        return DataResponse(
            data=pd.DataFrame(),
            mode_used=mode,
            source=collection,
            as_of=None,
            freshness="empty",
            is_stale=True,
            latency_ms=_elapsed_ms(start),
            errors=["canonical_empty"],
        )

    snapshot, source, as_of = _read_source_snapshots(domain)
    if not snapshot.empty:
        target_stale = bool(as_of and as_of < target)
        _write_data_freshness(
            domain, mode, source or "source_snapshot", as_of,
            "older_than_request" if target_stale else "",
        )
        return DataResponse(
            data=snapshot,
            mode_used=mode,
            source=source or "source_snapshot",
            as_of=as_of,
            freshness="stale" if target_stale else "fresh",
            is_stale=target_stale,
            latency_ms=_elapsed_ms(start),
            errors=errors,
        )

    collection = CANONICAL_COLLECTIONS[domain]
    canonical = _latest_df(collection)
    if canonical is not None:
        return DataResponse(
            data=_standardize_rank_df(canonical, domain),
            mode_used=mode,
            source=collection,
            as_of=_latest_as_of(canonical),
            freshness="stale",
            is_stale=True,
            latency_ms=_elapsed_ms(start),
            errors=errors + ["realtime_snapshot_empty_canonical_fallback"],
        )

    return DataResponse(
        data=pd.DataFrame(),
        mode_used=mode,
        source="none",
        freshness="empty",
        is_stale=True,
        latency_ms=_elapsed_ms(start),
        errors=errors or ["realtime_snapshot_empty"],
    )


def get_board_rank(request: DataRequest) -> DataResponse:
    return _get_rank(request, "board")


def get_concept_rank(request: DataRequest) -> DataResponse:
    return _get_rank(request, "concept")


def get_board_constituents(request: DataRequest) -> DataResponse:
    start = time.monotonic()
    board = request.board_name or ""
    if not board:
        return DataResponse([], resolve_mode(request), "board_constituents",
                            freshness="empty", latency_ms=_elapsed_ms(start),
                            errors=["missing_board_name"])
    try:
        from signals.data.db_source import get_mongo_source

        mongo = get_mongo_source()
        if mongo:
            symbols = mongo.get_board_constituents(board)
            if symbols:
                return DataResponse(symbols, resolve_mode(request), "board_constituents",
                                    freshness="fresh", latency_ms=_elapsed_ms(start))
    except Exception as e:
        return DataResponse([], resolve_mode(request), "board_constituents",
                            freshness="empty", is_stale=True,
                            latency_ms=_elapsed_ms(start), errors=[str(e)])
    return DataResponse([], resolve_mode(request), "board_constituents",
                        freshness="empty", latency_ms=_elapsed_ms(start))


def get_kline(request: DataRequest, legacy_fetcher: Optional[Callable[[], pd.DataFrame]] = None) -> DataResponse:
    """Read K-line data from local cache only.

    Runtime callers may pass a legacy_fetcher during migration, but the gateway
    deliberately does not call it. External provider access belongs to
    sync/backfill modules, not request-time API paths.
    """
    start = time.monotonic()
    mode = resolve_mode(request)
    df, source = _load_bars_from_mongo(request.symbol, request.freq)
    if df.empty:
        df, source = _load_bars_from_disk(request.symbol, request.freq)
    if not df.empty:
        as_of = getattr(df, "attrs", {}).get("as_of") or str(df.index.max().date())
        target = normalize_as_of(request.as_of)
        stale = bool(mode == "historical" and as_of and as_of < target)
        _write_data_freshness("kline", mode, source, as_of, "older_than_request" if stale else "")
        return DataResponse(
            df,
            mode_used=mode,
            source=source,
            as_of=as_of,
            freshness="stale" if stale else "fresh",
            is_stale=stale,
            latency_ms=_elapsed_ms(start),
            errors=[] if legacy_fetcher is None else ["legacy_fetcher_ignored_runtime_read_only"],
        )

    return DataResponse(
        pd.DataFrame(),
        mode_used=mode,
        source="bars+kline_cache+disk_kline_cache",
        freshness="empty",
        is_stale=True,
        latency_ms=_elapsed_ms(start),
        errors=["kline_cache_empty", "runtime_provider_fetch_disabled"],
    )


def get_stock_bars(request: DataRequest) -> DataResponse:
    return get_kline(request)


def get_index_bars(request: DataRequest) -> DataResponse:
    return get_kline(request)


def get_concept_constituents(request: DataRequest) -> DataResponse:
    start = time.monotonic()
    concept = request.concept_name or request.board_name or ""
    if not concept:
        return DataResponse([], resolve_mode(request), "concept_constituents",
                            freshness="empty", latency_ms=_elapsed_ms(start),
                            errors=["missing_concept_name"])
    try:
        from signals.data.mongo_fallback import get_db

        db = get_db()
        if db is None:
            return DataResponse([], resolve_mode(request), "concept_constituents",
                                freshness="empty", is_stale=True,
                                latency_ms=_elapsed_ms(start),
                                errors=["mongo_disabled"])
        docs = list(db["concept_constituents"].find(
            {"$or": [{"concept_name": concept}, {"board_name": concept}, {"name": concept}]},
            {"_id": 0},
        ))
        if not docs:
            return DataResponse([], resolve_mode(request), "concept_constituents",
                                freshness="empty", latency_ms=_elapsed_ms(start))
        as_of = _latest_as_of(pd.DataFrame(docs))
        return DataResponse(docs, resolve_mode(request), "concept_constituents",
                            as_of=as_of, freshness="fresh",
                            latency_ms=_elapsed_ms(start))
    except Exception as e:
        return DataResponse([], resolve_mode(request), "concept_constituents",
                            freshness="empty", is_stale=True,
                            latency_ms=_elapsed_ms(start), errors=[str(e)])


def get_social_heat(request: DataRequest) -> DataResponse:
    start = time.monotonic()
    symbol = request.symbol or ""
    try:
        from signals.data.mongo_fallback import get_db

        db = get_db()
        if db is None:
            return DataResponse(None, resolve_mode(request), "social_heat",
                                freshness="empty", is_stale=True,
                                latency_ms=_elapsed_ms(start),
                                errors=["mongo_disabled"])
        query = {"symbol": symbol} if symbol else {}
        doc = db["social_heat"].find_one(query, {"_id": 0}, sort=[("dt", -1), ("updated_at", -1)])
        if not doc:
            return DataResponse(None, resolve_mode(request), "social_heat",
                                freshness="pending", is_stale=True,
                                latency_ms=_elapsed_ms(start),
                                errors=["social_heat_cache_empty"])
        as_of = str(doc.get("dt") or doc.get("updated_at") or "")[:10] or None
        return DataResponse(doc, resolve_mode(request), "social_heat",
                            as_of=as_of, freshness="fresh",
                            latency_ms=_elapsed_ms(start))
    except Exception as e:
        return DataResponse(None, resolve_mode(request), "social_heat",
                            freshness="empty", is_stale=True,
                            latency_ms=_elapsed_ms(start), errors=[str(e)])


def get_data_freshness() -> dict:
    try:
        from signals.data.mongo_fallback import get_db

        db = get_db()
        if db is None:
            return {"enabled": False, "items": []}
        return {"enabled": True, "items": list(db.data_freshness.find({}, {"_id": 0}))}
    except Exception as e:
        return {"enabled": False, "items": [], "error": str(e)}


def get_cache_contracts() -> dict:
    try:
        from signals.data.contracts import evaluate_contracts, get_cache_contracts as _contracts
        from signals.data.mongo_fallback import get_db

        db = get_db()
        if db is None:
            return {"enabled": False, "items": _contracts()}
        return {"enabled": True, "items": evaluate_contracts(db)}
    except Exception as e:
        return {"enabled": False, "items": [], "error": str(e)}


def get_provider_health() -> dict:
    try:
        from signals.data.mongo_fallback import get_db

        db = get_db()
        if db is None:
            return {"enabled": False, "items": []}
        return {"enabled": True, "items": list(db.provider_health.find({}, {"_id": 0}))}
    except Exception as e:
        return {"enabled": False, "items": [], "error": str(e)}
