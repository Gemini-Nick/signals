# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.replay.word_style_renderer import render_word_style_review


def test_word_style_renderer_uses_evidence_and_marks_missing_fields():
    text = render_word_style_review(
        {"trade_date": "2026-06-29", "sector_boards": []},
        {
            "trade_date": "2026-06-29",
            "major_indices": [
                {
                    "name": "上证指数",
                    "close": 4073.9,
                    "change_pct": 1.20,
                    "amount_yi": 16662.2,
                    "amount_change_pct": 4.5,
                    "amplitude_pct": 2.05,
                }
            ],
            "market_breadth": {
                "status": "available",
                "up": 3520,
                "down": 1630,
                "limit_like_count": 92,
                "down_limit_like_count": 4,
                "evidence_level": "confirmed",
            },
            "daily_board_rankings": {
                "rows": [
                    {
                        "name": "生物制品",
                        "kind": "industry",
                        "change_pct": 7.43,
                        "amount_yi": 185.77,
                        "turnover_pct": None,
                        "leader_name": "禾元生物",
                        "leader_change_pct": 20.0,
                        "source": "board_ths:ths",
                    }
                ],
                "weak_rows": [
                    {
                        "name": "CPO概念",
                        "kind": "concept",
                        "change_pct": -3.21,
                        "leader_name": "unknown",
                        "source": "concept_ranking:canonical",
                    }
                ],
            },
            "high_turnover_cores": [
                {
                    "symbol": "SH.603986",
                    "code": "603986",
                    "name": "兆易创新",
                    "change_pct": 9.09,
                    "amount_yi": 431.5,
                    "open": 779,
                    "high": 846.66,
                    "low": 750,
                    "close": 840,
                }
            ],
            "structured_daily_review": {
                "data_completeness": [
                    {"item": "指数分钟线", "status": "missing", "source": "index_bars", "impact": "指数日内时间轴"},
                    {"item": "板块分钟线", "status": "missing", "source": "board_heat_ticks", "impact": "资金切换"},
                    {"item": "涨停/跌停/炸板", "status": "missing", "source": "market_limit_pools", "impact": "情绪"},
                ],
                "key_stock_pool": {
                    "gainers_top20": [
                        {
                            "symbol": "SH.688535",
                            "code": "688535",
                            "name": "华海清科",
                            "change_pct": 20.0,
                            "amount_yi": 80.2,
                            "open": 252,
                            "high": 302.4,
                            "low": 249,
                            "close": 302.4,
                        }
                    ],
                    "limit_pool_counts": {},
                },
            },
            "flow_availability": {"participant_flow_available": False, "order_size_flow_available": True, "order_size_sources": ["eastmoney_ulist_np"]},
            "index_cycle": {"pivot_date": "2026-06-23", "pivot_high": 4175.35, "trading_days_since": 4, "drop_pct": -2.43},
        },
    )

    assert "A股盘后复盘报告 | 2026年6月29日（周一）" in text
    assert "一、市场整体状态" in text
    assert "三、板块深度拆解" in text
    assert "七、明日观察清单" in text
    assert "4073.9" in text
    assert "生物制品" in text
    assert "兆易创新" in text
    assert "板块分钟线：missing" in text
    assert "账户级主力/散户资金：missing" in text
    assert "极度分化，科创单骑救主" not in text
