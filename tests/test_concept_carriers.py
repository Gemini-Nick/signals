from signals.core.concept_carriers import (
    build_mapping_coverage,
    industry_hints_for_concept,
    match_industry_chains,
    non_chain_reason,
    preferred_concept_carriers,
)


def _top_symbol(concept: str) -> str:
    rows = preferred_concept_carriers(concept)
    assert rows
    return rows[0]["symbol"]


def test_lithium_resource_uses_upstream_carrier():
    assert _top_symbol("锂") == "SZ.002466"
    assert "能源金属" in industry_hints_for_concept("锂")
    rows = preferred_concept_carriers("锂")
    assert rows[0]["chain_id"] == "lithium_battery"
    assert rows[0]["node_id"] == "lithium_resource"
    assert all(row["symbol"] != "SZ.002460" or row["chain_id"] == "lithium_battery" for row in rows[:4])


def test_precious_metals_do_not_use_lithium_resource_representatives():
    rows = preferred_concept_carriers("贵金属")
    assert rows
    assert rows[0]["chain_id"] == "nonferrous"
    assert rows[0]["node_id"] == "precious_metals"
    assert "SZ.002460" not in {row["symbol"] for row in rows[:4]}


def test_compute_service_operator_is_separate_from_hardware_and_chips():
    for concept in ["算力租赁", "智算中心", "GPU云", "IDC算力服务"]:
        rows = preferred_concept_carriers(concept)
        assert rows
        assert rows[0]["chain_id"] == "ai_compute"
        assert rows[0]["node_id"] == "compute_service_operator"

    storage = preferred_concept_carriers("存储")
    assert storage and storage[0]["node_id"] == "memory_chip"
    chip = preferred_concept_carriers("芯片")
    assert chip and chip[0]["chain_id"] == "semiconductor"


def test_segment_concepts_prefer_matching_chain_stage():
    assert _top_symbol("电解液") == "SZ.002709"
    electrolyte = preferred_concept_carriers("电解液")
    assert [row["symbol"] for row in electrolyte[:4]] == ["SZ.002709", "SH.603026", "SZ.300037", "SZ.002407"]
    assert electrolyte[0]["representative_type"] == "core"
    assert electrolyte[0]["node_id"] == "electrolyte"
    assert not any(row["symbol"] == "SZ.002759" and row["representative_type"] == "elastic" for row in electrolyte)
    lipf6 = preferred_concept_carriers("六氟磷酸锂")
    assert [row["symbol"] for row in lipf6[:2]] == ["SZ.002407", "SZ.002759"]
    assert {row["node_id"] for row in lipf6[:2]} == {"lipf6_lithium_salt"}
    for concept in ["6F", "6f", "LiPF6"]:
        matches = match_industry_chains(concept)
        assert matches
        assert matches[0]["node_id"] == "lipf6_lithium_salt"
    pvdf = preferred_concept_carriers("PVDF")
    assert pvdf
    assert pvdf[0]["node_id"] == "pvdf_binder"
    assert pvdf[0]["symbol"] == "SZ.002407"
    assert _top_symbol("光模块") == "SZ.300308"
    optical = preferred_concept_carriers("光模块")
    assert [row["symbol"] for row in optical[:3]] == ["SZ.300308", "SZ.300502", "SZ.300394"]
    ocs = preferred_concept_carriers("OCS光")
    assert ocs
    assert ocs[0]["symbol"] == "SH.688195"
    assert ocs[0]["node_id"] == "ocs_optical_switch"
    for concept in ["OCS", "光交换", "光开关"]:
        matches = match_industry_chains(concept)
        assert matches
        assert matches[0]["node_id"] == "ocs_optical_switch"
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


def test_copper_interconnect_requires_specific_connector_terms():
    for broad_name in ["元件", "通信设备", "消费电子"]:
        assert not any(row["chain_id"] == "copper_interconnect" for row in match_industry_chains(broad_name))

    matches = match_industry_chains("铜缆高速连接")
    copper = next(row for row in matches if row["chain_id"] == "copper_interconnect")

    assert copper["confidence"] >= 90
    assert "铜缆高速连接" in copper["hit_terms"]

    carriers = preferred_concept_carriers("铜缆高速连接")
    assert [row["symbol"] for row in carriers[:3]] == ["SZ.002130", "SZ.300563", "SZ.300913"]


def test_ascii_acronyms_do_not_match_inside_unrelated_words():
    micro_led_matches = match_industry_chains("MicroLED")
    assert micro_led_matches[0]["chain_id"] == "consumer_electronics"
    assert "MicroLED" in micro_led_matches[0]["hit_terms"]
    assert not any(row["chain_id"] == "medicine" for row in micro_led_matches)

    cro_matches = match_industry_chains("CRO")
    assert cro_matches[0]["chain_id"] == "medicine"


def test_aigc_uses_explicit_ai_compute_alias():
    matches = match_industry_chains("AIGC概念")

    assert matches[0]["chain_id"] == "ai_compute"
    assert matches[0]["confidence"] >= 90
    assert "AIGC" in matches[0]["hit_terms"]


def test_semiconductor_related_concepts_map_to_semiconductor_chain():
    for concept, node_id in [
        ("华为海思", "chip_design"),
        ("高带宽内存", "memory_chip"),
        ("HBM", "memory_chip"),
        ("半导体设备", "semiconductor_equipment"),
    ]:
        rows = preferred_concept_carriers(concept)
        assert rows
        assert rows[0]["chain_id"] == "semiconductor"
        assert rows[0]["node_id"] == node_id


def test_commercial_space_concepts_outrank_transport_airline_aliases():
    for concept in ["商业航天", "卫星互联网", "航天航空", "通用航空", "低空经济", "航天装备Ⅲ", "航天装备Ⅱ", "航空装备Ⅲ", "航空装备III"]:
        matches = match_industry_chains(concept)
        assert matches
        assert matches[0]["chain_id"] == "military"
        assert matches[0]["node_id"] == "commercial_space"

    for concept in ["商业航天", "通用航空", "航空装备III"]:
        carriers = preferred_concept_carriers(concept)
        assert [row["symbol"] for row in carriers[:3]] == ["SH.601698", "SZ.000547", "SH.600343"]
        assert not any(row["chain_id"] == "transport" for row in carriers[:5])


def test_generic_reverse_terms_do_not_steal_specific_chain_mapping():
    assert match_industry_chains("半导体设备")[0]["node_id"] == "semiconductor_equipment"
    assert match_industry_chains("半导体材料")[0]["node_id"] == "material_photoresist"

    assert not any(row["chain_id"] == "semiconductor" for row in match_industry_chains("交运设备")[:3])
    assert not any(row["chain_id"] == "semiconductor" for row in match_industry_chains("包装材料")[:3])
    assert not any(row["chain_id"] == "ai_compute" for row in match_industry_chains("租赁")[:3])


def test_latest_board_specific_owners_beat_broad_industry_terms():
    expected = {
        "小金属": ("nonferrous", "rare_earth_minor"),
        "小金属概念": ("nonferrous", "rare_earth_minor"),
        "汽车整车": ("new_energy_vehicle", "vehicle"),
        "小米汽车": ("new_energy_vehicle", "vehicle"),
        "绿色电力": ("wind_storage_grid", "grid_storage"),
        "熔盐储能": ("wind_storage_grid", "grid_storage"),
        "钒电池": ("wind_storage_grid", "grid_storage"),
        "光通信模块": ("optical_module", "optical_module_core"),
        "光伏电池组件": ("photovoltaic", "solar_module"),
        "化学原料": ("chemical", "chemical_material"),
        "化学制品": ("chemical", "chemical_material"),
    }

    for concept, (chain_id, node_id) in expected.items():
        matches = match_industry_chains(concept)
        assert matches
        assert (matches[0]["chain_id"], matches[0]["node_id"]) == (chain_id, node_id)

    for concept in ["被动元件", "光学元件", "元件"]:
        assert not any(row["chain_id"] == "optical_module" for row in match_industry_chains(concept)[:3])


def test_hot_ai_and_huawei_concepts_use_specific_owners():
    expected = {
        "AI应用": ("ai_compute", "ai_application"),
        "AI智能体": ("ai_compute", "ai_application"),
        "AI语料": ("ai_compute", "ai_application"),
        "智谱AI概念": ("ai_compute", "ai_application"),
        "Kimi概念": ("ai_compute", "ai_application"),
        "华为昇腾": ("ai_compute", "domestic_compute_ecosystem"),
        "华为鲲鹏": ("ai_compute", "domestic_compute_ecosystem"),
        "华为欧拉": ("ai_compute", "domestic_compute_ecosystem"),
        "信创": ("ai_compute", "domestic_compute_ecosystem"),
        "云计算": ("ai_compute", "compute_service_operator"),
    }

    for concept, (chain_id, node_id) in expected.items():
        matches = match_industry_chains(concept)
        assert matches
        assert (matches[0]["chain_id"], matches[0]["node_id"]) == (chain_id, node_id)

    carriers = preferred_concept_carriers("华为昇腾")
    assert carriers
    assert carriers[0]["chain_id"] == "ai_compute"
    assert not any(row["chain_id"] == "consumer_electronics" for row in carriers[:5])


def test_hot_terminal_chip_medical_and_material_concepts_map_explicitly():
    expected = {
        "AI手机": ("consumer_electronics", "ai_terminal"),
        "AIPC": ("consumer_electronics", "ai_terminal"),
        "3D摄像头": ("consumer_electronics", "electronics"),
        "3D玻璃": ("consumer_electronics", "electronics"),
        "OLED": ("consumer_electronics", "electronics"),
        "MLCC": ("consumer_electronics", "electronics"),
        "被动元件": ("consumer_electronics", "electronics"),
        "光学元件": ("consumer_electronics", "electronics"),
        "AI芯片": ("semiconductor", "chip_design"),
        "EDA概念": ("semiconductor", "chip_design"),
        "IGBT概念": ("semiconductor", "compound_semiconductor"),
        "F5G概念": ("telecom_network", "telecom_equipment"),
        "CAR-T细胞疗法": ("medicine", "medicine_core"),
        "DRG/DIP": ("medicine", "medicine_core"),
        "AI制药（医疗）": ("medicine", "medicine_core"),
        "PEEK材料概念": ("chemical", "chemical_material"),
        "互联网金融": ("finance", "finance_core"),
        "区块链": ("ai_compute", "ai_application"),
        "Web3.0": ("ai_compute", "ai_application"),
        "元宇宙概念": ("ai_compute", "ai_application"),
        "国资云概念": ("ai_compute", "ai_application"),
        "工业互联": ("ai_compute", "ai_application"),
        "数据要素": ("ai_compute", "ai_application"),
        "数字经济": ("ai_compute", "ai_application"),
        "数字货币": ("ai_compute", "ai_application"),
        "EDR概念": ("new_energy_vehicle", "vehicle"),
        "换电概念": ("new_energy_vehicle", "vehicle"),
        "新能源车": ("new_energy_vehicle", "vehicle"),
        "可控核聚变": ("wind_storage_grid", "grid_storage"),
        "抽水蓄能": ("wind_storage_grid", "grid_storage"),
        "核能核电": ("wind_storage_grid", "grid_storage"),
        "可燃冰": ("coal_oil_gas", "fossil_energy"),
        "地下管网": ("real_estate_infra", "infra_property"),
        "医废处理": ("environmental", "environmental_service"),
        "核污染防治": ("environmental", "environmental_service"),
        "土壤修复": ("environmental", "environmental_service"),
        "医美概念": ("medicine", "medicine_core"),
        "幽门螺杆菌概念": ("medicine", "medicine_core"),
        "单抗概念": ("medicine", "medicine_core"),
        "创投": ("finance", "finance_core"),
        "C2M概念": ("consumer", "consumer_goods"),
        "宠物经济": ("consumer", "consumer_goods"),
        "小红书概念": ("media_tourism", "media_content"),
        "抖音概念(字节概念)": ("media_tourism", "media_content"),
        "供销社概念": ("agriculture", "farming"),
        "机器人执行器": ("robotics", "automation"),
        "同步磁阻电机": ("robotics", "automation"),
        "中芯概念": ("semiconductor", "wafer_foundry"),
        "毫米波概念": ("telecom_network", "telecom_equipment"),
        "SPD概念": ("medicine", "medicine_core"),
        "精准医疗": ("medicine", "medicine_core"),
        "病毒防治": ("medicine", "medicine_core"),
        "肝素概念": ("medicine", "medicine_core"),
        "玻璃基板": ("semiconductor", "packaging_test"),
        "激光雷达": ("new_energy_vehicle", "vehicle"),
        "汽车一体化压铸": ("new_energy_vehicle", "vehicle"),
        "电子后视镜": ("new_energy_vehicle", "vehicle"),
        "电网概念": ("wind_storage_grid", "grid_storage"),
        "生物质能发电": ("wind_storage_grid", "grid_storage"),
        "网络安全": ("ai_compute", "ai_server"),
        "电子身份证": ("ai_compute", "ai_server"),
        "腾讯云": ("ai_compute", "compute_service_operator"),
        "英伟达概念": ("ai_compute", "ai_server"),
        "虚拟数字人": ("ai_compute", "ai_application"),
        "虚拟现实": ("consumer_electronics", "electronics"),
        "空间计算": ("consumer_electronics", "electronics"),
        "空间站概念": ("military", "commercial_space"),
        "化妆品概念": ("consumer", "consumer_goods"),
        "拼多多概念": ("consumer", "consumer_goods"),
        "电子烟": ("consumer", "consumer_goods"),
        "新型城镇化": ("real_estate_infra", "infra_property"),
        "房屋检测": ("real_estate_infra", "infra_property"),
        "新型工业化": ("robotics", "automation"),
        "有机硅概念": ("chemical", "chemical_material"),
        "环氧丙烷": ("chemical", "chemical_material"),
        "碳基材料": ("chemical", "chemical_material"),
        "低碳冶金": ("chemical", "chemical_material"),
        "汽车拆解": ("environmental", "environmental_service"),
        "电子竞技": ("media_tourism", "media_content"),
        "知识产权": ("media_tourism", "media_content"),
        "粮食概念": ("agriculture", "farming"),
        "蚂蚁概念": ("finance", "finance_core"),
        "空气能热泵": ("home_appliance", "appliance"),
        "磁悬浮概念": ("transport", "transport_core"),
        "超级电容": ("electric_equipment_new_energy", "broad_equipment"),
        "超超临界发电": ("wind_storage_grid", "grid_storage"),
        "雅下水电概念": ("wind_storage_grid", "grid_storage"),
        "轮毂电机": ("new_energy_vehicle", "vehicle"),
        "飞行汽车(eVTOL)": ("military", "military_core"),
        "边缘计算": ("ai_compute", "ai_server"),
        "量子科技": ("ai_compute", "ai_server"),
        "财税数字化": ("ai_compute", "ai_application"),
        "阿里概念": ("ai_compute", "compute_service_operator"),
        "跨境支付": ("finance", "finance_core"),
        "辅助生殖": ("medicine", "medicine_core"),
        "重组蛋白": ("medicine", "medicine_core"),
        "长寿药": ("medicine", "medicine_core"),
        "青蒿素": ("medicine", "medicine_core"),
        "降解塑料": ("chemical", "chemical_material"),
        "资源开采概念": ("nonferrous", "industrial_metals"),
        "调味品概念": ("consumer", "consumer_goods"),
        "谷子经济": ("consumer", "consumer_goods"),
        "退税商店": ("consumer", "consumer_goods"),
        "超清视频": ("media_tourism", "media_content"),
        "新能源汽车": ("new_energy_vehicle", "vehicle"),
        "新能源整车": ("new_energy_vehicle", "vehicle"),
        "储能电池": ("lithium_battery", "battery_cell"),
        "MCU": ("semiconductor", "chip_design"),
        "草甘膦": ("chemical", "chemical_material"),
        "锂盐": ("lithium_battery", "lithium_resource"),
    }

    for concept, (chain_id, node_id) in expected.items():
        matches = match_industry_chains(concept)
        assert matches
        assert (matches[0]["chain_id"], matches[0]["node_id"]) == (chain_id, node_id)
        assert matches[0]["confidence"] >= 90


def test_home_building_materials_do_not_fall_into_liquor_or_textile():
    expected = {
        "家居用品": ("home_building_materials", "home_decoration_materials"),
        "装修建材": ("home_building_materials", "home_decoration_materials"),
        "装饰建材": ("home_building_materials", "home_decoration_materials"),
        "瓷砖": ("home_building_materials", "ceramic_tile_sanitary"),
        "建筑陶瓷": ("home_building_materials", "ceramic_tile_sanitary"),
        "卫浴制品": ("home_building_materials", "ceramic_tile_sanitary"),
    }

    for concept, (chain_id, node_id) in expected.items():
        matches = match_industry_chains(concept)
        assert matches
        assert (matches[0]["chain_id"], matches[0]["node_id"]) == (chain_id, node_id)
        assert matches[0]["confidence"] >= 90

    carriers = preferred_concept_carriers("瓷砖")
    assert carriers
    assert carriers[0]["symbol"] == "SZ.002918"
    assert not any(row["chain_id"] == "consumer" for row in carriers[:5])
    assert not any(row["chain_id"] == "textile_light" for row in carriers[:5])


def test_specific_keywords_do_not_pull_adjacent_chain_representatives():
    glyphosate = preferred_concept_carriers("草甘膦")
    assert glyphosate
    assert {row["chain_id"] for row in glyphosate[:3]} == {"chemical"}
    assert not any(row["chain_id"] == "agriculture" for row in glyphosate)

    supercapacitor = preferred_concept_carriers("超级电容")
    assert supercapacitor
    assert supercapacitor[0]["chain_id"] == "electric_equipment_new_energy"
    assert not any(row["chain_id"] == "lithium_battery" for row in supercapacitor)

    mcu = preferred_concept_carriers("MCU")
    assert mcu
    assert {row["node_id"] for row in mcu[:2]} == {"chip_design"}

    lithium_salt = preferred_concept_carriers("锂盐")
    assert lithium_salt
    assert lithium_salt[0]["node_id"] == "lithium_resource"
    assert not any(row["node_id"] == "lipf6_lithium_salt" for row in lithium_salt)


def test_chain_node_candidates_include_upstream_and_downstream_representatives():
    carriers = preferred_concept_carriers("电解液")

    upstream = {row["symbol"] for row in carriers if row["representative_type"] == "upstream"}
    downstream = {row["symbol"] for row in carriers if row["representative_type"] == "downstream"}

    assert {"SZ.002407", "SZ.002759"} <= upstream
    assert {"SZ.300750", "SZ.300014", "SZ.002074"} <= downstream


def test_new_energy_segment_concepts_map_to_chain_nodes():
    composite = preferred_concept_carriers("复合集流体")
    assert composite
    assert composite[0]["chain_id"] == "lithium_battery"
    assert composite[0]["node_id"] == "battery_cell"

    perovskite = preferred_concept_carriers("钙钛矿")
    assert perovskite
    assert perovskite[0]["chain_id"] == "photovoltaic"
    assert perovskite[0]["node_id"] == "solar_module"


def test_market_board_exact_terms_do_not_remain_low_confidence():
    expected = {
        "HJT电池": ("photovoltaic", "solar_module"),
        "光伏发电": ("photovoltaic", "solar_module"),
        "油气设服": ("coal_oil_gas", "fossil_energy"),
        "油气资源": ("coal_oil_gas", "fossil_energy"),
        "稀土永磁": ("nonferrous", "rare_earth_minor"),
        "光刻机(胶)": ("semiconductor", "semiconductor_equipment"),
        "汽车芯片": ("semiconductor", "chip_design"),
        "农业种植": ("agriculture", "farming"),
        "铁路基建": ("real_estate_infra", "infra_property"),
        "建筑节能": ("real_estate_infra", "infra_property"),
        "冷链物流": ("transport", "transport_core"),
        "网络游戏": ("media_tourism", "media_content"),
        "体育产业": ("media_tourism", "media_content"),
        "职业教育": ("media_tourism", "media_content"),
        "跨境电商": ("consumer", "consumer_goods"),
    }

    for concept, (chain_id, node_id) in expected.items():
        matches = match_industry_chains(concept)
        assert matches
        assert (matches[0]["chain_id"], matches[0]["node_id"]) == (chain_id, node_id)
        assert matches[0]["confidence"] >= 90


def test_non_chain_theme_is_accounted_without_forced_mapping():
    assert non_chain_reason("本月解禁")
    assert non_chain_reason("科创50")
    assert non_chain_reason("2025年报预增")
    assert non_chain_reason("QFII重仓")
    assert non_chain_reason("低价股")
    assert non_chain_reason("中特估")
    assert non_chain_reason("一带一路")
    assert non_chain_reason("央国企改革")
    assert non_chain_reason("昨日涨停_含一字")
    assert non_chain_reason("微盘股")
    assert non_chain_reason("中盘价值")
    assert non_chain_reason("沪股通")
    assert non_chain_reason("机构重仓")
    assert non_chain_reason("证金持股")
    assert non_chain_reason("统一大市场")
    assert non_chain_reason("破增发价股")
    assert non_chain_reason("贬值受益")
    assert non_chain_reason("超级品牌")
    assert non_chain_reason("近期新高")
    assert non_chain_reason("首发经济")
    assert non_chain_reason("内贸流通")
    assert non_chain_reason("共享经济")
    assert non_chain_reason("冰雪经济")

    report = build_mapping_coverage([
        "本月解禁",
        "科创50",
        "2025年报预增",
        "QFII重仓",
        "中特估",
        "中盘成长",
        "沪股通",
        "首发经济",
        "内贸流通",
        "共享经济",
        "冰雪经济",
        "半导体设备",
    ])
    counts = report["counts"]
    assert counts["non_chain"] == 11
    assert counts["mapped"] == 1
    assert counts["accounted"] == counts["total"]


def test_semantic_mapping_coverage_accounts_for_every_name():
    names = ["电解液", "锂矿", "光模块", "CPO", "半导体设备", "光刻胶", "未收录主题XYZ"]
    report = build_mapping_coverage(names)
    counts = report["counts"]
    assert counts["total"] == len(names)
    assert counts["accounted"] == len(names)
    assert "未收录主题XYZ" in report["unmapped"]
    mapped_names = {row["name"] for row in report["mapped"]}
    assert {"电解液", "锂矿", "光模块", "CPO", "半导体设备", "光刻胶"} <= mapped_names
