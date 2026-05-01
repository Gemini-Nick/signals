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
    assert snapshot["candidates"][0]["decision_stage"] == "strategy_candidate"
    assert "hard_technical" in snapshot["candidates"][0]["missing_gates"]
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


def test_strategy_snapshot_prefers_chain_heat_as_primary_theme():
    from signals.strategy.snapshot import build_strategy_snapshot

    snapshot = build_strategy_snapshot(
        responses={
            "chain_heat": Resp([
                {
                    "chain_name": "半导体产业链",
                    "node_name": "晶圆制造",
                    "heat_score": 61.46,
                    "change_pct": 7.05,
                    "rank": 1,
                    "phase": "consensus_climax",
                    "representatives": [{"symbol": "SH.688981", "name": "中芯国际"}],
                    "integrated_domains": [{"leader_name": "灿芯股份"}],
                },
            ], "chain_heat_snapshots", as_of="2026-04-30"),
            "board": Resp([
                {"board_name": "稀土", "change_pct": 9.73, "leader_name": "北方稀土"},
            ], "board_ranking", freshness="stale", as_of="2026-04-29", is_stale=True),
            "concept": Resp([], "concept_ranking"),
            "market_pool": Resp({"symbols": ["SH.600460"]}, "market_pools"),
            "quote": Resp([], "quote_snapshots"),
            "signal": Resp([
                {
                    "symbol": "SH.600460",
                    "signal_date": "2026-04-30",
                    "signal_type": "日线候选: 活跃池趋势增强",
                    "freq": "日线",
                    "score": 77,
                    "pool_status": "candidate",
                    "source": "sync.signal_pool.generated",
                },
            ], "signals"),
        },
        journal_summary={"total": 1, "evaluated": 0, "pending": 1},
    )

    assert snapshot["daily_brief"]["primary_theme"] == "半导体产业链"
    assert snapshot["market_regime"]["primary_theme"] == "半导体产业链"
    assert snapshot["themes"][0]["domain"] == "chain_heat"
    assert snapshot["themes"][0]["representative_symbols"]


def test_strategy_snapshot_filters_backtest_and_stale_signals_from_candidates():
    from signals.strategy.snapshot import build_strategy_snapshot

    snapshot = build_strategy_snapshot(
        responses={
            "board": Resp([
                {"board_name": "半导体", "change_pct": 3.2, "leader_name": "龙头股份"},
            ], "board_ranking"),
            "concept": Resp([], "concept_ranking"),
            "market_pool": Resp({"symbols": []}, "market_pools"),
            "quote": Resp([], "quote_snapshots"),
            "signal": Resp([
                {
                    "symbol": "SZ.002812",
                    "signal_date": "2026-04-24",
                    "signal_type": "背驰买",
                    "freq": "15分钟",
                    "score": 96.7,
                    "pool_status": "candidate",
                    "source": "sqlite.backtest.signal_records",
                },
                {
                    "symbol": "SH.600487",
                    "signal_date": "2026-04-30",
                    "signal_type": "日线候选: 生成信号不进策略快照",
                    "freq": "日线",
                    "score": 99,
                    "pool_status": "candidate",
                    "source": "sync.signal_pool.generated",
                },
                {
                    "symbol": "SH.600460",
                    "signal_date": "2026-04-30",
                    "signal_type": "日线候选: 今日信号",
                    "freq": "日线",
                    "score": 77,
                    "pool_status": "candidate",
                    "source": "manual.strategy.signal",
                },
            ], "signals", as_of="2026-04-30"),
        },
        journal_summary={"total": 3, "evaluated": 0, "pending": 3},
    )

    symbols = [item["symbol"] for item in snapshot["candidates"]]
    assert symbols == ["SH.600460"]
    candidate = snapshot["candidates"][0]
    assert candidate["decision_stage"] == "strategy_candidate"
    assert "hard_technical" in candidate["missing_gates"]
    assert "优先复核" not in snapshot["daily_brief"]["summary"]
    assert "线索池观察 SH.600460" in snapshot["daily_brief"]["summary"]


def test_strategy_snapshot_uses_terminal_pool_before_generated_signal_pool():
    from signals.strategy.snapshot import build_strategy_snapshot

    snapshot = build_strategy_snapshot(
        responses={
            "chain_heat": Resp([
                {
                    "chain_name": "半导体产业链",
                    "node_name": "晶圆制造",
                    "heat_score": 59.19,
                    "change_pct": 6.53,
                    "rank": 1,
                    "phase": "consensus_climax",
                    "representatives": [{"symbol": "SH.688981", "name": "中芯国际"}],
                },
            ], "chain_heat_snapshots", as_of="2026-04-30"),
            "board": Resp([], "board_ranking"),
            "concept": Resp([], "concept_ranking"),
            "market_pool": Resp({"symbols": []}, "market_pools"),
            "quote": Resp([], "quote_snapshots"),
            "terminal_pool": Resp({
                "focus_stocks": [
                    {
                        "symbol": "SH.688981",
                        "name": "中芯国际",
                        "rank": 1,
                        "rank_score": 188.5,
                        "score": 89.1,
                        "pool_type": "focus",
                        "entry_gate_status": "entry_confirmed",
                        "latest_signal": "30分钟背驰买",
                        "signal_origin": "technical_trigger",
                        "source_collections": ["terminal_technical_signals"],
                        "technical_evidence": {"signal_type": "背驰买", "freq": "30分钟"},
                        "top_buy_reason": {"freq": "30分钟"},
                    },
                ],
                "watch_stocks": [
                    {
                        "symbol": "SH.600584",
                        "name": "长电科技",
                        "rank": 1,
                        "rank_score": 80,
                        "pool_type": "watch",
                        "entry_gate_status": "entry_waiting_right_side_confirm",
                        "blocked_by": ["5m_or_15m_missing"],
                        "latest_signal": "30分钟趋势买",
                        "technical_evidence": {"signal_type": "趋势买", "freq": "30分钟"},
                    },
                ],
                "risk_stocks": [
                    {
                        "symbol": "SH.688012",
                        "name": "中微公司",
                        "rank": 1,
                        "rank_score": 100,
                        "pool_type": "risk",
                        "entry_gate_status": "blocked_by_risk",
                        "latest_signal": "5分钟背驰卖",
                        "technical_evidence": {"signal_type": "背驰卖", "freq": "5分钟"},
                        "top_risk_reason": {"freq": "5分钟"},
                    },
                ],
            }, "terminal_stock_pool", as_of="2026-04-30"),
            "signal": Resp([
                {
                    "symbol": "SH.603906",
                    "signal_date": "2026-04-29",
                    "signal_type": "日线候选: 活跃池趋势增强",
                    "freq": "日线",
                    "score": 99,
                    "pool_status": "candidate",
                    "source": "sync.signal_pool.generated",
                },
            ], "signals", as_of="2026-04-29"),
        },
        journal_summary={"total": 1, "evaluated": 0, "pending": 1},
    )

    symbols = [item["symbol"] for item in snapshot["candidates"]]
    assert symbols[:2] == ["SH.688981", "SH.600584"]
    assert "SH.603906" not in symbols
    assert snapshot["candidates"][0]["decision_stage"] == "entry_ready"
    assert snapshot["candidates"][1]["decision_stage"] == "watch_preheat"
    assert snapshot["warnings"][0]["decision_stage"] == "risk_first"
    assert snapshot["daily_brief"]["top_candidate"] == "SH.688981"
    assert "先处理 1 个风险预警" in snapshot["daily_brief"]["summary"]
    assert "确认买点复核 SH.688981" in snapshot["daily_brief"]["summary"]
