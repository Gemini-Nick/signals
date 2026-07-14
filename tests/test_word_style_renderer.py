# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.replay.word_style_renderer import _chain_pressure_pool, _failed_samples_for_trend, _trend_candidates, render_word_style_review


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
            "major_index_intraday": {
                "status": "available",
                "source": "index_bars:5分钟",
                "common_low_window": {"start": "10:15", "end": "10:25", "span_minutes": 10},
                "dominant_low_cluster": {"start": "10:15", "end": "10:25", "span_minutes": 10, "count": 1, "total": 1},
                "rows": [
                    {
                        "name": "上证指数",
                        "low_bar": {"time": "10:15", "low": 3992.55},
                        "high_bar": {"time": "14:55", "high": 4075.33},
                        "close_bar": {"time": "15:00", "close": 4073.9},
                        "low_to_close_pct": 2.04,
                    }
                ],
            },
            "major_index_technical": {
                "status": "available",
                "source": "index_bars:日线",
                "note": "由指数日线收盘价计算。",
                "rows": [
                    {
                        "name": "上证指数",
                        "close": 4073.9,
                        "ma5": 4088.2,
                        "ma10": 4099.1,
                        "ma20": 4010.5,
                        "macd_dif": 0.15,
                        "macd_dea": -2.01,
                        "macd_bar": 4.33,
                        "rsi6": 47.93,
                        "bias20_pct": 1.58,
                    },
                    {
                        "name": "科创50",
                        "close": 2126.01,
                        "ma5": 2026.05,
                        "ma10": 1933.83,
                        "ma20": 1803.13,
                        "macd_dif": 100.93,
                        "macd_dea": 67.66,
                        "macd_bar": 66.55,
                        "rsi6": 80.82,
                        "bias20_pct": 17.91,
                    }
                ],
            },
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
                "trend_20d_boards": {
                    "status": "partial",
                    "rows": [
                        {
                            "name": "生物制品",
                            "kind": "industry",
                            "change_pct": 7.29,
                            "change_5d_pct": 8.0,
                            "change_20d_pct": 21.0,
                            "source": "board_ranking/concept_ranking:canonical",
                        }
                    ],
                },
            },
            "flow_availability": {"participant_flow_available": False, "order_size_flow_available": True, "order_size_sources": ["eastmoney_ulist_np"]},
            "index_cycle": {"pivot_date": "2026-06-23", "pivot_high": 4175.35, "trading_days_since": 4, "drop_pct": -2.43},
            "stock_daily_replays": [
                {
                    "symbol": "SH.688535",
                    "code": "688535",
                    "name": "华海清科",
                    "start_date": "2026-06-25",
                    "end_date": "2026-06-29",
                    "total_change_pct": 20.1,
                    "acceleration_date": "2026-06-29",
                    "acceleration_event": "涨停/强封、突破前高",
                    "chain_name": "半导体产业链",
                    "node_name": "半导体设备",
                    "chain_source": "security_chain_memberships",
                    "chain_evidence_date": "2026-06-24",
                    "amount_status": "partial",
                    "rows": [
                        {
                            "date": "2026-06-25",
                            "change_pct": 1.2,
                            "amount_yi": None,
                            "low": 249,
                            "close": 252,
                            "event": "普通交易日",
                        },
                        {
                            "date": "2026-06-29",
                            "change_pct": 20.0,
                            "amount_yi": 80.2,
                            "low": 249,
                            "close": 302.4,
                            "event": "涨停/强封、突破前高",
                        },
                    ],
                }
            ],
        },
    )

    assert "A股盘后复盘报告 | 2026年6月29日（周一）" in text
    assert "一、市场整体状态" in text
    assert "三、板块深度拆解" in text
    assert "七、明日观察清单" in text
    assert "4073.9" in text
    assert "生物制品" in text
    assert "兆易创新" in text
    assert "主要共同拐点集中在 10:15-10:25" in text
    assert "低点后修复" in text
    assert "DIF=0.15，DEA=-2.01，柱=4.33" in text
    assert "MA20乖离+1.58%" in text
    assert "科创50 RSI(6)=80.82、MA20乖离+17.91%" in text
    assert "方向定性" in text
    assert "生物制品（industry）——强趋势延续" not in text
    assert "5日/20日趋势 unknown" in text
    assert "归属半导体产业链/半导体设备" in text
    assert "日线加速点：2026-06-29，涨停/强封、突破前高" in text
    assert "2026-06-25至2026-06-29累计+20.10%" in text
    assert "涨停/强封、突破前高" in text
    assert "板块分钟线：missing" in text
    assert "账户级参与者资金：missing" in text
    assert "主力" not in text
    assert "散户" not in text
    assert "极度分化，科创单骑救主" not in text


def test_trend_candidates_use_replay_quality_without_board_bias():
    def stock(code: str, name: str, *, amount: float, change: float, pool: str = "") -> dict:
        return {
            "symbol": f"SH.{code}",
            "code": code,
            "name": name,
            "amount_yi": amount,
            "change_pct": change,
            "limit_pool": {"pool": pool} if pool else {},
        }

    def replay(code: str, name: str, *, industry: str, acceleration_date: str, total: float, pool: str = "") -> dict:
        return {
            "symbol": f"SH.{code}",
            "code": code,
            "name": name,
            "industry": industry,
            "acceleration_date": acceleration_date,
            "end_date": "2026-06-29",
            "total_change_pct": total,
            "row_count": 12,
            "rows": [{"date": "2026-06-29", "change_pct": 10}],
            "limit_pool": {"pool": pool, "industry": industry} if pool else {"industry": industry},
        }

    rows = [
        stock("688001", "同组高分", amount=50, change=20),
        stock("688002", "同组低分", amount=40, change=20),
        stock("688003", "炸板高分", amount=80, change=18, pool="failed_limit"),
        stock("688004", "不同组A", amount=30, change=20),
        stock("688005", "不同组B", amount=25, change=19),
        stock("688006", "不同组C", amount=20, change=18),
        stock("688007", "不同组D", amount=15, change=17),
    ]
    replays = {
        "688001": replay("688001", "同组高分", industry="样本行业", acceleration_date="2026-06-17", total=90),
        "688002": replay("688002", "同组低分", industry="样本行业", acceleration_date="2026-06-17", total=85),
        "688003": replay("688003", "炸板高分", industry="失败行业", acceleration_date="2026-06-29", total=110, pool="failed_limit"),
        "688004": replay("688004", "不同组A", industry="行业A", acceleration_date="2026-06-17", total=70),
        "688005": replay("688005", "不同组B", industry="行业B", acceleration_date="2026-06-18", total=65),
        "688006": replay("688006", "不同组C", industry="行业C", acceleration_date="2026-06-19", total=60),
        "688007": replay("688007", "不同组D", industry="行业D", acceleration_date="2026-06-20", total=55),
    }

    selected = _trend_candidates(rows, [], replays)
    selected_codes = [row["code"] for row in selected]

    assert selected_codes == ["688001", "688004", "688005", "688006", "688007"]
    assert "688002" not in selected_codes
    assert "688003" not in selected_codes


def test_failed_samples_for_trend_use_same_chain_evidence_not_unrelated_pressure():
    winner = {"symbol": "SH.688001", "code": "688001", "name": "强趋势样本"}
    winner_replay = {
        "symbol": "SH.688001",
        "code": "688001",
        "name": "强趋势样本",
        "chain_id": "semiconductor",
        "chain_name": "半导体产业链",
        "node_id": "material",
        "node_name": "材料",
        "rows": [{"date": "2026-06-29", "change_pct": 20.0, "amount_yi": 30.0, "event": "涨停/强封"}],
    }
    same_chain_weak = {
        "symbol": "SH.688002",
        "code": "688002",
        "name": "同链弱化",
        "chain_id": "semiconductor",
        "chain_name": "半导体产业链",
        "node_id": "material",
        "node_name": "材料",
        "rows": [
            {
                "date": "2026-06-29",
                "change_pct": -4.0,
                "amount_yi": 50.0,
                "high": 110,
                "low": 95,
                "close": 96,
                "event": "大幅回撤、冲高回落",
            }
        ],
    }
    unrelated_weaker = {
        "symbol": "SH.688003",
        "code": "688003",
        "name": "无关弱化",
        "chain_id": "telecom",
        "chain_name": "通信网络/5G产业链",
        "node_id": "optical",
        "node_name": "光模块",
        "rows": [
            {
                "date": "2026-06-29",
                "change_pct": -12.0,
                "amount_yi": 120.0,
                "high": 130,
                "low": 90,
                "close": 91,
                "event": "大幅回撤、冲高回落",
            }
        ],
    }

    samples = _failed_samples_for_trend(
        winner,
        winner_replay,
        {"688001": winner_replay, "688002": same_chain_weak, "688003": unrelated_weaker},
        [unrelated_weaker],
        exclude_keys={"688001"},
    )

    assert samples[0]["code"] == "688002"
    assert samples[0]["_same_chain_sample"] is True


def test_chain_pressure_pool_keeps_high_amount_same_chain_weak_samples_without_name_rules():
    trend_rows = [{"symbol": "SH.688001", "code": "688001", "name": "强趋势样本"}]
    winner_replay = {
        "symbol": "SH.688001",
        "code": "688001",
        "name": "强趋势样本",
        "chain_id": "semiconductor",
        "chain_name": "半导体产业链",
        "node_id": "material",
        "node_name": "材料",
        "rows": [{"date": "2026-06-29", "change_pct": 20.0, "amount_yi": 30.0, "event": "涨停/强封"}],
    }
    high_amount_same_chain = {
        "symbol": "SH.688146",
        "code": "688146",
        "name": "高成交同链弱化",
        "chain_id": "semiconductor",
        "chain_name": "半导体产业链",
        "node_id": "wafer",
        "node_name": "晶圆制造",
        "rows": [
            {
                "date": "2026-06-29",
                "change_pct": -4.0,
                "amount_yi": 76.0,
                "high": 120,
                "low": 98,
                "close": 113,
                "event": "冲高回落",
            }
        ],
    }
    lower_amount_same_chain = {
        "symbol": "SH.688002",
        "code": "688002",
        "name": "低成交同链弱化",
        "chain_id": "semiconductor",
        "chain_name": "半导体产业链",
        "node_id": "material",
        "node_name": "材料",
        "rows": [
            {
                "date": "2026-06-29",
                "change_pct": -9.0,
                "amount_yi": 18.0,
                "high": 100,
                "low": 80,
                "close": 81,
                "event": "大幅回撤、冲高回落",
            }
        ],
    }
    unrelated = {
        "symbol": "SH.688003",
        "code": "688003",
        "name": "无关弱化",
        "chain_id": "telecom",
        "chain_name": "通信网络/5G产业链",
        "node_id": "optical",
        "node_name": "光模块",
        "rows": [
            {
                "date": "2026-06-29",
                "change_pct": -18.0,
                "amount_yi": 150.0,
                "high": 120,
                "low": 80,
                "close": 82,
                "event": "大幅回撤、冲高回落",
            }
        ],
    }

    pool = _chain_pressure_pool(
        trend_rows,
        {
            "688001": winner_replay,
            "688146": high_amount_same_chain,
            "688002": lower_amount_same_chain,
            "688003": unrelated,
        },
    )

    assert [row["code"] for row in pool] == ["688146", "688002"]


def test_word_renderer_shows_fixed_slices_proxy_label_and_pressure_role_priority():
    text = render_word_style_review(
        {"trade_date": "2026-07-14"},
        {
            "trade_date": "2026-07-14",
            "report_stage": "formal_postmarket",
            "generation_status": "partial",
            "coverage": {"formal_ready": False, "latest_intraday_time": "14:55"},
            "major_indices": [],
            "market_breadth": {"status": "missing"},
            "daily_board_rankings": {
                "rows": [{"name": "元件", "kind": "industry", "change_pct": 8.15, "source": "canonical"}],
                "weak_rows": [],
            },
            "high_turnover_cores": [],
            "flow_availability": {"participant_flow_available": False, "order_size_flow_available": False},
            "dynamic_market_representatives": [
                {
                    "board": "元件",
                    "market_core": [{"code": "300001", "name": "样本股", "change_pct": -5, "amount_yi": 10}],
                    "pressure_core": [{"code": "300001", "name": "样本股", "change_pct": -5, "amount_yi": 10}],
                }
            ],
            "structured_daily_review": {
                "top_turnover_boards": {"status": "partial", "rows": []},
                "trend_20d_boards": {"status": "partial", "rows": [{"name": "元件", "change_20d_pct": 99}]},
                "fixed_time_slices": [
                    {
                        "slice": "午后第一段",
                        "time_range": "13:00-13:30",
                        "actual_range": "13:01-13:30",
                        "market_behavior": "单向增强",
                        "active_direction": {"name": "元件"},
                        "drained_direction": {"name": "银行"},
                        "evidence_level": "confirmed",
                    }
                ],
                "key_stock_pool": {"gainers_top20": [], "limit_pool_counts": {}},
            },
        },
    )

    assert "板块强度代理 TOP7" in text
    assert "固定半小时时间轴" in text
    assert "13:01-13:30" in text
    assert "压力核心" in text
    assert "主线容量/动态核心 | 样本股" not in text
    assert "20日+99.00%" not in text
    assert "最新分钟时点=14:55" in text
