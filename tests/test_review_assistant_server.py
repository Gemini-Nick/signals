# -*- coding: utf-8 -*-
from __future__ import annotations

import json

from signals.mcp import review_assistant_server
from signals.notify.trading_workbench_summary import build_market_replay_wechat_summary


BANNED_BODY_TERMS = (
    "缺失",
    "unknown",
    "unavailable",
    "数据边界",
    "字段缺失",
    "participant_flow",
    "market_replay",
    "signals_context",
)


def _payload() -> dict:
    return {
        "trade_date": "2026-07-03",
        "window": "two",
        "signals_context": {
            "trade_date": "2026-07-03",
            "window": "two",
            "sector_boards": [
                {"name": "机器人/自动化", "change_pct": 4.2},
                {"name": "有色金属/贵金属", "change_pct": 3.8},
                {"name": "军工商业航天", "change_pct": 2.6},
            ],
            "pools": {},
        },
        "market_replay": {
            "trade_date": "2026-07-03",
            "rotation_windows": [
                {"actual_time": "09:35", "top_boards": [{"name": "有色金属/贵金属", "change_pct": 3.4}]},
                {"actual_time": "13:48", "top_boards": [{"name": "机器人/自动化", "change_pct": 4.2}]},
            ],
            "rotation_shifts": [
                {
                    "from_time": "10:30",
                    "to_time": "13:48",
                    "strengthening": [{"name": "机器人/自动化"}],
                    "weakening": [{"name": "纺织轻工"}],
                }
            ],
            "board_timeline": [
                {"board": "机器人/自动化", "latest": {"change_pct": 4.2}},
                {"board": "有色金属/贵金属", "latest": {"change_pct": 3.8}},
            ],
            "dynamic_market_representatives": [
                {
                    "board": "机器人/自动化",
                    "market_core": [{"name": "丰光精密"}, {"name": "绿的谐波"}],
                    "market_elastic_confirmed": [{"name": "埃斯顿"}],
                },
                {
                    "board": "有色金属/贵金属",
                    "market_core": [{"name": "招金黄金"}],
                    "market_elastic_confirmed": [{"name": "赤峰黄金"}],
                },
            ],
            "failed_boards": [{"name": "京东方A"}, {"name": "盛科通信-U"}],
            "high_turnover_cores": [{"name": "中际旭创"}, {"name": "京东方A"}],
            "flow_availability": {
                "participant_flow_available": False,
                "order_size_flow_available": False,
            },
            "structured_daily_review": {
                "acceptance_pressure": {
                    "high_turnover_top10": [{"name": "中际旭创"}],
                }
            },
        },
    }


def test_market_replay_wechat_renderer_uses_market_replay_when_signals_context_lacks_fields():
    payload = _payload()
    result = build_market_replay_wechat_summary(payload, window="two", max_items=5)

    assert result["status"] == "NOTIFY"
    body = result["body"]
    assert "机器人/自动化" in body
    assert "丰光精密" in body
    assert "13:48机器人/自动化仍在前排" in body
    assert "market_replay.rotation_windows" in result["audit"]["used_paths"]
    for term in BANNED_BODY_TERMS:
        assert term not in body


def test_market_replay_wechat_renderer_hides_unavailable_flow_from_body():
    result = build_market_replay_wechat_summary(_payload(), window="two", max_items=5)
    body = result["body"]

    assert "主力" not in body
    assert "散户" not in body
    assert "资金字段缺失" not in body
    assert "market_replay.flow_availability.participant_flow" in result["audit"]["internal_gaps"]


def test_market_replay_wechat_renderer_blocks_when_core_evidence_missing():
    result = build_market_replay_wechat_summary(
        {"trade_date": "2026-07-03", "window": "two", "signals_context": {}, "market_replay": {}},
        window="two",
        max_items=5,
    )

    assert result["status"] == "DONT_NOTIFY"
    assert result["body"] == ""
    assert result["reason"] == "core_market_replay_evidence_missing"


def test_render_market_replay_wechat_body_mcp_tool(monkeypatch):
    monkeypatch.setattr(review_assistant_server, "_collect_market_context", lambda _args: _payload())

    response = review_assistant_server._handle(
        {
            "id": 1,
            "method": "tools/call",
            "params": {"name": "render_market_replay_wechat_body", "arguments": {"window": "two"}},
        }
    )

    assert response is not None
    text = response["result"]["content"][0]["text"]
    result = json.loads(text)
    assert result["status"] == "NOTIFY"
    assert "丰光精密" in result["body"]
    for term in BANNED_BODY_TERMS:
        assert term not in result["body"]
