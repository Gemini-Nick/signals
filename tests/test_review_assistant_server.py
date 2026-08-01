# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from types import SimpleNamespace

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
        "report_stage": "formal_postmarket",
        "coverage": {"formal_ready": True, "reason_codes": []},
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
            "report_stage": "formal_postmarket",
            "coverage": {"formal_ready": True, "reason_codes": []},
            "sector_transitions": {
                "timeline": [
                    {
                        "board": "机器人/自动化",
                        "state_label": "短周期修复",
                        "event_at": "2026-07-03T13:40:00",
                        "next_checks": [{"text": "链主和行业ETF能否同步"}],
                    }
                ],
                "next_checks": [],
            },
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
    assert "13:40机器人/自动化进入短周期修复" in body
    assert "接着看链主和行业ETF能否同步" in body
    assert "market_replay.rotation_windows" in result["audit"]["used_paths"]
    assert "market_replay.sector_transitions.timeline" in result["audit"]["used_paths"]
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
        {
            "trade_date": "2026-07-03",
            "window": "two",
            "report_stage": "close_flash",
            "signals_context": {},
            "market_replay": {},
        },
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


def test_market_replay_tool_exposes_report_stage_contract():
    tool = next(item for item in review_assistant_server._tool_schema() if item["name"] == "get_market_replay_context")
    report_stage = tool["inputSchema"]["properties"]["report_stage"]
    assert report_stage["enum"] == ["close_flash", "formal_postmarket"]
    assert report_stage["default"] == "formal_postmarket"


def test_generate_review_tool_exposes_report_stage_contract():
    tool = next(item for item in review_assistant_server._tool_schema() if item["name"] == "generate_signals_replay_review")
    report_stage = tool["inputSchema"]["properties"]["report_stage"]
    assert report_stage["enum"] == ["close_flash", "formal_postmarket"]
    assert "default" not in report_stage


def test_intraday_market_context_defaults_to_close_flash(monkeypatch):
    day = "2026-07-14"
    captured: dict[str, str] = {}
    fetched = SimpleNamespace(
        dashboard={"daily_brief": {"as_of": day}},
        shell={"watchlist_groups": {}},
        snapshot={"as_of": day},
    )
    monkeypatch.setattr(review_assistant_server, "fetch_inputs_safe", lambda *_args, **_kwargs: fetched)
    monkeypatch.setattr(review_assistant_server, "get_db", lambda: object())
    monkeypatch.setattr(review_assistant_server, "collect_replay_context", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(review_assistant_server, "fetch_market_event_lines", lambda *_args, **_kwargs: [])

    def build_context(*_args, **kwargs):
        captured["report_stage"] = kwargs["report_stage"]
        return {"trade_date": day, "coverage": {"formal_ready": False}, "generation_status": "success"}

    monkeypatch.setattr(review_assistant_server, "build_market_replay_context", build_context)

    result = review_assistant_server._collect_market_context({"window": "two"})

    assert captured["report_stage"] == "close_flash"
    assert result["report_stage"] == "close_flash"


def test_generate_review_blocks_formal_fallback_when_coverage_is_partial(monkeypatch):
    day = "2026-07-14"
    monkeypatch.setattr(
        review_assistant_server,
        "_collect_context",
        lambda _args: {
            "trade_date": day,
            "window": "postmarket",
            "max_items": 5,
            "_inputs": ({"daily_brief": {"as_of": day}}, {"watchlist_groups": {}}, {"as_of": day}),
        },
    )
    monkeypatch.setattr(review_assistant_server, "get_db", lambda: object())
    monkeypatch.setattr(
        review_assistant_server,
        "build_market_replay_context",
        lambda *_args, **_kwargs: {
            "trade_date": day,
            "coverage": {"formal_ready": False, "reason_codes": ["index_daily_provisional"]},
        },
    )
    text = review_assistant_server._generate_review({"trade_date": day, "report_stage": "formal_postmarket"})

    assert text.startswith("DONT_NOTIFY\nA股午后观察")


def test_formal_postmarket_renderer_blocks_when_close_is_not_ready():
    payload = _payload()
    payload["report_stage"] = "formal_postmarket"
    payload["coverage"] = {"formal_ready": False, "reason_codes": ["index_daily_provisional"]}
    payload["market_replay"]["report_stage"] = "formal_postmarket"
    payload["market_replay"]["coverage"] = payload["coverage"]

    result = build_market_replay_wechat_summary(payload, window="postmarket", max_items=5)

    assert result["status"] == "DONT_NOTIFY"
    assert result["body"] == ""
    assert result["reason"] == "formal_postmarket_not_ready"


def test_close_flash_renderer_uses_observation_title():
    payload = _payload()
    payload["report_stage"] = "close_flash"
    payload["coverage"] = {"formal_ready": False, "reason_codes": ["index_daily_provisional"]}
    payload["market_replay"]["report_stage"] = "close_flash"
    payload["market_replay"]["coverage"] = payload["coverage"]

    result = build_market_replay_wechat_summary(payload, window="close", max_items=5)

    assert result["status"] == "NOTIFY"
    assert "A股午后观察" in result["body"]


def test_market_replay_tool_exposes_backward_compatible_markets_contract():
    tool = next(item for item in review_assistant_server._tool_schema() if item["name"] == "get_market_replay_context")
    markets = tool["inputSchema"]["properties"]["markets"]

    assert markets["default"] == ["A"]
    assert markets["items"]["enum"] == ["A", "HK", "US", "KR"]
    assert "explicit-only external context" in markets["description"]


def test_kr_is_explicit_only_external_context_and_does_not_change_a_replay(monkeypatch):
    day = "2026-07-29"
    monkeypatch.delenv("SECTOR_TRANSITION_KR_CONTEXT_ENABLED", raising=False)
    fetched = SimpleNamespace(
        dashboard={"daily_brief": {"as_of": day}},
        shell={"watchlist_groups": {}},
        snapshot={"as_of": day},
    )
    a_replay = {
        "trade_date": day,
        "coverage": {"formal_ready": True, "official_close_as_of": f"{day}T15:00:00"},
        "generation_status": "success",
        "sector_transitions": {"timeline": [], "states": [], "next_checks": []},
    }
    monkeypatch.setattr(review_assistant_server, "fetch_inputs_safe", lambda *_args, **_kwargs: fetched)
    monkeypatch.setattr(review_assistant_server, "get_db", lambda: object())
    monkeypatch.setattr(review_assistant_server, "build_market_replay_context", lambda *_args, **_kwargs: a_replay)
    monkeypatch.setattr(review_assistant_server, "collect_replay_context", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(review_assistant_server, "latest_market_snapshot", lambda *_args, **_kwargs: None)

    default_result = review_assistant_server._collect_market_context({"trade_date": day})
    kr_result = review_assistant_server._collect_market_context({"trade_date": day, "markets": ["KR"]})

    assert "global_markets" not in default_result
    assert default_result["market_replay"] is a_replay
    assert kr_result["market_replay"] is a_replay
    assert kr_result["requested_markets"] == ["KR"]
    assert kr_result["global_markets"] == [
        {
            "market": "KR",
            "timezone": "Asia/Seoul",
            "currency": "KRW",
            "coverage_scope": "core_universe",
            "enabled_by_default": False,
            "session_date": None,
            "as_of": None,
            "session_state": "unavailable",
            "feature_status": "disabled",
            "disabled_reason": "SECTOR_TRANSITION_KR_CONTEXT_ENABLED=false",
            "source": "feature_gate",
            "context_role": "external_context_only",
        }
    ]

    monkeypatch.setenv("SECTOR_TRANSITION_KR_CONTEXT_ENABLED", "true")
    monkeypatch.setattr(
        review_assistant_server,
        "latest_market_snapshot",
        lambda *_args, **_kwargs: {
            "market": "KR",
            "timezone": "Asia/Seoul",
            "currency": "KRW",
            "coverage_scope": "core_universe",
            "session_date": "2026-07-29",
            "session_state": "complete",
            "source": "market_daily_snapshots",
        },
    )
    enabled_result = review_assistant_server._collect_market_context({"trade_date": day, "markets": ["KR"]})

    assert enabled_result["market_replay"] is a_replay
    assert enabled_result["global_markets"][0]["session_state"] == "complete"
    assert enabled_result["global_markets"][0]["enabled_by_default"] is False
    assert enabled_result["global_markets"][0]["context_role"] == "external_context_only"
