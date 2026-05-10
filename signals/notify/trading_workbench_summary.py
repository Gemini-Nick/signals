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
from urllib.request import urlopen


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
        fetch_json(base_url, "/api/workbench/shell"),
        fetch_json(base_url, "/api/strategy/snapshot"),
    )


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
) -> SummaryResult:
    action_rows, watch_rows, risk_rows = _collect_rows(shell)
    selected_action, selected_watch, selected_risk = _window_rows(window, action_rows, watch_rows, risk_rows)
    selected_action = selected_action[:max_items]
    selected_watch = selected_watch[:max_items]
    selected_risk = selected_risk[:max_items]

    actionable_count = sum(1 for row in selected_action if _is_actionable(row))
    notify = bool(actionable_count or (window == "close" and selected_risk))
    status = "NOTIFY" if notify else "DONT_NOTIFY"
    reason = "actionable_workbench_items" if notify else "no_actionable_workbench_items"

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
    result = build_summary(dashboard, shell, snapshot, window=args.window, max_items=max(1, args.max_items))
    print(result.text)

    if args.send and (result.notify or args.send_all):
        from signals.notify import send_text

        send_text(result.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
