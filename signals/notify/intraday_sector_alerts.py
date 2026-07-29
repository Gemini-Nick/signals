# -*- coding: utf-8 -*-
"""Shadow-safe notifications for deterministic sector-transition events."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime
from typing import Any, Callable

from pymongo.database import Database

from signals.sync.task_context import get_task_env


ALERT_COLLECTION = "notification_events"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mode() -> str:
    value = str(
        get_task_env(
            "SECTOR_TRANSITION_NOTIFY_MODE",
            os.getenv("SECTOR_TRANSITION_NOTIFY_MODE", "shadow"),
        )
        or "shadow"
    ).strip().lower()
    return value if value in {"off", "shadow", "live"} else "shadow"


def _enabled() -> bool:
    value = get_task_env("SECTOR_TRANSITION_ENABLED", os.getenv("SECTOR_TRANSITION_ENABLED", "false"))
    return str(value or "false").strip().lower() in {"1", "true", "yes", "on"}


def _alert_id(event: dict[str, Any]) -> str:
    """Deduplicate semantic transitions, not individual scan watermarks."""
    raw = "|".join(
        (
            _text(event.get("episode_id")),
            _text(event.get("from_state")),
            _text(event.get("to_state")),
            _text(event.get("rule_version")),
        )
    )
    return "sector-transition-alert:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _message(event: dict[str, Any]) -> str:
    evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
    board = _text(event.get("sector_name")) or _text(event.get("sector_id")) or "未知板块"
    to_state = _text(event.get("to_state"))
    state_labels = {
        "panic_release": "恐慌释放线索",
        "repairing": "分钟级修复",
        "confirmed_intraday": "分钟级确认",
        "failed": "转折失效",
        "stable_turn": "稳定转折",
    }
    lines = [
        "Signals 板块转折雷达",
        f"{board} · {state_labels.get(to_state, to_state or '状态变化')}",
        f"时间：{_text(event.get('trade_date'))} {_text(event.get('event_minute'))}".rstrip(),
    ]
    change_pct = evidence.get("change_pct")
    breadth_ratio = evidence.get("breadth_ratio")
    if change_pct is not None or breadth_ratio is not None:
        parts = []
        if change_pct is not None:
            parts.append(f"涨跌幅 {float(change_pct):+.2f}%")
        if breadth_ratio is not None:
            parts.append(f"上涨宽度 {float(breadth_ratio) * 100:.0f}%")
        lines.append("证据：" + " · ".join(parts))
    sentinels = event.get("sentinel_symbols") or []
    if sentinels:
        lines.append("哨兵：" + "、".join(str(item) for item in sentinels[:4]))
    if to_state in {"panic_release", "repairing", "confirmed_intraday"}:
        lines.append("边界：这是分钟级结构，稳定转折仍需正式收盘和跨日确认。")
    return "\n".join(lines)


def process_sector_transition_events(
    db: Database,
    events: list[dict[str, Any]],
    *,
    notify_func: Callable[[str], Any] | None = None,
    gate_status: str = "DONT_NOTIFY",
) -> dict[str, Any]:
    """Record transitions; external delivery requires an injected, approved NOTIFY gate."""
    mode = _mode()
    if not _enabled() or mode == "off" or not events:
        return {
            "status": "disabled" if not _enabled() or mode == "off" else "ok",
            "recorded": 0,
            "sent": 0,
            "failed": 0,
            "mode": mode,
        }

    collection = db[ALERT_COLLECTION]
    recorded = 0
    sent = 0
    failed = 0
    live_requested = mode == "live"
    delivery_authorized = (
        live_requested
        and notify_func is not None
        and _text(gate_status).upper() == "NOTIFY"
    )
    gate_reason = "" if delivery_authorized else (
        "live_gate_unavailable" if live_requested else ""
    )

    pending: list[tuple[dict[str, Any], str, dict[str, Any], str]] = []
    seen_alert_ids: set[str] = set()
    for event in events:
        event_id = _text(event.get("_id") or event.get("event_id"))
        if not event_id:
            continue
        alert_id = _alert_id(event)
        if alert_id in seen_alert_ids:
            continue
        seen_alert_ids.add(alert_id)
        existing = collection.find_one({"_id": alert_id}) or {}
        if existing and (
            not live_requested
            or _text(existing.get("delivery_status")) == "sent"
            or (not delivery_authorized and _text(existing.get("delivery_status")) == "gate_blocked")
        ):
            continue
        pending.append((event, alert_id, existing, _message(event)))

    delivered = False
    error = ""
    if delivery_authorized and pending and notify_func is not None:
        merged_message = "\n\n---\n\n".join(item[3] for item in pending)
        try:
            notify_func(merged_message)
            delivered = True
            sent = len(pending)
        except Exception as exc:  # delivery must not fail the detector
            error = f"{exc.__class__.__name__}: {str(exc)[:240]}"
            failed = len(pending)

    for event, alert_id, existing, message in pending:
        event_id = _text(event.get("_id") or event.get("event_id"))
        collection.update_one(
            {"_id": alert_id},
            {
                "$setOnInsert": {
                    "_id": alert_id,
                    "domain": "sector_transition",
                    "kind": _text(event.get("event_type")) or "state_change",
                    "trade_date": _text(event.get("trade_date")),
                    "sector_id": _text(event.get("sector_id")),
                    "sector_name": _text(event.get("sector_name")),
                    "from_state": _text(event.get("from_state")),
                    "to_state": _text(event.get("to_state")),
                    "source_event_id": event_id,
                    "episode_id": _text(event.get("episode_id")),
                    "rule_version": _text(event.get("rule_version")),
                    "message": message,
                    "recorded_at": datetime.now(),
                },
                "$set": {
                    "delivery_mode": mode,
                    "delivery_status": (
                        "sent"
                        if delivered
                        else "retry_pending"
                        if delivery_authorized
                        else "gate_blocked"
                        if live_requested
                        else "shadow_recorded"
                    ),
                    "last_error": error or gate_reason,
                    "last_attempt_at": datetime.now(),
                },
                "$inc": {"delivery_attempts": 1 if delivery_authorized else 0},
            },
            upsert=True,
        )
        if not existing:
            recorded += 1
    return {
        "status": "partial" if failed else ("blocked" if live_requested and not delivery_authorized else "ok"),
        "recorded": recorded,
        "sent": sent,
        "failed": failed,
        "mode": mode,
        "reason": error or gate_reason,
    }
