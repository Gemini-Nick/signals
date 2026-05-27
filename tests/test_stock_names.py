from __future__ import annotations


def test_current_stock_name_overrides_static_leader_alias(monkeypatch):
    from signals.core.stock_names import StockNameResolver
    from signals.layers import industry

    monkeypatch.setattr(industry, "_INDUSTRY_LEADERS", {"证券": ("SH.601211", "国泰君安")})
    monkeypatch.setattr(industry, "_build_name_to_code_map", lambda: {"国泰海通": "601211"})
    monkeypatch.setattr(industry, "_code6_to_futu", lambda code: f"SH.{str(code).zfill(6)}")

    resolver = StockNameResolver()

    assert resolver.get_code("国泰海通") == "SH.601211"
    assert resolver.get_code("国泰君安") == "SH.601211"
    assert resolver.get_code("国泰") == "SH.601211"
    assert resolver.get_name("SH.601211") == "国泰海通"
    assert resolver.search("国泰海通") == [("SH.601211", "国泰海通")]


def test_hk_english_alias_resolves_case_insensitively(monkeypatch):
    from signals.core.stock_names import StockNameResolver
    from signals.layers import industry

    monkeypatch.setattr(industry, "_INDUSTRY_LEADERS", {})
    monkeypatch.setattr(industry, "_build_name_to_code_map", lambda: {})
    monkeypatch.setattr(industry, "_code6_to_futu", lambda code: "")

    resolver = StockNameResolver()

    assert resolver.get_code("asmpt") == "HK.00522"
    assert resolver.get_code("ASMPT") == "HK.00522"
    assert resolver.get_code("ASM Pacific Technology") == "HK.00522"
    assert resolver.get_name("HK.00522") == "ASMPT"
    assert resolver.search("asmpt") == [("HK.00522", "ASMPT")]


def test_catl_h_alias_resolves_to_hk_listing(monkeypatch):
    from signals.core.stock_names import StockNameResolver
    from signals.layers import industry

    monkeypatch.setattr(industry, "_INDUSTRY_LEADERS", {})
    monkeypatch.setattr(industry, "_build_name_to_code_map", lambda: {})
    monkeypatch.setattr(industry, "_code6_to_futu", lambda code: "")

    resolver = StockNameResolver()

    assert resolver.get_code("宁德时代H") == "HK.03750"
    assert resolver.get_code("宁德时代 H") == "HK.03750"
    assert resolver.get_code("CATL-H") == "HK.03750"
    assert resolver.get_name("HK.03750") == "宁德时代 H"
    assert resolver.search("CATL H") == [("HK.03750", "宁德时代 H")]


def test_macro_industry_etf_names_resolve_even_with_wrong_prefix(monkeypatch):
    from signals.core.stock_names import StockNameResolver
    from signals.layers import industry

    monkeypatch.setattr(industry, "_INDUSTRY_LEADERS", {})
    monkeypatch.setattr(industry, "_build_name_to_code_map", lambda: {})
    monkeypatch.setattr(industry, "_code6_to_futu", lambda code: "")

    resolver = StockNameResolver()

    assert resolver.get_name("SH.562590") == "半导体设备ETF"
    assert resolver.get_name("SZ.562590") == "半导体设备ETF"
    assert resolver.get_code("半导体设备ETF") == "SH.562590"
