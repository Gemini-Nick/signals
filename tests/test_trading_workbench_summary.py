# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from signals.notify.trading_workbench_summary import (
    build_summary,
    build_narrative_review,
    build_wechat_summary,
    collect_replay_context,
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

    assert _index_kill_line(shell) == "上证-1.70%，深成指-3.22%，创业板-3.69%，科创50-4.30%"


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
    assert "板块15" in result.text
    assert "上证-0.74%" in result.text
    assert "创业板-3.20%" in result.text
    assert "最终强度更集中在机器人和航天装备" in result.text
    assert "受伤主线" in result.text
    assert "产业链确认的方向主要是：机器人/自动化产业链/自动化/机器人、军工装备产业链/商业航天/卫星互联网" in result.text
    assert "资金流动链条" in result.text
    assert "产业链确认" in result.text
    assert "明日验证点" in result.text
    assert "三池数量=板块" in result.text
    assert "不再单独写没有证据支撑的方向判断" in result.text
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
    assert "上证-0.74%" not in output


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
    assert "上证-0.74%" in output


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

    result = build_wechat_summary(
        _dashboard(),
        shell,
        _snapshot(),
        window="midday",
        max_items=5,
        event_lines=["上证11:15低点杀破10周线，按恐慌测试处理"],
    )

    assert result.status == "NOTIFY"
    assert "1) 上午盘面结论" in result.text
    assert "2) 三池共性" in result.text
    assert "3) 下午打开图复核" in result.text
    assert "关键盘面事件" in result.text
    assert "板块角色" in result.text
    assert "截至当前窗口" in result.text
    assert result.text.index("利通电子 SH.603629") < result.text.index("环旭电子 SH.601231")
    assert result.text.index("环旭电子 SH.601231") < result.text.index("立昂微 SH.605358")
    assert "特力A SZ.000025" not in result.text
    assert "共性不足" in result.text
    assert "Mongo" not in result.text
    assert "runtime" not in result.text


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
