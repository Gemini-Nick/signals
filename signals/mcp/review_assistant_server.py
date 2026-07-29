# -*- coding: utf-8 -*-
"""Minimal stdio MCP server for the Signals replay review assistant."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any

from signals.notify.trading_workbench_summary import (
    build_market_replay_wechat_summary,
    build_narrative_review,
    collect_replay_context,
    fetch_inputs,
    fetch_inputs_safe,
    fetch_market_event_lines,
    _historical_replay_inputs,
    _market_replay_sector_rows,
)
from signals.replay.market_replay import build_market_replay_context, replay_analysis_framework
from signals.core.global_market_universe import market_metadata, normalize_markets
from signals.sync.modules.global_market_foundation import KR_CONTEXT_FLAG, kr_context_enabled, latest_market_snapshot
from signals.sync.db import get_db


SERVER_NAME = "signals-replay-review"
SERVER_VERSION = "0.1.0"

DATA_REQUIREMENTS = """Signals 复盘助手需要这些数据：
1. shell.indices：上证指数、深证成指、创业板指、科创50 的涨跌幅。
2. shell.watchlist_groups.sector_boards：Agent OS 板块15，保留原排序、涨幅、trader_action、source_driver、representatives。
3. shell.watchlist_groups.focus_stocks / watch_stocks / risk_stocks：买点池、盯盘池、风险池数量和候选理由。
4. dashboard.overview.cluster_summary：行业/概念 top 榜，作为 sector_boards 缺失时的补充。
5. fullmarket_spot_snapshots：全市场日线结果、成交额核心、炸板/冲高回落结构。
6. board_heat_ticks：分钟级板块强度、全市场强弱切换、板块15时间轴。
7. bars：代表股5分钟路径、放量bar、低点/高点/收盘承接。
8. dashboard.daily_brief：交易日、主线、置信度、新增/丢失主题和候选。
9. structured_daily_review：数据完整性、固定半小时切片、固定重点个股池、Top7板块代理、20日趋势可用性、承接/抛压表。
10. 可选：外部订单大小口径资金流、分账户资金流、新闻/催化、用户截图或外部数据补充的精确事实。大中小单不等同于账户级主力/散户。
自动化要求：先跑本地 window gate；intraday 用 render_market_replay_wechat_body 的正文，postmarket/weekly 用 get_market_replay_context 后按 skill 做 AI-native 复盘；不要只凭 raw API 摘要写市场判断。
输出要求：首行保留 NOTIFY/DONT_NOTIFY；不要输出 runtime/Mongo/cache 日志；不要写直接买卖指令。"""


def _tool_schema() -> list[dict[str, Any]]:
    return [
        {
            "name": "generate_signals_replay_review",
            "description": "Render a deterministic evidence preview from local Signals endpoints. Do not use this tool output as the final WeChat body; recurring delivery follows the skill's renderer or synthesis mode.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "base_url": {
                        "type": "string",
                        "default": "http://127.0.0.1:8011",
                        "description": "Signals web base URL.",
                    },
                    "window": {
                        "type": "string",
                        "default": "postmarket",
                        "enum": ["postmarket", "close", "midday", "two", "ten", "manual", "weekly"],
                    },
                    "max_items": {
                        "type": "integer",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 12,
                    },
                    "include_event_lines": {
                        "type": "boolean",
                        "default": False,
                    },
                    "extra_facts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                        "description": "Externally verified facts from screenshots, news, or exports. The server packages them but does not invent them.",
                    },
                    "trade_date": {
                        "type": "string",
                        "description": "YYYY-MM-DD. Optional historical date for narrative replay; defaults to dashboard/snapshot trade date.",
                    },
                    "include_market_replay": {
                        "type": "boolean",
                        "default": True,
                        "description": "Use local Mongo minute-level market replay context when generating the narrative.",
                    },
                    "include_external_fund_flows": {
                        "type": "boolean",
                        "default": True,
                        "description": "Best-effort Eastmoney/THS public order-size fund-flow evidence for high-turnover cores.",
                    },
                },
            },
        },
        {
            "name": "get_signals_replay_context",
            "description": "Return a structured, date-agnostic data package for an A-share replay review. Use this when an agent should write the narrative itself.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "base_url": {
                        "type": "string",
                        "default": "http://127.0.0.1:8011",
                        "description": "Signals web base URL.",
                    },
                    "window": {
                        "type": "string",
                        "default": "postmarket",
                        "enum": ["postmarket", "close", "midday", "two", "ten", "manual", "weekly"],
                    },
                    "max_items": {
                        "type": "integer",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 12,
                    },
                    "include_event_lines": {
                        "type": "boolean",
                        "default": False,
                    },
                    "extra_facts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                        "description": "Externally verified facts from screenshots, news, or exports. The server packages them but does not invent them.",
                    },
                },
            },
        },
        {
            "name": "get_market_replay_context",
            "description": "Return a full-market event graph from local Mongo: rotation windows, board timelines, high-turnover cores, failed boards, and representative stock paths.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "base_url": {
                        "type": "string",
                        "default": "http://127.0.0.1:8011",
                        "description": "Signals web base URL used to get the current sector board list and trade date.",
                    },
                    "trade_date": {
                        "type": "string",
                        "description": "YYYY-MM-DD. Defaults to dashboard/snapshot trade date.",
                    },
                    "markets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["A", "HK", "US", "KR"]},
                        "default": ["A"],
                        "description": "Optional market set. A-only remains the backward-compatible default; HK/US use independently dated materialized snapshots; KR is explicit-only external context, requires SECTOR_TRANSITION_KR_CONTEXT_ENABLED=true, and does not alter A-share state.",
                    },
                    "cutoff_time": {
                        "type": "string",
                        "pattern": "^([01]\\d|2[0-3]):[0-5]\\d$",
                        "description": "Optional strict Beijing market cutoff HH:MM. When set, return only point-in-time evidence at or before the cutoff.",
                    },
                    "report_stage": {
                        "type": "string",
                        "default": "formal_postmarket",
                        "enum": ["close_flash", "formal_postmarket"],
                        "description": "close_flash may use provisional intraday bars; formal_postmarket requires same-day official daily closes.",
                    },
                    "window": {
                        "type": "string",
                        "default": "postmarket",
                        "enum": ["preopen", "postmarket", "close", "midday", "two", "ten", "manual", "weekly"],
                        "description": "Signals review window used for signals_context pools and event lines.",
                    },
                    "max_items": {
                        "type": "integer",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 12,
                    },
                    "include_event_lines": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include intraday event lines for ten/midday/two/close windows when available.",
                    },
                    "checkpoints": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": ["09:35", "10:30", "11:30", "13:30", "14:58"],
                        "description": "Market-time checkpoints for board rotation snapshots.",
                    },
                    "high_turnover_limit": {
                        "type": "integer",
                        "default": 20,
                        "minimum": 5,
                        "maximum": 50,
                    },
                    "representative_limit": {
                        "type": "integer",
                        "default": 30,
                        "minimum": 5,
                        "maximum": 80,
                    },
                    "include_external_fund_flows": {
                        "type": "boolean",
                        "default": False,
                        "description": "Best-effort Eastmoney/THS public order-size fund-flow evidence for high-turnover cores.",
                    },
                },
            },
        },
        {
            "name": "get_replay_analysis_framework",
            "description": "Return the AI-native thinking framework for turning the full-market event graph into a screenshot-style replay.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "render_market_replay_wechat_body",
            "description": "Render a send-ready Chinese WeChat body from get_market_replay_context evidence. The body omits optional missing-field prose; audit.internal_gaps is for logs only.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "base_url": {
                        "type": "string",
                        "default": "http://127.0.0.1:8011",
                        "description": "Signals web base URL used to get current sector boards and trade date.",
                    },
                    "trade_date": {
                        "type": "string",
                        "description": "YYYY-MM-DD. Defaults to dashboard/snapshot trade date.",
                    },
                    "window": {
                        "type": "string",
                        "default": "postmarket",
                        "enum": ["preopen", "postmarket", "close", "midday", "two", "ten", "manual", "weekly"],
                    },
                    "max_items": {
                        "type": "integer",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 12,
                    },
                    "include_event_lines": {
                        "type": "boolean",
                        "default": True,
                    },
                    "checkpoints": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": ["09:35", "10:30", "11:30", "13:30", "14:58"],
                    },
                    "high_turnover_limit": {
                        "type": "integer",
                        "default": 20,
                        "minimum": 5,
                        "maximum": 50,
                    },
                    "representative_limit": {
                        "type": "integer",
                        "default": 30,
                        "minimum": 5,
                        "maximum": 80,
                    },
                    "include_external_fund_flows": {
                        "type": "boolean",
                        "default": False,
                    },
                    "report_stage": {
                        "type": "string",
                        "default": "formal_postmarket",
                        "enum": ["close_flash", "formal_postmarket"],
                    },
                },
            },
        },
        {
            "name": "list_signals_replay_data_requirements",
            "description": "Return the data checklist required by the Signals replay review skill.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def _response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _generate_review(arguments: dict[str, Any]) -> str:
    context = _collect_context(arguments)
    dashboard, shell, snapshot = context.pop("_inputs")
    requested_trade_date = str(arguments.get("trade_date") or "").strip()
    source_trade_date = _as_of_date(dashboard, snapshot)
    market_replay_sections: list[str] = []
    if bool(arguments.get("include_market_replay", True)):
        try:
            from signals.replay.market_replay import format_market_replay_sections

            groups = shell.get("watchlist_groups") if isinstance(shell.get("watchlist_groups"), dict) else {}
            sector_boards = groups.get("sector_boards") if isinstance(groups.get("sector_boards"), list) else []
            historical_requested = bool(requested_trade_date and requested_trade_date != source_trade_date)
            replay_context = build_market_replay_context(
                get_db(),
                trade_date=requested_trade_date or str(context.get("trade_date") or source_trade_date),
                sector_boards=[] if historical_requested else sector_boards,
                include_external_fund_flows=bool(arguments.get("include_external_fund_flows", True)),
            )
            market_replay_sections = format_market_replay_sections(replay_context)
            if historical_requested:
                dashboard, shell, snapshot = _historical_replay_inputs(
                    dashboard,
                    shell,
                    snapshot,
                    trade_date=requested_trade_date,
                    replay_context=replay_context,
                )
        except Exception:
            market_replay_sections = []
    result = build_narrative_review(
        dashboard,
        shell,
        snapshot,
        window=str(context.get("window") or "postmarket"),
        max_items=int(context.get("max_items") or 5),
        event_lines=context.get("event_lines") if isinstance(context.get("event_lines"), list) else [],
        extra_facts=context.get("extra_facts") if isinstance(context.get("extra_facts"), list) else [],
        market_replay_sections=market_replay_sections,
    )
    return result.text


def _collect_context(arguments: dict[str, Any]) -> dict[str, Any]:
    base_url = str(arguments.get("base_url") or "http://127.0.0.1:8011")
    window = str(arguments.get("window") or "postmarket")
    try:
        max_items = max(1, min(12, int(arguments.get("max_items") or 5)))
    except (TypeError, ValueError):
        max_items = 5
    include_events = bool(arguments.get("include_event_lines", False))
    extra_facts = arguments.get("extra_facts") if isinstance(arguments.get("extra_facts"), list) else []
    extra_facts = [str(item).strip() for item in extra_facts if str(item).strip()]

    _fetched = fetch_inputs_safe(base_url, timeout=20.0)
    dashboard, shell, snapshot = _fetched.dashboard, _fetched.shell, _fetched.snapshot
    event_lines: list[str] = []
    if include_events and window in {"ten", "midday", "two", "close"}:
        event_lines = fetch_market_event_lines(base_url, window=window)
    context = collect_replay_context(
        dashboard,
        shell,
        snapshot,
        window=window,
        max_items=max_items,
        event_lines=event_lines,
        extra_facts=extra_facts,
    )
    context["base_url"] = base_url
    context["max_items"] = max_items
    context["_inputs"] = (dashboard, shell, snapshot)
    return context


def _as_of_date(dashboard: dict[str, Any], snapshot: dict[str, Any]) -> str:
    brief = dashboard.get("daily_brief") if isinstance(dashboard.get("daily_brief"), dict) else {}
    return str(brief.get("as_of") or snapshot.get("as_of") or "").strip()


_INTRADAY_INDEX_TARGETS = (
    ("上证指数", "sh000001"),
    ("创业板指", "sz399006"),
    ("科创50", "sh000688"),
    ("科创综指", "sh000680"),
    ("中证1000", "sh000852"),
    ("央企科技引领", "sh932038"),
    ("央企现代产业", "sh931837"),
)


def _cutoff_at(trade_date: str, cutoff_time: str) -> datetime:
    try:
        return datetime.strptime(f"{trade_date} {cutoff_time}", "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError("cutoff_time must be HH:MM") from exc


def _point_in_time_indices(db: Any, trade_date: str, cutoff_at: datetime) -> list[dict[str, Any]]:
    day_start = datetime.fromisoformat(trade_date)
    rows: list[dict[str, Any]] = []
    for name, symbol in _INTRADAY_INDEX_TARGETS:
        bars = list(
            db["index_bars"].find(
                {"meta.symbol": symbol, "meta.freq": "5分钟", "dt": {"$gte": day_start, "$lte": cutoff_at}},
                {"_id": 0, "dt": 1, "open": 1, "high": 1, "low": 1, "close": 1, "amount": 1},
            ).sort("dt", 1)
        )
        if not bars:
            continue
        first, latest = bars[0], bars[-1]
        open_value = float(first.get("open") or 0)
        latest_value = float(latest.get("close") or 0)
        rows.append(
            {
                "name": name,
                "symbol": symbol,
                "latest_time": latest["dt"].strftime("%H:%M"),
                "open": open_value,
                "latest": latest_value,
                "open_to_latest_pct": round((latest_value / open_value - 1) * 100, 2) if open_value else None,
                "high": max(float(row.get("high") or 0) for row in bars),
                "low": min(float(row.get("low") or 0) for row in bars),
                "bar_count": len(bars),
            }
        )
    return rows


def _point_in_time_boards(db: Any, trade_date: str, cutoff_at: datetime, limit: int = 15) -> dict[str, Any]:
    day_start = datetime.fromisoformat(trade_date)
    latest = db["board_heat_ticks"].find_one(
        {"source": "eastmoney_push2delay", "trade_minute": {"$gte": day_start, "$lte": cutoff_at}},
        {"_id": 0, "trade_minute": 1},
        sort=[("trade_minute", -1)],
    )
    if not latest:
        return {"actual_time": None, "rows": []}
    minute = latest["trade_minute"]
    rows = list(
        db["board_heat_ticks"].find(
            {"source": "eastmoney_push2delay", "trade_minute": minute},
            {"_id": 0, "kind": 1, "name": 1, "change_pct": 1, "rank_idx": 1, "leader_name": 1, "leader_change_pct": 1, "up_count": 1, "down_count": 1},
        ).sort([("rank_idx", 1), ("change_pct", -1)]).limit(limit)
    )
    return {"actual_time": minute.strftime("%H:%M"), "rows": rows}


def _point_in_time_stocks(db: Any, trade_date: str, cutoff_at: datetime, limit: int = 40) -> dict[str, Any]:
    day_start = datetime.fromisoformat(trade_date)
    pipeline = [
        {"$match": {"meta.freq": "5分钟", "meta.symbol": {"$regex": "^[0-9]{6}$"}, "dt": {"$gte": day_start, "$lte": cutoff_at}}},
        {"$sort": {"dt": 1}},
        {"$group": {"_id": "$meta.symbol", "open": {"$first": "$open"}, "latest": {"$last": "$close"}, "latest_dt": {"$last": "$dt"}, "high": {"$max": "$high"}, "low": {"$min": "$low"}, "amount": {"$sum": "$amount"}}},
        {"$match": {"open": {"$gt": 0}, "latest": {"$gt": 0}, "amount": {"$gt": 0}}},
        {"$sort": {"amount": -1}},
        {"$limit": 300},
    ]
    raw = list(db["bars"].aggregate(pipeline, allowDiskUse=True))
    codes = [str(row.get("_id") or "") for row in raw]
    identity: dict[str, dict[str, Any]] = {}
    membership_date_doc = db["security_chain_memberships"].find_one(
        {"trade_date": {"$lte": trade_date}}, {"_id": 0, "trade_date": 1}, sort=[("trade_date", -1)]
    )
    membership_date = str((membership_date_doc or {}).get("trade_date") or "")
    if membership_date and codes:
        for row in db["security_chain_memberships"].find(
            {"trade_date": membership_date, "raw_code": {"$in": codes}, "is_primary_chain": True},
            {"_id": 0, "raw_code": 1, "name": 1, "chain_id": 1, "chain_name": 1, "node_name": 1},
        ):
            identity.setdefault(str(row.get("raw_code") or ""), row)
    rows: list[dict[str, Any]] = []
    for row in raw:
        code = str(row.get("_id") or "")
        open_value = float(row.get("open") or 0)
        latest_value = float(row.get("latest") or 0)
        meta = identity.get(code, {})
        rows.append(
            {
                "code": code,
                "name": meta.get("name") or code,
                "chain_id": meta.get("chain_id"),
                "chain_name": meta.get("chain_name"),
                "node_name": meta.get("node_name"),
                "latest_time": row["latest_dt"].strftime("%H:%M"),
                "open": open_value,
                "latest": latest_value,
                "open_to_latest_pct": round((latest_value / open_value - 1) * 100, 2),
                "amount_yi": round(float(row.get("amount") or 0) / 100000000, 2),
                "high": row.get("high"),
                "low": row.get("low"),
            }
        )
    elastic = sorted(
        [row for row in rows if (row.get("amount_yi") or 0) >= 1],
        key=lambda row: (row.get("open_to_latest_pct") or -999, row.get("amount_yi") or 0),
        reverse=True,
    )[:limit]
    return {"turnover_leaders": rows[:limit], "elastic_candidates": elastic}


def _build_intraday_cutoff_context(db: Any, trade_date: str, cutoff_time: str) -> dict[str, Any]:
    cutoff_at = _cutoff_at(trade_date, cutoff_time)
    return {
        "trade_date": trade_date,
        "cutoff_time": cutoff_time,
        "cutoff_at": cutoff_at.isoformat(timespec="minutes"),
        "evidence_policy": "strict_point_in_time",
        "major_indices": _point_in_time_indices(db, trade_date, cutoff_at),
        "board15_snapshot": _point_in_time_boards(db, trade_date, cutoff_at),
        "dynamic_stock_roles": _point_in_time_stocks(db, trade_date, cutoff_at),
        "excluded_after_cutoff": [
            "fullmarket_spot_snapshots",
            "daily_board_rankings",
            "market_limit_pools",
            "postmarket pools and summaries",
        ],
    }


def _collect_market_context(arguments: dict[str, Any]) -> dict[str, Any]:
    base_url = str(arguments.get("base_url") or "http://127.0.0.1:8011")
    _fetched = fetch_inputs_safe(base_url, timeout=20.0)
    dashboard, shell, snapshot = _fetched.dashboard, _fetched.shell, _fetched.snapshot
    trade_date = str(arguments.get("trade_date") or _as_of_date(dashboard, snapshot)).strip()
    if not trade_date:
        raise ValueError("trade_date is required when dashboard/snapshot has no as_of date")
    window = str(arguments.get("window") or "postmarket")
    report_stage = str(arguments.get("report_stage") or "formal_postmarket")
    if report_stage not in {"close_flash", "formal_postmarket"}:
        raise ValueError(f"invalid report_stage: {report_stage}")
    cutoff_time = str(arguments.get("cutoff_time") or "").strip()
    try:
        max_items = max(1, min(12, int(arguments.get("max_items") or 5)))
    except (TypeError, ValueError):
        max_items = 5
    include_events = bool(arguments.get("include_event_lines", True))
    checkpoints = arguments.get("checkpoints") if isinstance(arguments.get("checkpoints"), list) else None
    checkpoints = [str(item).strip() for item in checkpoints if str(item).strip()] if checkpoints else None
    try:
        high_turnover_limit = max(5, min(50, int(arguments.get("high_turnover_limit") or 20)))
    except (TypeError, ValueError):
        high_turnover_limit = 20
    try:
        representative_limit = max(5, min(80, int(arguments.get("representative_limit") or 30)))
    except (TypeError, ValueError):
        representative_limit = 30

    groups = shell.get("watchlist_groups") if isinstance(shell.get("watchlist_groups"), dict) else {}
    sector_boards = groups.get("sector_boards") if isinstance(groups.get("sector_boards"), list) else []
    explicit_trade_date = bool(str(arguments.get("trade_date") or "").strip())
    source_trade_date = _as_of_date(dashboard, snapshot)
    historical_requested = bool(explicit_trade_date and trade_date != source_trade_date)
    db = get_db()
    requested_markets = normalize_markets(arguments.get("markets") if isinstance(arguments.get("markets"), list) else None)
    if cutoff_time:
        context = _build_intraday_cutoff_context(db, trade_date, cutoff_time)
        context["report_stage"] = report_stage
        context["generation_status"] = "partial" if report_stage == "formal_postmarket" else "success"
        context["coverage"] = {
            "trade_date": trade_date,
            "report_stage": report_stage,
            "daily_coverage_date": None,
            "official_close_source": None,
            "official_close_as_of": None,
            "latest_intraday_time": cutoff_time,
            "formal_ready": False,
            "generation_status": context["generation_status"],
            "note": "严格点时上下文不等同正式收盘；不得用分钟线冒充正式收盘。",
        }
        base_context = {
            "trade_date": trade_date,
            "window": window,
            "cutoff_time": cutoff_time,
            "evidence_policy": "strict_point_in_time",
        }
    else:
        context = build_market_replay_context(
            db,
            trade_date=trade_date,
            sector_boards=[] if historical_requested else sector_boards,
            checkpoints=checkpoints,
            high_turnover_limit=high_turnover_limit,
            representative_limit=representative_limit,
            include_external_fund_flows=bool(arguments.get("include_external_fund_flows", False)),
            report_stage=report_stage,
        )
        base_context = collect_replay_context(
            dashboard,
            shell,
            snapshot,
            window=window,
            max_items=max_items,
            event_lines=fetch_market_event_lines(base_url, window=window) if include_events and window in {"ten", "midday", "two", "close"} else [],
        )
    if historical_requested:
        base_context["source_trade_date"] = source_trade_date
        base_context["historical_context_note"] = (
            "signals_context came from the live dashboard; for this historical date use market_replay as the authoritative evidence."
        )
        sector_rows = _market_replay_sector_rows(context, limit=max_items)
        if sector_rows:
            base_context["sector_boards"] = [
                {
                    "name": str(row.get("name") or ""),
                    "change_pct": row.get("day_change_pct"),
                    "action": str(row.get("trader_action") or ""),
                    "source_driver": row.get("source_driver") if isinstance(row.get("source_driver"), dict) else {},
                    "representatives": [],
                }
                for row in sector_rows
            ]
        base_context["index_damage"] = {
            "line": "历史日期请求未复用当前 dashboard 指数；指数口径以 market_replay.index_cycle 和本地 index_bars 为准。",
            "changes": {},
        }
    result = {
        "trade_date": trade_date,
        "requested_trade_date": str(arguments.get("trade_date") or trade_date),
        "data_trade_date": context.get("trade_date") or trade_date,
        "base_url": base_url,
        "window": window,
        "report_stage": report_stage,
        "coverage": context.get("coverage", {}),
        "generation_status": context.get("generation_status", "success"),
        "send_allowed": False,
        "signals_context": base_context,
        "market_replay": context,
    }
    if "markets" in arguments:
        global_markets: list[dict[str, Any]] = []
        for market in requested_markets:
            if market == "A":
                global_markets.append(
                    {
                        **market_metadata("A"),
                        "session_date": trade_date,
                        "as_of": context.get("coverage", {}).get("official_close_as_of"),
                        "session_state": "complete" if context.get("coverage", {}).get("formal_ready") else "partial",
                        "source": "market_replay",
                    }
                )
                continue
            if market == "KR" and not kr_context_enabled():
                global_markets.append(
                    {
                        **market_metadata("KR"),
                        "session_date": None,
                        "as_of": None,
                        "session_state": "unavailable",
                        "feature_status": "disabled",
                        "disabled_reason": f"{KR_CONTEXT_FLAG}=false",
                        "source": "feature_gate",
                        "context_role": "external_context_only",
                    }
                )
                continue
            snapshot_doc = latest_market_snapshot(db, market, session_date=trade_date)
            if snapshot_doc:
                global_markets.append(
                    {
                        **snapshot_doc,
                        "enabled_by_default": market_metadata(market).get("enabled_by_default", True),
                        "context_role": "external_context_only",
                    }
                )
            else:
                global_markets.append(
                    {
                        **market_metadata(market),
                        "session_date": None,
                        "as_of": None,
                        "session_state": "unavailable",
                        "source": "market_daily_snapshots",
                        "context_role": "external_context_only",
                    }
                )
        result["requested_markets"] = requested_markets
        result["global_markets"] = global_markets
    return result


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    if request_id is None:
        return None

    if method == "initialize":
        return _response(
            request_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "tools/list":
        return _response(request_id, {"tools": _tool_schema()})
    if method == "tools/call":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name == "list_signals_replay_data_requirements":
            return _response(request_id, _text_result(DATA_REQUIREMENTS))
        if name == "get_replay_analysis_framework":
            return _response(request_id, _text_result(json.dumps(replay_analysis_framework(), ensure_ascii=False, indent=2)))
        if name == "get_signals_replay_context":
            try:
                context = _collect_context(arguments)
                context.pop("_inputs", None)
                return _response(request_id, _text_result(json.dumps(context, ensure_ascii=False, indent=2)))
            except Exception as exc:  # pragma: no cover - defensive server boundary
                return _response(request_id, _text_result(f"打包复盘数据失败：{exc}", is_error=True))
        if name == "get_market_replay_context":
            try:
                context = _collect_market_context(arguments)
                return _response(request_id, _text_result(json.dumps(context, ensure_ascii=False, indent=2)))
            except Exception as exc:  # pragma: no cover - defensive server boundary
                return _response(request_id, _text_result(f"打包全市场复盘事件失败：{exc}", is_error=True))
        if name == "render_market_replay_wechat_body":
            try:
                context = _collect_market_context(arguments)
                result = build_market_replay_wechat_summary(
                    context,
                    window=str(arguments.get("window") or context.get("window") or "postmarket"),
                    max_items=int(arguments.get("max_items") or 5),
                )
                return _response(request_id, _text_result(json.dumps(result, ensure_ascii=False, indent=2)))
            except Exception as exc:  # pragma: no cover - defensive server boundary
                return _response(request_id, _text_result(f"渲染微信正文失败：{exc}", is_error=True))
        if name == "generate_signals_replay_review":
            try:
                return _response(request_id, _text_result(_generate_review(arguments)))
            except Exception as exc:  # pragma: no cover - defensive server boundary
                return _response(request_id, _text_result(f"生成复盘失败：{exc}", is_error=True))
        return _error(request_id, -32601, f"Unknown tool: {name}")
    if method == "shutdown":
        return _response(request_id, None)
    return _error(request_id, -32601, f"Unknown method: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("message must be an object")
            response = _handle(message)
        except Exception as exc:
            response = _error(None, -32700, str(exc))
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
