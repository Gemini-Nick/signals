from signals.web2.api import cluster


def test_industry_chain_baskets_use_yaml_representatives():
    baskets = cluster._industry_chain_baskets("电解液", 3)

    assert baskets
    first = baskets[0]
    assert first["source"] == "自建产业链图谱"
    assert first["domain"] == "industry_chain_yaml"
    assert first["label"] == "电解液"
    assert {item["code"] for item in first["codes"]} >= {"002709", "300750"}


def test_dedupe_baskets_prefers_first_label():
    baskets = cluster._dedupe_baskets([
        {"id": "chain:1", "label": "半导体设备", "codes": [{"code": "002371"}]},
        {"id": "board:1", "label": "半导体设备", "codes": [{"code": "688012"}]},
    ], 8)

    assert baskets == [{"id": "chain:1", "label": "半导体设备", "codes": [{"code": "002371"}]}]
