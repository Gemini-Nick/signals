# -*- coding: utf-8 -*-
"""Deterministic sector-transition discovery and close rollup.

The live scan is cache-only and defaults to disabled.  It records current
state plus append-only state changes; it never requires a stock to be in the
terminal three-pool universe first.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta
from typing import Any

from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.core.trading_dates import a_share_realtime_day_key
from signals.notify.intraday_sector_alerts import process_sector_transition_events
from signals.sync.task_context import get_task_env


ACTIVE_STATES = {"panic_release", "repairing", "confirmed_intraday", "stable_turn"}
MINUTE_FREQS = ("5分钟", "5min", "5m", "F5")
RULE_VERSION = "sector-transition-v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int, *, minimum: int = 1, maximum: int = 3600) -> int:
    try:
        return min(maximum, max(minimum, int(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return min(maximum, max(minimum, default))


def _enabled() -> bool:
    value = _text(get_task_env("SECTOR_TRANSITION_ENABLED", os.getenv("SECTOR_TRANSITION_ENABLED", "false"))).lower()
    return value in {"1", "true", "yes", "on"}


def _mode() -> str:
    value = _text(
        get_task_env(
            "SECTOR_TRANSITION_NOTIFY_MODE",
            os.getenv("SECTOR_TRANSITION_NOTIFY_MODE", "shadow"),
        )
    ).lower()
    return value if value in {"off", "shadow", "live"} else "shadow"


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def _pure_code(value: Any) -> str:
    raw = _text(value).upper()
    if "." in raw:
        left, right = raw.split(".", 1)
        raw = right if left in {"SH", "SZ", "BJ"} else left
    raw = raw.replace("SH", "").replace("SZ", "").replace("BJ", "")
    return raw if raw.isdigit() and len(raw) == 6 else ""


def _prefixed_symbol(code: str) -> str:
    if code.startswith(("6", "9")):
        return f"SH.{code}"
    if code.startswith(("4", "8")):
        return f"BJ.{code}"
    return f"SZ.{code}"


def _latest_board_rows(db: Database, trade_date: str) -> tuple[datetime | None, list[dict[str, Any]]]:
    day_start = datetime.fromisoformat(trade_date)
    latest = db["board_heat_ticks"].find_one(
        {
            "trade_minute": {"$gte": day_start, "$lt": day_start + timedelta(days=1)},
            "kind": {"$in": ["industry", "concept"]},
        },
        {"trade_minute": 1},
        sort=[("trade_minute", -1)],
    ) or {}
    minute = _as_datetime(latest.get("trade_minute"))
    if minute is None:
        return None, []
    rows = list(
        db["board_heat_ticks"].find(
            {"trade_minute": minute, "kind": {"$in": ["industry", "concept"]}},
            {
                "_id": 0,
                "kind": 1,
                "name": 1,
                "code": 1,
                "change_pct": 1,
                "up_count": 1,
                "down_count": 1,
                "rank_idx": 1,
                "leader_name": 1,
                "leader_symbol": 1,
                "leader_change_pct": 1,
                "trade_minute": 1,
            },
        )
    )
    return minute, rows


def _previous_board_rows(
    db: Database,
    trade_date: str,
    latest_minute: datetime,
    *,
    lookback_minutes: int = 15,
) -> dict[tuple[str, str], dict[str, Any]]:
    day_start = datetime.fromisoformat(trade_date)
    target = latest_minute - timedelta(minutes=lookback_minutes)
    previous = db["board_heat_ticks"].find_one(
        {
            "trade_minute": {"$gte": day_start, "$lte": target},
            "kind": {"$in": ["industry", "concept"]},
        },
        {"trade_minute": 1},
        sort=[("trade_minute", -1)],
    ) or {}
    minute = _as_datetime(previous.get("trade_minute"))
    if minute is None:
        return {}
    return {
        (_text(row.get("kind")), _text(row.get("name"))): row
        for row in db["board_heat_ticks"].find(
            {"trade_minute": minute, "kind": {"$in": ["industry", "concept"]}},
            {"_id": 0, "kind": 1, "name": 1, "change_pct": 1, "up_count": 1, "down_count": 1},
        )
        if _text(row.get("kind")) and _text(row.get("name"))
    }


def _breadth(row: dict[str, Any]) -> float | None:
    up = int(_float(row.get("up_count"), 0) or 0)
    down = int(_float(row.get("down_count"), 0) or 0)
    total = up + down
    return round(up / total, 4) if total > 0 else None


def _constituents_by_sector(db: Database) -> dict[tuple[str, str], list[str]]:
    output: dict[tuple[str, str], list[str]] = {}
    for kind, collection, name_field in (
        ("industry", "board_constituents", "board_name"),
        ("concept", "concept_constituents", "concept_name"),
    ):
        try:
            cursor = db[collection].find(
                {"status": {"$in": ["ok", None]}},
                {"_id": 1, name_field: 1, "symbols": 1},
            )
            for row in cursor:
                name = _text(row.get(name_field) or row.get("_id"))
                codes = [_pure_code(value) for value in row.get("symbols") or []]
                if name:
                    output[(kind, name)] = [code for code in dict.fromkeys(codes) if code]
        except Exception:
            continue
    return output


def _spot_by_code(db: Database, trade_date: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    try:
        cursor = db["fullmarket_spot_snapshots"].find(
            {"$or": [{"trade_date": trade_date}, {"date_key": trade_date.replace("-", "")}]},
            {"code": 1, "symbol": 1, "amount": 1, "change_pct": 1, "market_cap": 1, "snapshot_at": 1},
        )
        for row in cursor:
            code = _pure_code(row.get("code") or row.get("symbol"))
            if code:
                result[code] = row
    except Exception:
        return {}
    return result


def _sentinels(
    row: dict[str, Any],
    constituents: dict[tuple[str, str], list[str]],
    spots: dict[str, dict[str, Any]],
    *,
    limit: int = 4,
) -> list[str]:
    kind = _text(row.get("kind"))
    name = _text(row.get("name"))
    leader = _pure_code(row.get("leader_symbol"))
    codes = list(constituents.get((kind, name), []))
    codes.sort(
        key=lambda code: (
            float(_float((spots.get(code) or {}).get("amount"), 0) or 0),
            abs(float(_float((spots.get(code) or {}).get("change_pct"), 0) or 0)),
        ),
        reverse=True,
    )
    ordered = [leader, *codes]
    return [_prefixed_symbol(code) for code in dict.fromkeys(ordered) if code][:limit]


def _eligible_sector_rows(
    rows: list[dict[str, Any]],
    constituents: dict[tuple[str, str], list[str]],
    spots: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Retain industries; admit liquid concepts and collapse >=80% Jaccard aliases."""
    industries: list[dict[str, Any]] = []
    concepts: list[tuple[dict[str, Any], set[str], float]] = []
    for original in rows:
        row = dict(original)
        kind = _text(row.get("kind"))
        name = _text(row.get("name"))
        members = set(constituents.get((kind, name), []))
        covered = [code for code in members if code in spots]
        amount = sum(float(_float(spots[code].get("amount"), 0.0) or 0.0) for code in covered)
        row["constituent_count"] = len(members)
        row["spot_coverage"] = round(len(covered) / len(members), 4) if members else None
        row["constituent_amount"] = round(amount, 2)
        if kind != "concept":
            industries.append(row)
            continue
        if len(members) >= 5 and len(covered) / len(members) >= 0.80:
            concepts.append((row, members, amount))
    if not concepts:
        return industries
    ranked_amounts = sorted(item[2] for item in concepts)
    p20 = ranked_amounts[max(0, int((len(ranked_amounts) - 1) * 0.20))]
    kept: list[tuple[dict[str, Any], set[str], float]] = []
    for item in sorted(concepts, key=lambda value: value[2], reverse=True):
        row, members, amount = item
        if amount < p20:
            continue
        if any(len(members & prior) / len(members | prior) >= 0.80 for _, prior, _ in kept):
            continue
        kept.append(item)
    return [*industries, *(row for row, _, _ in kept)]


def _latest_closed_5m(now: datetime) -> datetime:
    minute = now.minute - now.minute % 5
    return now.replace(minute=minute, second=0, microsecond=0) - timedelta(minutes=5)


def _sentinel_ma_evidence(
    db: Database,
    symbols: list[str],
    *,
    now: datetime,
) -> dict[str, Any]:
    if not symbols:
        return {
            "closed_bar_count": 0,
            "above_ma10_count": 0,
            "above_ma20_count": 0,
            "above_ma10_ratio": None,
            "above_ma20_ratio": None,
            "latest_closed_5m": None,
        }
    cutoff = _latest_closed_5m(now)
    max_age = _int_env("SIGNALS_SECTOR_TRANSITION_5M_MAX_AGE_MINUTES", 8, minimum=5, maximum=30)
    closed = above10 = above20 = 0
    latest_values: list[datetime] = []
    for symbol in symbols:
        code = _pure_code(symbol)
        docs = list(
            db["bars"].find(
                {
                    "meta.symbol": {"$in": [code, symbol]},
                    "meta.freq": {"$in": list(MINUTE_FREQS)},
                    "dt": {"$lte": cutoff},
                },
                {"_id": 0, "dt": 1, "close": 1},
            ).sort("dt", -1).limit(20)
        )
        docs.reverse()
        if len(docs) < 10:
            continue
        latest_dt = _as_datetime(docs[-1].get("dt"))
        if latest_dt is None or latest_dt.date() != now.date() or cutoff - latest_dt > timedelta(minutes=max_age):
            continue
        closes = [float(value) for value in (_float(doc.get("close")) for doc in docs) if value is not None]
        if len(closes) < 10:
            continue
        closed += 1
        latest_values.append(latest_dt)
        if closes[-1] >= sum(closes[-10:]) / 10:
            above10 += 1
        if len(closes) >= 20 and closes[-1] >= sum(closes[-20:]) / 20:
            above20 += 1
    return {
        "closed_bar_count": closed,
        "above_ma10_count": above10,
        "above_ma20_count": above20,
        "above_ma10_ratio": round(above10 / closed, 4) if closed else None,
        "above_ma20_ratio": round(above20 / closed, 4) if closed else None,
        "latest_closed_5m": max(latest_values).isoformat(timespec="minutes") if latest_values else None,
    }


def _limit_pool_evidence(
    db: Database,
    trade_date: str,
    *,
    now: datetime,
) -> dict[str, dict[str, Any]]:
    freshness = db["data_freshness"].find_one(
        {
            "domain": {"$in": ["market_limit_pools", "market_limit_pool"]},
            "market": "A",
            "trade_date": trade_date,
        },
        {
            "snapshot_at": 1,
            "snapshot_minute": 1,
            "updated_at": 1,
            "errors": 1,
            "freshness": 1,
            "pools": 1,
            "pool_status": 1,
        },
    ) or {}
    updated = _as_datetime(freshness.get("snapshot_at") or freshness.get("updated_at"))
    max_age = _int_env("SIGNALS_SECTOR_TRANSITION_LIMIT_MAX_AGE_MINUTES", 4, minimum=2, maximum=15)
    pool_status = freshness.get("pool_status") if isinstance(freshness.get("pool_status"), dict) else {}
    limit_down_status = pool_status.get("limit_down") if isinstance(pool_status.get("limit_down"), dict) else {}
    complete = bool(
        updated
        and now - updated <= timedelta(minutes=max_age)
        and _text(freshness.get("freshness")) == "fresh"
        and not (freshness.get("errors") or {})
        and "limit_down" in (freshness.get("pools") or [])
        and _text(limit_down_status.get("status")) in {"success", "success_empty"}
    )
    if not complete:
        return {}
    current_minute = _text(freshness.get("snapshot_minute"))
    if not current_minute:
        return {}
    minutes = list(
        db["market_limit_pools"].distinct(
            "snapshot_minute",
            {
                "trade_date": trade_date,
                "pool": "limit_down",
                "snapshot_minute": {"$lt": current_minute},
            },
        )
    )
    minutes = sorted({_text(value) for value in minutes if _text(value)})
    if not minutes:
        return {}
    previous_minute = minutes[-1]
    previous = list(
        db["market_limit_pools"].find(
            {"trade_date": trade_date, "snapshot_minute": previous_minute, "pool": "limit_down"},
            {"code": 1, "industry": 1, "seal_amount": 1},
        )
    )
    current = list(
        db["market_limit_pools"].find(
            {"trade_date": trade_date, "snapshot_minute": current_minute, "pool": "limit_down"},
            {"code": 1, "industry": 1, "seal_amount": 1},
        )
    )
    current_codes = {_pure_code(row.get("code")) for row in current}
    by_industry: dict[str, dict[str, Any]] = {}
    for row in previous:
        industry = _text(row.get("industry"))
        code = _pure_code(row.get("code"))
        if industry and code and code not in current_codes:
            item = by_industry.setdefault(industry, {"limit_down_exits": 0, "exited_symbols": []})
            item["limit_down_exits"] += 1
            item["exited_symbols"].append(_prefixed_symbol(code))
    for item in by_industry.values():
        item["previous_snapshot_minute"] = previous_minute
        item["current_snapshot_minute"] = current_minute
    return by_industry


def evaluate_transition(
    features: dict[str, Any],
    previous_state: str = "pressure",
) -> tuple[str, list[str]]:
    """Pure state decision; promotions above panic release require closed 5m bars."""
    blockers = list(features.get("freshness_blockers") or [])
    if blockers:
        return previous_state or "pressure", blockers

    change = float(features.get("change_pct") or 0.0)
    previous_change = features.get("previous_change_pct")
    breadth = features.get("breadth_ratio")
    previous_breadth = features.get("previous_breadth_ratio")
    delta = change - float(previous_change) if previous_change is not None else 0.0
    breadth_delta = (
        float(breadth) - float(previous_breadth)
        if breadth is not None and previous_breadth is not None
        else 0.0
    )
    exits = int(features.get("limit_down_exits") or 0)
    ma = features.get("sentinel_ma") if isinstance(features.get("sentinel_ma"), dict) else {}
    closed = int(ma.get("closed_bar_count") or 0)
    ma10 = ma.get("above_ma10_ratio")
    ma20 = ma.get("above_ma20_ratio")

    if closed <= 0:
        blockers.append("closed_5m_missing")
        return previous_state or "pressure", list(dict.fromkeys(blockers))

    risk_off = change <= -1.0 or (breadth is not None and float(breadth) < 0.30)
    if previous_state in ACTIVE_STATES and (risk_off or delta <= -0.8):
        return "failed", blockers

    release = bool(
        exits > 0
        or (
            previous_change is not None
            and float(previous_change) <= -0.5
            and delta >= 0.45
            and breadth_delta >= 0.08
        )
    )
    repair_candidate = bool(
        breadth is not None
        and float(breadth) >= 0.50
        and change >= -0.30
        and closed > 0
        and ma10 is not None
        and float(ma10) >= 0.50
    )
    confirm_candidate = bool(
        repair_candidate
        and previous_state in {"repairing", "confirmed_intraday", "stable_turn"}
        and float(breadth) >= 0.60
        and change >= 0.20
        and ma20 is not None
        and float(ma20) >= 0.50
    )
    if previous_state in {"pressure", "failed"}:
        return ("panic_release", blockers) if release else (previous_state, blockers)
    if confirm_candidate:
        return "confirmed_intraday", blockers
    if repair_candidate and (release or previous_state in ACTIVE_STATES):
        return "repairing", blockers
    if release:
        return "panic_release", blockers
    return "pressure", blockers


def _event_id(
    trade_date: str,
    sector_id: str,
    from_state: str,
    to_state: str,
    watermark: str,
    *,
    event_type: str = "state_change",
) -> str:
    raw = "|".join((trade_date, sector_id, from_state, to_state, watermark, event_type))
    return "sector-transition:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _freshness_contract(
    db: Database,
    *,
    now: datetime,
    trade_date: str,
    latest_minute: datetime | None,
) -> tuple[list[str], dict[str, Any]]:
    """Gate promotions on all four live inputs and expose their watermarks."""
    blockers: list[str] = []
    watermarks: dict[str, Any] = {
        "board_heat": latest_minute.isoformat(timespec="minutes") if latest_minute else None,
    }
    board_max_age = _int_env("SECTOR_TRANSITION_BOARD_MAX_AGE_SECONDS", 120, minimum=60, maximum=900)
    if latest_minute is None:
        blockers.append("board_heat_missing")
    elif now - latest_minute > timedelta(seconds=board_max_age):
        blockers.append("board_heat_stale")

    source_specs = (
        (
            "fullmarket",
            {
                "domain": "spot",
                "market": "A",
                "mode": "realtime",
                "collection": "fullmarket_spot_snapshots",
            },
            120,
        ),
        (
            "limit_pool",
            {
                "domain": {"$in": ["market_limit_pools", "market_limit_pool"]},
                "market": "A",
                "trade_date": trade_date,
            },
            4 * 60,
        ),
        (
            "technical",
            {
                "domain": "technical_signal",
                "market": "A",
                "mode": "realtime",
                "collection": "terminal_technical_signals",
            },
            6 * 60,
        ),
    )
    for label, query, max_age_seconds in source_specs:
        doc = db["data_freshness"].find_one(
            query,
            {
                "updated_at": 1,
                "snapshot_at": 1,
                "latest_dt": 1,
                "as_of": 1,
                "freshness": 1,
                "errors": 1,
                "pools": 1,
                "pool_status": 1,
                "stale_reason": 1,
            },
            sort=[("updated_at", -1)],
        ) or {}
        updated = _as_datetime(doc.get("snapshot_at") or doc.get("updated_at"))
        watermarks[label] = {
            "updated_at": updated.isoformat(timespec="seconds") if updated else None,
            "latest_dt": doc.get("latest_dt"),
            "as_of": doc.get("as_of"),
        }
        if not doc:
            blockers.append(f"{label}_missing")
        elif (
            _text(doc.get("freshness")) != "fresh"
            or (doc.get("errors") or {})
            or (label == "limit_pool" and "limit_down" not in (doc.get("pools") or []))
            or (
                label == "limit_pool"
                and _text(((doc.get("pool_status") or {}).get("limit_down") or {}).get("status"))
                not in {"success", "success_empty"}
            )
        ):
            blockers.append(f"{label}_incomplete")
        elif updated is None or now - updated > timedelta(seconds=max_age_seconds):
            blockers.append(f"{label}_stale")
    return blockers, watermarks


def _episode_id(sector_id: str, trade_date: str, observed_at: datetime) -> str:
    raw = f"{sector_id}|{trade_date}|{observed_at.isoformat(timespec='minutes')}|{RULE_VERSION}"
    return "sector-episode:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _next_checks(turn_state: str) -> list[str]:
    checks = {
        "panic_release": ["等待闭合5分钟K线", "观察上涨宽度是否继续扩散", "验证跌停退出是否延续"],
        "repairing": ["验证哨兵站稳5分钟MA20", "观察成交额承接", "等待分钟级确认"],
        "confirmed_intraday": ["等待正式收盘", "事件低点连续守住3个交易日", "验证日线MA5/MA10与量能回踩"],
        "stable_turn": ["观察事件低点与日线MA10是否继续守住", "跟踪资金代理是否扩散"],
        "failed": ["等待重新站回关键周期", "重新建立独立转折episode"],
        "pressure": ["等待恐慌释放或宽度改善"],
    }
    return checks.get(turn_state, checks["pressure"])


def _weaker_if(turn_state: str) -> list[str]:
    common = ["板块涨跌幅重新低于-1%", "上涨宽度跌破30%", "哨兵跌回事件低点"]
    if turn_state == "stable_turn":
        return [*common, "日线MA10失守且量能放大"]
    return common


def _flow_state(features: dict[str, Any], turn_state: str) -> str:
    """Conservative price/breadth proxy classifier; F4 is close-rollup only."""
    if turn_state in {"pressure", "failed"}:
        return "F0"
    delta = _float(features.get("change_delta_15m"), 0.0) or 0.0
    breadth_delta = _float(features.get("breadth_delta_15m"), 0.0) or 0.0
    breadth = _float(features.get("breadth_ratio"), 0.0) or 0.0
    amount_share = _float(features.get("amount_share"))
    if (
        turn_state == "confirmed_intraday"
        and features.get("donor_technology_decline") is True
        and amount_share is not None
        and amount_share > 0
        and delta > 0
        and breadth_delta > 0
        and breadth >= 0.60
    ):
        return "F3"
    if (
        turn_state in {"repairing", "confirmed_intraday"}
        and amount_share is not None
        and amount_share > 0
        and delta > 0
        and breadth_delta >= 0
    ):
        return "F2"
    return "F1"


def _funding_path(features: dict[str, Any], flow_state: str) -> dict[str, Any]:
    """Only describe observable price/breadth proxies; never assert fund causality."""
    delta = _float(features.get("change_delta_15m"), 0.0) or 0.0
    breadth_delta = _float(features.get("breadth_delta_15m"), 0.0) or 0.0
    proxy = "improving" if delta > 0 and breadth_delta > 0 else ("weakening" if delta < 0 else "mixed")
    return {
        "status": "causality_unproven",
        "causality": "unproven",
        "flow_state": flow_state,
        "proxy": proxy,
        "basis": ["board_change", "breadth", "turnover_proxy"],
    }


def _daily_sentinel_evidence(
    db: Database,
    sentinels: list[str],
    *,
    event_lows: dict[str, float],
) -> tuple[dict[str, Any], dict[str, float]]:
    checked = above5 = above10 = volume_ok = retest_ok = 0
    lows = dict(event_lows)
    for symbol in sentinels:
        code = _pure_code(symbol)
        docs = list(
            db["bars"].find(
                {"meta.symbol": code, "meta.freq": {"$in": ["日线", "daily"]}},
                {"dt": 1, "close": 1, "low": 1, "volume": 1, "vol": 1},
            ).sort("dt", -1).limit(11)
        )
        docs.reverse()
        closes = [_float(doc.get("close")) for doc in docs]
        if len(docs) < 10 or any(value is None for value in closes[-10:]):
            continue
        latest = docs[-1]
        close = float(closes[-1])
        low = _float(latest.get("low"))
        volume = _float(latest.get("volume") if latest.get("volume") is not None else latest.get("vol"))
        prior_volumes = [
            float(value)
            for value in (
                _float(doc.get("volume") if doc.get("volume") is not None else doc.get("vol"))
                for doc in docs[-6:-1]
            )
            if value is not None and value > 0
        ]
        if low is None:
            continue
        checked += 1
        event_low = lows.setdefault(code, float(low))
        above5 += int(close >= sum(float(value) for value in closes[-5:]) / 5)
        above10 += int(close >= sum(float(value) for value in closes[-10:]) / 10)
        volume_ok += int(bool(prior_volumes and volume is not None and volume >= 0.8 * (sum(prior_volumes) / len(prior_volumes))))
        retest_ok += int(float(low) >= event_low and close > event_low)
    ratio = lambda count: round(count / checked, 4) if checked else None
    return (
        {
            "checked_sentinels": checked,
            "above_ma5_ratio": ratio(above5),
            "above_ma10_ratio": ratio(above10),
            "volume_confirm_ratio": ratio(volume_ok),
            "retest_hold_ratio": ratio(retest_ok),
            "event_low_held": bool(checked and retest_ok == checked),
        },
        lows,
    )


def _stable_turn_blockers(daily_confirmation: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if int(daily_confirmation.get("event_low_hold_sessions") or 0) < 3:
        blockers.append("stable_requires_event_low_hold_3_sessions")
    if (_float(daily_confirmation.get("above_ma5_ratio"), 0.0) or 0.0) < 0.50:
        blockers.append("daily_ma5_not_confirmed")
    if (_float(daily_confirmation.get("above_ma10_ratio"), 0.0) or 0.0) < 0.50:
        blockers.append("daily_ma10_not_confirmed")
    if (_float(daily_confirmation.get("volume_confirm_ratio"), 0.0) or 0.0) < 0.50:
        blockers.append("volume_not_confirmed")
    if (_float(daily_confirmation.get("retest_hold_ratio"), 0.0) or 0.0) < 0.50:
        blockers.append("retest_not_confirmed")
    return blockers


def sync_sector_transition_scan(db: Database, proxy_url: str = None) -> dict[str, Any]:
    del proxy_url
    mode = _mode()
    if not _enabled():
        return {"status": "ok", "enabled": False, "mode": mode, "inserted": 0, "states": 0, "events": 0}

    now = naive_market_now("A")
    trade_date = a_share_realtime_day_key(now=now)
    latest_minute, rows = _latest_board_rows(db, trade_date)
    blockers, source_watermarks = _freshness_contract(
        db,
        now=now,
        trade_date=trade_date,
        latest_minute=latest_minute,
    )
    if latest_minute is None or not rows:
        return {
            "status": "partial",
            "mode": mode,
            "inserted": 0,
            "states": 0,
            "events": 0,
            "reason": ",".join(blockers or ["board_heat_empty"]),
        }

    previous_rows = _previous_board_rows(db, trade_date, latest_minute)
    constituents = _constituents_by_sector(db)
    spots = _spot_by_code(db, trade_date)
    rows = _eligible_sector_rows(rows, constituents, spots)
    total_market_amount = sum(float(_float(row.get("amount"), 0.0) or 0.0) for row in spots.values())
    board_changes = [float(value) for value in (_float(row.get("change_pct")) for row in rows) if value is not None]
    board_mean_change = sum(board_changes) / len(board_changes) if board_changes else 0.0
    limit_evidence = _limit_pool_evidence(db, trade_date, now=now)
    previous_states = {
        _text(doc.get("_id")): doc
        for doc in db["sector_transition_states"].find(
            {"market": "A"},
            {
                "_id": 1,
                "state": 1,
                "turn_state": 1,
                "episode_id": 1,
                "last_changed_at": 1,
                "updated_at": 1,
            },
        )
    }

    state_ops: list[UpdateOne] = []
    event_ops: list[UpdateOne] = []
    liquidity_ops: list[UpdateOne] = []
    event_docs: list[dict[str, Any]] = []
    ma_cache: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        kind = _text(row.get("kind"))
        name = _text(row.get("name"))
        if not kind or not name:
            continue
        sector_id = f"{kind}:{name}"
        previous_row = previous_rows.get((kind, name), {})
        current_breadth = _breadth(row)
        previous_breadth = _breadth(previous_row)
        sentinels = _sentinels(row, constituents, spots)
        previous_doc = previous_states.get(sector_id, {})
        previous_state = _text(previous_doc.get("turn_state") or previous_doc.get("state")) or "pressure"
        previous_change = _float(previous_row.get("change_pct"))
        change = _float(row.get("change_pct"), 0.0) or 0.0
        prelim_release = bool(
            previous_change is not None
            and previous_change <= -0.5
            and change - previous_change >= 0.45
            and current_breadth is not None
            and previous_breadth is not None
            and current_breadth - previous_breadth >= 0.08
        )
        active_context = previous_state in ACTIVE_STATES or prelim_release or name in limit_evidence
        sentinel_key = tuple(sentinels)
        if active_context and sentinel_key not in ma_cache:
            ma_cache[sentinel_key] = _sentinel_ma_evidence(db, sentinels, now=now)
        sentinel_ma = ma_cache.get(
            sentinel_key,
            {
                "closed_bar_count": 0,
                "above_ma10_count": 0,
                "above_ma20_count": 0,
                "above_ma10_ratio": None,
                "above_ma20_ratio": None,
                "latest_closed_5m": None,
            },
        )
        limit_item = limit_evidence.get(name, {})
        features = {
            "change_pct": round(change, 4),
            "previous_change_pct": previous_change,
            "breadth_ratio": current_breadth,
            "previous_breadth_ratio": previous_breadth,
            "change_delta_15m": round(change - previous_change, 4) if previous_change is not None else None,
            "breadth_delta_15m": (
                round(current_breadth - previous_breadth, 4)
                if current_breadth is not None and previous_breadth is not None
                else None
            ),
            "rank": row.get("rank_idx"),
            "leader_name": _text(row.get("leader_name")),
            "leader_symbol": _text(row.get("leader_symbol")),
            "leader_change_pct": _float(row.get("leader_change_pct")),
            "constituent_count": int(row.get("constituent_count") or 0),
            "spot_coverage": _float(row.get("spot_coverage")),
            "aggregated_amount": _float(row.get("constituent_amount"), 0.0) or 0.0,
            "amount_share": (
                round(float(row.get("constituent_amount") or 0.0) / total_market_amount, 8)
                if total_market_amount > 0
                else None
            ),
            "relative_strength": round(change - board_mean_change, 4),
            "limit_down_exits": int(limit_item.get("limit_down_exits") or 0),
            "exited_symbols": limit_item.get("exited_symbols") or [],
            "sentinel_ma": sentinel_ma,
            "freshness_blockers": blockers,
        }
        next_state, state_blockers = evaluate_transition(features, previous_state)
        if next_state in ACTIVE_STATES | {"failed"}:
            episode_id = _text(previous_doc.get("episode_id"))
            if not episode_id or (previous_state in {"pressure", "failed"} and next_state in ACTIVE_STATES):
                episode_id = _episode_id(sector_id, trade_date, latest_minute)
        else:
            episode_id = ""
        flow_state = _flow_state(features, next_state)
        if next_state in ACTIVE_STATES and flow_state == "F1" and features["amount_share"] is None:
            state_blockers = [*state_blockers, "amount_share_missing"]
        funding_path = _funding_path(features, flow_state)
        state_doc = {
            "_id": sector_id,
            "market": "A",
            "sector_id": sector_id,
            "sector_name": name,
            "kind": kind,
            "sector_kind": kind,
            "trade_date": trade_date,
            "episode_id": episode_id,
            "turn_state": next_state,
            "state": next_state,
            "flow_state": flow_state,
            "active": next_state in ACTIVE_STATES,
            "sentinel_symbols": sentinels,
            "sentinels": sentinels,
            "evidence": features,
            "metrics": features,
            "funding_path": funding_path,
            "next_checks": _next_checks(next_state),
            "weaker_if": _weaker_if(next_state),
            "rule_version": RULE_VERSION,
            "source_watermarks": source_watermarks,
            "blockers": state_blockers,
            "freshness_blockers": state_blockers,
            "mode": mode,
            "source": "sync.sector_transition_scan",
            "board_watermark": latest_minute,
            "observed_at": latest_minute,
            "last_changed_at": (
                now
                if next_state != previous_state
                else previous_doc.get("last_changed_at") or previous_doc.get("updated_at") or now
            ),
            "updated_at": now,
        }
        state_ops.append(UpdateOne({"_id": sector_id}, {"$set": state_doc}, upsert=True))
        liquidity_id = f"A:{sector_id}:{latest_minute.isoformat(timespec='minutes')}"
        liquidity_doc = {
            "_id": liquidity_id,
            "market": "A",
            "trade_date": trade_date,
            "sector_id": sector_id,
            "sector_name": name,
            "sector_kind": kind,
            "observed_at": latest_minute,
            "aggregated_amount": features["aggregated_amount"],
            "amount_share": features["amount_share"],
            "breadth": current_breadth,
            "relative_strength": features["relative_strength"],
            "sentinels": sentinels,
            "source_watermarks": source_watermarks,
            "rule_version": RULE_VERSION,
            "updated_at": now,
        }
        liquidity_ops.append(
            UpdateOne({"_id": liquidity_id}, {"$setOnInsert": liquidity_doc}, upsert=True)
        )
        if next_state != previous_state and (
            next_state in ACTIVE_STATES | {"failed"} or previous_state in ACTIVE_STATES
        ):
            watermark = latest_minute.isoformat(timespec="minutes")
            event_id = _event_id(trade_date, sector_id, previous_state, next_state, watermark)
            event_doc = {
                "_id": event_id,
                "event_id": event_id,
                "event_type": "state_change",
                "market": "A",
                "trade_date": trade_date,
                "event_minute": latest_minute.strftime("%H:%M"),
                "sector_id": sector_id,
                "sector_name": name,
                "kind": kind,
                "sector_kind": kind,
                "episode_id": episode_id,
                "from_state": previous_state,
                "to_state": next_state,
                "turn_state": next_state,
                "flow_state": flow_state,
                "sentinel_symbols": sentinels,
                "sentinels": sentinels,
                "evidence": features,
                "metrics": features,
                "funding_path": funding_path,
                "next_checks": _next_checks(next_state),
                "weaker_if": _weaker_if(next_state),
                "rule_version": RULE_VERSION,
                "source_watermarks": source_watermarks,
                "freshness_blockers": state_blockers,
                "blockers": state_blockers,
                "mode": mode,
                "source": "sync.sector_transition_scan",
                "observed_at": latest_minute,
                "updated_at": now,
                "created_at": now,
            }
            event_docs.append(event_doc)
            event_ops.append(UpdateOne({"_id": event_id}, {"$setOnInsert": event_doc}, upsert=True))

    if state_ops:
        db["sector_transition_states"].bulk_write(state_ops, ordered=False)
    if event_ops:
        db["sector_transition_events"].bulk_write(event_ops, ordered=False)
    if liquidity_ops:
        db["sector_liquidity_snapshots"].bulk_write(liquidity_ops, ordered=False)
    alert_result = process_sector_transition_events(db, event_docs)
    db["data_freshness"].update_one(
        {
            "domain": "sector_transition",
            "market": "A",
            "mode": "realtime",
            "collection": "sector_transition_states",
        },
        {
            "$set": {
                "domain": "sector_transition",
                "market": "A",
                "mode": "realtime",
                "lane": _text(get_task_env("SIGNALS_CURRENT_SYNC_LANE", "board_lane")) or "board_lane",
                "collection": "sector_transition_states",
                "freshness": "fresh" if not blockers else "partial",
                "latest_dt": latest_minute.isoformat(timespec="minutes"),
                "as_of": trade_date,
                "updated_at": now,
                "stale_reason": ",".join(blockers),
                "count": len(state_ops),
                "event_count": len(event_docs),
            }
        },
        upsert=True,
    )
    return {
        "status": "ok" if not blockers else "partial",
        "mode": mode,
        "inserted": len(event_docs),
        "states": len(state_ops),
        "events": len(event_docs),
        "liquidity_snapshots": len(liquidity_ops),
        "alerts": alert_result,
        "latest_dt": latest_minute,
        "freshness_blockers": blockers,
    }


def sync_sector_transition_rollup(db: Database, proxy_url: str = None) -> dict[str, Any]:
    """Close-confirm live transition states without promoting them to a trade entry."""
    del proxy_url
    mode = _mode()
    if not _enabled():
        return {"status": "ok", "enabled": False, "mode": mode, "inserted": 0, "sectors": 0}
    now = naive_market_now("A")
    trade_date = a_share_realtime_day_key(now=now)
    rows = list(db["sector_transition_states"].find({"market": "A", "trade_date": trade_date}))
    if not rows:
        return {"status": "ok", "mode": mode, "inserted": 0, "sectors": 0}

    operations: list[UpdateOne] = []
    event_ops: list[UpdateOne] = []
    event_docs: list[dict[str, Any]] = []
    for row in rows:
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        state = _text(row.get("turn_state") or row.get("state")) or "pressure"
        sentinels = list(row.get("sentinel_symbols") or [])
        codes = [_pure_code(value) for value in sentinels]
        climb_count = db["terminal_technical_signals"].count_documents(
            {
                "market": "A",
                "raw_code": {"$in": [code for code in codes if code]},
                "signal_family": "ma_climb",
                "active": {"$ne": False},
                "as_of": trade_date,
            }
        ) if codes else 0
        sector_id = _text(row.get("sector_id") or row.get("_id"))
        episode_id = _text(row.get("episode_id"))
        prior_daily = list(
            db["sector_transition_daily"].find(
                {
                    "market": "A",
                    "sector_id": sector_id,
                    "episode_id": episode_id,
                    "trade_date": {"$lt": trade_date},
                },
                {"trade_date": 1, "daily_confirmation": 1, "event_lows": 1},
            ).sort("trade_date", -1).limit(2)
        ) if episode_id else []
        event_lows = dict(row.get("event_lows") or {})
        if not event_lows and prior_daily:
            event_lows = dict(prior_daily[0].get("event_lows") or {})
        daily_confirmation, event_lows = _daily_sentinel_evidence(
            db,
            sentinels,
            event_lows=event_lows,
        )
        prior_holds = [
            bool((doc.get("daily_confirmation") or {}).get("event_low_held"))
            for doc in prior_daily
        ]
        held_sessions = 1 + len(prior_holds) if daily_confirmation["event_low_held"] and all(prior_holds) else 0
        daily_confirmation["event_low_hold_sessions"] = held_sessions
        stable_blockers = _stable_turn_blockers(daily_confirmation)
        if state == "confirmed_intraday" and not stable_blockers:
            close_state = "stable_turn"
        elif state in ACTIVE_STATES:
            close_state = state
        elif state == "failed":
            close_state = "failed"
        else:
            close_state = "pressure"
        daily_id = f"{trade_date}:{sector_id}"
        close_flow_state = "F4" if close_state == "stable_turn" else _text(row.get("flow_state")) or "F0"
        rollup_blockers = list(row.get("freshness_blockers") or [])
        if state == "confirmed_intraday" and close_state != "stable_turn":
            rollup_blockers.extend(stable_blockers)
        daily_doc = {
            "_id": daily_id,
            "market": "A",
            "trade_date": trade_date,
            "sector_id": sector_id,
            "sector_name": _text(row.get("sector_name")),
            "kind": _text(row.get("kind")),
            "sector_kind": _text(row.get("sector_kind") or row.get("kind")),
            "episode_id": episode_id,
            "intraday_state": state,
            "turn_state": close_state,
            "close_state": close_state,
            "flow_state": close_flow_state,
            "sentinel_symbols": sentinels,
            "sentinels": sentinels,
            "ma_climb_sentinel_count": int(climb_count),
            "evidence": evidence,
            "metrics": {**evidence, "daily_confirmation": daily_confirmation},
            "daily_confirmation": daily_confirmation,
            "event_lows": event_lows,
            "blockers": rollup_blockers,
            "funding_path": {
                **(row.get("funding_path") if isinstance(row.get("funding_path"), dict) else {}),
                "causality": "unproven",
                "flow_state": close_flow_state,
            },
            "next_checks": _next_checks(close_state),
            "weaker_if": _weaker_if(close_state),
            "rule_version": RULE_VERSION,
            "source_watermarks": row.get("source_watermarks") or {},
            "mode": mode,
            "source": "sync.sector_transition_rollup",
            "updated_at": now,
        }
        operations.append(UpdateOne({"_id": daily_id}, {"$set": daily_doc}, upsert=True))
        db["sector_transition_states"].update_one(
            {"_id": daily_doc["sector_id"]},
            {
                "$set": {
                    "turn_state": close_state,
                    "state": close_state,
                    "active": close_state in ACTIVE_STATES,
                    "close_state": close_state,
                    "flow_state": close_flow_state,
                    "event_lows": event_lows,
                    "blockers": rollup_blockers,
                    "next_checks": _next_checks(close_state),
                    "weaker_if": _weaker_if(close_state),
                    "observed_at": now.replace(hour=15, minute=0, second=0, microsecond=0),
                    "last_changed_at": (
                        now if close_state != state else row.get("last_changed_at") or row.get("updated_at") or now
                    ),
                    "updated_at": now,
                }
            },
        )
        if close_state != state:
            event_id = _event_id(
                trade_date,
                daily_doc["sector_id"],
                state,
                close_state,
                trade_date,
                event_type="close_rollup",
            )
            event_doc = {
                "_id": event_id,
                "event_id": event_id,
                "event_type": "close_rollup",
                "market": "A",
                "trade_date": trade_date,
                "event_minute": "15:00",
                "sector_id": daily_doc["sector_id"],
                "sector_name": daily_doc["sector_name"],
                "kind": daily_doc["kind"],
                "sector_kind": daily_doc["sector_kind"],
                "episode_id": episode_id,
                "from_state": state,
                "to_state": close_state,
                "turn_state": close_state,
                "flow_state": close_flow_state,
                "sentinel_symbols": sentinels,
                "sentinels": sentinels,
                "evidence": {**evidence, "ma_climb_sentinel_count": int(climb_count)},
                "metrics": {**evidence, "daily_confirmation": daily_confirmation},
                "funding_path": daily_doc["funding_path"],
                "next_checks": daily_doc["next_checks"],
                "weaker_if": daily_doc["weaker_if"],
                "rule_version": RULE_VERSION,
                "source_watermarks": daily_doc["source_watermarks"],
                "freshness_blockers": rollup_blockers,
                "blockers": rollup_blockers,
                "mode": mode,
                "source": "sync.sector_transition_rollup",
                "observed_at": now.replace(hour=15, minute=0, second=0, microsecond=0),
                "updated_at": now,
                "created_at": now,
            }
            event_docs.append(event_doc)
            event_ops.append(UpdateOne({"_id": event_id}, {"$setOnInsert": event_doc}, upsert=True))
    if operations:
        db["sector_transition_daily"].bulk_write(operations, ordered=False)
    if event_ops:
        db["sector_transition_events"].bulk_write(event_ops, ordered=False)
    alert_result = process_sector_transition_events(db, event_docs)
    db["data_freshness"].update_one(
        {
            "domain": "sector_transition",
            "market": "A",
            "mode": "postmarket",
            "collection": "sector_transition_daily",
        },
        {
            "$set": {
                "domain": "sector_transition",
                "market": "A",
                "mode": "postmarket",
                "lane": _text(get_task_env("SIGNALS_CURRENT_SYNC_LANE", "postmarket")) or "postmarket",
                "collection": "sector_transition_daily",
                "freshness": "fresh",
                "latest_dt": trade_date,
                "as_of": trade_date,
                "updated_at": now,
                "stale_reason": "",
                "count": len(operations),
                "event_count": len(event_docs),
            }
        },
        upsert=True,
    )
    return {
        "status": "ok",
        "mode": mode,
        "inserted": len(operations),
        "sectors": len(operations),
        "events": len(event_docs),
        "alerts": alert_result,
    }
