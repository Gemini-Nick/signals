from datetime import datetime

from signals.sync.modules import postmarket_chain_rebuild as rebuild


def test_tianji_taxonomy_representative_lives_under_lipf6_not_electrolyte():
    reps = rebuild._taxonomy_representatives_by_node()

    assert "002759" not in reps.get(("lithium_battery", "electrolyte"), {})
    tianji = reps[("lithium_battery", "lipf6_lithium_salt")]["002759"]
    assert tianji["symbol"] == "SZ.002759"
    assert tianji["representative_type"] == "core"
    assert "六氟磷酸锂" in tianji["representative_relation"]


def test_tengjing_taxonomy_representative_lives_under_ocs_optical_switch():
    reps = rebuild._taxonomy_representatives_by_node()

    tengjing = reps[("optical_module", "ocs_optical_switch")]["688195"]
    assert tengjing["symbol"] == "SH.688195"
    assert tengjing["representative_type"] == "core"
    assert "OCS" in tengjing["representative_relation"]


def test_rollup_prefers_chain_role_before_heat_exposure():
    rows = [
        {
            "market": "A",
            "chain_id": "lithium_battery",
            "chain_name": "电新/锂电池产业链",
            "node_id": "electrolyte",
            "node_name": "电解液",
            "symbol": "SH.603505",
            "raw_code": "603505",
            "name": "金石资源",
            "membership_type": "core",
            "confidence": 96,
            "exposure_score": 110,
            "is_primary_chain": True,
        },
        {
            "market": "A",
            "chain_id": "lithium_battery",
            "chain_name": "电新/锂电池产业链",
            "node_id": "electrolyte",
            "node_name": "电解液",
            "symbol": "SZ.002407",
            "raw_code": "002407",
            "name": "多氟多",
            "membership_type": "theme",
            "representative_type": "elastic",
            "representative_priority": 90,
            "confidence": 96,
            "exposure_score": 110,
            "is_primary_chain": False,
        },
        {
            "market": "A",
            "chain_id": "lithium_battery",
            "chain_name": "电新/锂电池产业链",
            "node_id": "electrolyte",
            "node_name": "电解液",
            "symbol": "SZ.002709",
            "raw_code": "002709",
            "name": "天赐材料",
            "membership_type": "core",
            "representative_type": "core",
            "representative_priority": 100,
            "taxonomy_representative": True,
            "confidence": 96,
            "exposure_score": 102,
            "is_primary_chain": False,
        },
    ]

    docs = rebuild._rollup_docs(
        rows,
        trade_date="2026-05-06",
        now=datetime(2026, 5, 6, 17, 0),
        heat_by_node={},
    )
    top = docs[0]["top_securities"]

    assert [item["symbol"] for item in top[:3]] == ["SZ.002709", "SZ.002407", "SH.603505"]
    assert top[0]["representative_type"] == "core"
    assert top[0]["taxonomy_representative"] is True


def test_non_chain_market_theme_does_not_build_mapping():
    catalog = {
        "trade_date": "2026-05-11",
        "source_board_id": "em:concept:首发经济",
        "raw_name": "首发经济",
        "canonical_name": "首发经济",
        "source": "em",
        "source_collection": "concept_em",
        "kind": "concept",
        "normalization_status": "ok",
    }

    doc, match = rebuild._mapping_doc({}, catalog, trade_date="2026-05-11", now=datetime(2026, 5, 11, 17, 0))

    assert match is None
    assert doc["mapping_status"] == "non_chain"
    assert doc["confidence"] == 0
    assert "消费政策" in doc["reason"]


def test_security_concept_evidence_splits_industry_concept_and_market_theme(monkeypatch):
    def fake_constituent(db, *, kind, name):
        return {
            "_id": name,
            "symbols": ["002918"],
            "stock_names": {"002918": "蒙娜丽莎"},
            "source": "unit_test",
            "status": "ok",
        }

    monkeypatch.setattr(rebuild, "_constituent_doc", fake_constituent)
    catalog = [
        {
            "source_board_id": "em:industry:瓷砖地板",
            "canonical_name": "瓷砖地板",
            "kind": "industry",
            "source": "em",
            "as_of": "2026-05-11",
        },
        {
            "source_board_id": "ths:concept:跨境电商",
            "canonical_name": "跨境电商",
            "kind": "concept",
            "source": "ths",
            "as_of": "2026-05-11",
        },
        {
            "source_board_id": "em:concept:内贸流通",
            "canonical_name": "内贸流通",
            "kind": "concept",
            "source": "em",
            "as_of": "2026-05-11",
        },
    ]
    mappings = [
        {
            "source_board_id": "em:industry:瓷砖地板",
            "mapping_status": "mapped",
            "chain_id": "home_building_materials",
            "chain_name": "家居家居产业链",
            "node_id": "ceramic_tile_sanitary",
            "node_name": "瓷砖/卫浴/建筑陶瓷",
            "layer": "midstream",
            "stage": "中游",
            "confidence": 96,
            "mapping_specificity": 4,
        },
        {
            "source_board_id": "ths:concept:跨境电商",
            "mapping_status": "mapped",
            "chain_id": "consumer",
            "chain_name": "消费品产业链",
            "node_id": "consumer_goods",
            "node_name": "食品饮料/零售消费",
            "layer": "terminal",
            "stage": "终端",
            "confidence": 90,
            "mapping_specificity": 3,
        },
        {
            "source_board_id": "em:concept:内贸流通",
            "mapping_status": "non_chain",
            "confidence": 0,
            "reason": "流通政策/渠道主题，不对应稳定产业链。",
        },
    ]

    docs = rebuild._build_security_concept_evidence(
        {},
        trade_date="2026-05-11",
        now=datetime(2026, 5, 11, 17, 0),
        catalog=catalog,
        mapping_docs=mappings,
        security_master={},
        security_names={},
        include_market_context=False,
    )

    by_board = {doc["source_board_name"]: doc for doc in docs}
    assert by_board["瓷砖地板"]["evidence_layer"] == "stable_industry"
    assert by_board["瓷砖地板"]["primary_policy"] == "direct"
    assert by_board["瓷砖地板"]["evidence_type"] == "vendor_industry_membership"
    assert by_board["跨境电商"]["evidence_layer"] == "candidate_theme"
    assert by_board["跨境电商"]["primary_policy"] == "fallback"
    assert by_board["内贸流通"]["evidence_layer"] == "market_theme"
    assert by_board["内贸流通"]["primary_policy"] == "blocked"
    assert by_board["内贸流通"]["promotable"] is False
    assert "chain_id" not in by_board["内贸流通"]


def test_memberships_are_grouped_from_security_concept_evidence(monkeypatch):
    monkeypatch.setattr(rebuild, "_load_security_chain_overrides", lambda: [])
    evidence = [
        {
            "_id": "ev1",
            "trade_date": "2026-05-11",
            "security_id": "A:SZ:002918",
            "issuer_id": "issuer:A:蒙娜丽莎",
            "symbol": "SZ.002918",
            "raw_code": "002918",
            "name": "蒙娜丽莎",
            "source_board_id": "em:industry:瓷砖地板",
            "source_board_name": "瓷砖地板",
            "source_board_kind": "industry",
            "source": "em",
            "source_collection": "board_constituents",
            "evidence_type": "vendor_industry_membership",
            "evidence_layer": "stable_industry",
            "primary_policy": "direct",
            "chain_mapping_status": "mapped",
            "chain_id": "home_building_materials",
            "chain_name": "家居家居产业链",
            "node_id": "ceramic_tile_sanitary",
            "node_name": "瓷砖/卫浴/建筑陶瓷",
            "layer": "midstream",
            "stage": "中游",
            "confidence": 96,
            "mapping_confidence": 96,
            "chain_specificity_score": 4,
            "membership_type_candidate": "core",
            "volume_driver_score": 3.2,
            "market_context": {"source_board": {"change_pct": 1.5}},
        }
    ]

    rows = rebuild._build_memberships(
        {},
        trade_date="2026-05-11",
        now=datetime(2026, 5, 11, 17, 0),
        mappings=[],
        security_master={},
        security_names={},
        evidence_docs=evidence,
    )

    primary = next(row for row in rows if row["raw_code"] == "002918" and row["is_primary_chain"])
    assert primary["chain_id"] == "home_building_materials"
    assert primary["membership_type"] == "core"
    assert primary["evidence_layers"] == ["stable_industry"]
    assert primary["primary_policies"] == ["direct"]
    assert rebuild.SECURITY_CONCEPT_EVIDENCE_COLLECTION in primary["evidence_sources"]
    assert primary["source_boards"][0]["volume_driver_score"] == 3.2


def test_company_business_fact_evidence_is_fallback_business_context(monkeypatch):
    monkeypatch.setattr(rebuild, "_latest_business_fact_docs", lambda db: [
        {
            "_id": "A:SZ:002918",
            "security_id": "A:SZ:002918",
            "symbol": "SZ.002918",
            "raw_code": "002918",
            "name": "蒙娜丽莎",
            "industry": "家居用品",
            "business_rows": [
                {
                    "term": "建筑陶瓷制品制造",
                    "category": "按行业分类",
                    "report_date": "2025-12-31",
                    "revenue_ratio": 0.988614,
                    "gross_margin": 0.272637,
                }
            ],
            "source": "eastmoney_f10",
            "as_of": "2025-12-31",
        }
    ])

    docs = rebuild._build_company_business_fact_evidence(
        {},
        trade_date="2026-05-11",
        now=datetime(2026, 5, 11, 17, 0),
        security_master={},
        security_names={},
        quote_by_code={},
    )

    assert docs
    by_term = {doc["source_board_name"]: doc for doc in docs}
    assert by_term["家居用品"]["source_board_kind"] == "company_fact"
    assert by_term["家居用品"]["evidence_layer"] == "company_fact"
    assert by_term["家居用品"]["primary_policy"] == "fallback"
    assert by_term["家居用品"]["chain_id"] == "home_building_materials"
    assert by_term["家居用品"]["evidence_type"] == "company_business_fact"


def test_reviewed_override_beats_unrelated_high_confidence_theme(monkeypatch):
    class _Collection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "symbols": ["300209"],
                "stock_names": {"300209": "行云科技"},
                "source": "unit_test",
                "status": "ok",
            }

    class _Db(dict):
        def __missing__(self, key):
            self[key] = _Collection()
            return self[key]

    monkeypatch.setattr(rebuild, "_load_security_chain_overrides", lambda: [{
        "symbol": "SZ.300209",
        "raw_code": "300209",
        "name": "行云科技",
        "chain_id": "ai_compute",
        "chain_name": "AI算力/数据中心产业链",
        "node_id": "compute_service_operator",
        "node_name": "算力租赁/智算云服务",
        "layer": "downstream",
        "stage": "下游",
        "role": "算力租赁/云计算服务",
        "confidence": 98,
        "source_note": "人工确认算力租赁归属",
        "concept_name": "算力租赁",
        "reviewed_by": "unit_test",
        "effective_from": "2026-05-11",
    }])

    mappings = [
        (
            {"canonical_name": "内贸流通", "kind": "concept", "source_board_id": "em:concept:内贸流通", "source": "em"},
            {
                "chain_id": "consumer",
                "chain_name": "消费品产业链",
                "node_id": "consumer_goods",
                "node_name": "食品饮料/零售消费",
                "layer": "terminal",
                "stage": "终端",
                "confidence": 96,
                "mapping_specificity": 3,
            },
            {"evidence_sources": ["alias"], "chain_id": "consumer", "node_id": "consumer_goods"},
        ),
    ]

    rows = rebuild._build_memberships(
        _Db(),
        trade_date="2026-05-11",
        now=datetime(2026, 5, 11, 17, 0),
        mappings=mappings,
        security_master={
            "300209": {
                "symbol": "SZ.300209",
                "security_id": "A:SZ:300209",
                "issuer_id": "issuer:A:行云科技",
                "name": "行云科技",
            }
        },
        security_names={"300209": "行云科技"},
    )

    primary = next(row for row in rows if row["raw_code"] == "300209" and row["is_primary_chain"])
    assert primary["chain_id"] == "ai_compute"
    assert primary["node_id"] == "compute_service_operator"
    assert primary["reviewed_override"] is True


def test_primary_chain_prefers_core_industry_over_elastic_theme_representative():
    consumer_theme = {
        "chain_id": "consumer",
        "node_id": "consumer_goods",
        "membership_type": "theme",
        "representative_type": "elastic",
        "representative_priority": 74,
        "taxonomy_representative": True,
        "confidence": 96,
        "exposure_score": 102,
    }
    hotel_core = {
        "chain_id": "media_tourism",
        "node_id": "media_content",
        "membership_type": "core",
        "confidence": 96,
        "exposure_score": 112,
    }

    primary = max([consumer_theme, hotel_core], key=rebuild._primary_membership_sort_key)

    assert primary is hotel_core


def test_primary_chain_uses_hot_market_logic_over_stale_traditional_label():
    oilgas_label = {
        "chain_id": "coal_oil_gas",
        "node_id": "fossil_energy",
        "membership_type": "theme",
        "chain_specificity_score": 3,
        "confidence": 96,
        "exposure_score": 104,
        "market_driver_score": 1.2,
    }
    aidc_power_logic = {
        "chain_id": "ai_compute",
        "node_id": "data_center_power",
        "membership_type": "theme",
        "chain_specificity_score": 3,
        "confidence": 92,
        "exposure_score": 98,
        "market_driver_score": 15.0,
    }

    primary = max([oilgas_label, aidc_power_logic], key=rebuild._primary_membership_sort_key)

    assert primary is aidc_power_logic


def test_specific_industry_source_counts_as_core_membership(monkeypatch):
    class _Collection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "symbols": ["603059"],
                "stock_names": {"603059": "倍加洁"},
                "source": "unit_test",
                "status": "ok",
            }

    class _Db(dict):
        def __missing__(self, key):
            self[key] = _Collection()
            return self[key]

    monkeypatch.setattr(rebuild, "_load_security_chain_overrides", lambda: [])
    rows = rebuild._build_memberships(
        _Db(),
        trade_date="2026-05-11",
        now=datetime(2026, 5, 11, 17, 0),
        mappings=[
            (
                {"canonical_name": "商贸零售", "kind": "industry", "source_board_id": "ths:industry:商贸零售", "source": "ths"},
                {
                    "chain_id": "consumer",
                    "chain_name": "消费品产业链",
                    "node_id": "consumer_goods",
                    "node_name": "食品饮料/零售消费",
                    "layer": "terminal",
                    "stage": "终端",
                    "confidence": 60,
                    "mapping_specificity": 3,
                },
                {"evidence_sources": ["alias", "node_keyword"], "chain_id": "consumer", "node_id": "consumer_goods"},
            ),
        ],
        security_master={},
        security_names={"603059": "倍加洁"},
    )

    primary = next(row for row in rows if row["raw_code"] == "603059" and row["is_primary_chain"])
    assert primary["membership_type"] == "core"


def test_membership_type_upgrades_when_stronger_concept_evidence_arrives(monkeypatch):
    class _Collection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "symbols": ["603839"],
                "stock_names": {"603839": "安正时尚"},
                "source": "unit_test",
                "status": "ok",
            }

    class _Db(dict):
        def __missing__(self, key):
            self[key] = _Collection()
            return self[key]

    monkeypatch.setattr(rebuild, "_load_security_chain_overrides", lambda: [])
    rows = rebuild._build_memberships(
        _Db(),
        trade_date="2026-05-11",
        now=datetime(2026, 5, 11, 17, 0),
        mappings=[
            (
                {"canonical_name": "跨境电商", "kind": "concept", "source_board_id": "ths:concept:跨境电商", "source": "ths"},
                {
                    "chain_id": "consumer",
                    "chain_name": "消费品产业链",
                    "node_id": "consumer_goods",
                    "node_name": "食品饮料/零售消费",
                    "layer": "terminal",
                    "stage": "终端",
                    "confidence": 60,
                    "mapping_specificity": 3,
                },
                {"evidence_sources": ["alias", "node_keyword"], "chain_id": "consumer", "node_id": "consumer_goods"},
            ),
            (
                {"canonical_name": "电商概念", "kind": "concept", "source_board_id": "ths:concept:电商概念", "source": "ths"},
                {
                    "chain_id": "consumer",
                    "chain_name": "消费品产业链",
                    "node_id": "consumer_goods",
                    "node_name": "食品饮料/零售消费",
                    "layer": "terminal",
                    "stage": "终端",
                    "confidence": 96,
                    "mapping_specificity": 3,
                },
                {"evidence_sources": ["alias", "node_keyword"], "chain_id": "consumer", "node_id": "consumer_goods"},
            ),
        ],
        security_master={},
        security_names={"603839": "安正时尚"},
    )

    primary = next(row for row in rows if row["raw_code"] == "603839" and row["is_primary_chain"])
    assert primary["membership_type"] == "theme"
    assert primary["confidence"] == 96
