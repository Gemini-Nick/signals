# -*- coding: utf-8 -*-
"""Trader-facing WeChat summary for the Signals workbench.

This module deliberately formats trading decisions, not runtime health. It can
be used by Codex automations, launchd jobs, or manual dry-runs.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, time
from time import monotonic
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
WECHAT_HEADINGS = {
    "preopen": ("盘前推演", "开盘后复核清单", "开盘后验证"),
    "ten": ("9:45 盘面先判", "10:00前复核清单", "下一窗口验证"),
    "midday": ("上午盘面总结", "下午复核清单", "午后验证"),
    "two": ("13:45 盘面再判", "14:00前复核清单", "收盘前验证"),
    "close": ("全天盘面归纳", "收盘前复核清单", "盘后验证"),
    "postmarket": ("今日大盘/行业归因", "明日观察", "回测入口"),
    "weekly": ("本周大盘/行业", "候选/板块", "回测入口"),
    "manual": ("盘面结论", "打开图复核", "回测入口"),
}


@dataclass
class SummaryResult:
    status: str
    text: str
    notify: bool
    reason: str


@dataclass
class InputFetchResult:
    dashboard: dict[str, Any]
    shell: dict[str, Any]
    snapshot: dict[str, Any]
    errors: dict[str, str]


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


def _fetch_error_message(exc: BaseException) -> str:
    detail = str(exc).strip()
    label = type(exc).__name__
    if detail:
        return f"{label}: {detail}"[:180]
    return label


def fetch_inputs_safe(base_url: str = "http://127.0.0.1:8011", *, timeout: float = 6.0) -> InputFetchResult:
    bounded_timeout = max(0.5, float(timeout))
    jobs = {
        "dashboard": ("/api/pack/dashboard", bounded_timeout),
        "shell": ("/api/workbench/shell", bounded_timeout),
        "snapshot": ("/api/strategy/snapshot", bounded_timeout),
    }
    results: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    started_at = monotonic()
    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        futures = {
            executor.submit(fetch_json, base_url, path, timeout=path_timeout): name
            for name, (path, path_timeout) in jobs.items()
        }
        done, pending = wait(futures, timeout=bounded_timeout)
        for future in done:
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                errors[name] = _fetch_error_message(exc)
        for future in pending:
            name = futures[future]
            future.cancel()
            elapsed = monotonic() - started_at
            errors[name] = f"TimeoutError: exceeded {bounded_timeout:g}s input budget after {elapsed:.1f}s"
    return InputFetchResult(
        dashboard=results.get("dashboard", {}),
        shell=results.get("shell", {}),
        snapshot=results.get("snapshot", {}),
        errors=errors,
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


def _intraday_return(rows: list[tuple[datetime, dict[str, Any]]]) -> float | None:
    if not rows:
        return None
    first = rows[0][1]
    latest = rows[-1][1]
    base = _float_value(first.get("open"))
    if base is None:
        base = _float_value(first.get("close"))
    close = _float_value(latest.get("close"))
    if base is None or close is None or base <= 0:
        return None
    return (close / base - 1.0) * 100.0


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
        key = _event_line_key(line)
        if key in seen:
            continue
        seen.add(key)
        result.append(line)
    return result


def _event_line_key(line: str) -> str:
    if "创业板" in line and "上证" in line and "权重" in line:
        return "growth_vs_weight"
    if "上证" in line and ("低点" in line or "杀破" in line or "逼近" in line):
        return "sh_support"
    return line


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
    support = _nearest_support_context(payload, latest_close or low, max_distance=0.02)
    if support is None:
        support = _nearest_support_context(payload, low, max_distance=0.02)
    if support is None:
        return None
    level_text, level_value = support
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
    common: list[tuple[datetime, dict[str, Any], dict[str, Any]]] = []
    for dt, cy_row in cy_rows:
        sh_row = sh_by_time.get(dt)
        if not sh_row:
            continue
        if _float_value(cy_row.get("close")) is not None and _float_value(sh_row.get("close")) is not None:
            common.append((dt, sh_row, cy_row))
    if not common:
        return None
    latest_dt, _, _ = common[-1]
    sh_return = _intraday_return([(dt, row) for dt, row, _ in common])
    cy_return = _intraday_return([(dt, row) for dt, _, row in common])
    if sh_return is None or cy_return is None:
        return None
    if cy_return >= sh_return:
        condition = "成长日内相对强，若延续才提高科技修复权重；回落则降级为反抽"
    else:
        condition = "成长日内弱于权重，科技反抽不能直接当主线"
    return (
        f"{label}：截至{latest_dt.strftime('%H:%M')}创业板日内{_fmt_pct(cy_return)}、"
        f"上证{_fmt_pct(sh_return)}，{condition}，{next_check}复核"
    )


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
        common: list[tuple[datetime, dict[str, Any], dict[str, Any]]] = []
        for dt, cy_row in cy_rows:
            sh_row = sh_by_time.get(dt)
            if not sh_row:
                continue
            cy_close = _float_value(cy_row.get("close"))
            sh_close = _float_value(sh_row.get("close"))
            if cy_close is None or sh_close is None:
                continue
            common.append((dt, sh_row, cy_row))
        if common:
            latest_dt, _, _ = common[-1]
            sh_return = _intraday_return([(dt, row) for dt, row, _ in common])
            cy_return = _intraday_return([(dt, row) for dt, _, row in common])
            if sh_return is not None and cy_return is not None:
                if cy_return >= sh_return:
                    lines.append(
                        f"创业板截至{latest_dt.strftime('%H:%M')}日内{_fmt_pct(cy_return)}，"
                        f"上证{_fmt_pct(sh_return)}，成长修复强于权重"
                    )
                else:
                    lines.append(
                        f"创业板截至{latest_dt.strftime('%H:%M')}日内{_fmt_pct(cy_return)}，"
                        f"上证{_fmt_pct(sh_return)}，成长仍弱于权重"
                    )

    return lines[:2]


def _nearest_support_context(payload: dict[str, Any], low: float, *, max_distance: float = 0.006) -> tuple[str, float] | None:
    context = _nearest_key_level_context(payload, low, max_distance=max_distance)
    if context is None:
        return None
    level_text, trigger_value, _ = context
    if any(marker in level_text for marker in ("5日线", "10日线", "13日线")):
        return None
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


def _fmt_pct(value: Any, *, signed: bool = True) -> str:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return "N/A"
    sign = "+" if signed and pct > 0 else ""
    return f"{sign}{pct:.2f}%"


def _market_line(dashboard: dict[str, Any], shell: dict[str, Any], snapshot: dict[str, Any]) -> str:
    brief = dashboard.get("daily_brief") if isinstance(dashboard.get("daily_brief"), dict) else {}
    market = shell.get("market") if isinstance(shell.get("market"), dict) else {}
    regime = snapshot.get("market_regime") if isinstance(snapshot.get("market_regime"), dict) else {}
    theme = _text(brief.get("primary_theme") or regime.get("primary_theme"), "证据不足")
    stance = _text(brief.get("market_line") or regime.get("label") or market.get("overall_direction"), "证据不足")
    style = _text(market.get("recommended_style"), "证据不足")
    position = _text(market.get("position_suggestion"), "证据不足")
    return f"{stance}；主线：{theme}；风格：{style}；仓位：{position}"


def _as_of_date(dashboard: dict[str, Any], snapshot: dict[str, Any]) -> str:
    brief = dashboard.get("daily_brief") if isinstance(dashboard.get("daily_brief"), dict) else {}
    return _text(brief.get("as_of") or snapshot.get("as_of") or dashboard.get("status"), "unknown")


def _index_change_map(shell: dict[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    groups = shell.get("watchlist_groups") if isinstance(shell.get("watchlist_groups"), dict) else {}
    rows = [
        *_as_list(shell.get("indices")),
        *_as_list(groups.get("major_indices")),
    ]
    for row in rows:
        name = _text(row.get("name") or row.get("label") or row.get("symbol"))
        if not name:
            continue
        value = _float_value(
            row.get("day_change_pct")
            if row.get("day_change_pct") is not None
            else row.get("quote_change_pct")
        )
        if value is not None:
            result[name] = value
    return result


def _index_kill_line(shell: dict[str, Any]) -> str:
    changes = _index_change_map(shell)
    ordered = [
        ("上证指数", "上证"),
        ("深证成指", "深成指"),
        ("创业板指", "创业板"),
        ("科创50", "科创50"),
    ]
    parts = [f"{label}{_fmt_pct(changes[name], signed=False)}" for name, label in ordered if name in changes]
    if not parts:
        return "指数结构缺少可直接引用的涨跌幅，先以板块强弱和三池变化复盘。"
    return "，".join(parts)


def _sector_board_rows(shell: dict[str, Any], dashboard: dict[str, Any], *, limit: int = 8) -> list[dict[str, Any]]:
    groups = shell.get("watchlist_groups") if isinstance(shell.get("watchlist_groups"), dict) else {}
    rows = _as_list(groups.get("sector_boards"))
    if rows:
        return rows[:limit]

    overview = dashboard.get("overview") if isinstance(dashboard.get("overview"), dict) else {}
    cluster = overview.get("cluster_summary") if isinstance(overview.get("cluster_summary"), dict) else {}
    fallback: list[dict[str, Any]] = []
    for key, kind in (("industry_top", "行业"), ("concept_top", "概念")):
        for item in _as_list(cluster.get(key)):
            fallback.append(
                {
                    "name": _text(item.get("label")),
                    "day_change_pct": item.get("change_pct"),
                    "trader_action": f"{kind}强势，leader={_text(item.get('leader'), '未知')}",
                    "source": "cluster_summary",
                }
            )
    fallback.sort(key=lambda item: _float_value(item.get("day_change_pct")) or -999, reverse=True)
    return fallback[:limit]


def _sector_name(row: dict[str, Any]) -> str:
    name = _text(row.get("name") or row.get("label") or row.get("title"))
    if " · " in name:
        left, right = name.split(" · ", 1)
        display = left if right == left or right in left else f"{left}/{right}"
    else:
        display = name
    display = display or "未知板块"
    driver = row.get("source_driver") if isinstance(row.get("source_driver"), dict) else {}
    driver_name = _text(driver.get("name"))
    if driver_name in {"裸眼3D", "数字孪生"} and driver_name not in display:
        display = f"{display}/{driver_name}"
    return display


def _sector_change(row: dict[str, Any]) -> float | None:
    for key in ("day_change_pct", "daily_change_pct", "quote_change_pct", "latest_change_pct", "change_pct"):
        value = _float_value(row.get(key))
        if value is not None:
            return value
    driver = row.get("source_driver") if isinstance(row.get("source_driver"), dict) else {}
    return _float_value(driver.get("change_pct"))


def _sector_strength_line(rows: list[dict[str, Any]], *, limit: int = 6) -> str:
    if not rows:
        return "板块强度缺少可引用排序。"
    parts = []
    for row in rows[:limit]:
        change = _sector_change(row)
        suffix = f" {_fmt_pct(change)}" if change is not None else ""
        parts.append(f"{_sector_name(row)}{suffix}")
    return "、".join(parts)


def _clean_sector_alias(value: str) -> str:
    text = _text(value)
    for suffix in ("Ⅰ", "Ⅱ", "Ⅲ", "I", "II", "III"):
        text = text.replace(suffix, "")
    return text.strip(" /·")


def _replay_sector_alias(row: dict[str, Any]) -> str:
    driver = row.get("primary_domain") if isinstance(row.get("primary_domain"), dict) else {}
    if not driver:
        driver = row.get("source_driver") if isinstance(row.get("source_driver"), dict) else {}
    driver_name = _clean_sector_alias(_text(driver.get("name")))
    if driver_name and not driver_name.startswith("其他"):
        return driver_name
    name = _sector_name(row)
    parts = [_clean_sector_alias(part) for part in name.split("/") if _clean_sector_alias(part)]
    if parts:
        return parts[-1]
    return "未知方向"


def _replay_strength_names(rows: list[dict[str, Any]], *, limit: int = 2) -> str:
    selected: list[str] = []
    for row in rows:
        action = _text(row.get("trader_action") or row.get("rank_reason") or row.get("trace_summary"))
        if "产业链确认" not in action:
            continue
        alias = _replay_sector_alias(row)
        if alias and alias not in selected:
            selected.append(alias)
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        for row in rows:
            alias = _replay_sector_alias(row)
            if alias and alias not in selected:
                selected.append(alias)
            if len(selected) >= limit:
                break
    if not selected:
        return "少数强方向"
    if len(selected) == 1:
        return selected[0]
    return "和".join(selected[:limit])


def _sector_interpretation(rows: list[dict[str, Any]], *, limit: int = 6) -> str:
    if not rows:
        return "板块页没有返回可解释的产业链行。"
    parts = []
    for row in rows[:limit]:
        action = _text(row.get("trader_action") or row.get("rank_reason") or row.get("trace_summary"))
        if action:
            parts.append(f"{_sector_name(row)}：{action}")
    return "；".join(parts) or "板块有强弱，但缺少动作解释。"


def _pool_counts(shell: dict[str, Any]) -> dict[str, int]:
    groups = shell.get("watchlist_groups") if isinstance(shell.get("watchlist_groups"), dict) else {}
    return {
        "focus": len(_as_list(groups.get("focus_stocks"))),
        "watch": len(_as_list(groups.get("watch_stocks"))),
        "risk": len(_as_list(groups.get("risk_stocks"))),
        "sectors": len(_as_list(groups.get("sector_boards"))),
        "buy": len(_as_list(groups.get("buy_candidates") or shell.get("buy_candidates"))),
    }


def _compact_row_names(rows: list[dict[str, Any]], *, limit: int = 3) -> str:
    names = [_symbol_name(row) for row in rows[:limit] if _symbol_name(row) != "未知对象"]
    return "、".join(names) if names else "暂无明确对象"


def _narrative_notify_reason(
    window: str,
    selected_action: list[dict[str, Any]],
    selected_watch: list[dict[str, Any]],
    selected_risk: list[dict[str, Any]],
    sector_rows: list[dict[str, Any]],
    event_lines: list[str],
) -> tuple[bool, str]:
    actionable_count = sum(1 for row in selected_action if _is_actionable(row))
    if actionable_count:
        return True, "actionable_workbench_items"
    if sector_rows and window in {"postmarket", "weekly", "manual"}:
        return True, "sector_review_available"
    if event_lines:
        return True, "market_event_lines"
    if window == "close" and selected_risk:
        return True, "close_risk_items"
    if selected_watch or selected_risk:
        return True, "review_rows_available"
    return False, "no_review_context"


def _paragraph_lines(paragraphs: list[str]) -> list[str]:
    lines: list[str] = []
    for paragraph in paragraphs:
        lines.extend([paragraph, ""])
    if lines:
        lines.pop()
    return lines


def _sector_names_by_action(rows: list[dict[str, Any]], keyword: str, *, limit: int = 3) -> str:
    names = [
        _sector_name(row)
        for row in rows
        if keyword in _text(row.get("trader_action") or row.get("rank_reason") or row.get("trace_summary"))
    ]
    return "、".join(names[:limit]) if names else "暂无明确方向"


def _generic_replay_paragraphs(
    *,
    index_line: str,
    primary_theme: str,
    sector_rows: list[dict[str, Any]],
    counts: dict[str, int],
    action_names: str,
    watch_names: str,
    risk_names: str,
    event_lines: list[str],
    extra_facts: list[str],
    market_replay_sections: list[str] | None = None,
) -> list[str]:
    board_line = _sector_strength_line(sector_rows, limit=8)
    validation_line = (
        "明日验证点要落到这些数据："
        f"板块前排={board_line}；"
        f"三池数量=板块{counts['sectors']}、盯盘{counts['watch']}、买点/机会{counts['focus']}、风险{counts['risk']}；"
        f"买点池={action_names}；继续观察={watch_names}；风险/排雷={risk_names}；"
        f"指数线={index_line if index_line.endswith('。') else index_line + '。'}"
        "下一交易日只验证这些数据是否延续、修复或恶化，不再单独写没有证据支撑的方向判断。"
    )
    confirmed = _sector_names_by_action(sector_rows, "产业链确认")
    source_weak = _sector_names_by_action(sector_rows, "源强链弱")
    split = _sector_names_by_action(sector_rows, "链内分化")
    factual_lines = [line for line in [*event_lines[:4], *extra_facts[:6]] if _text(line)]
    if factual_lines:
        flow_paragraph = (
            f"先说资金流动链条。盘中可引用的转折线和补充事实是：{'；'.join(factual_lines)}。"
            "没有事实覆盖的时间段不编具体时间和价位，而是把资金切换写成可验证的结构："
            "指数是否继续杀、强板块是否扩散、弱链条是否拖累高位核心，以及尾盘是否还有承接。"
        )
    else:
        flow_paragraph = (
            "先说资金流动链条。当前没有传入可验证的分钟级转折线，所以不编具体时间和价位；"
            "资金切换先按指数损伤、板块15强弱和三池变化来复盘：指数是否继续杀、强板块是否扩散、"
            "弱链条是否拖累高位核心，以及尾盘是否还有承接。"
        )
    has_tech_replay = bool(
        market_replay_sections
        and any("科技链先集中恐慌" in section or "科技高成交" in section for section in market_replay_sections)
    )
    if has_tech_replay:
        opening = (
            "今天复盘先定两个坐标：方向和节奏。方向上，涨幅榜前排不能直接代表主线；"
            "真正决定账户体感和明日验证的是科技高成交链的压力测试，CPO/通信线缆、半导体、PCB/算力同时承担"
            "“受伤主线”和“验证锚”。节奏上，是早盘恐慌、盘中局部抄底反弹、午后承接失败。"
            f"{index_line if index_line.endswith('。') else index_line + '。'}"
            "所以今天不能写成“某两个板块最强”，而要判断这轮下跌是主线换挡，还是修复前的二次压力测试。"
        )
    else:
        opening = (
            "今天市场的真实结构是：盘面看着热闹，尾盘一锅端。"
            f"给你一个最直接的结论—最终强度更集中在{_replay_strength_names(sector_rows, limit=2)}，"
            "但其他交易量前排仍有复盘价值，关键要看它们是主线确认、受伤主线，还是压力锚。"
            f"{index_line if index_line.endswith('。') else index_line + '。'}"
            "但数字掩盖了真实杀伤力—高成交核心冲高回落无承接，会直接拖累尾盘情绪。"
            "这类盘面不能只看最终涨幅，要同时看强度、链主确认、弹性跟随、炸板回落和尾盘承接。"
        )
    sections = [
        opening,
    ]
    if market_replay_sections:
        sections.extend(market_replay_sections)
        sections.extend(
            [
                validation_line,
            ]
        )
        return sections
    else:
        sections.append(flow_paragraph)
    sections.extend(
        [
        (
            f"板块结构上，产业链确认的方向主要是：{confirmed}；源强链弱的方向主要是：{source_weak}；"
            f"链内分化的方向主要是：{split}。确认方向可以作为明日复核对象，源强链弱只能算卡位，"
            "链内分化则只看强分支，不把整条产业链直接升级为主线。"
        ),
        (
            f"三池结构上，板块池{counts['sectors']}个、盯盘池{counts['watch']}个、买点池/机会池{counts['focus']}个、风险池{counts['risk']}个。"
            f"买点池先复核：{action_names}；继续观察：{watch_names}；暂不参与或先排雷：{risk_names}。"
            "这些对象要和板块强弱共振才有复盘价值，单独的技术形态不应该脱离主线结构被放大。"
        ),
        (
            "尾盘情绪的判断标准很简单：强板块有没有扩散，核心高位有没有承接，风险池有没有继续增加。"
            "如果板块强但核心承接弱，就是卡位而不是主线；如果指数弱但板块池仍有清晰排序，"
            "明日要看的不是今天谁涨得多，而是谁能在竞价和开盘阶段继续维持优势。"
        ),
        validation_line,
        ]
    )
    return sections


def _representative_summary(row: dict[str, Any], *, limit: int = 4) -> list[dict[str, Any]]:
    reps = row.get("representatives") if isinstance(row.get("representatives"), dict) else {}
    result: list[dict[str, Any]] = []
    for tier in ("core", "elastic"):
        for item in _as_list(reps.get(tier)):
            result.append(
                {
                    "tier": tier,
                    "symbol": _text(item.get("symbol") or item.get("code")),
                    "name": _text(item.get("name")),
                    "day_change_pct": _float_value(item.get("day_change_pct")),
                    "role": _text(item.get("chain_role")),
                }
            )
            if len(result) >= limit:
                return result
    return result


def _row_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": _text(row.get("symbol") or row.get("code") or row.get("target_symbol")),
        "name": _text(row.get("name") or row.get("display_name") or row.get("title")),
        "chain": _get_chain(row),
        "action": _get_action(row),
        "trigger": _get_trigger(row),
        "invalidates_when": _get_invalidation(row),
        "score": _get_score(row),
    }


def collect_replay_context(
    dashboard: dict[str, Any],
    shell: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    window: str = "postmarket",
    max_items: int = 5,
    event_lines: list[str] | None = None,
    extra_facts: list[str] | None = None,
) -> dict[str, Any]:
    selected_action, selected_watch, selected_risk = _selected_rows(window, shell, max_items=max_items)
    sector_rows = _sector_board_rows(shell, dashboard, limit=max(15, max_items))
    counts = _pool_counts(shell)
    return {
        "trade_date": _as_of_date(dashboard, snapshot),
        "window": window,
        "primary_theme": _primary_theme(dashboard, snapshot),
        "index_damage": {
            "line": _index_kill_line(shell),
            "changes": _index_change_map(shell),
        },
        "sector_boards": [
            {
                "name": _sector_name(row),
                "change_pct": _sector_change(row),
                "action": _text(row.get("trader_action") or row.get("rank_reason") or row.get("trace_summary")),
                "source_driver": row.get("source_driver") if isinstance(row.get("source_driver"), dict) else {},
                "representatives": _representative_summary(row),
            }
            for row in sector_rows
        ],
        "pool_counts": counts,
        "pools": {
            "focus": [_row_summary(row) for row in selected_action],
            "watch": [_row_summary(row) for row in selected_watch],
            "risk": [_row_summary(row) for row in selected_risk],
        },
        "event_lines": event_lines or [],
        "extra_facts": extra_facts or [],
        "style_contract": [
            "先写真实市场结构，再写资金链条。",
            "板块15是主数据面，不用 daily_brief.primary_theme 覆盖板块排序。",
            "时间、价格、成交额、净流入等事实只从 event_lines、context 或额外事实里引用；缺失时明确不编。",
            "结尾只写验证点，不写直接买卖指令。",
        ],
    }


def build_narrative_review(
    dashboard: dict[str, Any],
    shell: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    window: str = "postmarket",
    max_items: int = 5,
    event_lines: list[str] | None = None,
    extra_facts: list[str] | None = None,
    market_replay_sections: list[str] | None = None,
) -> SummaryResult:
    selected_action, selected_watch, selected_risk = _selected_rows(window, shell, max_items=max_items)
    event_lines = event_lines or []
    extra_facts = extra_facts or []
    sector_rows = _sector_board_rows(shell, dashboard, limit=max(8, max_items))
    notify, reason = _narrative_notify_reason(
        window,
        selected_action,
        selected_watch,
        selected_risk,
        sector_rows,
        event_lines,
    )
    if not notify and market_replay_sections and window in {"postmarket", "weekly", "manual"}:
        notify = True
        reason = "market_replay_sections"
    status = "NOTIFY" if notify else "DONT_NOTIFY"
    as_of = _as_of_date(dashboard, snapshot)
    counts = _pool_counts(shell)
    primary_theme = _primary_theme(dashboard, snapshot)
    index_line = _index_kill_line(shell)
    action_names = _compact_row_names(selected_action, limit=3)
    watch_names = _compact_row_names(selected_watch, limit=3)
    risk_names = _compact_row_names(selected_risk, limit=3)

    lines = [
        status,
        _replay_date_title(as_of),
        "",
    ]
    lines.extend(
        _paragraph_lines(
            _generic_replay_paragraphs(
                index_line=index_line,
                primary_theme=primary_theme,
                sector_rows=sector_rows,
                counts=counts,
                action_names=action_names,
                watch_names=watch_names,
                risk_names=risk_names,
                event_lines=event_lines,
                extra_facts=extra_facts,
                market_replay_sections=market_replay_sections,
            )
        )
    )
    return SummaryResult(status=status, text="\n".join(lines), notify=notify, reason=reason)


def _replay_date_title(as_of: str) -> str:
    if as_of == "unknown":
        return "盘后复盘"
    try:
        year, month, day = [int(part) for part in as_of.split("-", 2)]
    except (TypeError, ValueError):
        return f"{as_of}复盘"
    return f"{year}年{month}月{day}日复盘"


def _market_replay_sector_rows(replay_context: dict[str, Any], *, limit: int = 8) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in _as_list(replay_context.get("board_timeline")):
        latest = item.get("latest") if isinstance(item.get("latest"), dict) else {}
        name = _text(item.get("driver_name") or item.get("board"))
        if not name:
            continue
        change = _float_value(latest.get("change_pct"))
        rows.append(
            {
                "name": name,
                "day_change_pct": change,
                "trader_action": "全市场分钟强度：按当日 board_heat_ticks 识别",
                "source_driver": {"kind": item.get("kind") or "board_heat", "name": name, "change_pct": change},
            }
        )
        if len(rows) >= limit:
            return rows
    windows = replay_context.get("rotation_windows") if isinstance(replay_context.get("rotation_windows"), list) else []
    latest_window = windows[-1] if windows else {}
    for item in _as_list(latest_window.get("top_boards")):
        name = _text(item.get("name"))
        if not name:
            continue
        change = _float_value(item.get("change_pct"))
        rows.append(
            {
                "name": name,
                "day_change_pct": change,
                "trader_action": "全市场分钟强度：按当日 checkpoint 排序识别",
                "source_driver": {"kind": item.get("kind") or "board_heat", "name": name, "change_pct": change},
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _historical_replay_inputs(
    dashboard: dict[str, Any],
    shell: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    trade_date: str,
    replay_context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    adjusted_dashboard = copy.deepcopy(dashboard)
    adjusted_shell = copy.deepcopy(shell)
    adjusted_snapshot = copy.deepcopy(snapshot)

    brief = adjusted_dashboard.setdefault("daily_brief", {})
    if isinstance(brief, dict):
        brief["as_of"] = trade_date
        sector_rows = _market_replay_sector_rows(replay_context)
        if sector_rows:
            brief["primary_theme"] = _sector_name(sector_rows[0])
    adjusted_snapshot["as_of"] = trade_date
    regime = adjusted_snapshot.setdefault("market_regime", {})
    if isinstance(regime, dict):
        regime["primary_theme"] = _text(brief.get("primary_theme")) if isinstance(brief, dict) else regime.get("primary_theme")

    groups = adjusted_shell.setdefault("watchlist_groups", {})
    if isinstance(groups, dict):
        sector_rows = _market_replay_sector_rows(replay_context)
        if sector_rows:
            groups["sector_boards"] = sector_rows
    adjusted_shell["indices"] = []
    return adjusted_dashboard, adjusted_shell, adjusted_snapshot


def _send_body(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip() in {"NOTIFY", "DONT_NOTIFY"}:
        body = "\n".join(lines[1:]).strip()
        return body or text
    return text


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


def _selected_rows(
    window: str,
    shell: dict[str, Any],
    *,
    max_items: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    action_rows, watch_rows, risk_rows = _collect_rows(shell)
    selected_action, selected_watch, selected_risk = _window_rows(window, action_rows, watch_rows, risk_rows)
    return (
        selected_action[:max_items],
        selected_watch[:max_items],
        selected_risk[:max_items],
    )


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


def _ordered_review_rows(
    action_rows: list[dict[str, Any]],
    watch_rows: list[dict[str, Any]],
    risk_rows: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    rows = _dedupe(action_rows + watch_rows)
    if len(rows) < limit:
        rows = _dedupe(rows + risk_rows)
    return rows[:limit]


def _wechat_action_label(row: dict[str, Any]) -> str:
    lane = _text(row.get("queue_lane"))
    status = _text(row.get("action_status") or row.get("trade_stage"))
    action = _get_action(row)
    trigger = _get_trigger(row)
    if lane.startswith("risk") or "暂不参与" in action or "放弃" in action:
        return "忽略"
    if _is_actionable(row) and "未确认" not in trigger and "等待" not in action:
        return "看"
    if status in ACTIONABLE_STATUSES and "缺" not in trigger:
        return "看"
    return "等"


def _compact_trigger(row: dict[str, Any]) -> str:
    trigger = _get_trigger(row)
    parts = [part.strip() for part in trigger.split("；") if part.strip()]
    filtered = [part for part in parts if not part.startswith("产业链:")]
    if not filtered:
        return trigger
    kept = filtered[:3]
    missing = next((part for part in filtered[3:] if "还差" in part or "缺" in part), "")
    if missing:
        kept.append(missing)
    return "；".join(kept)


def _format_wechat_review_rows(
    action_rows: list[dict[str, Any]],
    watch_rows: list[dict[str, Any]],
    risk_rows: list[dict[str, Any]],
    *,
    limit: int,
) -> list[str]:
    rows = _ordered_review_rows(action_rows, watch_rows, risk_rows, limit=limit)
    if not rows:
        return ["- 无；等下一次触发。"]
    return [
        f"- {_symbol_name(row)} | 触发：{_compact_trigger(row)}。{_wechat_action_label(row)}"
        for row in rows
    ]


def _primary_theme(dashboard: dict[str, Any], snapshot: dict[str, Any]) -> str:
    brief = dashboard.get("daily_brief") if isinstance(dashboard.get("daily_brief"), dict) else {}
    regime = snapshot.get("market_regime") if isinstance(snapshot.get("market_regime"), dict) else {}
    return _text(brief.get("primary_theme") or regime.get("primary_theme"), "当前主线")


INDEX_DISPLAY_ORDER = (
    ("上证指数", "上证"),
    ("深证成指", "深成"),
    ("创业板指", "创业板"),
    ("科创50", "科创50"),
)


def _major_index_rows(shell: dict[str, Any]) -> dict[str, dict[str, Any]]:
    groups = shell.get("watchlist_groups") if isinstance(shell.get("watchlist_groups"), dict) else {}
    result: dict[str, dict[str, Any]] = {}
    for row in [*_as_list(shell.get("indices")), *_as_list(groups.get("major_indices"))]:
        name = _text(row.get("name") or row.get("label") or row.get("symbol"))
        if not name:
            continue
        current = result.get(name)
        if current is None or len(row) >= len(current):
            result[name] = row
    return result


def _index_change(row: dict[str, Any]) -> float | None:
    for key in ("day_change_pct", "quote_change_pct", "latest_change_pct", "change_pct"):
        value = _float_value(row.get(key))
        if value is not None:
            return value
    return None


def _index_structure_text(row: dict[str, Any], label: str) -> str:
    change = _index_change(row)
    change_text = _fmt_pct(change) if change is not None else "N/A"
    daily = _text(row.get("daily_trend") or row.get("daily_stage"))
    f30 = _text(row.get("f30_trend") or row.get("m30_trend") or row.get("thirty_trend"))
    f15 = _text(row.get("f15_trend") or row.get("m15_trend") or row.get("fifteen_trend"))
    structure = [f"日:{daily}" for daily in [daily] if daily]
    if f30:
        structure.append(f"30m:{f30}")
    if f15:
        structure.append(f"15m:{f15}")
    signal = _text(row.get("latest_signal") or row.get("f15_latest_signal") or row.get("signal"))
    ma_context = row.get("ma_context") if isinstance(row.get("ma_context"), dict) else {}
    ma_summary = _text(ma_context.get("trend_summary"))
    detail = "/".join(structure)
    if not detail and ma_summary:
        detail = ma_summary
    suffix = f"({detail})" if detail else ""
    if signal:
        suffix = f"{suffix}{'，' if suffix else '('}{signal}{')' if not suffix else ''}"
    return f"{label}{change_text}{suffix}"


def _weak_index_labels(shell: dict[str, Any]) -> list[str]:
    rows = _major_index_rows(shell)
    labels: list[str] = []
    for name, label in INDEX_DISPLAY_ORDER:
        row = rows.get(name)
        if not row:
            continue
        change = _index_change(row)
        daily = _text(row.get("daily_trend"))
        f30 = _text(row.get("f30_trend") or row.get("m30_trend"))
        signal = _text(row.get("latest_signal") or row.get("f15_latest_signal") or row.get("signal"))
        if "下跌" in f30 or "卖" in signal or (change is not None and change < -0.5 and "上涨" not in daily):
            labels.append(label)
    return labels


def _wechat_index_structure_lines(shell: dict[str, Any]) -> list[str]:
    rows = _major_index_rows(shell)
    parts = []
    for name, label in INDEX_DISPLAY_ORDER:
        row = rows.get(name)
        if row:
            parts.append(_index_structure_text(row, label))
    if not parts:
        return ["- 指数结构：缺少四大指数明细，不能用概括性风格判断替代。"]
    weak = _weak_index_labels(shell)
    if weak:
        read = f"{'、'.join(weak)}仍偏弱，先按指数压力处理"
    else:
        read = "四大指数未同时转弱，但仍要看30m结构能否延续"
    return [
        f"- 指数结构：{'；'.join(parts)}。",
        f"- 指数结论：{read}；金融拉升只能托指数，成长线要看创业板、科创50和恒科代理是否同步修复。",
    ]


def _row_search_text(row: dict[str, Any]) -> str:
    return " ".join(
        [
            _symbol_name(row),
            _get_chain(row),
            _get_action(row),
            _get_trigger(row),
            _text(row.get("daily_weekly_signal")),
        ]
    )


def _find_pool_row(shell: dict[str, Any], keywords: tuple[str, ...]) -> tuple[str, dict[str, Any]] | None:
    action_rows, watch_rows, risk_rows = _collect_rows(shell)
    for pool_name, rows in (("买点池", action_rows), ("盯盘池", watch_rows), ("风险池", risk_rows)):
        for row in rows:
            text = _row_search_text(row)
            if any(keyword in text for keyword in keywords):
                return pool_name, row
    return None


def _wechat_hstech_line(shell: dict[str, Any]) -> str | None:
    found = _find_pool_row(shell, ("恒生科技", "恒科", "HK.800700", "513130"))
    if not found:
        return None
    pool_name, row = found
    return (
        f"- 恒科代理：{_symbol_name(row)}在{pool_name}，触发：{_compact_trigger(row)}；"
        f"未脱离{pool_name}前，只按反抽观察。"
    )


def _sector_search_text(row: dict[str, Any]) -> str:
    driver = row.get("source_driver") if isinstance(row.get("source_driver"), dict) else {}
    primary = row.get("primary_domain") if isinstance(row.get("primary_domain"), dict) else {}
    reps = _representative_summary(row, limit=4)
    rep_names = " ".join(_text(item.get("name")) for item in reps)
    return " ".join(
        [
            _sector_name(row),
            _text(driver.get("name")),
            _text(primary.get("name")),
            _text(row.get("trader_action") or row.get("rank_reason") or row.get("trace_summary")),
            rep_names,
        ]
    )


def _sector_rows_matching(rows: list[dict[str, Any]], keywords: tuple[str, ...], *, limit: int = 2) -> list[dict[str, Any]]:
    selected = []
    for row in rows:
        text = _sector_search_text(row)
        if any(keyword in text for keyword in keywords):
            selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def _sector_representatives(row: dict[str, Any], *, limit: int = 3) -> str:
    names = [_text(item.get("name")) for item in _representative_summary(row, limit=limit) if _text(item.get("name"))]
    return "、".join(names[:limit]) if names else "代表未明"


def _sector_representatives_for_rows(rows: list[dict[str, Any]], *, limit: int = 4) -> str:
    names: list[str] = []
    for row in rows:
        for item in _representative_summary(row, limit=limit):
            name = _text(item.get("name"))
            if name and name not in names:
                names.append(name)
            if len(names) >= limit:
                return "、".join(names)
    return "、".join(names) if names else "代表未明"


def _sector_brief(row: dict[str, Any]) -> str:
    change = _sector_change(row)
    change_text = f" {_fmt_pct(change)}" if change is not None else ""
    return f"{_sector_name(row)}{change_text}"


def _sector_brief_list(rows: list[dict[str, Any]], *, limit: int = 2) -> str:
    return "、".join(_sector_brief(row) for row in rows[:limit]) if rows else "未进前排"


def _daily_weekly_parts(row: dict[str, Any]) -> list[str]:
    text = "；".join([_text(row.get("daily_weekly_signal")), _get_trigger(row)])
    for sep in ("，", "、", ",", "\n"):
        text = text.replace(sep, "；")
    negative_markers = ("缺日/周", "缺日", "缺周", "等待日", "等待周", "未确认", "还差", "缺少")
    parts: list[str] = []
    for part in [item.strip() for item in text.split("；") if item.strip()]:
        part = part.removeprefix("日/周:").strip()
        if not ("日线" in part or "周线" in part):
            continue
        if any(marker in part for marker in negative_markers):
            continue
        if part not in parts:
            parts.append(part)
    return parts[:2]


def _missing_daily_weekly(row: dict[str, Any]) -> bool:
    text = _row_search_text(row)
    return any(marker in text for marker in ("缺日/周", "等待日/周", "等待日线", "等待周线", "缺日", "缺周"))


def _signal_object(row: dict[str, Any], parts: list[str] | None = None) -> str:
    parts = parts if parts is not None else _daily_weekly_parts(row)
    if parts:
        return f"{_symbol_name(row)}({'/'.join(parts[:2])})"
    return _symbol_name(row)


def _wechat_pool_structure_lines(
    action_rows: list[dict[str, Any]],
    watch_rows: list[dict[str, Any]],
    risk_rows: list[dict[str, Any]],
) -> list[str]:
    rows = _dedupe(action_rows + watch_rows + risk_rows)
    chain_counts: dict[str, int] = {}
    for row in rows:
        chain = _get_chain(row)
        if chain:
            chain_counts[chain] = chain_counts.get(chain, 0) + 1
    if chain_counts:
        top_chain, top_count = max(chain_counts.items(), key=lambda item: item[1])
        common_line = f"- 三池共性：{top_chain}出现{top_count}次；先分清日/周确认和分钟反抽，不把同板块全部升级。"
    else:
        common_line = "- 三池共性：当前候选未形成稳定同板块合力，按单票证据分层复核。"

    positive = [(row, parts) for row in rows if (parts := _daily_weekly_parts(row))]
    missing = [row for row in rows if not _daily_weekly_parts(row) and _missing_daily_weekly(row)]
    if positive:
        positive_line = "- 日/周有买点：" + "、".join(_signal_object(row, parts) for row, parts in positive[:4]) + "。"
    else:
        positive_line = "- 日/周有买点：当前复核池未给出明确日线/周线买点。"
    if missing:
        missing_line = "- 只有分钟反抽/缺日周：" + "、".join(_symbol_name(row) for row in missing[:4]) + "；午后不按进攻主线处理。"
    else:
        missing_line = "- 只有分钟反抽/缺日周：当前入选对象未集中暴露该问题。"
    return [common_line, positive_line, missing_line]


def _window_check_text(window: str) -> str:
    return {
        "preopen": "开盘后",
        "ten": "10:00前",
        "midday": "下午开盘后",
        "two": "14:00前",
        "close": "收盘前",
        "postmarket": "明日竞价",
        "weekly": "下周开盘",
    }.get(window, "下一窗口")


def _wechat_market_read_line(window: str, shell: dict[str, Any]) -> str:
    check = _window_check_text(window)
    weak = _weak_index_labels(shell)
    weak_text = "、".join(weak) if weak else "指数"
    if window == "preopen":
        return f"- 盘面含义：先定今日观察框架，{check}只看指数承接和强板块扩散，不临盘扩散新题材。"
    if window in {"ten", "two"}:
        deadline = check.removesuffix("前")
        return f"- 盘面含义：{weak_text}若到{deadline}仍未修复30m结构，强板块也只能按局部修复看。"
    if window == "midday":
        return f"- 盘面含义：{weak_text}上午仍有压力，下午先验证金融护盘能否传导到科技修复；传导失败就只算反抽。"
    if window == "close":
        return "- 盘面含义：先分清全天是主线延续、链内轮动还是尾盘退潮，再决定哪些对象进入盘后回测。"
    return "- 盘面含义：先看方向、节奏和证据质量，再决定是否进入下一轮复核。"


def _wechat_market_lines(
    dashboard: dict[str, Any],
    shell: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    window: str,
    event_lines: list[str],
) -> list[str]:
    brief = dashboard.get("daily_brief") if isinstance(dashboard.get("daily_brief"), dict) else {}
    as_of = _text(brief.get("as_of") or snapshot.get("as_of") or dashboard.get("status"), "unknown")
    lines = [f"- 交易日：{as_of}。"]
    lines.extend(_wechat_index_structure_lines(shell))
    hstech_line = _wechat_hstech_line(shell)
    if hstech_line:
        lines.append(hstech_line)
    if event_lines:
        lines.append(f"- 关键盘面事件：{'；'.join(event_lines[:2])}。")
    else:
        lines.append("- 关键盘面事件：暂未看到新的指数/风格异常，继续按主线和候选触发复核。")
    lines.append(_wechat_market_read_line(window, shell))
    return lines


def _wechat_sector_role_lines(dashboard: dict[str, Any], shell: dict[str, Any], *, window: str) -> list[str]:
    rows = _sector_board_rows(shell, dashboard, limit=8)
    scope = "截至当前窗口" if window in {"ten", "midday", "two", "close"} else "当前"
    if not rows:
        return [f"- 板块卡位：{scope}缺少板块15排序，不写主线判断，只按候选触发复核。"]
    financial = _sector_rows_matching(rows, ("大金融", "银行", "保险", "券商"), limit=2)
    tech = _sector_rows_matching(
        rows,
        ("半导体", "芯片", "光刻", "科创", "AI", "算力", "数据中心", "PCB", "通信", "消费电子", "机器人", "软件"),
        limit=3,
    )
    used = {id(row) for row in [*financial, *tech]}
    rotation = [row for row in rows if id(row) not in used][:2]
    financial_line = (
        f"- 金融护盘：{scope}{_sector_brief_list(financial)}；代表{_sector_representatives(financial[0])}。"
        "这条线负责托指数，不等于成长线确认。"
        if financial
        else f"- 金融护盘：{scope}大金融未进板块前排，指数判断以四大指数结构为准。"
    )
    tech_line = (
        f"- 科技修复：{_sector_brief_list(tech, limit=3)}；代表{_sector_representatives_for_rows(tech)}。"
        "需要创业板、科创50和恒科代理同步修复，否则按局部反抽。"
        if tech
        else "- 科技修复：半导体/芯片/成长线未进前排，不把单票反弹外推为主线。"
    )
    rotation_line = (
        f"- 轮动补充：{_sector_brief_list(rotation)}；只做卡位观察，不覆盖金融护盘和科技修复这两条主判断。"
        if rotation
        else "- 轮动补充：暂无第三类强轮动，午后重点仍在金融护盘和科技修复。"
    )
    return [
        f"- 前排强度：{scope}前排={_sector_strength_line(rows, limit=5)}。",
        financial_line,
        tech_line,
        rotation_line,
    ]


def _wechat_abandon_lines(
    dashboard: dict[str, Any],
    snapshot: dict[str, Any],
    shell: dict[str, Any],
    selected_action: list[dict[str, Any]],
    selected_watch: list[dict[str, Any]],
    selected_risk: list[dict[str, Any]],
) -> list[str]:
    theme = _primary_theme(dashboard, snapshot)
    weak = _weak_index_labels(shell)
    weak_text = "、".join(weak) if weak else "指数"
    rows = _dedupe(selected_action + selected_watch + selected_risk)
    missing = [row for row in rows if _missing_daily_weekly(row)]
    lines = [
        f"- {weak_text}午后仍不能修复30m结构，上午结论降级为弱反抽。",
        f"- 金融/保险继续独拉，但{theme}、半导体、创业板或恒科代理不扩散，不把盘面升级。",
    ]
    if missing:
        lines.append(f"- {_compact_row_names(missing, limit=3)}仍缺日/周买点，只看分钟级反抽。")
    else:
        lines.append("- 复核对象若只剩5m/15m触发、上级周期转弱，直接移出午后复核。")
    if selected_risk:
        lines.append(f"- 风险池{_compact_row_names(selected_risk, limit=3)}未解除前，相关方向按排雷处理。")
    return lines


def _wechat_backtest_line(
    window: str,
    dashboard: dict[str, Any],
    shell: dict[str, Any],
    snapshot: dict[str, Any],
    action_rows: list[dict[str, Any]],
    watch_rows: list[dict[str, Any]],
) -> str:
    action_chains = {_get_chain(row) for row in action_rows if _get_chain(row)}
    watch_chains = {_get_chain(row) for row in watch_rows if _get_chain(row)}
    common = sorted(action_chains & watch_chains)
    if common:
        target = f"“{common[0]} + 今日触发信号”是否真的有承接"
    else:
        theme = _primary_theme(dashboard, snapshot)
        target = f"{theme}是否维持、关键盘面事件是否修复"
    weak = _weak_index_labels(shell)
    weak_text = "、".join(weak) if weak else "指数"
    if window == "preopen":
        return f"- 开盘后只验预设主线、四大指数承接和候选对象，不因为竞价单点异动临时扩散；盘后再复盘{target}。"
    if window == "ten":
        return f"- 10:00前只验{weak_text}能否修复30m、前排板块能否继续扩散；盘后只回测{target}。"
    if window == "midday":
        return (
            "- 午后只验三件事：大金融护盘能否稳住上证；半导体/芯片能否保持前排且链主不回落；"
            "创业板、科创50、恒科代理能否从分钟反抽转成30m修复。任一环节断，上午科技修复降级。"
        )
    if window == "two":
        return f"- 14:00前只验变盘方向有没有链主确认、{weak_text}有没有修复；尾盘不新增研究对象，盘后只复盘{target}。"
    if window == "close":
        return f"- 收盘前先处理风险和隔夜资格；盘后只回测{target}。"
    return f"- 盘后只复盘{target}；候选分散时不扩散回测。"


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
    selected_action, selected_watch, selected_risk = _selected_rows(window, shell, max_items=max_items)
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


def build_wechat_summary(
    dashboard: dict[str, Any],
    shell: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    window: str = "manual",
    max_items: int = 3,
    event_lines: list[str] | None = None,
) -> SummaryResult:
    selected_action, selected_watch, selected_risk = _selected_rows(window, shell, max_items=max_items)
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

    market_heading, review_heading, backtest_heading = WECHAT_HEADINGS.get(window, WECHAT_HEADINGS["manual"])
    lines = [
        status,
        f"1) {market_heading}",
        *_wechat_market_lines(dashboard, shell, snapshot, window=window, event_lines=event_lines),
        "",
        "2) 板块卡位",
        *_wechat_sector_role_lines(dashboard, shell, window=window),
        "",
        f"3) {review_heading}",
        *_wechat_pool_structure_lines(selected_action, selected_watch, selected_risk),
        *_format_wechat_review_rows(selected_action, selected_watch, selected_risk, limit=max(1, min(max_items, 3))),
        "",
        "4) 降级条件",
        *_wechat_abandon_lines(dashboard, snapshot, shell, selected_action, selected_watch, selected_risk),
        "",
        f"5) {backtest_heading}",
        _wechat_backtest_line(window, dashboard, shell, snapshot, selected_action, selected_watch),
    ]
    return SummaryResult(status=status, text="\n".join(lines), notify=notify, reason=reason)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a trader-facing Signals workbench summary.")
    parser.add_argument("--base-url", default=os.getenv("SIGNALS_WEB_BASE_URL", "http://127.0.0.1:8011"))
    parser.add_argument("--window", choices=sorted(WINDOW_LABELS), default="manual")
    parser.add_argument("--trade-date", default="", help="Historical YYYY-MM-DD trade date for narrative replay.")
    parser.add_argument("--max-items", type=int, default=3)
    parser.add_argument(
        "--safe-inputs",
        action="store_true",
        help="Use bounded parallel API fetches and emit a gate even when optional workbench inputs time out.",
    )
    parser.add_argument("--input-timeout", type=float, default=6.0, help="Per-run input budget in seconds for --safe-inputs.")
    parser.add_argument("--send", action="store_true", help="Send to configured notification channels when result is NOTIFY.")
    parser.add_argument("--send-all", action="store_true", help="Send even when result is DONT_NOTIFY.")
    parser.add_argument("--ignore-time", action="store_true", help="Bypass A-share day/window gating for dry-run review.")
    parser.add_argument(
        "--allow-ignore-time-notify",
        action="store_true",
        help="Allow --ignore-time dry-runs to keep a NOTIFY gate. Normal automation must leave this off.",
    )
    parser.add_argument("--format", choices=["workbench", "wechat", "narrative"], default="workbench")
    parser.add_argument("--training-sample", default="", help="Render a structured replay training sample; normal daily runs leave this empty.")
    parser.add_argument(
        "--allow-training-sample-send",
        action="store_true",
        help="Explicitly allow a golden training sample to use NOTIFY and --send. Normal automation must leave this off.",
    )
    parser.add_argument("--eval-target", default="", help="Evaluate the generated body against a replay reference before sending.")
    parser.add_argument("--min-similarity", type=float, default=0.0, help="Minimum replay target similarity required when --eval-target is set.")
    parser.add_argument("--require-eval-phrases", action="store_true", help="Require all replay target key phrases before sending.")
    parser.add_argument(
        "--extra-fact",
        action="append",
        default=[],
        help="Add an externally verified replay fact such as a high-volume core, failed board, net-flow figure, or exact intraday price.",
    )
    args = parser.parse_args(argv)

    allowed, gate_reason = window_gate(args.window)
    if not allowed and not args.ignore_time and not args.training_sample:
        title = WINDOW_LABELS.get(args.window, WINDOW_LABELS["manual"])
        print(f"DONT_NOTIFY\n[Signals 工作台 | {title}]\n原因：{gate_reason}")
        return 0

    if args.training_sample:
        from signals.replay.training_renderer import load_training_facts, render_training_sample

        body = render_training_sample(load_training_facts(args.training_sample))
        status = "NOTIFY" if args.allow_training_sample_send else "DONT_NOTIFY"
        result = SummaryResult(
            status=status,
            text=f"{status}\n{body}",
            notify=args.allow_training_sample_send,
            reason="training_sample" if args.allow_training_sample_send else "training_sample_send_blocked",
        )
    else:
        if args.safe_inputs:
            fetched = fetch_inputs_safe(args.base_url, timeout=args.input_timeout)
            dashboard, shell, snapshot = fetched.dashboard, fetched.shell, fetched.snapshot
            if not (dashboard or shell or snapshot):
                title = WINDOW_LABELS.get(args.window, WINDOW_LABELS["manual"])
                details = ";".join(f"{name}={error}" for name, error in sorted(fetched.errors.items()))
                reason = f"input_unavailable:{details}" if details else "input_unavailable"
                print(f"DONT_NOTIFY\n[Signals 工作台 | {title}]\n原因：{reason}")
                return 0
        else:
            dashboard, shell, snapshot = fetch_inputs(args.base_url)
        source_trade_date = _as_of_date(dashboard, snapshot)
        requested_trade_date = _text(args.trade_date)
        event_lines = (
            fetch_market_event_lines(args.base_url, window=args.window)
            if args.window in {"ten", "midday", "two", "close"}
            else []
        )
        if args.format == "wechat":
            builder = build_wechat_summary
        elif args.format == "narrative":
            builder = build_narrative_review
        else:
            builder = build_summary
        market_replay_sections: list[str] = []
        replay_trade_date = requested_trade_date or source_trade_date
        if args.format == "narrative" and replay_trade_date != "unknown":
            try:
                from signals.replay.market_replay import build_market_replay_context, format_market_replay_sections
                from signals.sync.db import get_db

                groups = shell.get("watchlist_groups") if isinstance(shell.get("watchlist_groups"), dict) else {}
                sector_boards = _as_list(groups.get("sector_boards"))
                historical_requested = bool(requested_trade_date and requested_trade_date != source_trade_date)
                replay_context = build_market_replay_context(
                    get_db(),
                    trade_date=replay_trade_date,
                    sector_boards=[] if historical_requested else sector_boards,
                    representative_limit=max(20, args.max_items * 4),
                    include_external_fund_flows=args.window in {"postmarket", "manual", "weekly"},
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
        builder_kwargs: dict[str, Any] = {
            "window": args.window,
            "max_items": max(1, args.max_items),
            "event_lines": event_lines,
        }
        if args.format == "narrative":
            builder_kwargs["extra_facts"] = args.extra_fact
            builder_kwargs["market_replay_sections"] = market_replay_sections
        result = builder(dashboard, shell, snapshot, **builder_kwargs)

    if (
        args.ignore_time
        and not allowed
        and not args.allow_ignore_time_notify
        and not args.training_sample
        and result.status == "NOTIFY"
    ):
        title = WINDOW_LABELS.get(args.window, WINDOW_LABELS["manual"])
        result = SummaryResult(
            status="DONT_NOTIFY",
            text=f"DONT_NOTIFY\n[Signals 工作台 | {title}]\n原因：dry_run:{gate_reason}\n\n{_send_body(result.text)}",
            notify=False,
            reason=f"dry_run:{gate_reason}",
        )

    eval_failed = False
    eval_report: dict[str, Any] | None = None
    if args.eval_target:
        from signals.replay.evaluate import evaluate_text, load_text

        eval_report = evaluate_text(_send_body(result.text), load_text(args.eval_target))
        eval_failed = eval_report["char_similarity"] < args.min_similarity
        if args.require_eval_phrases and eval_report["phrase_coverage"]["missing"]:
            eval_failed = True
        if eval_failed:
            block_line = (
                "[replay-eval] send blocked: "
                f"similarity={eval_report['char_similarity']}, "
                f"missing_phrases={len(eval_report['phrase_coverage']['missing'])}"
            )
            result = SummaryResult(
                status="DONT_NOTIFY",
                text=f"DONT_NOTIFY\n{block_line}\n\n{_send_body(result.text)}",
                notify=False,
                reason="replay_eval_failed",
            )

    print(result.text)
    if eval_report is not None:
        print("[replay-eval] " + json.dumps(eval_report, ensure_ascii=False, sort_keys=True))
        if eval_failed:
            print(
                "[replay-eval] send blocked: "
                f"similarity={eval_report['char_similarity']}, "
                f"missing_phrases={len(eval_report['phrase_coverage']['missing'])}"
            )

    training_sample_send_allowed = not args.training_sample or args.allow_training_sample_send
    ignore_time_send_allowed = not (args.ignore_time and not allowed and not args.allow_ignore_time_notify)
    if (
        args.send
        and (result.notify or args.send_all)
        and not eval_failed
        and training_sample_send_allowed
        and ignore_time_send_allowed
    ):
        from signals.notify import send_text

        send_text(_send_body(result.text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
