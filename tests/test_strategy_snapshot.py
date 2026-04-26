# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Resp:
    data: Any
    source: str
    freshness: str = "fresh"
    as_of: str = "2026-04-24"
    is_stale: bool = False
    errors: list[str] = field(default_factory=list)

    def to_meta(self):
        return {
            "source": self.source,
            "freshness": self.freshness,
            "as_of": self.as_of,
            "is_stale": self.is_stale,
            "errors": self.errors,
        }


def test_strategy_snapshot_builds_decision_objects():
    from signals.strategy.snapshot import build_strategy_snapshot

    snapshot = build_strategy_snapshot(
        responses={
            "board": Resp([
                {"board_name": "半导体", "change_pct": 3.2, "leader_name": "龙头股份"},
            ], "board_ranking"),
            "concept": Resp([
                {"name": "机器人", "change_pct": 2.1, "leader_name": "测试龙头"},
            ], "concept_ranking"),
            "market_pool": Resp({
                "symbols": ["SZ.002759"],
                "items": [{"symbol": "SZ.002759", "sources": ["signals"]}],
            }, "market_pools"),
            "quote": Resp([
                {"symbol": "SZ.002759", "price": 12.3, "name": "测试股份"},
            ], "quote_snapshots"),
            "signal": Resp([
                {
                    "symbol": "SZ.002759",
                    "signal_date": "2026-04-24",
                    "signal_type": "日线候选: 活跃池趋势增强",
                    "freq": "日线",
                    "score": 78,
                    "confidence": 0.72,
                    "price": 12.2,
                    "pool_status": "candidate",
                },
                {
                    "symbol": "SH.600519",
                    "signal_date": "2026-04-24",
                    "signal_type": "日线预警: 跌破二十日均线",
                    "freq": "日线",
                    "score": 35,
                    "confidence": 0.55,
                    "pool_status": "warning",
                },
            ], "signals"),
        },
        journal_summary={"total": 2, "evaluated": 1, "pending": 1},
        previous_snapshot={
            "themes": [{"name": "旧主线"}],
            "candidates": [{"symbol": "SZ.000001"}],
            "warnings": [],
        },
    )

    assert snapshot["daily_brief"]["primary_theme"] == "半导体"
    assert snapshot["daily_brief"]["changed_since_last"]["new_themes"] == ["半导体", "机器人"]
    assert snapshot["candidates"][0]["symbol"] == "SZ.002759"
    assert snapshot["candidates"][0]["metadata"]["evidence"]["strategy_thesis"]
    assert snapshot["warnings"][0]["symbol"] == "SH.600519"
    assert snapshot["decision_queue"]
    assert snapshot["strategy_kpis"]["signals_total"] == 2
    assert snapshot["source_confidence"]["overall"] > 0
    assert snapshot["data_lineage"]["canonical_store"] == "mongodb.strategy_snapshots"


def test_strategy_snapshot_degrades_without_data():
    from signals.strategy.snapshot import build_strategy_snapshot

    snapshot = build_strategy_snapshot(
        responses={
            "board": Resp([], "board_ranking", freshness="empty", errors=["empty"]),
            "concept": Resp([], "concept_ranking", freshness="empty", errors=["empty"]),
            "market_pool": Resp(None, "market_pools", freshness="empty", errors=["empty"]),
            "quote": Resp([], "quote_snapshots", freshness="empty", errors=["empty"]),
            "signal": Resp([], "signals", freshness="empty", errors=["empty"]),
        },
        journal_summary={"total": 0, "evaluated": 0, "pending": 0},
    )

    assert snapshot["candidates"] == []
    assert snapshot["warnings"] == []
    assert snapshot["daily_brief"]["changed_since_last"]["new_candidates"] == []
    assert snapshot["source_confidence"]["overall"] < 0.5
