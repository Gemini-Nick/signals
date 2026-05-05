# -*- coding: utf-8 -*-
from __future__ import annotations


def test_optical_idea_maps_lumentum_coherent_fabrinet_to_a_share_optical_pool():
    from signals.core.cross_market_chains import build_ai_hardware_portfolio, match_cross_market_nodes

    idea = "Lumentum、Coherent、Fabrinet 光器件链走强后，A股光模块/CPO 是否 T+1 承接"
    nodes = match_cross_market_nodes(idea)
    node_ids = [node["node_id"] for node in nodes]

    assert node_ids[0] == "optical_interconnect"

    portfolio = build_ai_hardware_portfolio(idea)
    first_us = portfolio["us_trigger_basket"][0]
    first_cn = portfolio["cn_reaction_basket"][0]

    assert first_us["node_id"] == "optical_interconnect"
    assert {"COHR", "LITE", "FN", "AAOI", "CIEN"}.issubset(set(first_us["symbols"]))
    assert first_cn["group"] == "光模块/CPO"
    assert {"SZ.300308", "SZ.300502", "SZ.300394"}.issubset(set(first_cn["symbols"]))
    assert {"中际旭创", "新易盛", "天孚通信"}.issubset(set(first_cn["core_representatives"]))
    assert first_cn["core_candidates"][0]["symbol"] == "SZ.300308"
    assert "AVGO" not in first_us["symbols"]
    assert "T+1" in portfolio["mapping_rule"]
    assert portfolio["us_driver_nodes"][0]["node_id"] == "optical_interconnect"
    assert portfolio["cn_mapping_nodes"][0]["top_candidates"][0]["symbol"] == "SZ.300308"
    assert portfolio["rhythm_windows"][0]["label"] == "昨夜美股"


def test_liquid_cooling_idea_uses_vertiv_eaton_nvent_as_primary_us_chain():
    from signals.core.cross_market_chains import build_ai_hardware_portfolio, match_cross_market_nodes

    idea = "Vertiv、Eaton、nVent 数据中心液冷订单上修后，A股液冷/热管理是否扩散"
    nodes = match_cross_market_nodes(idea)
    node_ids = [node["node_id"] for node in nodes]

    assert node_ids[0] == "thermal_liquid_cooling"

    portfolio = build_ai_hardware_portfolio(idea)
    first_us = portfolio["us_trigger_basket"][0]
    first_cn = portfolio["cn_reaction_basket"][0]

    assert first_us["node_id"] == "thermal_liquid_cooling"
    assert first_us["symbols"] == ["VRT", "ETN", "NVT"]
    assert {"SMCI", "DELL", "HPE"}.issubset(set(first_us["conditional_symbols"]))
    assert first_cn["group"] == "液冷/热管理"
    assert {"SZ.002837", "SZ.301018", "SZ.300499"}.issubset(set(first_cn["symbols"]))
    assert {"英维克", "申菱环境", "高澜股份"}.issubset(set(first_cn["core_representatives"]))
    assert {"同飞股份", "佳力图", "依米康"}.issubset(set(first_cn["elastic_representatives"]))
    assert "只有在 rack-scale" in first_us["role"] or "liquid-cooled server" in first_us["role"]


def test_copper_and_pcb_are_separate_mapping_nodes():
    from signals.core.cross_market_chains import build_ai_hardware_portfolio

    idea = "Amphenol 和 TTM Technologies 走强后，A股铜连接、PCB/CCL 是否有分化联动"
    portfolio = build_ai_hardware_portfolio(idea)
    us_nodes = [item["node_id"] for item in portfolio["us_trigger_basket"]]
    cn_groups = [item["group"] for item in portfolio["cn_reaction_basket"]]

    assert "copper_interconnect" in us_nodes
    assert "pcb_ccl_materials" in us_nodes
    assert "铜连接/高速连接器" in cn_groups
    assert "PCB/CCL" in cn_groups

    copper = portfolio["us_trigger_basket"][us_nodes.index("copper_interconnect")]
    pcb = portfolio["us_trigger_basket"][us_nodes.index("pcb_ccl_materials")]
    copper_cn = portfolio["cn_reaction_basket"][cn_groups.index("铜连接/高速连接器")]
    pcb_cn = portfolio["cn_reaction_basket"][cn_groups.index("PCB/CCL")]
    assert {"APH", "TEL", "GLW"}.issubset(set(copper["symbols"]))
    assert {"TTMI", "SANM", "FLEX", "JBL"}.issubset(set(pcb["symbols"]))
    assert {"SZ.002130", "SZ.300563", "SZ.300913"}.issubset(set(copper_cn["symbols"]))
    assert {"SZ.300476", "SZ.002463", "SZ.002916"}.issubset(set(pcb_cn["symbols"]))


def test_avgo_silicon_photonics_stays_networking_not_pure_optical():
    from signals.core.cross_market_chains import build_ai_hardware_portfolio

    portfolio = build_ai_hardware_portfolio("AVGO 硅光和网络 ASIC 订单上修后，A股 CPO 是否扩散")
    first_us = portfolio["us_trigger_basket"][0]

    assert first_us["node_id"] == "networking_switch_asic"
    assert "AVGO" in first_us["symbols"]
    assert "COHR" not in first_us["symbols"]
    assert portfolio["cn_reaction_basket"][0]["group"] == "交换机/光互联"


def test_hyperscaler_capex_is_terminal_evidence_not_a_share_candidate_pool():
    from signals.core.cross_market_chains import build_ai_hardware_portfolio

    portfolio = build_ai_hardware_portfolio("MSFT AMZN GOOGL META capex 上修后，A股 AI 硬件是否扩散")

    terminal_nodes = [item["node_id"] for item in portfolio["terminal_evidence"]]
    direct_nodes = [item["source_node_id"] for item in portfolio["cn_reaction_basket"]]

    assert "hyperscaler_capex_terminal" in terminal_nodes
    assert direct_nodes == []
    terminal = next(item for item in portfolio["terminal_evidence"] if item["node_id"] == "hyperscaler_capex_terminal")
    assert terminal["direct_a_share_candidates"] is False
    assert "不直接生成 A股候选" in terminal["role"]
