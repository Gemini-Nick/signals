# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.notify.trading_workbench_summary import build_summary, window_gate


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


def test_window_gate_blocks_weekend_intraday_summary():
    allowed, reason = window_gate("ten", datetime(2026, 5, 9, 9, 45))

    assert allowed is False
    assert reason.startswith("not_a_share_trading_day")


def test_window_gate_allows_trading_window():
    allowed, reason = window_gate("ten", datetime(2026, 5, 8, 9, 45))

    assert allowed is True
    assert reason == "window_open"
