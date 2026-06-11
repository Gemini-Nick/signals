# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.sync.modules.chain_heat import (
    _aggregate,
    _filter_mapping_matches,
    _source_concept_overlays,
    _source_constituent_representatives,
)


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
    assert node["source_driver"]["kind"] == "industry"
    assert node["source_driver"]["name"] == "半导体"
    assert node["source_driver"]["change_pct"] == 2.4
    assert node["route_explain"] == "源[行业] 半导体 -> 半导体产业链/晶圆制造"
    assert node["representatives"][0]["symbol"] == "SH.688981"


def test_chain_heat_prefers_real_rising_source_constituents_over_static_representatives():
    latest = datetime(2026, 6, 10, 10, 30)
    rows = [{
        "kind": "concept",
        "name": "燃气轮机",
        "source": "eastmoney_push2delay",
        "rank": 1,
        "change_pct": 4.2,
        "up_count": 12,
        "down_count": 2,
        "leader_name": "杰瑞股份",
        "leader_change_pct": 9.7,
        "trade_minute": latest,
        "heat_score": 52.0,
        "momentum_5m": 0.4,
        "momentum_15m": 0.8,
        "momentum_30m": 1.1,
        "chain_id": "ai_compute",
        "chain_name": "AI算力/数据中心产业链",
        "node_id": "data_center_power",
        "node_name": "AIDC电源/配套供能",
        "mapping_confidence": 96,
        "source_representatives": [
            {
                "symbol": "SZ.002353",
                "name": "杰瑞股份",
                "representative_type": "source_leader",
                "priority": 280,
                "day_change_pct": 9.7,
                "source_board_name": "燃气轮机",
            }
        ],
        "representatives": [
            {"symbol": "SZ.002335", "name": "科华数据", "representative_type": "core", "priority": 100},
        ],
        "source_concept_overlays": [
            {
                "kind": "concept",
                "name": "AIDC电源",
                "change_pct": 6.6,
                "leader_name": "杰瑞股份",
                "matched_symbols": ["002353"],
                "matched_names": ["杰瑞股份"],
                "source_boards": ["燃气轮机"],
                "matched_count": 1,
            }
        ],
    }]

    snapshots = _aggregate(rows, latest)
    node = snapshots[0]

    assert node["representatives"][0]["symbol"] == "SZ.002353"
    assert node["representatives"][0]["representative_type"] == "source_leader"
    assert node["representatives"][1]["symbol"] == "SZ.002335"
    assert node["source_concept_overlays"][0]["name"] == "AIDC电源"
    assert node["market_logic"]["status"] == "hot_concept_overlay"
    assert node["market_logic"]["logic_name"] == "AIDC电源"
    assert node["market_logic"]["matched_names"] == ["杰瑞股份"]


def test_chain_heat_promotes_semiconductor_gas_logic_from_hot_overlays():
    latest = datetime(2026, 6, 10, 10, 30)
    rows = [{
        "kind": "industry",
        "name": "半导体材料",
        "source": "eastmoney_push2delay",
        "rank": 1,
        "change_pct": 4.2,
        "up_count": 28,
        "down_count": 4,
        "leader_name": "中晶科技",
        "leader_change_pct": 10.0,
        "trade_minute": latest,
        "heat_score": 58.0,
        "momentum_5m": 0.4,
        "momentum_15m": 0.8,
        "momentum_30m": 1.1,
        "chain_id": "semiconductor",
        "chain_name": "半导体产业链",
        "node_id": "material_photoresist",
        "node_name": "材料/光刻胶",
        "mapping_confidence": 96,
        "source_representatives": [
            {
                "symbol": "SH.688146",
                "name": "中船特气",
                "representative_type": "industry_constituent",
                "priority": 300,
                "day_change_pct": 16.0,
                "source_board_name": "半导体材料",
            }
        ],
        "representatives": [
            {"symbol": "SZ.300236", "name": "上海新阳", "representative_type": "core", "priority": 100},
        ],
        "source_concept_overlays": [
            {
                "kind": "concept",
                "name": "氟化工概念",
                "change_pct": 1.6,
                "leader_name": "昊华科技",
                "matched_symbols": ["688146", "002409"],
                "matched_names": ["中船特气", "雅克科技"],
                "source_boards": ["半导体材料"],
                "matched_count": 2,
            },
            {
                "kind": "concept",
                "name": "工业气体",
                "change_pct": 2.8,
                "leader_name": "和远气体",
                "matched_symbols": ["688268", "300346"],
                "matched_names": ["华特气体", "南大光电"],
                "source_boards": ["电子化学品Ⅲ"],
                "matched_count": 2,
            },
        ],
    }]

    node = _aggregate(rows, latest)[0]

    assert node["node_id"] == "material_photoresist"
    assert node["market_logic_node"]["node_id"] == "specialty_gas_precursor"
    assert node["market_logic_node"]["node_name"] == "半导体特气/前驱体"
    assert node["market_logic_node"]["taxonomy_node_id"] == "material_photoresist"
    assert node["market_logic"]["route_node_name"] == "半导体特气/前驱体"
    assert node["market_logic"]["route_status"] == "hot_overlay_constituent_route"


def test_chain_heat_does_not_route_pvdf_to_lipf6_from_one_shared_constituent():
    latest = datetime(2026, 6, 10, 10, 30)
    rows = [{
        "kind": "concept",
        "name": "PVDF概念",
        "source": "eastmoney_push2delay",
        "rank": 180,
        "change_pct": 1.2,
        "up_count": 9,
        "down_count": 7,
        "leader_name": "昊华科技",
        "leader_change_pct": 10.0,
        "trade_minute": latest,
        "heat_score": 18.0,
        "momentum_5m": 0.1,
        "momentum_15m": 0.2,
        "momentum_30m": 0.3,
        "chain_id": "lithium_battery",
        "chain_name": "电新/锂电池产业链",
        "node_id": "pvdf_binder",
        "node_name": "PVDF/锂电氟材料",
        "mapping_confidence": 96,
        "source_representatives": [
            {
                "symbol": "SH.600378",
                "name": "昊华科技",
                "representative_type": "source_leader",
                "priority": 320,
                "day_change_pct": 10.0,
                "source_board_name": "PVDF概念",
            },
            {
                "symbol": "SZ.002407",
                "name": "多氟多",
                "representative_type": "concept_constituent",
                "priority": 260,
                "day_change_pct": 10.0,
                "source_board_name": "PVDF概念",
            },
        ],
        "representatives": [
            {"symbol": "SZ.002407", "name": "多氟多", "representative_type": "core", "priority": 94},
            {"symbol": "SZ.300343", "name": "ST联创", "representative_type": "elastic", "priority": 82},
        ],
        "source_concept_overlays": [
            {
                "kind": "concept",
                "name": "工业气体",
                "change_pct": 4.8,
                "leader_name": "和远气体",
                "matched_symbols": ["600378", "600160"],
                "matched_names": ["昊华科技", "巨化股份"],
                "source_boards": ["PVDF概念"],
                "matched_count": 2,
                "matched_source_leader": True,
            },
            {
                "kind": "concept",
                "name": "氟化工概念",
                "change_pct": 3.6,
                "leader_name": "兴发集团",
                "matched_symbols": ["002407", "300343", "600673", "600378"],
                "matched_names": ["多氟多", "ST联创", "东阳光", "昊华科技"],
                "source_boards": ["PVDF概念"],
                "matched_count": 4,
                "matched_source_leader": True,
            },
        ],
    }]

    node = _aggregate(rows, latest)[0]

    assert node["node_id"] == "pvdf_binder"
    assert node["market_logic_node"] == {}
    assert "route_node_id" not in node["market_logic"]


def test_chain_heat_market_logic_stays_anchored_to_top_source_board():
    latest = datetime(2026, 6, 10, 10, 30)
    rows = [
        {
            "kind": "industry",
            "name": "保险Ⅲ",
            "rank": 1,
            "change_pct": 3.1,
            "up_count": 5,
            "down_count": 0,
            "leader_name": "中国人寿",
            "trade_minute": latest,
            "heat_score": 42.0,
            "momentum_5m": 0.1,
            "momentum_15m": 0.2,
            "momentum_30m": 0.3,
            "chain_id": "finance",
            "chain_name": "大金融产业链",
            "node_id": "finance_core",
            "node_name": "银行/券商/保险",
            "mapping_confidence": 96,
            "representatives": [],
            "source_concept_overlays": [
                {
                    "kind": "concept",
                    "name": "参股期货",
                    "change_pct": 0.2,
                    "leader_name": "中化国际",
                    "matched_names": ["中国人寿"],
                    "source_boards": ["保险Ⅲ"],
                    "matched_count": 1,
                    "matched_source_leader": True,
                }
            ],
        },
        {
            "kind": "concept",
            "name": "参股期货",
            "rank": 90,
            "change_pct": 0.1,
            "up_count": 20,
            "down_count": 18,
            "leader_name": "中化国际",
            "trade_minute": latest,
            "heat_score": 12.0,
            "momentum_5m": 0.1,
            "momentum_15m": 0.2,
            "momentum_30m": 0.3,
            "chain_id": "finance",
            "chain_name": "大金融产业链",
            "node_id": "finance_core",
            "node_name": "银行/券商/保险",
            "mapping_confidence": 70,
            "representatives": [],
            "source_concept_overlays": [
                {
                    "kind": "concept",
                    "name": "环氧丙烷",
                    "change_pct": 3.9,
                    "leader_name": "中化国际",
                    "matched_names": ["中化国际"],
                    "source_boards": ["参股期货"],
                    "matched_count": 1,
                    "matched_source_leader": True,
                }
            ],
        },
    ]

    node = _aggregate(rows, latest)[0]

    assert node["market_logic"]["status"] == "source_board"
    assert node["market_logic"]["logic_name"] == "保险Ⅲ"
    assert {item["name"] for item in node["source_concept_overlays"]} == {"参股期货", "环氧丙烷"}


def test_source_concept_overlays_find_actual_hot_logic_from_shared_constituents():
    class _Cursor(list):
        pass

    class _ConstituentCollection:
        def __init__(self, docs):
            self.docs = docs

        def find_one(self, query=None, projection=None, sort=None):
            names = {
                clause.get("_id") or clause.get("board_name") or clause.get("concept_name") or clause.get("name")
                for clause in (query or {}).get("$or", [])
            }
            for doc in self.docs:
                if doc.get("board_name") in names or doc.get("concept_name") in names or doc.get("_id") in names:
                    return doc
            return {}

        def find(self, query=None, projection=None):
            symbols = set(((query or {}).get("symbols") or {}).get("$in") or [])
            return _Cursor([
                doc for doc in self.docs
                if symbols.intersection(set(doc.get("symbols") or []))
            ])

    class _HeatCollection:
        def __init__(self, docs):
            self.docs = docs

        def find_one(self, query=None, projection=None, sort=None):
            for doc in self.docs:
                if doc.get("kind") == (query or {}).get("kind") and doc.get("name") == (query or {}).get("name"):
                    return doc
            return {}

    db = {
        "concept_constituents": _ConstituentCollection([
            {"concept_name": "燃气轮机", "symbols": ["SZ.002353"], "stock_names": {"002353": "杰瑞股份", "SZ.002353": "杰瑞股份"}},
            {"concept_name": "AIDC电源", "symbols": ["SZ.002353"], "stock_names": {"002353": "杰瑞股份", "SZ.002353": "杰瑞股份"}},
        ]),
        "board_constituents": _ConstituentCollection([]),
        "board_heat_ticks": _HeatCollection([
            {"kind": "concept", "name": "AIDC电源", "change_pct": 6.6, "leader_name": "杰瑞股份", "leader_change_pct": 9.7},
        ]),
    }

    overlays = _source_concept_overlays(db, [{"kind": "concept", "name": "燃气轮机"}])

    assert overlays[0]["name"] == "AIDC电源"
    assert overlays[0]["matched_names"] == ["杰瑞股份"]
    assert overlays[0]["source_boards"] == ["燃气轮机"]


def test_source_concept_overlays_prefer_source_leader_match_over_weak_single_overlap():
    class _Cursor(list):
        pass

    class _ConstituentCollection:
        def __init__(self, docs):
            self.docs = docs

        def find_one(self, query=None, projection=None, sort=None):
            names = {
                clause.get("_id") or clause.get("board_name") or clause.get("concept_name") or clause.get("name")
                for clause in (query or {}).get("$or", [])
            }
            for doc in self.docs:
                if doc.get("board_name") in names or doc.get("concept_name") in names or doc.get("_id") in names:
                    return doc
            return {}

        def find(self, query=None, projection=None):
            symbols = set(((query or {}).get("symbols") or {}).get("$in") or [])
            return _Cursor([
                doc for doc in self.docs
                if symbols.intersection(set(doc.get("symbols") or []))
            ])

    class _HeatCollection:
        def __init__(self, docs):
            self.docs = docs

        def find_one(self, query=None, projection=None, sort=None):
            for doc in self.docs:
                if doc.get("kind") == (query or {}).get("kind") and doc.get("name") == (query or {}).get("name"):
                    return doc
            return {}

    db = {
        "concept_constituents": _ConstituentCollection([
            {
                "concept_name": "PVDF概念",
                "symbols": ["SH.600378", "SH.605020", "SZ.002407"],
                "stock_names": {"600378": "昊华科技", "605020": "永和股份", "002407": "多氟多"},
            },
            {
                "concept_name": "环氧丙烷",
                "symbols": ["SH.605020"],
                "stock_names": {"605020": "永和股份"},
            },
            {
                "concept_name": "工业气体",
                "symbols": ["SH.600378", "SH.600160"],
                "stock_names": {"600378": "昊华科技", "600160": "巨化股份"},
            },
        ]),
        "board_constituents": _ConstituentCollection([]),
        "board_heat_ticks": _HeatCollection([
            {"kind": "concept", "name": "环氧丙烷", "change_pct": 3.9, "leader_name": "中化国际", "leader_change_pct": 10.0},
            {"kind": "concept", "name": "工业气体", "change_pct": 2.8, "leader_name": "和远气体", "leader_change_pct": 10.0},
        ]),
    }

    overlays = _source_concept_overlays(
        db,
        [{"kind": "concept", "name": "PVDF概念", "leader_name": "昊华科技"}],
    )

    assert overlays[0]["name"] == "工业气体"
    assert overlays[0]["matched_source_leader"] is True
    assert overlays[0]["matched_source_leaders"] == ["昊华科技"]
    assert overlays[1]["name"] == "环氧丙烷"


def test_source_constituent_representatives_use_actual_positive_quotes():
    class _ConstituentCollection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "concept_name": "PVDF概念",
                "symbols": ["SZ.002407", "SZ.300343"],
                "stock_names": {"002407": "多氟多", "300343": "ST联创"},
            }

    db = {"concept_constituents": _ConstituentCollection()}
    quote_by_code = {
        "002407": {"symbol": "SZ.002407", "name": "多氟多", "change_pct": 2.9, "amount": 120000000},
        "300343": {"symbol": "SZ.300343", "name": "ST联创", "change_pct": -1.2, "amount": 30000000},
    }

    reps = _source_constituent_representatives(
        db,
        {"kind": "concept", "name": "PVDF概念", "change_pct": 0.2, "leader_name": "多氟多", "leader_change_pct": 2.9},
        quote_by_code,
    )

    assert [row["symbol"] for row in reps] == ["SZ.002407"]
    assert reps[0]["representative_type"] == "source_leader"
    assert reps[0]["day_change_pct"] == 2.9


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
