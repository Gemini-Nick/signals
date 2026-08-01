# -*- coding: utf-8 -*-
"""Runtime data contracts for Signals cache preheating.

The contract registry is intentionally declarative. Runtime consumers should
read through the gateway; sync/backfill code can use these contracts to decide
which Mongo collections must be warm before the UI and agents are considered
ready.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


ContractStatus = Literal["ready", "stale", "missing", "unknown"]


@dataclass(frozen=True)
class DataContract:
    consumer: str
    domain: str
    mode: str
    market: str
    freq: str
    symbols_selector: str
    required_freshness: str
    stale_policy: str
    sync_job: str
    fallback_collections: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["fallback_collections"] = list(self.fallback_collections)
        return data


CONTRACTS: tuple[DataContract, ...] = (
    DataContract(
        consumer="web.dashboard",
        domain="board",
        mode="historical",
        market="A",
        freq="daily",
        symbols_selector="all_boards",
        required_freshness="last_close",
        stale_policy="show_stale_badge",
        sync_job="board_ranking",
        fallback_collections=("board_ranking", "board_em", "board_ths", "board_sina"),
    ),
    DataContract(
        consumer="web.dashboard",
        domain="concept",
        mode="historical",
        market="A",
        freq="daily",
        symbols_selector="all_concepts",
        required_freshness="last_close",
        stale_policy="show_stale_badge",
        sync_job="board_ranking",
        fallback_collections=("concept_ranking", "concept_sina", "concept_em", "concept_ths"),
    ),
    DataContract(
        consumer="signals.backtest",
        domain="kline",
        mode="historical",
        market="A",
        freq="daily",
        symbols_selector="requested_symbol",
        required_freshness="available_history",
        stale_policy="fail_with_source_meta",
        sync_job="stock_daily",
        fallback_collections=("bars", "kline_cache"),
    ),
    DataContract(
        consumer="signals.chart",
        domain="kline",
        mode="historical",
        market="A",
        freq="daily",
        symbols_selector="requested_symbol",
        required_freshness="available_history",
        stale_policy="show_stale_badge",
        sync_job="stock_daily",
        fallback_collections=("bars", "kline_cache"),
    ),
    DataContract(
        consumer="run.cluster",
        domain="board",
        mode="realtime",
        market="A",
        freq="quote",
        symbols_selector="all_boards",
        required_freshness="intraday_snapshot",
        stale_policy="canonical_fallback",
        sync_job="board_ranking",
        fallback_collections=("board_em", "board_ths", "board_sina", "board_ranking"),
    ),
    DataContract(
        consumer="run.cluster",
        domain="concept",
        mode="realtime",
        market="A",
        freq="quote",
        symbols_selector="all_concepts",
        required_freshness="intraday_snapshot",
        stale_policy="canonical_fallback",
        sync_job="board_ranking",
        fallback_collections=("concept_sina", "concept_em", "concept_ths", "concept_ranking"),
    ),
    DataContract(
        consumer="run.review",
        domain="kline",
        mode="historical",
        market="A",
        freq="daily",
        symbols_selector="active_pool",
        required_freshness="last_close",
        stale_policy="partial_ok",
        sync_job="stock_daily",
        fallback_collections=("bars", "kline_cache"),
    ),
    DataContract(
        consumer="L1.index",
        domain="index",
        mode="historical",
        market="all",
        freq="daily",
        symbols_selector="core_indices",
        required_freshness="last_close",
        stale_policy="partial_ok",
        sync_job="index_daily",
        fallback_collections=("index_bars", "bars"),
    ),
    DataContract(
        consumer="signals.index_bars",
        domain="index",
        mode="historical",
        market="all",
        freq="daily",
        symbols_selector="core_indices",
        required_freshness="last_close",
        stale_policy="partial_ok",
        sync_job="index_daily",
        fallback_collections=("index_bars", "bars"),
    ),
    DataContract(
        consumer="signals.market_pool",
        domain="market_pool",
        mode="realtime",
        market="A",
        freq="quote",
        symbols_selector="active_pool",
        required_freshness="same_day",
        stale_policy="pending_ok",
        sync_job="market_pools",
        fallback_collections=("market_pools",),
    ),
    DataContract(
        consumer="signals.quote_snapshot",
        domain="quote",
        mode="realtime",
        market="A",
        freq="quote",
        symbols_selector="active_pool",
        required_freshness="intraday_snapshot",
        stale_policy="stale_badge",
        sync_job="quote_snapshots",
        fallback_collections=("quote_snapshots",),
    ),
    DataContract(
        consumer="signals.signal_pool",
        domain="signal",
        mode="historical",
        market="A",
        freq="daily",
        symbols_selector="active_pool",
        required_freshness="persistent_pool",
        stale_policy="show_empty_state",
        sync_job="signal_pool",
        fallback_collections=("signals",),
    ),
    DataContract(
        consumer="L2.industry",
        domain="constituents",
        mode="historical",
        market="A",
        freq="daily",
        symbols_selector="all_boards",
        required_freshness="weekly",
        stale_policy="stale_ok",
        sync_job="board_cons",
        fallback_collections=("board_constituents", "market_pools"),
    ),
    DataContract(
        consumer="social.theme",
        domain="social",
        mode="historical",
        market="A",
        freq="daily",
        symbols_selector="hot_symbols",
        required_freshness="same_day_or_recent",
        stale_policy="pending_ok",
        sync_job="social_preheat",
        fallback_collections=("social_heat", "social_comment", "social_weibo", "concept_constituents"),
    ),
    DataContract(
        consumer="WeClaw.intents",
        domain="board",
        mode="auto",
        market="A",
        freq="daily",
        symbols_selector="intent_scope",
        required_freshness="purpose_dependent",
        stale_policy="include_source_meta",
        sync_job="board_ranking",
        fallback_collections=("board_ranking", "board_em", "board_ths", "board_sina"),
    ),
)


def get_cache_contracts() -> list[dict]:
    return [contract.to_dict() for contract in CONTRACTS]


def evaluate_contracts(db) -> list[dict]:
    """Attach coarse collection-level readiness to each contract."""
    def collection_count(collection: str) -> int:
        # ``bars``/``index_bars`` are time-series collections.  Their
        # estimated count can degrade into a bucket COLLSCAN, which is far too
        # expensive for a health/contract read.  The freshness ledger is the
        # canonical count watermark maintained by the sync engine.
        try:
            positive_counts = [
                int(doc.get("count") or 0)
                for doc in db["data_freshness"].find(
                    {"collection": collection},
                    {"count": 1},
                )
                if int(doc.get("count") or 0) > 0
            ]
            if positive_counts:
                return max(positive_counts)
        except Exception:
            pass
        try:
            freshness = db["data_freshness"].find_one(
                {"collection": collection, "count": {"$gt": 0}},
                {"count": 1},
                sort=[("updated_at", -1), ("latest_dt", -1)],
            ) or {}
            if "count" in freshness:
                return max(0, int(freshness.get("count") or 0))
        except Exception:
            pass
        if collection in {"bars", "index_bars"}:
            # A missing watermark is unknown, not a reason to scan tens of
            # millions of buckets on a user-facing endpoint.
            return 1
        try:
            try:
                return int(db[collection].estimated_document_count(maxTimeMS=250))
            except TypeError:
                return int(db[collection].estimated_document_count())
        except Exception:
            return 0

    items = []
    for contract in CONTRACTS:
        status: ContractStatus = "missing"
        latest_dt = None
        matched_collection = None
        count = 0
        for collection in contract.fallback_collections:
            try:
                col = db[collection]
                count = collection_count(collection)
                if count <= 0:
                    continue
                if collection in {"bars", "index_bars"}:
                    latest = db["data_freshness"].find_one(
                        {"collection": collection},
                        {"updated_at": 1, "as_of": 1, "latest_dt": 1},
                        sort=[("updated_at", -1), ("latest_dt", -1)],
                    ) or {}
                else:
                    latest = col.find_one(
                        {},
                        {"dt": 1, "updated_at": 1, "as_of": 1, "latest_dt": 1},
                        sort=[("dt", -1), ("updated_at", -1)],
                    ) or {}
                status = "ready"
                latest_dt = str(
                    latest.get("dt")
                    or latest.get("latest_dt")
                    or latest.get("as_of")
                    or latest.get("updated_at")
                    or ""
                )
                matched_collection = collection
                count = collection_count
                break
            except Exception:
                status = "unknown"
        data = contract.to_dict()
        data.update({
            "status": status,
            "collection": matched_collection,
            "count": count,
            "latest_dt": latest_dt,
        })
        items.append(data)
    return items
