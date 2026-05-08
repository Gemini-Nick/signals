from datetime import datetime

from signals.sync.modules import postmarket_chain_rebuild as rebuild


def test_tianji_taxonomy_representative_lives_under_lipf6_not_electrolyte():
    reps = rebuild._taxonomy_representatives_by_node()

    assert "002759" not in reps.get(("lithium_battery", "electrolyte"), {})
    tianji = reps[("lithium_battery", "lipf6_lithium_salt")]["002759"]
    assert tianji["symbol"] == "SZ.002759"
    assert tianji["representative_type"] == "core"
    assert "六氟磷酸锂" in tianji["representative_relation"]


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
