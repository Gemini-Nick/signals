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
