# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from signals.notify.trading_workbench_summary import (
    build_summary,
    window_gate,
    _breakpoint_watch_lines,
    _board_heat_event_lines_from_docs,
    _limit_contexts_to_window,
    _market_event_lines,
)


def _dashboard():
    return {
        "status": "healthy",
        "daily_brief": {
            "as_of": "2026-05-08",
            "market_line": "偏进攻",
            "primary_theme": "军工装备产业链",
        },
        "source_confidence": {
            "overall": 0.77,
            "sources": [
                {"name": "quote", "freshness": "fresh"},
                {"name": "terminal_pool", "freshness": "fresh"},
            ],
        },
        "connector_health": [{"connector_id": "mongodb", "status": "ok"}],
    }


def _snapshot():
    return {
        "as_of": "2026-05-08",
        "market_regime": {
            "label": "偏进攻",
            "primary_theme": "军工装备产业链",
            "confidence": 0.77,
        },
    }


def _shell():
    return {
        "market": {
            "overall_direction": "分化",
            "recommended_style": "均衡",
            "position_suggestion": "3成底仓+1成进攻+6成现金",
        },
        "decision_queue": [
            {
                "decision_id": "focus:SH.600127",
                "symbol": "SH.600127",
                "name": "金健米业",
                "queue_lane": "entry_waiting_confirm",
                "trader_action": "低吸进攻复核",
                "entry_logic_summary": "30m一买，5m/15m右侧确认",
                "invalidates_when": "5m/15m无法确认或上级周期转弱",
                "primary_chain": "农业养殖产业链",
                "rank_score": 279.2,
            }
        ],
        "watchlist_groups": {
            "focus_stocks": [],
            "watch_stocks": [
                {
                    "symbol": "SH.688017",
                    "name": "绿的谐波",
                    "queue_lane": "watch_preheat",
                    "trader_action": "盯盘复核",
                    "entry_logic_summary": "5分钟趋势买，等待日/周背景确认",
                    "invalidates_when": "跌破信号触发价",
                    "primary_chain": "机器人/自动化产业链",
                    "rank_score": 160.0,
                }
            ],
            "risk_stocks": [
                {
                    "symbol": "SZ.002829",
                    "name": "星网宇达",
                    "queue_lane": "risk_exit_first",
                    "trader_action": "暂不参与",
                    "entry_logic_summary": "有卖点或冲突，先排雷",
                    "invalidates_when": "卖点解除并重新走出买点",
                    "primary_chain": "军工装备产业链",
                    "rank_score": 190.0,
                }
            ],
        },
        "buy_candidates": [],
    }


def test_trading_workbench_summary_uses_trader_language():
    result = build_summary(_dashboard(), _shell(), _snapshot(), window="ten")

    assert result.notify is True
    assert result.status == "NOTIFY"
    assert "Signals 工作台" in result.text
    assert "金健米业 SH.600127" in result.text
    assert "动作：低吸进攻复核" in result.text
    assert "触发：30m一买，5m/15m右侧确认" in result.text
    assert "放弃：5m/15m无法确认或上级周期转弱" in result.text
    assert "接下来15分钟打开 AgentOS 买点池和策略图" in result.text
    assert "Mongo" not in result.text
    assert "runtime" not in result.text


def test_trading_workbench_summary_dont_notify_when_no_actionable_rows():
    shell = _shell()
    shell["decision_queue"] = []
    shell["watchlist_groups"]["focus_stocks"] = []

    result = build_summary(_dashboard(), shell, _snapshot(), window="ten")

    assert result.notify is False
    assert result.status == "DONT_NOTIFY"
    assert "结论：只观察，不打断" in result.text


def test_trading_workbench_summary_keeps_market_event_lines():
    result = build_summary(
        _dashboard(),
        _shell(),
        _snapshot(),
        window="close",
        event_lines=["上证14:10低点4068.80杀破4070，按恐慌测试处理"],
    )

    assert "关键盘面事件：" in result.text
    assert "杀破4070" in result.text


def test_market_event_lines_trigger_notify_without_stock_candidates():
    shell = _shell()
    shell["decision_queue"] = []
    shell["watchlist_groups"]["focus_stocks"] = []

    result = build_summary(
        _dashboard(),
        shell,
        _snapshot(),
        window="ten",
        event_lines=["创业板10:30首次按点位超过上证，成长强于权重"],
    )

    assert result.notify is True
    assert result.status == "NOTIFY"
    assert result.reason == "market_event_lines"


def test_market_event_lines_detects_index_anomalies():
    def ts(hour: int, minute: int) -> int:
        return int(datetime(2026, 5, 27, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())

    def payload(label: str, rows: list[dict]) -> dict:
        return {
            "target": {"label": label, "market_timezone": "Asia/Shanghai"},
            "chart": {"ohlcv": rows},
        }

    contexts = {
        "上证指数": payload(
            "上证指数",
            [
                {"time": ts(9, 35), "low": 4101.0, "close": 4102.0},
                {"time": ts(10, 30), "low": 4075.0, "close": 4080.0},
                {"time": ts(14, 10), "low": 4068.8, "close": 4072.0},
            ],
        ),
        "创业板指": payload(
            "创业板指",
            [
                {"time": ts(9, 35), "low": 4098.0, "close": 4099.0},
                {"time": ts(10, 30), "low": 4082.0, "close": 4083.0},
                {"time": ts(14, 10), "low": 4062.0, "close": 4070.0},
            ],
        ),
    }
    contexts["上证指数"]["summary"] = {
        "key_levels": [{"name": "20周线", "value": 4069.21}]
    }

    lines = _market_event_lines(contexts)

    assert any("杀破4070" in line for line in lines)
    assert any("创业板10:30" in line and "首次按点位超过上证" in line for line in lines)
    assert any("未维持" in line for line in lines)


def test_breakpoint_watch_lines_frame_ten_oclock_confirmation():
    def ts(hour: int, minute: int) -> int:
        return int(datetime(2026, 5, 27, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())

    contexts = {
        "上证指数": {
            "target": {"label": "上证指数", "market_timezone": "Asia/Shanghai"},
            "summary": {"key_levels": [{"name": "20周线", "value": 4069.21}]},
            "chart": {
                "ohlcv": [
                    {"time": ts(9, 35), "low": 4072.0, "close": 4080.0},
                    {"time": ts(9, 45), "low": 4071.0, "close": 4082.0},
                ]
            },
        },
        "创业板指": {
            "target": {"label": "创业板指", "market_timezone": "Asia/Shanghai"},
            "chart": {
                "ohlcv": [
                    {"time": ts(9, 35), "low": 4075.0, "close": 4078.0},
                    {"time": ts(9, 45), "low": 4088.0, "close": 4090.0},
                ]
            },
        },
    }

    lines = _breakpoint_watch_lines(contexts, "ten")

    assert any("9:45变盘前" in line and "10:00前" in line for line in lines)
    assert any("创业板4090.00 vs 上证4082.00" in line for line in lines)


def test_ten_window_does_not_look_past_945_when_replayed_later():
    def ts(hour: int, minute: int) -> int:
        return int(datetime(2026, 5, 27, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())

    def payload(rows: list[dict]) -> dict:
        return {
            "target": {"market_timezone": "Asia/Shanghai"},
            "chart": {"ohlcv": rows},
        }

    contexts = {
        "上证指数": payload(
            [
                {"time": ts(9, 45), "low": 4080.0, "close": 4081.0},
                {"time": ts(10, 30), "low": 4077.0, "close": 4130.0},
            ]
        ),
        "创业板指": payload(
            [
                {"time": ts(9, 45), "low": 4078.0, "close": 4079.0},
                {"time": ts(10, 30), "low": 4131.0, "close": 4136.0},
            ]
        ),
    }

    limited = _limit_contexts_to_window(contexts, "ten")
    lines = _market_event_lines(limited)

    assert not any("10:30" in line for line in lines)


def test_breakpoint_watch_lines_frame_two_oclock_confirmation():
    def ts(hour: int, minute: int) -> int:
        return int(datetime(2026, 5, 27, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())

    contexts = {
        "上证指数": {
            "target": {"label": "上证指数", "market_timezone": "Asia/Shanghai"},
            "summary": {"key_levels": [{"name": "20周线", "value": 4069.21}]},
            "chart": {
                "ohlcv": [
                    {"time": ts(11, 25), "low": 4080.0, "close": 4085.0},
                    {"time": ts(13, 45), "low": 4068.0, "close": 4071.0},
                ]
            },
        },
        "创业板指": {
            "target": {"label": "创业板指", "market_timezone": "Asia/Shanghai"},
            "chart": {
                "ohlcv": [
                    {"time": ts(11, 25), "low": 4070.0, "close": 4074.0},
                    {"time": ts(13, 45), "low": 4074.0, "close": 4078.0},
                ]
            },
        },
    }

    lines = _breakpoint_watch_lines(contexts, "two")

    assert any("13:45变盘前" in line and "14:00后" in line for line in lines)
    assert any("已破4070" in line for line in lines)


def test_board_heat_event_lines_detects_generic_afternoon_reversal():
    def t(hour: int, minute: int) -> datetime:
        return datetime(2026, 5, 27, hour, minute)

    latest_docs = [
        {"kind": "industry", "name": "白酒Ⅱ", "change_pct": 2.96, "leader_name": "水井坊"},
        {"kind": "industry", "name": "超市", "change_pct": 4.47, "leader_name": "步步高"},
    ]
    morning_docs = [
        {"kind": "industry", "name": "白酒Ⅱ", "change_pct": -0.31},
        {"kind": "industry", "name": "超市", "change_pct": 3.31},
    ]
    pm_docs = [
        {"kind": "industry", "name": "白酒Ⅱ", "change_pct": 5.2, "trade_minute": t(13, 45), "leader_name": "水井坊"},
        {"kind": "industry", "name": "白酒Ⅲ", "change_pct": 5.2, "trade_minute": t(13, 45), "leader_name": "水井坊"},
        {"kind": "industry", "name": "超市", "change_pct": 5.53, "trade_minute": t(13, 51), "leader_name": "步步高"},
    ]

    lines = _board_heat_event_lines_from_docs(latest_docs, morning_docs, pm_docs)

    assert lines[0].startswith("白酒午后异动")
    assert len([line for line in lines if line.startswith("白酒")]) == 1


def test_window_gate_blocks_weekend_intraday_summary():
    allowed, reason = window_gate("ten", datetime(2026, 5, 9, 9, 45))

    assert allowed is False
    assert reason.startswith("not_a_share_trading_day")


def test_window_gate_allows_trading_window():
    allowed, reason = window_gate("ten", datetime(2026, 5, 8, 9, 45))

    assert allowed is True
    assert reason == "window_open"
