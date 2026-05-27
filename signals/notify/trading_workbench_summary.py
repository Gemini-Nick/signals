# -*- coding: utf-8 -*-
"""Trader-facing WeChat summary for the Signals workbench.

This module deliberately formats trading decisions, not runtime health. It can
be used by Codex automations, launchd jobs, or manual dry-runs.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from urllib.parse import quote
from urllib.request import urlopen
from zoneinfo import ZoneInfo


WINDOW_LABELS = {
    "preopen": "09:15 盘前策略地图",
    "ten": "09:45 10点窗口",
    "midday": "11:15 午间重规划",
    "two": "13:45 14点窗口",
    "close": "14:30 收盘风险",
    "postmarket": "20:30 盘后复盘",
    "weekly": "周末策略校准",
    "manual": "手动工作台摘要",
}

ACTIONABLE_LANES = {"entry_ready", "entry_waiting_confirm", "entry_waiting_upper_context"}
ACTIONABLE_STATUSES = {"attack_entry", "entry_ready", "left_attack", "confirmed_entry"}
WINDOW_RANGES = {
    "preopen": (time(9, 5), time(9, 25)),
    "ten": (time(9, 35), time(10, 0)),
    "midday": (time(11, 5), time(11, 30)),
    "two": (time(13, 35), time(14, 0)),
    "close": (time(14, 20), time(15, 0)),
    "postmarket": (time(19, 0), time(23, 30)),
}
WINDOW_EVENT_CUTOFFS = {
    "ten": time(9, 45),
    "midday": time(11, 15),
    "two": time(13, 45),
    "close": time(14, 30),
}


@dataclass
class SummaryResult:
    status: str
    text: str
    notify: bool
    reason: str


def _text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _get_chain(row: dict[str, Any]) -> str:
    for key in ("primary_chain", "chain_name"):
        value = _text(row.get(key))
        if value:
            return value
    chain = row.get("chain_position") if isinstance(row.get("chain_position"), dict) else {}
    return _text(chain.get("chain") or chain.get("board_or_concept"))


def _get_action(row: dict[str, Any]) -> str:
    for key in ("trader_action", "recommended_action", "next_action", "action_label", "action"):
        value = _text(row.get(key))
        if value:
            return value
    return _text(row.get("stage_label") or row.get("action_status"), "复核")


def _get_trigger(row: dict[str, Any]) -> str:
    for key in ("entry_logic_summary", "missing_condition", "trigger_reason", "entry_reason", "latest_signal", "reason"):
        value = _text(row.get(key))
        if value:
            return value
    tech = row.get("technical_evidence") if isinstance(row.get("technical_evidence"), dict) else {}
    return _text(tech.get("signal_type") or tech.get("details"), "证据不足")


def _get_invalidation(row: dict[str, Any]) -> str:
    for key in ("invalidates_when", "invalidation", "exit_condition"):
        value = _text(row.get(key))
        if value:
            return value
    return "跌破信号位、上级周期转弱或主线失效"


def _get_score(row: dict[str, Any]) -> float:
    for key in ("rank_score", "sort_score", "score", "priority_score"):
        try:
            return float(row.get(key))
        except (TypeError, ValueError):
            continue
    return 0.0


def _symbol_name(row: dict[str, Any]) -> str:
    symbol = _text(row.get("symbol") or row.get("code") or row.get("target_symbol"))
    name = _text(row.get("name") or row.get("display_name") or row.get("title"))
    if name and symbol:
        return f"{name} {symbol}"
    return symbol or name or "未知对象"


def _row_key(row: dict[str, Any]) -> str:
    return _text(row.get("symbol") or row.get("code") or row.get("target_symbol") or row.get("decision_id") or row.get("title"))


def _is_actionable(row: dict[str, Any]) -> bool:
    if row.get("can_trade_now") is True:
        return True
    lane = _text(row.get("queue_lane"))
    status = _text(row.get("action_status") or row.get("trade_stage"))
    return lane in ACTIONABLE_LANES or status in ACTIONABLE_STATUSES


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        key = _row_key(row)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda item: (_is_actionable(item), _get_score(item)), reverse=True)


def fetch_json(base_url: str, path: str, *, timeout: float = 8.0) -> dict[str, Any]:
    url = base_url.rstrip("/") + path
    with urlopen(url, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data if isinstance(data, dict) else {}


def fetch_inputs(base_url: str = "http://127.0.0.1:8011") -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    return (
        fetch_json(base_url, "/api/pack/dashboard"),
        fetch_json(base_url, "/api/workbench/shell", timeout=45.0),
        fetch_json(base_url, "/api/strategy/snapshot", timeout=30.0),
    )


def _bar_dt(row: dict[str, Any], timezone_name: str) -> datetime | None:
    try:
        ts = float(row.get("time"))
    except (TypeError, ValueError):
        return None
    try:
        tz = ZoneInfo(timezone_name or "Asia/Shanghai")
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    return datetime.fromtimestamp(ts, tz)


def _float_value(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _day_chart_rows(payload: dict[str, Any]) -> list[tuple[datetime, dict[str, Any]]]:
    chart = payload.get("chart") if isinstance(payload.get("chart"), dict) else {}
    rows = _as_list(chart.get("ohlcv"))
    if not rows:
        return []
    target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
    timezone_name = _text(target.get("market_timezone"), "Asia/Shanghai")
    dated_rows = [(dt, row) for row in rows if (dt := _bar_dt(row, timezone_name)) is not None]
    if not dated_rows:
        return []
    latest_date = dated_rows[-1][0].date()
    return [(dt, row) for dt, row in dated_rows if dt.date() == latest_date]


def _fetch_symbol_payload(base_url: str, name: str, *, freq: str = "5min") -> dict[str, Any]:
    return fetch_json(base_url, f"/api/workbench/symbol/{quote(name)}?freq={quote(freq)}", timeout=45.0)


def fetch_market_event_lines(base_url: str, *, window: str = "manual") -> list[str]:
    contexts: dict[str, dict[str, Any]] = {}
    for name in ("上证指数", "创业板指", "深证成指", "沪深300"):
        try:
            contexts[name] = _fetch_symbol_payload(base_url, name)
        except Exception:
            continue
    contexts = _limit_contexts_to_window(contexts, window)
    lines = _breakpoint_watch_lines(contexts, window)
    lines.extend(_market_event_lines(contexts))
    if window in {"midday", "two", "close"}:
        lines.extend(_fetch_board_heat_event_lines(window=window))
    return _dedupe_lines(lines)[:4]


def _limit_contexts_to_window(contexts: dict[str, dict[str, Any]], window: str) -> dict[str, dict[str, Any]]:
    cutoff = WINDOW_EVENT_CUTOFFS.get(window)
    if cutoff is None:
        return contexts
    result: dict[str, dict[str, Any]] = {}
    for name, payload in contexts.items():
        chart = payload.get("chart") if isinstance(payload.get("chart"), dict) else {}
        day_rows = _day_chart_rows(payload)
        filtered_rows = [row for dt, row in day_rows if dt.time() <= cutoff]
        limited = dict(payload)
        limited["chart"] = {**chart, "ohlcv": filtered_rows}
        result[name] = limited
    return result


def _dedupe_lines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        result.append(line)
    return result


def _breakpoint_watch_lines(contexts: dict[str, dict[str, Any]], window: str) -> list[str]:
    if window not in {"ten", "midday", "two"}:
        return []

    sh_rows = _day_chart_rows(contexts.get("上证指数", {}))
    cy_rows = _day_chart_rows(contexts.get("创业板指", {}))
    if window == "ten":
        label = "9:45变盘前"
        next_check = "10:00前"
    elif window == "two":
        label = "13:45变盘前"
        next_check = "14:00后"
    else:
        label = "午盘前"
        next_check = "下午开盘后"

    lines: list[str] = []
    support_line = _support_watch_line(contexts.get("上证指数", {}), sh_rows, label, next_check)
    if support_line:
        lines.append(support_line)
    style_line = _style_watch_line(sh_rows, cy_rows, label, next_check)
    if style_line:
        lines.append(style_line)
    return lines[:2]


def _support_watch_line(
    payload: dict[str, Any],
    rows: list[tuple[datetime, dict[str, Any]]],
    label: str,
    next_check: str,
) -> str | None:
    if not rows:
        return None
    low_points = [
        (low, dt, row)
        for dt, row in rows
        if (low := _float_value(row.get("low"))) is not None
    ]
    if not low_points:
        return None
    low, _, _ = min(low_points, key=lambda item: item[0])
    latest_close = _float_value(rows[-1][1].get("close"))
    support = _nearest_key_level_context(payload, latest_close or low, max_distance=0.02)
    if support is None:
        support = _nearest_key_level_context(payload, low, max_distance=0.02)
    if support is None:
        return None
    level_text, level_value, _ = support
    close_text = f"、最新{latest_close:.2f}" if latest_close is not None else ""
    if low < level_value:
        condition = f"已破{level_text}，{next_check}先看能否收回，否则按风险扩散处理"
    else:
        condition = f"围绕{level_text}，{next_check}看守住还是击穿"
    return f"{label}：上证日内低点{low:.2f}{close_text}，{condition}"


def _style_watch_line(
    sh_rows: list[tuple[datetime, dict[str, Any]]],
    cy_rows: list[tuple[datetime, dict[str, Any]]],
    label: str,
    next_check: str,
) -> str | None:
    if not sh_rows or not cy_rows:
        return None
    sh_by_time = {dt: row for dt, row in sh_rows}
    common: list[tuple[datetime, float, float]] = []
    for dt, cy_row in cy_rows:
        sh_row = sh_by_time.get(dt)
        if not sh_row:
            continue
        cy_close = _float_value(cy_row.get("close"))
        sh_close = _float_value(sh_row.get("close"))
        if cy_close is not None and sh_close is not None:
            common.append((dt, sh_close, cy_close))
    if not common:
        return None
    _, sh_close, cy_close = common[-1]
    if cy_close > sh_close:
        condition = "成长暂强于权重，若延续则按进攻线索优先，若回落到上证下方则降级为观察"
    else:
        condition = "成长暂未强于权重，只有创业板首次超过上证并维持，才提高进攻确认"
    return f"{label}：创业板{cy_close:.2f} vs 上证{sh_close:.2f}，{condition}，{next_check}复核"


def _market_event_lines(contexts: dict[str, dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    sh_rows = _day_chart_rows(contexts.get("上证指数", {}))
    cy_rows = _day_chart_rows(contexts.get("创业板指", {}))

    if sh_rows:
        low_points = [
            (low, dt, row)
            for dt, row in sh_rows
            if (low := _float_value(row.get("low"))) is not None
        ]
        if low_points:
            low, dt, row = min(low_points, key=lambda item: item[0])
            close = _float_value(row.get("close"))
            close_text = f"，收{close:.2f}" if close is not None else ""
            support = _nearest_support_context(contexts.get("上证指数", {}), low)
            if support:
                level_text, level_value = support
                if low < level_value:
                    lines.append(f"上证{dt.strftime('%H:%M')}低点{low:.2f}杀破{level_text}{close_text}，按恐慌测试处理")
                elif low <= level_value * 1.003:
                    lines.append(f"上证{dt.strftime('%H:%M')}低点{low:.2f}逼近{level_text}{close_text}，需按恐慌测试观察")

    if sh_rows and cy_rows:
        sh_by_time = {dt: row for dt, row in sh_rows}
        common: list[tuple[datetime, float, float]] = []
        crossovers: list[tuple[datetime, float, float]] = []
        for dt, cy_row in cy_rows:
            sh_row = sh_by_time.get(dt)
            if not sh_row:
                continue
            cy_close = _float_value(cy_row.get("close"))
            sh_close = _float_value(sh_row.get("close"))
            if cy_close is None or sh_close is None:
                continue
            common.append((dt, sh_close, cy_close))
            if cy_close > sh_close:
                crossovers.append((dt, sh_close, cy_close))
        if crossovers:
            dt, sh_close, cy_close = crossovers[0]
            latest_dt, latest_sh_close, latest_cy_close = common[-1]
            if latest_cy_close > latest_sh_close:
                lines.append(
                    f"创业板{dt.strftime('%H:%M')}首次按点位超过上证"
                    f"（{cy_close:.2f} vs {sh_close:.2f}），截至{latest_dt.strftime('%H:%M')}仍维持，成长强于权重"
                )
            else:
                lines.append(
                    f"创业板{dt.strftime('%H:%M')}曾首次按点位超过上证"
                    f"（{cy_close:.2f} vs {sh_close:.2f}），但截至{latest_dt.strftime('%H:%M')}未维持，成长确认降级"
                )

    return lines[:2]


def _nearest_support_context(payload: dict[str, Any], low: float) -> tuple[str, float] | None:
    context = _nearest_key_level_context(payload, low, max_distance=0.006)
    if context is None:
        return None
    level_text, trigger_value, _ = context
    return level_text, trigger_value


def _nearest_key_level_context(payload: dict[str, Any], price: float, *, max_distance: float) -> tuple[str, float, float] | None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    levels = _as_list(summary.get("key_levels"))
    candidates: list[tuple[float, float, str]] = []
    for item in levels:
        value = _float_value(item.get("value"))
        if value is None or value <= 0:
            continue
        distance = abs(price - value) / value
        if distance > max_distance:
            continue
        name = _text(item.get("name"), "关键位")
        round_level = round(value / 10) * 10
        if abs(round_level - value) <= 3:
            label = f"{round_level:.0f}({name}{value:.2f})"
            trigger_value = float(round_level)
        else:
            label = f"{name}{value:.2f}"
            trigger_value = value
        candidates.append((distance, trigger_value, label))
    if not candidates:
        return None
    distance, trigger_value, label = min(candidates, key=lambda item: item[0])
    return label, trigger_value, distance


def _board_display_name(name: str) -> str:
    return name.rstrip("ⅠⅡⅢⅣIV")


def _fetch_board_heat_event_lines(*, window: str = "manual") -> list[str]:
    try:
        from signals.sync.db import get_db

        col = get_db()["board_heat_ticks"]
        latest_doc = col.find_one({}, {"trade_date": 1, "trade_minute": 1}, sort=[("trade_minute", -1)])
        if not latest_doc:
            return []
        trade_date = _text(latest_doc.get("trade_date"))
        absolute_latest_minute = latest_doc.get("trade_minute")
        if not trade_date or absolute_latest_minute is None:
            return []
        latest_minute = absolute_latest_minute
        cutoff = WINDOW_EVENT_CUTOFFS.get(window)
        if cutoff is not None:
            day_start = latest_minute.replace(hour=0, minute=0, second=0, microsecond=0)
            cutoff_minute = latest_minute.replace(
                hour=cutoff.hour,
                minute=cutoff.minute,
                second=0,
                microsecond=0,
            )
            cutoff_doc = col.find_one(
                {
                    "trade_date": trade_date,
                    "trade_minute": {"$gte": day_start, "$lte": cutoff_minute},
                },
                {"trade_minute": 1},
                sort=[("trade_minute", -1)],
            )
            if not cutoff_doc:
                return []
            latest_minute = cutoff_doc.get("trade_minute")
            if latest_minute is None:
                return []
        noon_doc = col.find_one(
            {"trade_date": trade_date, "trade_minute": {"$lt": latest_minute.replace(hour=12, minute=0, second=0, microsecond=0)}},
            {"trade_minute": 1},
            sort=[("trade_minute", -1)],
        )
        if not noon_doc:
            return []
        noon_minute = noon_doc.get("trade_minute")
        common = {"trade_date": trade_date, "kind": {"$in": ["industry", "concept"]}}
        projection = {"_id": 0, "kind": 1, "name": 1, "change_pct": 1, "trade_minute": 1, "leader_name": 1}
        latest_docs = list(col.find({**common, "trade_minute": latest_minute}, projection))
        morning_docs = list(col.find({**common, "trade_minute": noon_minute}, projection))
        pm_start = latest_minute.replace(hour=13, minute=0, second=0, microsecond=0)
        pm_docs = list(col.find({**common, "trade_minute": {"$gte": pm_start, "$lte": latest_minute}}, projection))
        return _board_heat_event_lines_from_docs(latest_docs, morning_docs, pm_docs)
    except Exception:
        return []


def _board_heat_event_lines_from_docs(
    latest_docs: list[dict[str, Any]],
    morning_docs: list[dict[str, Any]],
    pm_docs: list[dict[str, Any]],
    *,
    limit: int = 2,
) -> list[str]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    morning: dict[tuple[str, str], dict[str, Any]] = {}
    pm_high: dict[tuple[str, str], tuple[float, datetime | None, str]] = {}

    for doc in latest_docs:
        key = (_text(doc.get("kind")), _board_display_name(_text(doc.get("name"))))
        if key[0] and key[1]:
            latest[key] = doc
    for doc in morning_docs:
        key = (_text(doc.get("kind")), _board_display_name(_text(doc.get("name"))))
        if key[0] and key[1]:
            morning[key] = doc
    for doc in pm_docs:
        key = (_text(doc.get("kind")), _board_display_name(_text(doc.get("name"))))
        value = _float_value(doc.get("change_pct"))
        if not key[0] or not key[1] or value is None:
            continue
        old = pm_high.get(key)
        if old is None or value > old[0]:
            minute = doc.get("trade_minute") if isinstance(doc.get("trade_minute"), datetime) else None
            pm_high[key] = (value, minute, _text(doc.get("leader_name")))

    candidates: list[tuple[float, float, float, str, str, str]] = []
    for key, (high, minute, leader) in pm_high.items():
        am_change = _float_value(morning.get(key, {}).get("change_pct"))
        latest_change = _float_value(latest.get(key, {}).get("change_pct"))
        if am_change is None or latest_change is None:
            continue
        jump = high - am_change
        if high < 3.0 or jump < 1.5 or latest_change < 1.5:
            continue
        time_text = minute.strftime("%H:%M") if minute else "午后"
        leader_text = f"，领涨{leader}" if leader else ""
        line = (
            f"{key[1]}午后异动：上午{am_change:+.2f}%，"
            f"{time_text}最高{high:+.2f}%，最新{latest_change:+.2f}%{leader_text}"
        )
        candidates.append((jump, high, latest_change, key[0], key[1], line))

    candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    seen: set[str] = set()
    lines: list[str] = []
    for _, _, _, _, name, line in candidates:
        if name in seen:
            continue
        seen.add(name)
        lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def window_gate(window: str, now: datetime | None = None) -> tuple[bool, str]:
    """Return whether a scheduled summary should run for the current A-share window."""
    if window == "manual":
        return True, "manual"
    try:
        from signals.core.trading_dates import is_trading_day, to_market_naive_now

        local = to_market_naive_now("A", now)
        is_trade_day = bool(is_trading_day("A", local.date()))
    except Exception:
        local = now or datetime.now()
        is_trade_day = local.weekday() < 5

    if window == "weekly":
        if local.weekday() in {5, 6}:
            return True, "weekly_window"
        return False, f"not_weekend:{local.date().isoformat()}"
    if not is_trade_day:
        return False, f"not_a_share_trading_day:{local.date().isoformat()}"

    bounds = WINDOW_RANGES.get(window)
    if bounds is None:
        return True, "unknown_window_allowed"
    start, end = bounds
    if start <= local.time() <= end:
        return True, "window_open"
    return False, f"outside_window:{window}:{local.strftime('%H:%M')}"


def _source_quality(dashboard: dict[str, Any], snapshot: dict[str, Any]) -> str:
    confidence = dashboard.get("source_confidence") if isinstance(dashboard.get("source_confidence"), dict) else {}
    score = confidence.get("overall")
    if score is None:
        regime = snapshot.get("market_regime") if isinstance(snapshot.get("market_regime"), dict) else {}
        score = regime.get("confidence")
    try:
        score_f = float(score)
    except (TypeError, ValueError):
        score_f = 0.0

    stale_sources = [
        _text(item.get("name") or item.get("source"))
        for item in _as_list(confidence.get("sources"))
        if _text(item.get("freshness")) == "stale"
    ]
    if score_f < 0.55:
        return f"证据不足，confidence={score_f:.2f}"
    if stale_sources:
        return f"谨慎，confidence={score_f:.2f}，陈旧源：{', '.join(stale_sources[:3])}"
    return f"可用，confidence={score_f:.2f}"


def _market_line(dashboard: dict[str, Any], shell: dict[str, Any], snapshot: dict[str, Any]) -> str:
    brief = dashboard.get("daily_brief") if isinstance(dashboard.get("daily_brief"), dict) else {}
    market = shell.get("market") if isinstance(shell.get("market"), dict) else {}
    regime = snapshot.get("market_regime") if isinstance(snapshot.get("market_regime"), dict) else {}
    theme = _text(brief.get("primary_theme") or regime.get("primary_theme"), "证据不足")
    stance = _text(brief.get("market_line") or regime.get("label") or market.get("overall_direction"), "证据不足")
    style = _text(market.get("recommended_style"), "证据不足")
    position = _text(market.get("position_suggestion"), "证据不足")
    return f"{stance}；主线：{theme}；风格：{style}；仓位：{position}"


def _collect_rows(shell: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    groups = shell.get("watchlist_groups") if isinstance(shell.get("watchlist_groups"), dict) else {}
    focus = _as_list(groups.get("focus_stocks"))
    watch = _as_list(groups.get("watch_stocks"))
    risk = _as_list(groups.get("risk_stocks"))
    queue = _as_list(shell.get("decision_queue"))
    candidates = _as_list(shell.get("buy_candidates"))

    action_rows = _sort_rows(_dedupe(queue + focus))
    watch_rows = _sort_rows(_dedupe(watch + candidates))
    risk_rows = _sort_rows(_dedupe(risk))
    return action_rows, watch_rows, risk_rows


def _format_section(title: str, rows: list[dict[str, Any]], *, limit: int) -> list[str]:
    lines = [f"{title}："]
    if not rows:
        lines.append("- 无")
        return lines
    for index, row in enumerate(rows[:limit], start=1):
        chain = _get_chain(row)
        prefix = f"{index}. {_symbol_name(row)}"
        if chain:
            prefix += f" | {chain}"
        lines.extend(
            [
                prefix,
                f"   动作：{_get_action(row)}",
                f"   触发：{_get_trigger(row)}",
                f"   放弃：{_get_invalidation(row)}",
            ]
        )
    return lines


def _window_rows(
    window: str,
    action_rows: list[dict[str, Any]],
    watch_rows: list[dict[str, Any]],
    risk_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if window == "close":
        return action_rows[:2], watch_rows[:2], risk_rows[:4]
    if window in {"ten", "two"}:
        return action_rows[:3], watch_rows[:2], risk_rows[:2]
    if window == "midday":
        return action_rows[:2], watch_rows[:3], risk_rows[:2]
    if window in {"postmarket", "weekly"}:
        return action_rows[:3], watch_rows[:3], risk_rows[:3]
    return action_rows[:3], watch_rows[:2], risk_rows[:2]


def _next_step(window: str, notify: bool) -> str:
    if not notify:
        return "无需打断，保留在 AgentOS 工作台观察。"
    if window in {"ten", "two"}:
        return "接下来15分钟打开 AgentOS 买点池和策略图，只看上述对象。"
    if window == "close":
        return "先处理风险池和不能隔夜对象，再决定明日保留清单。"
    if window == "preopen":
        return "开盘前只盯上述对象，不临盘扩散到新票。"
    return "打开 AgentOS 工作台复核重点对象，证据不足的不追。"


def build_summary(
    dashboard: dict[str, Any],
    shell: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    window: str = "manual",
    max_items: int = 3,
    event_lines: list[str] | None = None,
) -> SummaryResult:
    action_rows, watch_rows, risk_rows = _collect_rows(shell)
    selected_action, selected_watch, selected_risk = _window_rows(window, action_rows, watch_rows, risk_rows)
    selected_action = selected_action[:max_items]
    selected_watch = selected_watch[:max_items]
    selected_risk = selected_risk[:max_items]
    event_lines = event_lines or []

    actionable_count = sum(1 for row in selected_action if _is_actionable(row))
    notify = bool(actionable_count or event_lines or (window == "close" and selected_risk))
    status = "NOTIFY" if notify else "DONT_NOTIFY"
    if actionable_count:
        reason = "actionable_workbench_items"
    elif event_lines:
        reason = "market_event_lines"
    elif window == "close" and selected_risk:
        reason = "close_risk_items"
    else:
        reason = "no_actionable_workbench_items"

    title = WINDOW_LABELS.get(window, WINDOW_LABELS["manual"])
    brief = dashboard.get("daily_brief") if isinstance(dashboard.get("daily_brief"), dict) else {}
    as_of = _text(brief.get("as_of") or snapshot.get("as_of") or dashboard.get("status"), "unknown")
    quality = _source_quality(dashboard, snapshot)

    lines = [
        status,
        f"[Signals 工作台 | {title}]",
        f"交易日：{as_of}",
        f"结论：{'需要打开图复核' if notify else '只观察，不打断'}",
        f"市场：{_market_line(dashboard, shell, snapshot)}",
        f"证据质量：{quality}",
        "",
    ]
    if event_lines:
        lines.append("关键盘面事件：")
        lines.extend(f"- {line}" for line in event_lines[:4])
        lines.append("")
    lines.extend(_format_section("马上看", selected_action, limit=max_items))
    lines.append("")
    lines.extend(_format_section("继续盯", selected_watch, limit=max_items))
    lines.append("")
    lines.extend(_format_section("风险", selected_risk, limit=max_items))
    lines.extend(["", f"下一步：{_next_step(window, notify)}"])
    return SummaryResult(status=status, text="\n".join(lines), notify=notify, reason=reason)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a trader-facing Signals workbench summary.")
    parser.add_argument("--base-url", default=os.getenv("SIGNALS_WEB_BASE_URL", "http://127.0.0.1:8011"))
    parser.add_argument("--window", choices=sorted(WINDOW_LABELS), default="manual")
    parser.add_argument("--max-items", type=int, default=3)
    parser.add_argument("--send", action="store_true", help="Send to configured notification channels when result is NOTIFY.")
    parser.add_argument("--send-all", action="store_true", help="Send even when result is DONT_NOTIFY.")
    parser.add_argument("--ignore-time", action="store_true", help="Bypass A-share day/window gating for dry-run review.")
    args = parser.parse_args(argv)

    allowed, gate_reason = window_gate(args.window)
    if not allowed and not args.ignore_time:
        title = WINDOW_LABELS.get(args.window, WINDOW_LABELS["manual"])
        print(f"DONT_NOTIFY\n[Signals 工作台 | {title}]\n原因：{gate_reason}")
        return 0

    dashboard, shell, snapshot = fetch_inputs(args.base_url)
    event_lines = (
        fetch_market_event_lines(args.base_url, window=args.window)
        if args.window in {"ten", "midday", "two", "close"}
        else []
    )
    result = build_summary(
        dashboard,
        shell,
        snapshot,
        window=args.window,
        max_items=max(1, args.max_items),
        event_lines=event_lines,
    )
    print(result.text)

    if args.send and (result.notify or args.send_all):
        from signals.notify import send_text

        send_text(result.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
