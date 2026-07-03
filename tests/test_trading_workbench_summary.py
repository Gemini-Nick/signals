# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from signals.notify.trading_workbench_summary import (
    InputFetchResult,
    build_summary,
    build_narrative_review,
    build_wechat_summary,
    collect_replay_context,
    fetch_inputs_safe,
    main,
    window_gate,
    _breakpoint_watch_lines,
    _board_heat_event_lines_from_docs,
    _index_kill_line,
    _limit_contexts_to_window,
    _market_event_lines,
    _send_body,
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


def test_index_kill_line_reads_major_indices_from_watchlist_groups():
    shell = {
        "watchlist_groups": {
            "major_indices": [
                {"name": "上证指数", "day_change_pct": -1.6982},
                {"name": "深证成指", "day_change_pct": -3.2225},
                {"name": "创业板指", "day_change_pct": -3.6926},
                {"name": "科创50", "day_change_pct": -4.3013},
            ]
        }
    }

    assert _index_kill_line(shell) == "上证跌1.70%，深成指跌3.22%，创业板跌3.69%，科创50跌4.30%"


def test_index_kill_line_keeps_positive_kechuang50_readable():
    shell = {
        "watchlist_groups": {
            "major_indices": [
                {"name": "上证指数", "day_change_pct": 1.125},
                {"name": "深证成指", "day_change_pct": 0.812},
                {"name": "创业板指", "day_change_pct": 0.551},
                {"name": "科创50", "day_change_pct": 0.056},
            ]
        }
    }

    line = _index_kill_line(shell)

    assert line == "上证涨1.12%，深成指涨0.81%，创业板涨0.55%，科创50涨0.06%"
    assert "科创500.06%" not in line


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


def _june5_shell():
    shell = _shell()
    shell["indices"] = [
        {"name": "深证成指", "day_change_pct": -2.2148},
        {"name": "上证指数", "day_change_pct": -0.7403},
        {"name": "创业板指", "day_change_pct": -3.2023},
        {"name": "科创50", "day_change_pct": -4.0119},
    ]
    shell["watchlist_groups"]["sector_boards"] = [
        {
            "name": "机器人/自动化产业链 · 自动化/机器人",
            "day_change_pct": 6.03,
            "trader_action": "产业链确认：链主/弹性跟随，复核扩散延续",
            "source_driver": {"kind": "industry", "name": "机器人", "change_pct": 6.03},
        },
        {
            "name": "军工装备产业链 · 商业航天/卫星互联网",
            "day_change_pct": 5.31,
            "trader_action": "产业链确认：链主/弹性跟随，复核扩散延续",
            "source_driver": {"kind": "industry", "name": "航天装备Ⅲ", "change_pct": 5.31},
        },
        {
            "name": "传媒旅游产业链 · 游戏/影视/文旅",
            "day_change_pct": 8.11,
            "trader_action": "源强链弱：行业其他数字媒体在涨，等链主确认后再当主线",
        },
    ]
    return shell


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


def test_narrative_review_uses_sector_board_without_date_hardcoding():
    dashboard = _dashboard()
    dashboard["daily_brief"]["as_of"] = "2026-06-05"
    dashboard["daily_brief"]["primary_theme"] = "机器人/自动化产业链"
    snapshot = _snapshot()
    snapshot["as_of"] = "2026-06-05"

    result = build_narrative_review(
        dashboard,
        _june5_shell(),
        snapshot,
        window="postmarket",
        max_items=5,
    )

    assert result.status == "NOTIFY"
    assert result.text.splitlines()[1] == "2026年6月5日复盘"
    assert "[Signals 复盘助手" not in result.text
    assert "指数收红，但盘面不是普涨" in result.text
    assert "上证跌0.74%" in result.text
    assert "创业板跌3.20%" in result.text
    assert "强度主要在机器人和航天装备" in result.text
    assert "受伤主线" not in result.text
    assert "产业链确认的方向主要是：机器人/自动化产业链/自动化/机器人、军工装备产业链/商业航天/卫星互联网" in result.text
    assert "没有传入可验证的分钟级转折线" in result.text
    assert "产业链确认" in result.text
    assert "明天只盯三点" in result.text
    assert "三池数量=板块" not in result.text
    assert "不再单独写没有证据支撑的方向判断" not in result.text
    assert "中际旭创" not in result.text
    assert "9点33分" not in result.text
    assert "谁能在竞价阶段就赢出来" not in result.text
    assert "Mongo" not in result.text
    assert "runtime" not in result.text


def test_narrative_cli_trade_date_uses_historical_replay_context(monkeypatch, capsys):
    dashboard = _dashboard()
    dashboard["daily_brief"]["as_of"] = "2026-06-05"
    dashboard["daily_brief"]["primary_theme"] = "机器人/自动化产业链"
    shell = _june5_shell()
    snapshot = _snapshot()
    snapshot["as_of"] = "2026-06-05"
    seen: dict[str, str] = {}

    def fake_fetch_inputs(base_url: str):
        return dashboard, shell, snapshot

    def fake_build_market_replay_context(db, *, trade_date: str, **kwargs):
        seen["trade_date"] = trade_date
        return {
            "trade_date": trade_date,
            "board_timeline": [
                {
                    "driver_name": "硅料硅片",
                    "kind": "industry",
                    "latest": {"change_pct": 5.22},
                },
                {
                    "driver_name": "半导体材料",
                    "kind": "industry",
                    "latest": {"change_pct": 3.88},
                },
            ],
            "rotation_windows": [],
        }

    monkeypatch.setattr("signals.notify.trading_workbench_summary.fetch_inputs", fake_fetch_inputs)
    monkeypatch.setattr("signals.sync.db.get_db", lambda: object())
    monkeypatch.setattr("signals.replay.market_replay.build_market_replay_context", fake_build_market_replay_context)
    monkeypatch.setattr(
        "signals.replay.market_replay.format_market_replay_sections",
        lambda context: ["6月4日证据段：硅料硅片增强，工业富联承压。"],
    )

    exit_code = main(
        [
            "--window",
            "postmarket",
            "--format",
            "narrative",
            "--trade-date",
            "2026-06-04",
            "--ignore-time",
            "--allow-ignore-time-notify",
        ]
    )

    output = capsys.readouterr().out

    assert exit_code == 0
    assert seen["trade_date"] == "2026-06-04"
    assert output.startswith("NOTIFY\n2026年6月4日复盘")
    assert "硅料硅片" in output
    assert "工业富联承压" in output
    assert "机器人和航天装备" not in output
    assert "上证跌0.74%" not in output


def test_narrative_cli_historical_trade_date_clears_stale_live_inputs(monkeypatch, capsys):
    dashboard = _dashboard()
    dashboard["daily_brief"]["as_of"] = "2026-06-05"
    dashboard["daily_brief"]["primary_theme"] = "机器人/自动化产业链"
    shell = _june5_shell()
    snapshot = _snapshot()
    snapshot["as_of"] = "2026-06-05"

    def fake_fetch_inputs(base_url: str):
        return dashboard, shell, snapshot

    monkeypatch.setattr("signals.notify.trading_workbench_summary.fetch_inputs", fake_fetch_inputs)
    monkeypatch.setattr("signals.sync.db.get_db", lambda: object())
    monkeypatch.setattr(
        "signals.replay.market_replay.build_market_replay_context",
        lambda db, *, trade_date, **kwargs: {"trade_date": trade_date, "board_timeline": [], "rotation_windows": []},
    )
    monkeypatch.setattr("signals.replay.market_replay.format_market_replay_sections", lambda context: [])

    exit_code = main(
        [
            "--window",
            "postmarket",
            "--format",
            "narrative",
            "--trade-date",
            "2026-06-04",
            "--ignore-time",
            "--allow-ignore-time-notify",
        ]
    )

    output = capsys.readouterr().out

    assert exit_code == 0
    assert output.startswith("DONT_NOTIFY\n2026年6月4日复盘")
    assert "历史回放证据不足" in output
    assert "指数结构缺少可直接引用的涨跌幅" in output
    assert "指数收红" not in output
    assert "机器人和航天装备" not in output
    assert "上证跌0.74%" not in output
    assert "绿的谐波" not in output


def test_narrative_cli_same_trade_date_keeps_live_sector_and_indices(monkeypatch, capsys):
    dashboard = _dashboard()
    dashboard["daily_brief"]["as_of"] = "2026-06-05"
    dashboard["daily_brief"]["primary_theme"] = "机器人/自动化产业链"
    shell = _june5_shell()
    snapshot = _snapshot()
    snapshot["as_of"] = "2026-06-05"
    seen: dict[str, int | str] = {}

    def fake_fetch_inputs(base_url: str):
        return dashboard, shell, snapshot

    def fake_build_market_replay_context(db, *, trade_date: str, sector_boards: list[dict], **kwargs):
        seen["trade_date"] = trade_date
        seen["sector_boards_count"] = len(sector_boards)
        return {
            "trade_date": trade_date,
            "board_timeline": [
                {
                    "driver_name": "硅料硅片",
                    "kind": "industry",
                    "latest": {"change_pct": 5.22},
                }
            ],
            "rotation_windows": [],
        }

    monkeypatch.setattr("signals.notify.trading_workbench_summary.fetch_inputs", fake_fetch_inputs)
    monkeypatch.setattr("signals.sync.db.get_db", lambda: object())
    monkeypatch.setattr("signals.replay.market_replay.build_market_replay_context", fake_build_market_replay_context)
    monkeypatch.setattr(
        "signals.replay.market_replay.format_market_replay_sections",
        lambda context: ["6月5日证据段：中际旭创高成交承压。"],
    )

    exit_code = main(
        [
            "--window",
            "postmarket",
            "--format",
            "narrative",
            "--trade-date",
            "2026-06-05",
            "--ignore-time",
            "--allow-ignore-time-notify",
        ]
    )

    output = capsys.readouterr().out

    assert exit_code == 0
    assert seen["trade_date"] == "2026-06-05"
    assert seen["sector_boards_count"] == 3
    assert output.startswith("NOTIFY\n2026年6月5日复盘")
    assert "机器人和航天装备" in output
    assert "上证跌0.74%" in output


def test_word_cli_trade_date_renders_word_style_from_replay_context(monkeypatch, capsys):
    dashboard = _dashboard()
    dashboard["daily_brief"]["as_of"] = "2026-06-26"
    shell = _june5_shell()
    snapshot = _snapshot()
    snapshot["as_of"] = "2026-06-26"
    seen: dict[str, str] = {}

    def fake_fetch_inputs(base_url: str):
        return dashboard, shell, snapshot

    def fake_build_market_replay_context(db, *, trade_date: str, **kwargs):
        seen["trade_date"] = trade_date
        return {
            "trade_date": trade_date,
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
                ],
                "key_stock_pool": {"gainers_top20": [], "limit_pool_counts": {}},
            },
            "flow_availability": {"participant_flow_available": False},
        }

    monkeypatch.setattr("signals.notify.trading_workbench_summary.fetch_inputs", fake_fetch_inputs)
    monkeypatch.setattr("signals.sync.db.get_db", lambda: object())
    monkeypatch.setattr("signals.replay.market_replay.build_market_replay_context", fake_build_market_replay_context)

    exit_code = main(
        [
            "--window",
            "postmarket",
            "--format",
            "word",
            "--trade-date",
            "2026-06-29",
            "--ignore-time",
            "--allow-ignore-time-notify",
        ]
    )

    output = capsys.readouterr().out

    assert exit_code == 0
    assert seen["trade_date"] == "2026-06-29"
    assert output.startswith("NOTIFY\n6月29日A股盘后复盘报告（Signals组装版）")
    assert "A股盘后复盘报告 | 2026年6月29日（周一）" in output
    assert "4073.9" in output
    assert "生物制品" in output
    assert "兆易创新" in output
    assert "极度分化，科创单骑救主" not in output


def test_fetch_inputs_safe_returns_partial_inputs_when_snapshot_fails(monkeypatch):
    def fake_fetch_json(base_url: str, path: str, *, timeout: float = 8.0):
        if path == "/api/strategy/snapshot":
            raise TimeoutError("timed out")
        return {"path": path, "timeout": timeout}

    monkeypatch.setattr("signals.notify.trading_workbench_summary.fetch_json", fake_fetch_json)

    result = fetch_inputs_safe("http://example.test", timeout=1.0)

    assert result.dashboard["path"] == "/api/pack/dashboard"
    assert result.shell["path"] == "/api/workbench/shell"
    assert result.snapshot == {}
    assert "snapshot" in result.errors
    assert "timed out" in result.errors["snapshot"]


def test_narrative_cli_safe_inputs_notifies_from_replay_when_snapshot_times_out(monkeypatch, capsys):
    dashboard = _dashboard()
    dashboard["daily_brief"]["as_of"] = "2026-06-05"
    dashboard["daily_brief"]["primary_theme"] = "科技高成交链"

    def fake_fetch_inputs_safe(base_url: str, *, timeout: float):
        return InputFetchResult(
            dashboard=dashboard,
            shell={"watchlist_groups": {}},
            snapshot={},
            errors={"snapshot": "TimeoutError: timed out"},
        )

    monkeypatch.setattr("signals.notify.trading_workbench_summary.fetch_inputs_safe", fake_fetch_inputs_safe)
    monkeypatch.setattr(
        "signals.notify.trading_workbench_summary.fetch_inputs",
        lambda base_url: (_ for _ in ()).throw(AssertionError("unsafe fetch should not run")),
    )
    monkeypatch.setattr("signals.sync.db.get_db", lambda: object())
    monkeypatch.setattr(
        "signals.replay.market_replay.build_market_replay_context",
        lambda db, **kwargs: {"trade_date": kwargs["trade_date"]},
    )
    monkeypatch.setattr(
        "signals.replay.market_replay.format_market_replay_sections",
        lambda context: ["科技链先集中恐慌，之后中天科技最先翻红，工业富联承接修复。"],
    )

    exit_code = main(
        [
            "--window",
            "postmarket",
            "--format",
            "narrative",
            "--safe-inputs",
            "--input-timeout",
            "0.1",
            "--ignore-time",
            "--allow-ignore-time-notify",
        ]
    )

    output = capsys.readouterr().out

    assert exit_code == 0
    assert output.startswith("NOTIFY\n2026年6月5日复盘")
    assert "科技高成交的体感偏分歧" in output
    assert "中天科技最先翻红" in output


def test_narrative_cli_safe_inputs_emits_gate_when_all_inputs_fail(monkeypatch, capsys):
    def fake_fetch_inputs_safe(base_url: str, *, timeout: float):
        return InputFetchResult(
            dashboard={},
            shell={},
            snapshot={},
            errors={
                "dashboard": "TimeoutError: timed out",
                "shell": "TimeoutError: timed out",
                "snapshot": "TimeoutError: timed out",
            },
        )

    monkeypatch.setattr("signals.notify.trading_workbench_summary.fetch_inputs_safe", fake_fetch_inputs_safe)

    exit_code = main(
        [
            "--window",
            "postmarket",
            "--format",
            "narrative",
            "--safe-inputs",
            "--input-timeout",
            "0.1",
            "--ignore-time",
        ]
    )

    output = capsys.readouterr().out

    assert exit_code == 0
    assert output.startswith("DONT_NOTIFY\n[Signals 工作台 | 20:30 盘后复盘]\n原因：input_unavailable:")
    assert "dashboard=TimeoutError" in output
    assert "snapshot=TimeoutError" in output


def test_send_body_strips_notify_gate_for_wechat_delivery():
    assert _send_body("NOTIFY\n正文") == "正文"
    assert _send_body("DONT_NOTIFY\n原因") == "原因"
    assert _send_body("正文") == "正文"


def test_ignore_time_dry_run_blocks_send_even_with_send_all(monkeypatch, capsys):
    sent: list[str] = []

    def fake_fetch_inputs(base_url: str):
        dashboard = _dashboard()
        dashboard["daily_brief"]["as_of"] = "2026-06-08"
        snapshot = _snapshot()
        snapshot["as_of"] = "2026-06-08"
        return dashboard, _june5_shell(), snapshot

    monkeypatch.setattr(
        "signals.notify.trading_workbench_summary.window_gate",
        lambda window: (False, "outside_window:postmarket:13:33"),
    )
    monkeypatch.setattr("signals.notify.trading_workbench_summary.fetch_inputs", fake_fetch_inputs)
    monkeypatch.setattr("signals.sync.db.get_db", lambda: object())
    monkeypatch.setattr(
        "signals.replay.market_replay.build_market_replay_context",
        lambda db, **kwargs: {"trade_date": kwargs["trade_date"]},
    )
    monkeypatch.setattr(
        "signals.replay.market_replay.format_market_replay_sections",
        lambda context: [" dry-run evidence body"],
    )
    monkeypatch.setattr("signals.notify.send_text", lambda text: sent.append(text))

    exit_code = main(
        [
            "--window",
            "postmarket",
            "--format",
            "narrative",
            "--ignore-time",
            "--send",
            "--send-all",
        ]
    )

    output = capsys.readouterr().out

    assert exit_code == 0
    assert output.startswith("DONT_NOTIFY\n[Signals 工作台 | 20:30 盘后复盘]\n原因：dry_run:outside_window:postmarket:13:33")
    assert "dry-run evidence body" in output
    assert sent == []


def test_training_sample_cli_does_not_notify_by_default(capsys):
    exit_code = main(
        [
            "--window",
            "postmarket",
            "--format",
            "narrative",
            "--training-sample",
            "2026-06-05-screenshot",
            "--eval-target",
            "2026-06-05-screenshot",
            "--min-similarity",
            "1.0",
            "--require-eval-phrases",
        ]
    )

    output = capsys.readouterr().out

    assert exit_code == 0
    assert output.startswith("DONT_NOTIFY\n2026年6月5日复盘")
    assert '"char_similarity": 1.0' in output
    assert "[replay-eval] send blocked" not in output


def test_training_sample_send_all_still_requires_explicit_allow(monkeypatch, capsys):
    sent: list[str] = []

    def fake_send_text(text: str) -> None:
        sent.append(text)

    monkeypatch.setattr("signals.notify.send_text", fake_send_text)

    exit_code = main(
        [
            "--window",
            "postmarket",
            "--format",
            "narrative",
            "--training-sample",
            "2026-06-05-screenshot",
            "--send",
            "--send-all",
        ]
    )

    output = capsys.readouterr().out

    assert exit_code == 0
    assert output.startswith("DONT_NOTIFY\n2026年6月5日复盘")
    assert sent == []


def test_eval_failure_changes_cli_first_line_to_dont_notify(capsys):
    exit_code = main(
        [
            "--window",
            "postmarket",
            "--format",
            "narrative",
            "--training-sample",
            "2026-06-05-screenshot",
            "--allow-training-sample-send",
            "--eval-target",
            "2026-06-05-screenshot",
            "--min-similarity",
            "1.01",
        ]
    )

    output = capsys.readouterr().out

    assert exit_code == 0
    assert output.startswith("DONT_NOTIFY\n[replay-eval] send blocked:")
    assert "2026年6月5日复盘" in output


def test_narrative_review_uses_explicit_extra_facts():
    dashboard = _dashboard()
    dashboard["daily_brief"]["as_of"] = "2026-06-05"
    snapshot = _snapshot()
    snapshot["as_of"] = "2026-06-05"

    result = build_narrative_review(
        dashboard,
        _june5_shell(),
        snapshot,
        window="postmarket",
        max_items=5,
        extra_facts=["中际旭创单日成交约583亿，冲高回落无承接"],
    )

    assert "中际旭创单日成交约583亿" in result.text


def test_collect_replay_context_packages_generic_data():
    dashboard = _dashboard()
    dashboard["daily_brief"]["as_of"] = "2026-06-05"
    dashboard["daily_brief"]["primary_theme"] = "机器人/自动化产业链"
    snapshot = _snapshot()
    snapshot["as_of"] = "2026-06-05"

    context = collect_replay_context(
        dashboard,
        _june5_shell(),
        snapshot,
        window="postmarket",
        max_items=5,
        event_lines=["上证尾盘继续走弱，强板块仍需次日确认"],
        extra_facts=["中际旭创单日成交约583亿"],
    )

    assert context["trade_date"] == "2026-06-05"
    assert context["primary_theme"] == "机器人/自动化产业链"
    assert context["index_damage"]["changes"]["创业板指"] == -3.2023
    assert context["sector_boards"][0]["name"] == "机器人/自动化产业链/自动化/机器人"
    assert context["pool_counts"]["sectors"] == 3
    assert context["event_lines"] == ["上证尾盘继续走弱，强板块仍需次日确认"]
    assert context["extra_facts"] == ["中际旭创单日成交约583亿"]
    assert "style_contract" in context


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


def test_wechat_summary_preserves_signals_candidate_order():
    shell = _shell()
    shell["decision_queue"] = [
        {
            "decision_id": "focus:SH.603629",
            "symbol": "SH.603629",
            "name": "利通电子",
            "queue_lane": "entry_waiting_confirm",
            "trader_action": "低吸进攻复核",
            "entry_logic_summary": "30m未确认；5m/15m: 15分钟 MACD绿柱扩大_零上",
            "invalidates_when": "卖出/风险信号解除或重新站回关键周期",
            "primary_chain": "消费电子/华为链",
            "rank_score": 310.0,
        },
        {
            "decision_id": "focus:SH.601231",
            "symbol": "SH.601231",
            "name": "环旭电子",
            "queue_lane": "entry_waiting_confirm",
            "trader_action": "右侧买点复核",
            "entry_logic_summary": "30分钟 MACD绿柱扩大_零上；5m/15m未确认",
            "invalidates_when": "卖出/风险信号解除或重新站回关键周期",
            "primary_chain": "消费电子/华为链",
            "rank_score": 300.0,
        },
    ]
    shell["watchlist_groups"]["watch_stocks"] = [
        {
            "symbol": "SH.605358",
            "name": "立昂微",
            "queue_lane": "watch_preheat",
            "trader_action": "低吸进攻复核",
            "entry_logic_summary": "5分钟 一买；日线200日新高突破",
            "invalidates_when": "卖出/风险信号解除或重新站回关键周期",
            "primary_chain": "半导体产业链",
            "rank_score": 290.0,
        },
        {
            "symbol": "SZ.000025",
            "name": "特力A",
            "queue_lane": "watch_preheat",
            "trader_action": "低吸进攻复核",
            "entry_logic_summary": "5分钟 MACD绿柱扩大_零上；日线一买",
            "invalidates_when": "卖出/风险信号解除或重新站回关键周期",
            "primary_chain": "有色金属产业链",
            "rank_score": 280.0,
        },
    ]
    shell["watchlist_groups"]["risk_stocks"].append(
        {
            "symbol": "SH.513130",
            "name": "恒生科技ETF",
            "queue_lane": "risk_exit_first",
            "trader_action": "暂不参与",
            "entry_logic_summary": "日/周: 缺日/周买点；30m: 一买；15m/5m右侧混合",
            "invalidates_when": "日/周买点修复前不升级",
            "primary_chain": "港股科技",
            "rank_score": 210.0,
        }
    )
    shell["watchlist_groups"]["major_indices"] = [
        {
            "name": "上证指数",
            "day_change_pct": -0.58,
            "daily_trend": "中枢震荡",
            "f30_trend": "下跌趋势",
            "f15_trend": "上涨趋势",
            "latest_signal": "MA20回踩承接",
        },
        {
            "name": "深证成指",
            "day_change_pct": -1.94,
            "daily_trend": "回调修正",
            "f30_trend": "下跌趋势",
            "f15_trend": "下跌趋势",
            "latest_signal": "MA13回踩承接",
        },
        {
            "name": "创业板指",
            "day_change_pct": -2.29,
            "daily_trend": "上涨趋势",
            "f30_trend": "下跌趋势",
            "f15_trend": "上涨趋势",
            "latest_signal": "MA21反抽未过",
        },
        {
            "name": "科创50",
            "day_change_pct": -0.02,
            "daily_trend": "中枢震荡",
            "f30_trend": "下跌趋势",
            "f15_trend": "下跌趋势",
            "latest_signal": "MA13反抽未过",
        },
    ]
    shell["watchlist_groups"]["sector_boards"] = [
        {
            "name": "半导体产业链 · 材料/光刻胶",
            "day_change_pct": 4.22,
            "trader_action": "产业链确认：链主/弹性跟随，复核扩散延续",
            "source_driver": {"kind": "industry", "name": "半导体材料", "change_pct": 4.22},
            "representatives": {"core": [{"name": "上海新阳", "symbol": "SZ.300236"}]},
        },
        {
            "name": "大金融产业链 · 银行/券商/保险",
            "day_change_pct": 3.20,
            "trader_action": "待确认：保险和银行拉指数，等扩散",
            "source_driver": {"kind": "industry", "name": "保险Ⅲ", "change_pct": 3.20},
            "representatives": {
                "core": [
                    {"name": "工商银行", "symbol": "SH.601398"},
                    {"name": "中国平安", "symbol": "SH.601318"},
                ]
            },
        },
        {
            "name": "半导体产业链 · 半导体设备",
            "day_change_pct": 1.64,
            "trader_action": "产业链确认：链主/弹性跟随，复核扩散延续",
            "source_driver": {"kind": "industry", "name": "半导体设备", "change_pct": 1.64},
            "representatives": {
                "core": [
                    {"name": "北方华创", "symbol": "SZ.002371"},
                    {"name": "中微公司", "symbol": "SH.688012"},
                ]
            },
        },
        {
            "name": "传媒旅游产业链 · 游戏/影视/文旅",
            "day_change_pct": 2.45,
            "trader_action": "源强链弱：等链主确认后再当主线",
        },
        {
            "name": "光伏产业链 · 硅料/硅片/组件",
            "day_change_pct": 7.56,
            "trader_action": "链内分化：只看强分支，不当整链共振",
            "source_driver": {"kind": "industry", "name": "光伏主材", "change_pct": 7.56},
            "representatives": {"core": [{"name": "隆基绿能", "symbol": "SH.601012"}]},
        },
    ]

    dashboard = _dashboard()
    dashboard["daily_brief"]["primary_theme"] = "大金融产业链"
    snapshot = _snapshot()
    snapshot["market_regime"]["primary_theme"] = "大金融产业链"

    result = build_wechat_summary(
        dashboard,
        shell,
        snapshot,
        window="midday",
        max_items=5,
        event_lines=["创业板11:15日内修复强于上证，但30m仍待确认"],
    )

    assert result.status == "NOTIFY"
    assert "1) 上午盘面总结" not in result.text
    assert "交易日：" not in result.text
    assert "【2026-05-08｜11:15 午间重规划】" in result.text
    assert "盘面：" in result.text
    assert "证据：" in result.text
    assert "复核：午后只看主线承接、跟随扩散和弱指数能否收回。" in result.text
    assert "失效：" in result.text
    assert "回看：" in result.text
    assert "指数：" in result.text
    assert "上证-0.58%" in result.text
    assert "创业板-2.29%" in result.text
    assert "科创50-0.02%" in result.text
    assert "AI判断" not in result.text
    assert "半导体产业链先当主线" in result.text
    assert "光伏产业链看跟随" in result.text
    assert "大金融产业链先列观察" in result.text
    assert "港股科技观察" in result.text
    assert "恒生科技ETF SH.513130" in result.text
    assert "事件：创业板11:15日内修复强于上证" in result.text
    assert "盘面含义" not in result.text
    assert "主次排序" not in result.text
    assert "第一主线" not in result.text
    assert "第二梯队：" not in result.text
    assert "次日验证" not in result.text
    assert "验证线" not in result.text
    assert "大金融产业链/银行/券商/保险" in result.text
    assert "半导体产业链/材料/光刻胶" in result.text
    assert "光伏产业链/硅料/硅片/组件" in result.text
    assert "隆基绿能" in result.text
    assert "半导体产业链/半导体设备" in result.text
    assert "先看" in result.text
    assert "只留观察" in result.text
    assert "N/A" not in result.text
    assert "unknown" not in result.text
    assert "处理：" not in result.text
    assert "暂不动" not in result.text
    assert "排雷名单" not in result.text
    assert "均线策略" not in result.text
    assert "不追" in result.text
    assert "交易含义" not in result.text
    assert "角色拆分" not in result.text
    assert "金融护盘 + 科技局部修复 + 尾盘新线试探" not in result.text
    assert "护盘主线" not in result.text
    assert "科技修复线" not in result.text
    assert "硅线验证" not in result.text
    assert "前排强度" not in result.text
    assert "触发：" not in result.text
    assert "截至当前窗口" not in result.text
    review_block = result.text.split("失效：", 1)[0].split("复核：", 1)[1]
    assert review_block.rindex("利通电子 SH.603629") < review_block.rindex("环旭电子 SH.601231")
    assert review_block.rindex("环旭电子 SH.601231") < review_block.rindex("立昂微 SH.605358")
    assert "特力A SZ.000025(日线一买)" in result.text
    assert "特力A SZ.000025：" not in review_block
    assert "Mongo" not in result.text
    assert "runtime" not in result.text


def test_wechat_summary_sector_roles_are_signal_driven_not_fixed_names():
    result = build_wechat_summary(
        _dashboard(),
        _june5_shell(),
        _snapshot(),
        window="midday",
        max_items=5,
        event_lines=["机器人分支继续扩散，传媒旅游尾盘异动"],
    )

    assert result.status == "NOTIFY"
    assert "机器人/自动化产业链先当主线" in result.text
    assert "军工装备产业链看跟随" in result.text
    assert "传媒旅游产业链先列观察" in result.text
    assert "主线机器人/自动化产业链/自动化/机器人" in result.text
    assert "跟随军工装备产业链/商业航天/卫星互联网" in result.text
    assert "观察传媒旅游产业链/游戏/影视/文旅" in result.text
    assert "第一主线" not in result.text
    assert "第二梯队：" not in result.text
    assert "次日验证" not in result.text
    assert "验证线" not in result.text
    assert "金融护盘" not in result.text
    assert "半导体材料/设备" not in result.text
    assert "硅线" not in result.text


def test_wechat_intraday_windows_use_trader_note_voice():
    old_or_engineering_terms = (
        "1) 上午盘面总结",
        "2) 板块主次",
        "3) 下午复核清单",
        "交易日：",
        "AI判断",
        "盘面含义",
        "主次排序",
        "第一主线",
        "第二梯队：",
        "次日验证",
        "验证线",
        "均线策略",
        "候选共性",
        "复核对象",
        "今日触发信号",
        "回测",
        "N/A",
        "unknown",
        "处理：",
        "暂不动",
        "排雷名单",
        "缺失",
        "unavailable",
        "数据边界",
        "字段缺失",
    )
    for window in ("preopen", "ten", "midday", "two", "close"):
        result = build_wechat_summary(
            _dashboard(),
            _june5_shell(),
            _snapshot(),
            window=window,
            max_items=3,
            event_lines=["指数分歧，主线待承接"],
        )

        assert result.status == "NOTIFY"
        assert "盘面：" in result.text
        assert "证据：" in result.text
        assert "复核：" in result.text
        assert "失效：" in result.text
        assert "回看：" in result.text
        assert "先当主线" in result.text
        assert "看跟随" in result.text
        assert "先列观察" in result.text
        assert "票池共性" in result.text
        assert "先看：" in result.text
        assert "只留观察：" in result.text
        for term in old_or_engineering_terms:
            assert term not in result.text


def test_market_event_lines_trigger_notify_without_stock_candidates():
    shell = _shell()
    shell["decision_queue"] = []
    shell["watchlist_groups"]["focus_stocks"] = []

    result = build_summary(
        _dashboard(),
        shell,
        _snapshot(),
        window="ten",
        event_lines=["创业板10:30日内相对上证走强，成长修复强于权重"],
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
    assert any("创业板截至14:10日内" in line and "成长修复强于权重" in line for line in lines)
    assert not any("按点位" in line for line in lines)


def test_breakpoint_watch_lines_frame_ten_oclock_confirmation():
    def ts(hour: int, minute: int) -> int:
        return int(datetime(2026, 5, 27, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())

    contexts = {
        "上证指数": {
            "target": {"label": "上证指数", "market_timezone": "Asia/Shanghai"},
            "summary": {
                "key_levels": [
                    {"name": "5日线", "value": 4070.50},
                    {"name": "20周线", "value": 4069.21},
                ]
            },
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
    assert any("创业板日内+0.29%" in line and "上证+0.05%" in line for line in lines)
    assert not any("5日线" in line for line in lines)


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


def test_breakpoint_watch_lines_marks_stale_two_oclock_data():
    def ts(hour: int, minute: int) -> int:
        return int(datetime(2026, 5, 27, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())

    contexts = {
        "上证指数": {
            "target": {"label": "上证指数", "market_timezone": "Asia/Shanghai"},
            "summary": {"key_levels": [{"name": "20周线", "value": 4069.21}]},
            "chart": {"ohlcv": [{"time": ts(11, 25), "low": 4068.0, "close": 4071.0}]},
        },
        "创业板指": {
            "target": {"label": "创业板指", "market_timezone": "Asia/Shanghai"},
            "chart": {"ohlcv": [{"time": ts(11, 25), "low": 4074.0, "close": 4078.0}]},
        },
    }

    lines = _breakpoint_watch_lines(contexts, "two")

    assert any("13:45变盘前（数据只到11:25）" in line for line in lines)
    assert any("可用最新" in line for line in lines)


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
