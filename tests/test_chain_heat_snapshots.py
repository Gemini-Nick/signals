# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.sync.modules.chain_heat import _aggregate, _filter_mapping_matches


def test_chain_heat_drops_ambiguous_industry_only_matches():
    matches = [
        {"chain_id": "optical_module", "score": 76, "evidence_sources": ["industry"]},
        {"chain_id": "semiconductor", "score": 76, "evidence_sources": ["industry"]},
        {"chain_id": "pcb_ccl", "score": 76, "evidence_sources": ["industry"]},
    ]

    filtered, reason = _filter_mapping_matches(matches)

    assert filtered == []
    assert reason == "ambiguous_industry_only"


def test_chain_heat_prefers_specific_evidence_over_industry_fallback():
    matches = [
        {"chain_id": "photovoltaic", "score": 64, "evidence_sources": ["alias", "node_keyword"]},
        {"chain_id": "lithium_battery", "score": 60, "evidence_sources": ["industry", "node_keyword"]},
    ]

    filtered, reason = _filter_mapping_matches(matches)

    assert [row["chain_id"] for row in filtered] == ["photovoltaic"]
    assert reason == "specific_only"


def test_chain_heat_keeps_single_chain_industry_only_match():
    matches = [
        {"chain_id": "medicine", "score": 76, "evidence_sources": ["industry"]},
    ]

    filtered, reason = _filter_mapping_matches(matches)

    assert filtered == matches
    assert reason == "industry_only"


def test_postmarket_chain_rebuild_ai_can_adjudicate_rule_candidates(monkeypatch):
    from signals.sync.modules import postmarket_chain_rebuild

    matches = [
        {"chain_id": "optical_module", "node_id": "optical_module_core", "score": 76, "confidence": 76, "evidence_sources": ["industry"], "hit_terms": ["元件"]},
        {"chain_id": "semiconductor", "node_id": "wafer_foundry", "score": 76, "confidence": 76, "evidence_sources": ["industry"], "hit_terms": ["元件"]},
    ]
    monkeypatch.setattr(postmarket_chain_rebuild, "decide_chain_mapping", lambda db, source, matches, now: {
        "status": "mapped",
        "decisions": [{
            "candidate_id": "optical_module:optical_module_core",
            "confidence": 91,
            "reason": "元件板块今日由光通信链带动",
            "matched_terms": ["光通信"],
        }],
    })

    filtered, reason = postmarket_chain_rebuild._resolve_mapping_matches(
        None,
        {"kind": "industry", "name": "元件"},
        matches,
        now=datetime(2026, 5, 7, 15, 0),
    )

    assert reason == "ai_mapped"
    assert [row["chain_id"] for row in filtered] == ["optical_module"]
    assert filtered[0]["ai_confidence"] == 91
    assert "ai_semantic_mapper" in filtered[0]["evidence_sources"]


def test_postmarket_chain_rebuild_skips_ai_for_specific_rule_match(monkeypatch):
    from signals.sync.modules import postmarket_chain_rebuild

    matches = [
        {
            "chain_id": "copper_interconnect",
            "node_id": "copper_connector_core",
            "score": 92,
            "confidence": 92,
            "evidence_sources": ["alias", "node_keyword"],
            "hit_terms": ["铜缆高速连接"],
        },
    ]

    def fail_decision(*args, **kwargs):
        raise AssertionError("AI should not run for a specific one-candidate rule match")

    monkeypatch.setattr(postmarket_chain_rebuild, "decide_chain_mapping", fail_decision)

    filtered, reason = postmarket_chain_rebuild._resolve_mapping_matches(
        None,
        {"kind": "concept", "name": "铜缆高速连接"},
        matches,
        now=datetime(2026, 5, 7, 15, 0),
    )

    assert reason == "specific_only"
    assert filtered == matches


def test_ai_mapping_parser_accepts_json_fences():
    from signals.core.chain_ai_mapping import _parse_json_content

    parsed = _parse_json_content("""```json\n{"status":"ambiguous","decisions":[],"reason":"宽行业"}\n```""")

    assert parsed["status"] == "ambiguous"


def test_chain_heat_aggregate_builds_realtime_node_fields():
    latest = datetime(2026, 4, 28, 10, 30)
    rows = [{
        "kind": "industry",
        "name": "半导体",
        "source": "eastmoney_push2delay",
        "rank": 1,
        "change_pct": 2.4,
        "up_count": 44,
        "down_count": 6,
        "leader_name": "测试龙头",
        "leader_change_pct": 8.8,
        "trade_minute": latest,
        "heat_score": 61.5,
        "momentum_5m": 0.4,
        "momentum_15m": 0.8,
        "momentum_30m": 1.2,
        "chain_id": "semiconductor",
        "chain_name": "半导体产业链",
        "node_id": "wafer_foundry",
        "node_name": "晶圆制造",
        "layer": "midstream",
        "stage": "",
        "mapping_confidence": 92,
        "representatives": [
            {"symbol": "SH.688981", "name": "中芯国际", "representative_type": "core", "priority": 100},
            {"symbol": "SZ.002371", "name": "北方华创", "representative_type": "elastic", "priority": 90},
        ],
    }]

    snapshots = _aggregate(rows, latest)

    assert len(snapshots) == 1
    node = snapshots[0]
    assert node["chain_id"] == "semiconductor"
    assert node["node_id"] == "wafer_foundry"
    assert node["phase"] == "accelerating"
    assert node["trading_signal"] == "chain_acceleration"
    assert node["heat_source"] == "eastmoney_push2delay"
    assert node["taxonomy_source"] == "industry_chains.yaml"
    assert node["trade_date"] == "2026-04-28"
    assert node["momentum_5m"] == 0.4
    assert node["representatives"][0]["symbol"] == "SH.688981"


def test_chain_heat_marks_consensus_climax_as_risk_context():
    latest = datetime(2026, 4, 28, 10, 30)
    rows = [{
        "kind": "industry",
        "name": "半导体",
        "source": "eastmoney_push2delay",
        "rank": 1,
        "change_pct": 3.6,
        "up_count": 90,
        "down_count": 8,
        "leader_name": "测试龙头",
        "leader_change_pct": 9.8,
        "trade_minute": latest,
        "heat_score": 88,
        "momentum_5m": 0.05,
        "momentum_15m": 1.1,
        "momentum_30m": 1.8,
        "chain_id": "semiconductor",
        "chain_name": "半导体产业链",
        "node_id": "wafer_foundry",
        "node_name": "晶圆制造",
        "mapping_confidence": 92,
        "representatives": [],
    }]

    snapshots = _aggregate(rows, latest)

    assert snapshots[0]["phase"] == "consensus_climax"
    assert snapshots[0]["trading_signal"] == "chain_consensus_climax"


def test_chain_heat_recent_jump_without_extension_is_not_climax():
    latest = datetime(2026, 5, 6, 14, 30)
    rows = [{
        "kind": "industry",
        "name": "贵金属",
        "source": "eastmoney_push2delay",
        "rank": 1,
        "change_pct": 5.56,
        "up_count": 90,
        "down_count": 8,
        "leader_name": "测试龙头",
        "leader_change_pct": 9.8,
        "trade_minute": latest,
        "heat_score": 88,
        "momentum_5m": 0.02,
        "momentum_15m": -0.03,
        "momentum_30m": 0.08,
        "chain_id": "nonferrous",
        "chain_name": "有色金属产业链",
        "node_id": "precious_metals",
        "node_name": "贵金属",
        "mapping_confidence": 92,
        "representatives": [],
    }]

    snapshots = _aggregate(rows, latest)

    assert snapshots[0]["phase"] == "warming"
    assert snapshots[0]["trading_signal"] == "chain_warming"
