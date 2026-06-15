# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Any

from signals.replay.market_replay import build_market_replay_context, format_market_replay_sections


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows

    def sort(self, spec, direction=None):
        if isinstance(spec, list):
            keys = spec
        else:
            keys = [(spec, direction)]
        rows = self.rows
        for key, order in reversed(keys):
            reverse = order == -1
            rows = sorted(rows, key=lambda item: item.get(key) if item.get(key) is not None else -10**12, reverse=reverse)
        self.rows = rows
        return self

    def limit(self, count: int):
        self.rows = self.rows[:count]
        return self

    def __iter__(self):
        return iter(self.rows)


def _get_path(row: dict[str, Any], dotted: str) -> Any:
    value: Any = row
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _matches(row: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(row, item) for item in expected):
                return False
            continue
        if key == "$and":
            if not all(_matches(row, item) for item in expected):
                return False
            continue
        actual = _get_path(row, key)
        if isinstance(expected, dict):
            if "$gte" in expected and not (actual is not None and actual >= expected["$gte"]):
                return False
            if "$gt" in expected and not (actual is not None and actual > expected["$gt"]):
                return False
            if "$lte" in expected and not (actual is not None and actual <= expected["$lte"]):
                return False
            if "$lt" in expected and not (actual is not None and actual < expected["$lt"]):
                return False
            if "$in" in expected and actual not in expected["$in"]:
                return False
            continue
        if actual != expected:
            return False
    return True


def _project(row: dict[str, Any], projection: dict[str, int] | None) -> dict[str, Any]:
    if not projection:
        return dict(row)
    if not any(include for key, include in projection.items() if key != "_id"):
        return {key: value for key, value in row.items() if projection.get(key, 1)}
    result: dict[str, Any] = {}
    for key, include in projection.items():
        if not include or key == "_id":
            continue
        value = _get_path(row, key)
        if value is not None:
            result[key] = value
    return result


class FakeCollection:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows

    def find(self, query: dict[str, Any], projection: dict[str, int] | None = None):
        return FakeCursor([_project(row, projection) for row in self.rows if _matches(row, query)])

    def find_one(self, query: dict[str, Any], projection: dict[str, int] | None = None, sort=None):
        cursor = self.find(query, projection)
        if sort:
            cursor.sort(sort)
        return cursor.rows[0] if cursor.rows else None


class FakeDB(dict):
    def list_collection_names(self):
        return list(self.keys())


def test_market_replay_context_extracts_event_graph():
    day = "2026-06-05"
    db = FakeDB(
        {
            "fullmarket_spot_snapshots": FakeCollection(
                [
                    {
                        "date_key": day,
                        "trade_date": day,
                        "symbol": "SZ.300308",
                        "code": "300308",
                        "name": "中际旭创",
                        "open": 1273.2,
                        "high": 1301.51,
                        "low": 1160,
                        "close": 1180,
                        "prev_close": 1280,
                        "change_pct": -7.81,
                        "amount": 58324832701.61,
                        "turnover_pct": 4.28,
                    },
                    {
                        "date_key": day,
                        "trade_date": day,
                        "symbol": "SH.600828",
                        "code": "600828",
                        "name": "茂业商业",
                        "open": 4.92,
                        "high": 5.45,
                        "low": 4.84,
                        "close": 5.13,
                        "prev_close": 4.96,
                        "change_pct": 3.64,
                        "amount": 547692684,
                    },
                    {
                        "date_key": day,
                        "trade_date": day,
                        "symbol": "SH.688017",
                        "code": "688017",
                        "name": "绿的谐波",
                        "open": 315,
                        "high": 393,
                        "low": 314.9,
                        "close": 393,
                        "prev_close": 327.5,
                        "change_pct": 20.0,
                        "amount": 7928887040,
                        "turnover_pct": 12.21,
                        "volume_ratio": 3.2,
                    },
                    {
                        "date_key": day,
                        "trade_date": day,
                        "symbol": "SZ.300024",
                        "code": "300024",
                        "name": "机器人新星",
                        "open": 18.2,
                        "high": 21.6,
                        "low": 18.1,
                        "close": 21.6,
                        "prev_close": 18,
                        "change_pct": 20.0,
                        "amount": 1360000000,
                        "turnover_pct": 18.4,
                        "volume_ratio": 8.3,
                    },
                    {
                        "date_key": day,
                        "trade_date": day,
                        "symbol": "SZ.301000",
                        "code": "301000",
                        "name": "N新睿高",
                        "open": 260,
                        "high": 280,
                        "low": 220,
                        "close": 226.88,
                        "prev_close": 18,
                        "change_pct": 200.0,
                        "amount": 1200000000,
                    },
                ]
            ),
            "board_constituents": FakeCollection(
                [
                    {
                        "board_name": "机器人",
                        "concept_name": "机器人",
                        "updated_at": datetime(2026, 6, 5, 15),
                        "symbols": ["300308", "688017"],
                    }
                ]
            ),
            "market_limit_pools": FakeCollection(
                [
                    {
                        "trade_date": day,
                        "pool": "zt",
                        "code": "688017",
                        "name": "绿的谐波",
                        "first_limit_up_time": "130501",
                        "last_limit_up_time": "130501",
                        "open_count": 0,
                        "seal_amount": 120000000,
                        "consecutive_limit_count": 1,
                        "limit_up_stat": "1/1",
                        "volume_ratio": 3.2,
                        "industry": "机器人",
                    },
                    {
                        "trade_date": day,
                        "pool": "zt",
                        "code": "300024",
                        "name": "机器人新星",
                        "first_limit_up_time": "095012",
                        "last_limit_up_time": "095012",
                        "open_count": 0,
                        "seal_amount": 220000000,
                        "consecutive_limit_count": 1,
                        "limit_up_stat": "1/1",
                        "volume_ratio": 8.3,
                        "industry": "机器人",
                    }
                ]
            ),
            "bars": FakeCollection(
                [
                    {
                        "meta": {"symbol": "300308", "freq": "5分钟"},
                        "dt": datetime(2026, 6, 5, 9, 35),
                        "open": 1273.2,
                        "high": 1295.15,
                        "low": 1260.25,
                        "close": 1265.83,
                        "amount": 3568748809,
                    },
                    {
                        "meta": {"symbol": "300308", "freq": "5分钟"},
                        "dt": datetime(2026, 6, 5, 10, 25),
                        "open": 1299.06,
                        "high": 1301.51,
                        "low": 1292,
                        "close": 1295,
                        "amount": 852251933,
                    },
                    {
                        "meta": {"symbol": "300308", "freq": "5分钟"},
                        "dt": datetime(2026, 6, 5, 13, 30),
                        "open": 1220.98,
                        "high": 1221,
                        "low": 1194.13,
                        "close": 1203.04,
                        "amount": 2903146377,
                    },
                ]
            ),
            "board_heat_ticks": FakeCollection(
                [
                    {
                        "source": "eastmoney_push2delay",
                        "kind": "industry",
                        "name": "2026一季报扭亏",
                        "trade_minute": datetime(2026, 6, 5, 9, 35),
                        "change_pct": 8.5,
                        "rank_idx": 0,
                        "leader_name": "噪声样本",
                        "leader_change_pct": 10,
                    },
                    {
                        "source": "eastmoney_push2delay",
                        "kind": "industry",
                        "name": "机器人",
                        "trade_minute": datetime(2026, 6, 5, 9, 35),
                        "change_pct": 0.5,
                        "rank_idx": 166,
                        "leader_name": "鼎智科技",
                        "leader_change_pct": 2.1,
                    },
                    {
                        "source": "eastmoney_push2delay",
                        "kind": "industry",
                        "name": "机器人",
                        "trade_minute": datetime(2026, 6, 5, 14, 58),
                        "change_pct": 6.03,
                        "rank_idx": 2,
                        "leader_name": "绿的谐波",
                        "leader_change_pct": 20,
                    },
                    {
                        "source": "eastmoney_push2delay",
                        "kind": "industry",
                        "name": "其他数字媒体",
                        "trade_minute": datetime(2026, 6, 5, 14, 58),
                        "change_pct": 8.11,
                        "rank_idx": 0,
                        "leader_name": "凡拓数创",
                        "leader_change_pct": 11.05,
                    },
                ]
            ),
            "index_bars": FakeCollection(
                [
                    {
                        "meta": {"symbol": "sh000001", "freq": "日线"},
                        "dt": datetime(2026, 5, 29),
                        "high": 4112.955,
                        "close": 4068.569,
                    },
                    {
                        "meta": {"symbol": "sh000001", "freq": "日线"},
                        "dt": datetime(2026, 6, 1),
                        "high": 4093.041,
                        "close": 4057.74,
                    },
                    {
                        "meta": {"symbol": "sh000001", "freq": "日线"},
                        "dt": datetime(2026, 6, 2),
                        "high": 4089.575,
                        "close": 4075.102,
                    },
                    {
                        "meta": {"symbol": "sh000001", "freq": "日线"},
                        "dt": datetime(2026, 6, 3),
                        "high": 4107.046,
                        "close": 4083.974,
                    },
                    {
                        "meta": {"symbol": "sh000001", "freq": "日线"},
                        "dt": datetime(2026, 6, 4),
                        "high": 4080.718,
                        "close": 4057.781,
                    },
                    {
                        "meta": {"symbol": "sh000001", "freq": "日线"},
                        "dt": datetime(2026, 6, 5),
                        "high": 4078.932,
                        "close": 4027.736,
                    },
                ]
            ),
        }
    )

    context = build_market_replay_context(
        db,
        trade_date=day,
        sector_boards=[
                {
                    "name": "机器人/自动化产业链 · 自动化/机器人",
                    "day_change_pct": 6.03,
                    "source_driver": {"kind": "industry", "name": "机器人"},
                    "representatives": {"core": [{"symbol": "SZ.300308", "name": "中际旭创"}]},
                }
        ],
        checkpoints=["09:35", "14:58"],
        high_turnover_limit=2,
    )

    assert context["high_turnover_cores"][0]["name"] == "中际旭创"
    assert context["high_turnover_cores"][0]["amount_yi"] == 583.25
    assert context["failed_boards"][0]["name"] == "茂业商业"
    assert all(row["name"] != "N新睿高" for row in context["failed_boards"])
    assert context["failed_boards"][0]["failed_from_high_pct"] == 6.24
    assert context["failed_boards"][0]["price_drawdown_pct"] == 5.87
    assert context["board_timeline"][0]["change_delta_from_first"] == 5.53
    assert context["rotation_windows"][-1]["top_boards"][0]["name"] == "其他数字媒体"
    assert all("2026" not in row["name"] for row in context["rotation_windows"][0]["top_boards"])
    assert context["rotation_shifts"][0]["strengthening"][0]["name"] == "机器人"
    assert context["representative_paths"][0]["intraday_path"]["high_bar"]["time"] == "10:25"
    dynamic = context["dynamic_market_representatives"][0]
    assert dynamic["static_representatives"][0]["name"] == "中际旭创"
    assert any(row["name"] == "绿的谐波" for row in dynamic["market_core"])
    assert dynamic["market_elastic"][0]["name"] == "机器人新星"
    assert dynamic["market_elastic"][0]["limit_pool"]["first_limit_up_time"] == "095012"
    assert dynamic["market_elastic"][0]["limit_pool"]["pools"] == ["zt"]
    assert dynamic["market_elastic_confirmed"][0]["name"] == "机器人新星"
    assert dynamic["failed_emotion"] == []
    assert any(row["name"] == "机器人新星" for row in dynamic["market_elastic"])
    assert dynamic["market_elastic"][0]["volume_ratio"] == 8.3
    assert dynamic["pressure_core"][0]["name"] == "中际旭创"
    assert context["stock_event_chains"][0]["name"] == "中际旭创"
    assert "高成交负反馈" in context["stock_event_chains"][0]["labels"]
    assert context["index_cycle"]["pivot_date"] == "2026-05-29"
    assert context["index_cycle"]["trading_days_since"] == 5
    assert context["index_cycle"]["drop_pct"] == -2.07
    assert context["flow_availability"]["participant_flow_available"] is False
    structured = context["structured_daily_review"]
    assert structured["contract_version"] == "stock-daily-review-v2.1"
    assert structured["key_stock_pool"]["top_amount_50"][0]["name"] == "中际旭创"
    assert structured["top_turnover_boards"]["rows"][0]["state"] == "轮动"
    assert structured["trend_20d_boards"]["status"] == "missing"
    assert structured["fixed_time_slices"][0]["slice"] == "竞价"
    assert any(row["item"] == "主力/散户分账户资金" and row["status"] == "missing" for row in structured["data_completeness"])
    assert structured["acceptance_pressure"]["high_turnover_top10"][0]["acceptance_level"] == "天量无承接"
    role_map = context["board_role_map"]
    robot_role = next(row for row in role_map if "机器人" in row["name"])
    assert "主线/前排观察" in robot_role["roles"]
    assert "高成交核心锚" in robot_role["roles"]
    assert "压力锚" in robot_role["roles"]
    assert any("动态核心=" in evidence and "绿的谐波" in evidence for evidence in robot_role["evidence"])
    assert any("压力核心=中际旭创" in evidence for evidence in robot_role["evidence"])
    assert context["analysis_framework"]["ai_native_contract"].startswith("代码层只输出全市场证据图")
    context["external_fund_flows"] = [
        {
            "symbol": "SZ.300308",
            "code": "300308",
            "eastmoney_quote": {
                "source": "eastmoney_quote_stock_get",
                "observed_trade_date": day,
                "main_order": {"buy_yi": 419.45, "sell_yi": 483.26, "net_yi": -63.81},
                "retail_proxy": {
                    "buy_yi": 155.4,
                    "sell_yi": 91.59,
                    "net_yi": 63.81,
                    "basis": "medium_order_flow",
                },
                "amount_coverage_gap_yi": 8.4,
                "order_size_buy_sell_available": True,
                "participant_flow_available": False,
            },
        }
    ]

    sections = format_market_replay_sections(context)
    assert any("东财订单资金口径显示" in section for section in sections)
    assert any("涨幅回吐" in section for section in sections)
    assert "pct" not in "\n".join(sections)
    assert "当日市场认可" in dynamic["selection_note"]
    assert any("方向切换" in section for section in sections)
    assert any("机器人增强" in section for section in sections)
    assert any("午后盘面开始转弱" in section for section in sections)
    assert any("情绪上，今天的问题不在指数" in section for section in sections)
    assert any("周期上，" in section for section in sections)
    assert not any("从高点回落+" in section for section in sections)
    text = "\n".join(sections)
    assert "盘中最正确的策略就是不动" not in text
    assert "给你希望再掐灭" not in text
    assert "冲锋号" not in text
    assert "N/A" not in text
    assert "周五收盘" not in text


def test_market_replay_sections_do_not_leak_june5_theme_template():
    context = {
        "trade_date": "2026-06-04",
        "opening_pressure_boards": [{"name": "半导体材料"}],
        "rotation_windows": [
            {
                "actual_time": "09:35",
                "top_boards": [
                    {"name": "百日新高", "change_pct": 0.05},
                    {"name": "半导体材料", "change_pct": 0.02},
                ],
            },
            {
                "actual_time": "10:30",
                "top_boards": [
                    {"name": "硅料硅片", "change_pct": 4.41},
                    {"name": "电子化学品Ⅲ", "change_pct": 2.86},
                ],
            },
            {
                "actual_time": "14:49",
                "top_boards": [
                    {"name": "硅料硅片", "change_pct": 5.22},
                    {"name": "半导体材料", "change_pct": 3.88},
                ],
            },
        ],
        "rotation_shifts": [
            {
                "from_time": "09:35",
                "to_time": "10:30",
                "strengthening": [{"name": "硅料硅片", "delta_pct": 4.12}],
                "weakening": [{"name": "百日新高", "delta_pct": -0.3}],
            },
            {
                "from_time": "10:30",
                "to_time": "13:30",
                "strengthening": [{"name": "半导体材料", "delta_pct": 2.21}],
                "weakening": [{"name": "焦炭Ⅲ", "delta_pct": -0.34}],
            },
        ],
        "board_timeline": [
            {
                "board": "硅料硅片",
                "driver_name": "硅料硅片",
                "points": [{"time": "09:35", "change_pct": 0.1}],
                "latest": {"time": "14:49", "change_pct": 5.22, "leader_name": "测试硅料"},
                "change_delta_from_first": 5.12,
            },
            {
                "board": "半导体材料",
                "driver_name": "半导体材料",
                "points": [{"time": "09:35", "change_pct": 0.0}],
                "latest": {"time": "14:49", "change_pct": 3.88, "leader_name": "测试材料"},
                "change_delta_from_first": 3.88,
            },
        ],
        "high_turnover_cores": [
            {"name": "兆易创新", "open": 141.0, "high": 153.0, "low": 139.0, "close": 151.0, "change_pct": 7.53, "amount": 29302000000},
            {"name": "工业富联", "open": 81.05, "high": 82.0, "low": 77.5, "close": 78.55, "change_pct": -3.08, "amount": 22120000000},
        ],
        "stock_event_chains": [
            {
                "name": "工业富联",
                "symbol": "SH.601138",
                "open": 81.05,
                "high": 82.0,
                "low": 77.5,
                "close": 78.55,
                "close_change_pct": -3.08,
                "amount_yi": 221.2,
                "labels": ["高成交负反馈", "低开承压"],
                "open_bar": {"open": 81.05},
                "high_bar": {"high": 82.0},
                "low_bar": {"low": 77.5},
                "phrase": "工业富联高82收78.55，单日成交221.2亿，承接转弱",
            }
        ],
        "failed_boards": [
            {"name": "惠丰钻石", "high": 41.0, "close": 37.0, "failed_from_high_pct": 8.1, "price_drawdown_pct": 9.8},
            {"name": "北京文化", "high": 6.6, "close": 6.1, "failed_from_high_pct": 6.2, "price_drawdown_pct": 7.6},
        ],
        "flow_availability": {"participant_flow_available": False},
        "index_cycle": {
            "pivot_date": "2026-05-29",
            "pivot_high": 4112.95,
            "latest_close": 4057.78,
            "drop_pct": -1.34,
            "trading_days_since": 4,
            "latest_weekday": 3,
        },
    }

    text = "\n".join(format_market_replay_sections(context))

    assert "工业富联" in text
    assert "硅料硅片" in text
    assert "科技方向开盘就弱，资金立刻去攻击消费" not in text
    assert "开盘后消费攻击科技撤退" not in text
    assert "9点33分" not in text
    assert "电力板块内部" not in text
    assert "10点半锂电启动" not in text
    assert "下午机器人方向" not in text
    assert "商业航天" not in text


def test_market_replay_sections_summarize_opening_panic_and_partial_tech_rebound():
    context = {
        "structured_daily_review": {
            "top_turnover_boards": {
                "rows": [
                    {
                        "rank": 1,
                        "board": "通信线缆及配套",
                        "change_pct": -1.64,
                        "state": "分歧",
                        "evidence_level": "inferred",
                    },
                    {
                        "rank": 2,
                        "board": "CPO概念",
                        "change_pct": -2.11,
                        "state": "分歧",
                        "evidence_level": "inferred",
                    },
                    {
                        "rank": 3,
                        "board": "半导体材料",
                        "change_pct": -1.27,
                        "state": "分歧",
                        "evidence_level": "inferred",
                    },
                ]
            },
            "fixed_time_slices": [
                {
                    "slice": "开盘段",
                    "time_range": "09:30-10:00",
                    "actual_range": "09:30-10:00",
                    "active_direction": {"name": "其他数字媒体", "delta_pct": 10.28},
                    "drained_direction": {"name": "焦煤", "delta_pct": -1.82},
                    "evidence_level": "confirmed",
                },
                {
                    "slice": "早盘二段",
                    "time_range": "10:00-10:30",
                    "actual_range": "10:00-10:30",
                    "active_direction": {"name": "机器人", "delta_pct": 3.07},
                    "drained_direction": {"name": "图片媒体", "delta_pct": -1.77},
                    "evidence_level": "confirmed",
                },
            ]
        },
        "opening_pressure_boards": [
            {"name": "通信线缆及配套", "change_pct": -6.29},
            {"name": "CPO概念", "change_pct": -5.83},
            {"name": "集成电路制造", "change_pct": -6.13},
            {"name": "印制电路板", "change_pct": -5.84},
            {"name": "半导体材料", "change_pct": -5.8},
        ],
        "rotation_windows": [],
        "rotation_shifts": [
            {
                "from_time": "09:35",
                "to_time": "10:30",
                "strengthening": [
                    {"name": "机器人", "delta_pct": 5.43},
                    {"name": "通信线缆及配套", "delta_pct": 4.65},
                ],
                "weakening": [{"name": "焦炭Ⅲ", "delta_pct": -2.93}],
            },
            {
                "from_time": "13:30",
                "to_time": "14:27",
                "strengthening": [{"name": "半导体设备", "delta_pct": 1.37}],
                "weakening": [{"name": "图片媒体", "delta_pct": -1.31}],
            },
        ],
        "high_turnover_cores": [],
        "stock_event_chains": [],
        "failed_boards": [],
        "flow_availability": {"participant_flow_available": False},
    }

    sections = format_market_replay_sections(context)
    first = sections[0]

    assert "早盘不是普通分化，而是科技链先集中恐慌" in first
    assert "通信线缆及配套-6.29%" in first
    assert "部分科技分支出现抄底反弹" in first
    assert "机器人增强+5.43%" in first
    assert "半导体设备增强+1.37%" in first
    text = "\n".join(sections)
    assert "板块上，先把强方向和压力线分开看" in text
    assert "压力主要在通信线缆及配套" in text
    assert "半导体材料" in text
    assert "明天如果压力线止跌、修复线能扩散" in text
    assert "非主线" not in text


def test_market_replay_turnover_representatives_are_programmatic():
    context = {
        "turnover_representatives": [
            {
                "rank": 3,
                "name": "中天科技",
                "amount_yi": 252.05,
                "change_pct": 7.5103,
                "role": "低开转强/修复锚",
                "chain_name": "通信网络/5G产业链",
                "first_red": {"status": "missing", "note": "缺5分钟路径，不能判定首次翻红时间。"},
            },
            {
                "rank": 9,
                "name": "沪电股份",
                "amount_yi": 155.45,
                "change_pct": 2.8374,
                "role": "低开转强/修复锚",
                "chain_name": "PCB/CCL/服务器材料产业链",
                "first_red": {"status": "confirmed", "first_close_above_time": "10:15", "basis": "5分钟 bars 越过昨收"},
            },
            {
                "rank": 12,
                "name": "工业富联",
                "amount_yi": 140.14,
                "change_pct": -4.8339,
                "role": "高成交压力锚",
                "chain_name": "AI算力/数据中心产业链",
                "first_red": {"status": "not_observed"},
            },
        ],
        "rotation_windows": [],
        "rotation_shifts": [],
        "high_turnover_cores": [],
        "stock_event_chains": [],
        "failed_boards": [],
        "flow_availability": {"participant_flow_available": False},
    }

    text = "\n".join(format_market_replay_sections(context))

    assert "高成交前排分化很明显" in text
    assert "拖累的是工业富联140亿收跌4.83%" in text
    assert "收红的是中天科技252亿收涨7.51%" in text
    assert "沪电股份155亿收涨2.84%" in text
    assert "压力锚=" not in text
    assert "修复/翻红锚=" not in text
    assert "最先翻红" not in text


def test_market_replay_does_not_mix_unrelated_pressure_board_with_cpo_stocks():
    context = {
        "opening_pressure_boards": [{"name": "油气开采", "change_pct": -2.5}],
        "turnover_representatives": [],
        "rotation_windows": [],
        "rotation_shifts": [],
        "high_turnover_cores": [],
        "failed_boards": [],
        "flow_availability": {"participant_flow_available": False},
        "stock_event_chains": [
            {
                "name": "杰瑞股份",
                "labels": ["低开承压", "高成交负反馈"],
                "open": 41.1,
                "prev_close": 42.0,
                "amount_yi": 180.0,
                "close_change_pct": -3.2,
                "phrase": "杰瑞股份低开后承压",
                "limit_pool": {"industry": "油气开采"},
            },
            {
                "name": "新易盛",
                "labels": ["低开承压", "高成交负反馈"],
                "open": 120.0,
                "prev_close": 124.0,
                "amount_yi": 130.0,
                "close_change_pct": -3.5,
                "phrase": "新易盛低开后承压",
                "limit_pool": {"industry": "CPO概念"},
            },
        ],
    }

    text = "\n".join(format_market_replay_sections(context))

    assert "新易盛" in text
    assert "开盘后油气开采方向直接承压。承压个股包括杰瑞股份，新易盛" not in text
    assert "开盘后油气开采方向直接承压—新易盛" not in text
