# -*- coding: utf-8 -*-
"""Intraday alerts for stocks entering the actionable buy pool."""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, time
from typing import Any, Callable

from pymongo.database import Database

logger = logging.getLogger(__name__)

ALERT_COLLECTION = "notification_events"
ENTRY_STAGES = {"attack_entry", "confirmed_entry"}
ENTRY_STATUSES = {"attack_entry", "entry_ready"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, default)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    text = _text(value)
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate)
        except Exception:
            continue
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt)], fmt)
        except Exception:
            continue
    return None


def _trade_day(value: Any) -> str:
    parsed = _parse_dt(value)
    if parsed:
        return parsed.date().isoformat()
    return _text(value)[:10]


def _time_label(value: Any) -> str:
    parsed = _parse_dt(value)
    if parsed:
        return parsed.strftime("%H:%M")
    return _text(value)


def _within_intraday_window(value: Any) -> bool:
    if not _env_bool("SIGNALS_INTRADAY_POOL_ALERT_REQUIRE_TRADING_WINDOW", True):
        return True
    parsed = _parse_dt(value)
    if parsed is None:
        return False
    start = time(9, 30)
    end = time(15, 10)
    return start <= parsed.time() <= end


def _channel_configured() -> bool:
    try:
        import config
    except Exception:
        return False

    weclaw_ok = bool(
        getattr(config, "WECLAW_ENABLED", False)
        and _text(getattr(config, "WECLAW_SEND_TO", ""))
    )
    feishu_ok = bool(
        _text(getattr(config, "FEISHU_APP_ID", ""))
        and _text(getattr(config, "FEISHU_APP_SECRET", ""))
        and _text(getattr(config, "FEISHU_RECEIVE_ID", ""))
    )
    return weclaw_ok or feishu_ok


def _alert_runtime_enabled(pool_doc: dict[str, Any], *, require_channel: bool = True) -> tuple[bool, str]:
    if not _env_bool("SIGNALS_INTRADAY_POOL_ALERT_ENABLED", True):
        return False, "disabled_by_env"
    if _env_bool("SIGNALS_INTRADAY_POOL_ALERT_REQUIRE_LIVE_LANE", True):
        lane = _text(os.getenv("SIGNALS_CURRENT_SYNC_LANE"))
        if lane != "workbench_lane":
            return False, f"not_live_workbench_lane:{lane or 'empty'}"
    if not _within_intraday_window(pool_doc.get("updated_at")):
        return False, "outside_intraday_window"
    if require_channel and not _channel_configured():
        return False, "notification_channel_not_configured"
    return True, ""


def _change_pct(row: dict[str, Any]) -> float | None:
    for key in ("change_pct", "day_change_pct", "daily_change_pct", "today_change_pct", "pct_chg"):
        value = _float(row.get(key))
        if value is not None:
            return value
    quote = row.get("quote") if isinstance(row.get("quote"), dict) else {}
    for key in ("change_pct", "day_change_pct", "pct_chg"):
        value = _float(quote.get(key))
        if value is not None:
            return value
    return None


def _latest_price(row: dict[str, Any]) -> float | None:
    for key in ("latest_price", "price", "close", "realtime_price"):
        value = _float(row.get(key))
        if value is not None:
            return value
    quote = row.get("quote") if isinstance(row.get("quote"), dict) else {}
    for key in ("latest_price", "price", "close"):
        value = _float(quote.get(key))
        if value is not None:
            return value
    return None


def _is_entry_row(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("can_trade_now") is True:
        return True
    return (
        _text(row.get("trade_stage")) in ENTRY_STAGES
        or _text(row.get("action_status")) in ENTRY_STATUSES
        or _text(row.get("queue_lane")) == "entry_ready"
    )


def _is_same_trade_day(row: dict[str, Any], trade_date: str) -> bool:
    event_day = _trade_day(
        row.get("event_latest_dt")
        or row.get("latest_dt")
        or (row.get("top_buy_reason") or {}).get("event_dt")
        or (row.get("top_buy_reason") or {}).get("dt")
    )
    return not event_day or event_day == trade_date


def _event_kind(row: dict[str, Any]) -> str:
    strong_pct = _env_float("SIGNALS_INTRADAY_POOL_ALERT_STRONG_CHANGE_PCT", 9.5)
    change_pct = _change_pct(row)
    if change_pct is not None and change_pct >= strong_pct:
        return "limit_move"
    return "entry"


def _event_id(row: dict[str, Any], trade_date: str, kind: str) -> str:
    symbol = _text(row.get("symbol") or row.get("code") or row.get("raw_code")).upper()
    return f"intraday-buy-pool:{trade_date}:{symbol}:{kind}"


def _format_pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def _format_price(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f}"


def _signal_text(row: dict[str, Any]) -> str:
    top = row.get("top_buy_reason") if isinstance(row.get("top_buy_reason"), dict) else {}
    for value in (
        row.get("latest_signal"),
        row.get("entry_reason"),
        top.get("signal_type"),
        row.get("reason"),
    ):
        text = _text(value)
        if text:
            return text
    return _text(row.get("stage_label") or "买点")


def _message_for(row: dict[str, Any], *, trade_date: str, pool_updated_at: Any, kind: str) -> str:
    name = _text(row.get("name")) or _text(row.get("display_name")) or "未知股票"
    symbol = _text(row.get("symbol") or row.get("code") or row.get("raw_code")).upper()
    change_pct = _change_pct(row)
    latest_price = _latest_price(row)
    stage = _text(row.get("stage_label") or row.get("trade_intent_label") or row.get("action_status"))
    action = _text(row.get("trader_action") or row.get("recommended_action") or row.get("next_action"))
    chain = _text(row.get("primary_chain") or (row.get("chain_position") or {}).get("chain")) if isinstance(row.get("chain_position"), dict) else _text(row.get("primary_chain"))
    logic = _text(row.get("entry_logic_summary"))
    invalidation = _text(row.get("invalidates_when") or row.get("invalidation"))
    title = "Signals 买点池提醒"
    if kind == "limit_move":
        title = "Signals 买点池提醒：强势/涨停"

    lines = [
        title,
        f"{name} {symbol}",
        f"状态：{stage or '买点池'} / {action or '复核'}",
        f"涨幅：{_format_pct(change_pct)}  最新：{_format_price(latest_price)}",
        f"触发：{_signal_text(row)}",
        f"时间：{trade_date} {_time_label(pool_updated_at)}",
    ]
    if chain:
        lines.append(f"产业链：{chain}")
    if logic:
        lines.append(f"路径：{logic}")
    if invalidation:
        lines.append(f"失效：{invalidation}")
    return "\n".join(lines)


def process_terminal_stock_pool_alerts(
    db: Database,
    pool_doc: dict[str, Any],
    *,
    notify_func: Callable[[str], Any] | None = None,
    require_channel: bool = True,
) -> dict[str, Any]:
    """Send deduped intraday notifications for actionable buy-pool rows."""
    enabled, reason = _alert_runtime_enabled(pool_doc, require_channel=require_channel)
    if not enabled:
        return {"status": "disabled", "reason": reason, "sent": 0, "candidates": 0}

    trade_date = _text(pool_doc.get("trade_date")) or _trade_day(pool_doc.get("dt") or pool_doc.get("updated_at"))
    rows = pool_doc.get("focus_stocks") or pool_doc.get("stocks") or []
    max_per_run = _env_int("SIGNALS_INTRADAY_POOL_ALERT_MAX_PER_RUN", 5)
    min_change_pct = _env_float("SIGNALS_INTRADAY_POOL_ALERT_MIN_CHANGE_PCT", 0.0)
    events = db[ALERT_COLLECTION]
    sent = 0
    candidates = 0

    if notify_func is None:
        from signals.notify import send_text as notify_func

    for row in rows:
        if sent >= max_per_run:
            break
        if not isinstance(row, dict) or not _is_entry_row(row) or not _is_same_trade_day(row, trade_date):
            continue
        change_pct = _change_pct(row)
        if change_pct is not None and change_pct < min_change_pct:
            continue
        kind = _event_kind(row)
        event_id = _event_id(row, trade_date, kind)
        candidates += 1
        if events.find_one({"_id": event_id}, {"_id": 1}):
            continue
        message = _message_for(row, trade_date=trade_date, pool_updated_at=pool_doc.get("updated_at"), kind=kind)
        notify_func(message)
        now = datetime.now()
        events.update_one(
            {"_id": event_id},
            {"$setOnInsert": {
                "_id": event_id,
                "domain": "terminal_pool",
                "kind": kind,
                "trade_date": trade_date,
                "symbol": _text(row.get("symbol") or row.get("code") or row.get("raw_code")).upper(),
                "name": _text(row.get("name") or row.get("display_name")),
                "action_status": _text(row.get("action_status")),
                "trade_stage": _text(row.get("trade_stage")),
                "change_pct": change_pct,
                "latest_price": _latest_price(row),
                "event_latest_dt": _text(row.get("event_latest_dt")),
                "notified_at": now,
                "pool_updated_at": pool_doc.get("updated_at"),
                "message": message,
            }},
            upsert=True,
        )
        sent += 1

    return {"status": "ok", "sent": sent, "candidates": candidates}
