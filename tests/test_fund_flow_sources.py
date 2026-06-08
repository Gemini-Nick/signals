# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.replay.fund_flow_sources import (
    parse_eastmoney_quote_fund_flow,
    parse_eastmoney_ulist_fund_flow,
    parse_ths_real_funds,
)


def test_parse_eastmoney_ulist_fund_flow_prefers_page_numeric_endpoint():
    parsed = parse_eastmoney_ulist_fund_flow(
        {
            "f6": 58324832701.61,
            "f62": -6380715008.0,
            "f64": 24164267776.0,
            "f65": 30128671232.0,
            "f66": -5964403456.0,
            "f70": 17781157376.0,
            "f71": 18197468928.0,
            "f72": -416311552.0,
            "f76": 15539998720.0,
            "f77": 9158622720.0,
            "f78": 6381376000.0,
            "f82": 0.0,
            "f83": 661049.0,
            "f84": -661049.0,
            "f124": 1780644885,
        },
        requested_trade_date="2026-06-05",
    )

    assert parsed["source"] == "eastmoney_ulist_np"
    assert parsed["observed_trade_date"] == "2026-06-05"
    assert parsed["amount_yi"] == 583.25
    assert parsed["main_order"]["buy_yi"] == 419.45
    assert parsed["main_order"]["sell_yi"] == 483.26
    assert parsed["main_order"]["net_yi"] == -63.81
    assert parsed["retail_proxy"]["buy_yi"] == 155.4
    assert parsed["retail_proxy"]["sell_yi"] == 91.6
    assert parsed["retail_proxy"]["net_yi"] == 63.8
    assert parsed["small_order"]["net_yi"] == -0.01
    assert parsed["participant_flow_available"] is False


def test_parse_eastmoney_quote_fund_flow_keeps_order_size_boundary():
    parsed = parse_eastmoney_quote_fund_flow(
        {
            "f48": 58324832701.61,
            "f57": "300308",
            "f58": "中际旭创",
            "f86": 1780644885,
            "f135": 41945425152.0,
            "f136": 48326140160.0,
            "f137": -6380715008.0,
            "f138": 24164267776.0,
            "f139": 30128671232.0,
            "f140": -5964403456.0,
            "f141": 17781157376.0,
            "f142": 18197468928.0,
            "f143": -416311552.0,
            "f144": 15539998720.0,
            "f145": 9158622720.0,
            "f146": 6381376000.0,
            "f149": -661049.0,
        },
        requested_trade_date="2026-06-05",
    )

    assert parsed["observed_trade_date"] == "2026-06-05"
    assert parsed["amount_yi"] == 583.25
    assert parsed["main_order"]["buy_yi"] == 419.45
    assert parsed["main_order"]["sell_yi"] == 483.26
    assert parsed["main_order"]["net_yi"] == -63.81
    assert parsed["retail_proxy"]["buy_yi"] == 155.4
    assert parsed["retail_proxy"]["sell_yi"] == 91.59
    assert parsed["participant_flow_available"] is False
    assert parsed["order_size_buy_sell_available"] is True
    assert "order-size" in parsed["note"]


def test_parse_ths_real_funds_order_distribution():
    parsed = parse_ths_real_funds(
        {
            "title": {"zlr": "2742083.58", "zlc": "3094019.47", "je": "-351935.89"},
            "flash": [
                {"name": "大单流出", "sr": "2662634.38"},
                {"name": "中单流出", "sr": "427605.34"},
                {"name": "小单流出", "sr": "179.75"},
                {"name": "小单流入", "sr": "79.98"},
                {"name": "中单流入", "sr": "631760.29"},
                {"name": "大单流入", "sr": "2110243.31"},
            ],
        },
        requested_trade_date="2026-06-05",
    )

    assert parsed["total_in_yi"] == 274.21
    assert parsed["total_out_yi"] == 309.4
    assert parsed["buckets"]["big_order"]["in_yi"] == 211.02
    assert parsed["buckets"]["big_order"]["out_yi"] == 266.26
    assert parsed["buckets"]["medium_order"]["net_yi"] == 20.42
    assert parsed["participant_flow_available"] is False
