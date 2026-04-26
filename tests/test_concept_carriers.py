from signals.core.concept_carriers import (
    build_mapping_coverage,
    industry_hints_for_concept,
    preferred_concept_carriers,
)


def _top_symbol(concept: str) -> str:
    rows = preferred_concept_carriers(concept)
    assert rows
    return rows[0]["symbol"]


def test_lithium_resource_uses_upstream_carrier():
    assert _top_symbol("锂") == "SZ.002466"
    assert "能源金属" in industry_hints_for_concept("锂")


def test_segment_concepts_prefer_matching_chain_stage():
    assert _top_symbol("电解液") == "SZ.002709"
    electrolyte = preferred_concept_carriers("电解液")
    assert [row["symbol"] for row in electrolyte[:3]] == ["SZ.002709", "SZ.002407", "SZ.002759"]
    assert electrolyte[0]["representative_type"] == "core"
    assert electrolyte[0]["node_id"] == "electrolyte"
    assert _top_symbol("光模块") == "SZ.300308"
    optical = preferred_concept_carriers("光模块")
    assert [row["symbol"] for row in optical[:3]] == ["SZ.300308", "SZ.300502", "SZ.300394"]
    assert _top_symbol("猪肉") == "SZ.002714"


def test_broad_concepts_keep_primary_chain_leader():
    assert _top_symbol("半导体") == "SH.688981"
    assert _top_symbol("白酒") == "SH.600519"
    assert _top_symbol("银行") == "SH.601398"


def test_major_chains_are_not_collapsed_to_lithium():
    assert _top_symbol("电新") == "SZ.300750"
    assert _top_symbol("半导体设备") == "SZ.002371"
    assert _top_symbol("存储芯片") == "SH.603986"
    assert _top_symbol("光刻胶") == "SZ.300236"
    assert _top_symbol("CPO") == "SZ.300308"


def test_semantic_mapping_coverage_accounts_for_every_name():
    names = ["电解液", "锂矿", "光模块", "CPO", "半导体设备", "光刻胶", "未收录主题XYZ"]
    report = build_mapping_coverage(names)
    counts = report["counts"]
    assert counts["total"] == len(names)
    assert counts["accounted"] == len(names)
    assert "未收录主题XYZ" in report["unmapped"]
    mapped_names = {row["name"] for row in report["mapped"]}
    assert {"电解液", "锂矿", "光模块", "CPO", "半导体设备", "光刻胶"} <= mapped_names
