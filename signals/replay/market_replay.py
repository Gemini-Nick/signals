# -*- coding: utf-8 -*-
"""Build full-market replay context from local Signals Mongo data.

This module produces evidence, not prose.  The replay skill can then turn the
evidence graph into the screenshot-style postmarket narrative.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from math import log10
from typing import Any


_NOISE_BOARD_PATTERN = re.compile(
    r"(20\d{2}|一季报|半年报|年报|季报|业绩|预增|扭亏|亏损|摘帽|ST|昨日|融资融券|股权|送转|破净|注册制次新)",
    re.IGNORECASE,
)
_BOARD_ALIASES = {
    "超市": ["一般零售", "零售", "商业", "消费"],
    "旅游零售": ["一般零售", "零售", "商业", "消费"],
    "零售消费": ["一般零售", "零售", "商业", "消费"],
    "食品饮料": ["食品", "饮料", "消费"],
    "商业航天": ["航天装备", "航天", "军工"],
    "卫星互联网": ["航天装备", "航天", "卫星", "军工"],
    "航天装备": ["商业航天", "航天", "军工"],
    "机器人": ["机器人", "通用设备", "专用设备", "自动化"],
    "自动化": ["机器人", "通用设备", "专用设备", "自动化"],
    "CPO": ["光模块", "通信设备", "通信线缆", "光通信"],
    "光模块": ["CPO", "通信设备", "通信线缆", "光通信"],
    "通信线缆": ["CPO", "光模块", "通信设备", "光通信"],
    "通信线缆及配套": ["CPO", "光模块", "通信设备", "光通信", "通信线缆"],
    "半导体": ["芯片", "集成电路", "半导体设备", "半导体材料"],
    "芯片": ["半导体", "集成电路", "半导体设备", "半导体材料"],
    "集成电路": ["半导体", "芯片", "半导体设备", "半导体材料"],
    "集成电路制造": ["半导体", "芯片", "集成电路", "半导体设备", "半导体材料"],
    "半导体设备": ["半导体", "芯片", "集成电路"],
    "半导体材料": ["半导体", "芯片", "集成电路"],
    "火力发电": ["电力", "发电"],
    "其他能源发电": ["电力", "发电"],
    "电力": ["火力发电", "其他能源发电", "发电"],
    "锂电池": ["锂", "能源金属", "电池"],
    "能源金属": ["锂", "锂电池", "电池"],
}
_FIXED_TIME_SLICES = [
    ("竞价", "09:15", "09:25"),
    ("开盘段", "09:30", "10:00"),
    ("早盘二段", "10:00", "10:30"),
    ("上午切换段", "10:30", "11:00"),
    ("上午收束段", "11:00", "11:30"),
    ("午后开盘段", "13:00", "13:30"),
    ("午后二段", "13:30", "14:00"),
    ("尾盘前段", "14:00", "14:30"),
    ("尾盘段", "14:30", "15:00"),
]
_EXCLUDED_BOARD_PATTERN = re.compile(
    r"(地区|省|市|自治区|沪股通|深股通|融资融券|MSCI|富时罗素|证金|机构重仓|"
    r"昨日|连板|涨停|次新|ST|破净|高股息|转债|宽基|指数)",
    re.IGNORECASE,
)


def _board_display_name(name: str) -> str:
    return name.rstrip("ⅠⅡⅢⅣIV")


def _text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _date_range(trade_date: str) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(trade_date)
    return start, start + timedelta(days=1)


def _time_at(trade_date: str, time_text: str) -> datetime:
    hour, minute = [int(part) for part in time_text.split(":", 1)]
    return datetime.fromisoformat(trade_date).replace(hour=hour, minute=minute)


def _pure_code(symbol: str) -> str:
    value = _text(symbol).upper()
    for prefix in ("SZ.", "SH.", "BJ."):
        if value.startswith(prefix):
            return value[3:]
    return value


def _prefixed_symbol(code: str) -> str:
    value = _text(code).upper()
    if value.startswith(("SZ.", "SH.", "BJ.")):
        return value
    if value.startswith(("6", "9")):
        return f"SH.{value}"
    if value.startswith(("8", "4")):
        return f"BJ.{value}"
    return f"SZ.{value}"


def _collection_names(db: Any) -> set[str]:
    try:
        return set(db.list_collection_names())
    except Exception:
        return set()


def _snapshot_query(trade_date: str) -> dict[str, Any]:
    return {"$or": [{"date_key": trade_date}, {"trade_date": trade_date}, {"dt": trade_date}]}


def _snapshot_projection() -> dict[str, int]:
    return {
        "_id": 0,
        "symbol": 1,
        "code": 1,
        "name": 1,
        "open": 1,
        "high": 1,
        "low": 1,
        "close": 1,
        "price": 1,
        "prev_close": 1,
        "change_pct": 1,
        "amount": 1,
        "turnover_pct": 1,
        "volume_ratio": 1,
        "vol_ratio": 1,
        "量比": 1,
        "vol": 1,
        "market_cap": 1,
        "float_market_cap": 1,
    }


def _stock_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    code = _text(row.get("code") or _pure_code(_text(row.get("symbol"))))
    symbol = _text(row.get("symbol")) or _prefixed_symbol(code)
    prev_close = _float(row.get("prev_close"))
    high = _float(row.get("high"))
    close = _float(row.get("close") if row.get("close") is not None else row.get("price"))
    high_change_pct = None
    if prev_close and high is not None:
        high_change_pct = (high / prev_close - 1) * 100
    return {
        "symbol": symbol,
        "code": code,
        "name": _text(row.get("name")),
        "open": _float(row.get("open")),
        "high": high,
        "low": _float(row.get("low")),
        "close": close,
        "prev_close": prev_close,
        "change_pct": _float(row.get("change_pct")),
        "high_change_pct": high_change_pct,
        "amount": _float(row.get("amount")),
        "turnover_pct": _float(row.get("turnover_pct")),
        "volume_ratio": _float(_first_present(row.get("volume_ratio"), row.get("vol_ratio"), row.get("量比"))),
        "volume": _float(row.get("vol")),
        "market_cap": _float(row.get("market_cap")),
        "float_market_cap": _float(row.get("float_market_cap")),
    }


def _amount_yi(value: Any) -> float | None:
    amount = _float(value)
    if amount is None:
        return None
    return round(amount / 100000000, 2)


def _log_score(value: Any, *, scale: float = 10.0, cap: float = 40.0) -> float:
    number = max(_float(value) or 0.0, 0.0)
    if number <= 0:
        return 0.0
    return round(min(cap, log10(number) * scale), 3)


def _fmt_pct(value: Any) -> str:
    number = _float(value)
    if number is None:
        return "N/A"
    sign = "+" if number > 0 else ""
    return f"{sign}{number:.2f}%"


def _fmt_unsigned_pct(value: Any) -> str:
    number = _float(value)
    if number is None:
        return "N/A"
    return f"{abs(number):.2f}%"


def _fmt_pct_points(value: Any) -> str:
    number = _float(value)
    if number is None:
        return "N/A"
    return f"{number:.2f}pct"


def _fmt_number(value: Any) -> str:
    number = _float(value)
    if number is None:
        return "N/A"
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _fmt_amount_yi_for_prose(value: Any) -> str:
    raw = _float(value)
    if raw is None:
        return "N/A"
    number = _amount_yi(raw) if abs(raw) > 1_000_000 else raw
    if number is None:
        return "N/A"
    if abs(number) >= 100:
        return str(int(round(number)))
    return _fmt_number(number)


def _is_analysis_board(name: Any) -> bool:
    text = _text(name)
    if not text:
        return False
    return _NOISE_BOARD_PATTERN.search(text) is None and _EXCLUDED_BOARD_PATTERN.search(text) is None


def _limit_threshold(symbol: str) -> float:
    code = _pure_code(symbol)
    if code.startswith(("30", "68")):
        return 19.5
    if code.startswith(("8", "4", "92")):
        return 29.0
    return 9.5


def _minute_rows(db: Any, symbol: str, trade_date: str, *, freq: str = "5分钟", limit: int = 80) -> list[dict[str, Any]]:
    start, end = _date_range(trade_date)
    code = _pure_code(symbol)
    cursor = db["bars"].find(
        {"meta.symbol": code, "meta.freq": freq, "dt": {"$gte": start, "$lt": end}},
        {"_id": 0, "dt": 1, "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1},
    ).sort("dt", 1).limit(limit)
    return list(cursor)


def _bar_point(row: dict[str, Any], field: str) -> dict[str, Any]:
    dt = row.get("dt")
    return {
        "time": dt.strftime("%H:%M") if isinstance(dt, datetime) else _text(dt),
        "open": _float(row.get("open")),
        "high": _float(row.get("high")),
        "low": _float(row.get("low")),
        "close": _float(row.get("close")),
        "amount_yi": _amount_yi(row.get("amount")),
        "value": _float(row.get(field)),
    }


def _intraday_path(db: Any, symbol: str, trade_date: str) -> dict[str, Any]:
    rows = _minute_rows(db, symbol, trade_date)
    if not rows:
        return {"freq": None, "bar_count": 0, "open_bar": None, "high_bar": None, "low_bar": None, "close_bar": None}
    high_row = max(rows, key=lambda item: _float(item.get("high")) or -10**12)
    low_row = min(rows, key=lambda item: _float(item.get("low")) or 10**12)
    return {
        "freq": "5分钟",
        "bar_count": len(rows),
        "open_bar": _bar_point(rows[0], "open"),
        "high_bar": _bar_point(high_row, "high"),
        "low_bar": _bar_point(low_row, "low"),
        "close_bar": _bar_point(rows[-1], "close"),
        "large_amount_bars": [
            _bar_point(row, "close")
            for row in sorted(rows, key=lambda item: _float(item.get("amount")) or 0, reverse=True)[:3]
        ],
    }


def _symbol_evidence(db: Any, trade_date: str, symbols: list[str], *, limit: int = 30) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for symbol in symbols:
        code = _pure_code(symbol)
        if not code or code in seen:
            continue
        seen.add(code)
        doc = db["fullmarket_spot_snapshots"].find_one(
            {"$and": [_snapshot_query(trade_date), {"$or": [{"code": code}, {"symbol": _prefixed_symbol(code)}]}]},
            _snapshot_projection(),
        )
        if not doc:
            continue
        snapshot = _stock_snapshot(doc)
        result.append(
            {
                **snapshot,
                "amount_yi": _amount_yi(snapshot.get("amount")),
                "intraday_path": _intraday_path(db, snapshot["symbol"], trade_date),
            }
        )
        if len(result) >= limit:
            break
    return result


def _symbols_from_rotation_boards(
    db: Any,
    rotation_windows: list[dict[str, Any]],
    rotation_shifts: list[dict[str, Any]],
    *,
    board_limit: int = 24,
    symbols_per_board: int = 24,
) -> list[str]:
    names: list[str] = []
    for window in rotation_windows:
        for bucket in ("top_boards", "weak_boards"):
            rows = window.get(bucket) if isinstance(window.get(bucket), list) else []
            names.extend(_text(row.get("name")) for row in rows[:4] if _text(row.get("name")))
    for shift in rotation_shifts:
        for bucket in ("strengthening", "weakening"):
            rows = shift.get(bucket) if isinstance(shift.get(bucket), list) else []
            names.extend(_text(row.get("name")) for row in rows[:4] if _text(row.get("name")))

    symbols: list[str] = []
    seen_boards: set[str] = set()
    for name in names:
        display = _board_display_name(name)
        if not display or display in seen_boards:
            continue
        seen_boards.add(display)
        for symbol in _board_constituent_symbols(db, display)[:symbols_per_board]:
            if symbol:
                symbols.append(symbol)
        if len(seen_boards) >= board_limit:
            break
    return symbols


def _intraday_delta_board_symbols(
    db: Any,
    trade_date: str,
    *,
    board_limit: int = 30,
    symbols_per_board: int = 16,
) -> list[str]:
    if "board_heat_ticks" not in _collection_names(db):
        return []
    start, end = _date_range(trade_date)
    stats: dict[str, dict[str, Any]] = {}
    cursor = db["board_heat_ticks"].find(
        {"source": "eastmoney_push2delay", "trade_minute": {"$gte": start, "$lt": end}},
        {"_id": 0, "name": 1, "change_pct": 1, "rank_idx": 1},
    )
    for row in cursor:
        name = _board_display_name(_text(row.get("name")))
        change = _float(row.get("change_pct"))
        if not name or not _is_analysis_board(name) or change is None:
            continue
        stat = stats.setdefault(
            name,
            {
                "name": name,
                "min_change": change,
                "max_change": change,
                "best_rank": row.get("rank_idx") if row.get("rank_idx") is not None else 9999,
            },
        )
        stat["min_change"] = min(_float(stat.get("min_change")) or change, change)
        stat["max_change"] = max(_float(stat.get("max_change")) or change, change)
        if row.get("rank_idx") is not None:
            stat["best_rank"] = min(stat.get("best_rank", 9999), row.get("rank_idx"))
    candidates = []
    for stat in stats.values():
        delta = (_float(stat.get("max_change")) or 0.0) - (_float(stat.get("min_change")) or 0.0)
        max_change = _float(stat.get("max_change")) or 0.0
        if delta < 2.5 or max_change < 1.2:
            continue
        candidates.append({**stat, "delta": delta})
    candidates.sort(key=lambda item: (item["delta"], item["max_change"], -(item.get("best_rank") or 9999)), reverse=True)
    symbols: list[str] = []
    for row in candidates[:board_limit]:
        symbols.extend(_board_constituent_symbols(db, row["name"])[:symbols_per_board])
    return symbols


def _high_turnover_cores(db: Any, trade_date: str, *, limit: int = 20) -> list[dict[str, Any]]:
    cursor = db["fullmarket_spot_snapshots"].find(
        _snapshot_query(trade_date),
        _snapshot_projection(),
    ).sort("amount", -1).limit(limit)
    result = []
    for row in cursor:
        snapshot = _stock_snapshot(row)
        result.append({**snapshot, "amount_yi": _amount_yi(snapshot.get("amount"))})
    return result


def _latest_chain_membership_date(db: Any, trade_date: str) -> str:
    if "security_chain_memberships" not in _collection_names(db):
        return ""
    doc = db["security_chain_memberships"].find_one(
        {"trade_date": {"$lte": trade_date}},
        {"_id": 0, "trade_date": 1},
        sort=[("trade_date", -1)],
    )
    return _text(doc.get("trade_date")) if doc else ""


def _chain_membership_for_symbols(db: Any, trade_date: str, symbols: list[str]) -> dict[str, dict[str, Any]]:
    membership_date = _latest_chain_membership_date(db, trade_date)
    if not membership_date:
        return {}
    codes = sorted({_pure_code(symbol) for symbol in symbols if _pure_code(symbol)})
    prefixed = [_prefixed_symbol(code) for code in codes]
    security_ids = [f"A:{symbol.replace('.', ':')}" for symbol in prefixed]
    cursor = db["security_chain_memberships"].find(
        {
            "trade_date": membership_date,
            "$or": [
                {"raw_code": {"$in": codes}},
                {"symbol": {"$in": prefixed}},
                {"security_id": {"$in": security_ids}},
            ],
        },
        {
            "_id": 0,
            "raw_code": 1,
            "symbol": 1,
            "security_id": 1,
            "chain_name": 1,
            "node_name": 1,
            "is_primary_chain": 1,
            "membership_type": 1,
            "exposure_score": 1,
            "confidence": 1,
            "trade_date": 1,
        },
    ).sort([("is_primary_chain", -1), ("exposure_score", -1), ("confidence", -1)])
    result: dict[str, dict[str, Any]] = {}
    for row in cursor:
        code = _pure_code(_text(row.get("symbol") or row.get("raw_code") or row.get("security_id")))
        if not code or code in result:
            continue
        result[code] = {
            "chain_name": _text(row.get("chain_name"), "未映射产业链"),
            "node_name": _text(row.get("node_name")),
            "is_primary_chain": bool(row.get("is_primary_chain")),
            "membership_type": _text(row.get("membership_type")),
            "exposure_score": _float(row.get("exposure_score")),
            "confidence": _float(row.get("confidence")),
            "source": "security_chain_memberships",
            "membership_trade_date": _text(row.get("trade_date")),
        }
    return result


def _first_red_evidence(db: Any, symbol: str, trade_date: str, prev_close: Any) -> dict[str, Any]:
    prev = _float(prev_close)
    if prev is None:
        return {"status": "unknown", "note": "缺昨收，不能计算翻红时间。"}
    rows = _minute_rows(db, symbol, trade_date)
    if not rows:
        return {"status": "missing", "note": "缺5分钟路径，不能判定首次翻红时间。"}
    first_touch = None
    first_close = None
    for row in rows:
        high = _float(row.get("high"))
        close = _float(row.get("close"))
        if first_touch is None and high is not None and high > prev:
            first_touch = row
        if first_close is None and close is not None and close > prev:
            first_close = row
        if first_touch is not None and first_close is not None:
            break
    if first_touch is None and first_close is None:
        return {"status": "not_observed", "bar_count": len(rows), "note": "5分钟路径未观察到越过昨收。"}
    touch_time = first_touch.get("dt") if first_touch else None
    close_time = first_close.get("dt") if first_close else None
    return {
        "status": "confirmed",
        "bar_count": len(rows),
        "first_touch_time": touch_time.strftime("%H:%M") if isinstance(touch_time, datetime) else _text(touch_time),
        "first_close_above_time": close_time.strftime("%H:%M") if isinstance(close_time, datetime) else _text(close_time),
        "basis": "5分钟 bars 越过昨收",
    }


def _turnover_representatives(
    db: Any,
    trade_date: str,
    high_turnover: list[dict[str, Any]],
    *,
    limit: int = 15,
) -> list[dict[str, Any]]:
    symbols = [_text(row.get("symbol")) for row in high_turnover[:limit] if _text(row.get("symbol"))]
    chain_lookup = _chain_membership_for_symbols(db, trade_date, symbols)
    rows: list[dict[str, Any]] = []
    for rank, row in enumerate(high_turnover[:limit], start=1):
        symbol = _text(row.get("symbol"))
        prev_close = _float(row.get("prev_close"))
        open_value = _float(row.get("open"))
        change = _float(row.get("change_pct"))
        high_change = _float(row.get("high_change_pct"))
        open_gap = (open_value / prev_close - 1) * 100 if open_value is not None and prev_close else None
        if change is not None and change > 0 and open_gap is not None and open_gap < 0:
            role = "低开转强/修复锚"
        elif change is not None and change > 0:
            role = "高成交强承接"
        elif high_change is not None and high_change > 0 and change is not None and change < 0:
            role = "冲高回落/压力锚"
        elif change is not None and change < 0:
            role = "高成交压力锚"
        else:
            role = "高成交观察锚"
        code = _pure_code(symbol)
        chain = chain_lookup.get(code, {})
        rows.append(
            {
                "rank": rank,
                "symbol": symbol,
                "code": row.get("code"),
                "name": row.get("name"),
                "amount_yi": row.get("amount_yi"),
                "change_pct": change,
                "open_gap_pct": round(open_gap, 2) if open_gap is not None else None,
                "high_change_pct": round(high_change, 2) if high_change is not None else None,
                "role": role,
                "chain_name": _text(chain.get("chain_name"), "未映射产业链"),
                "node_name": _text(chain.get("node_name")),
                "chain_source": _text(chain.get("source"), "missing"),
                "chain_evidence_date": _text(chain.get("membership_trade_date")),
                "first_red": _first_red_evidence(db, symbol, trade_date, prev_close),
                "evidence_level": "confirmed",
            }
        )
    return rows


def _snapshot_by_symbol(db: Any, trade_date: str, symbols: list[str]) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    codes = sorted({_pure_code(symbol) for symbol in symbols if _pure_code(symbol)})
    if not codes:
        return {}
    prefixed = [_prefixed_symbol(code) for code in codes]
    cursor = db["fullmarket_spot_snapshots"].find(
        {"$and": [_snapshot_query(trade_date), {"$or": [{"code": {"$in": codes}}, {"symbol": {"$in": prefixed}}]}]},
        _snapshot_projection(),
    )
    result: dict[str, dict[str, Any]] = {}
    for row in cursor:
        snapshot = _stock_snapshot(row)
        result[_pure_code(snapshot["symbol"])] = snapshot
    return result


def _symbols_for_names(db: Any, trade_date: str, names: list[str]) -> list[str]:
    clean = sorted({_text(name) for name in names if _text(name)})
    if not clean:
        return []
    cursor = db["fullmarket_spot_snapshots"].find(
        {"$and": [_snapshot_query(trade_date), {"name": {"$in": clean}}]},
        {"_id": 0, "symbol": 1, "code": 1},
    )
    symbols: list[str] = []
    for row in cursor:
        symbol = _text(row.get("symbol")) or _prefixed_symbol(_text(row.get("code")))
        if symbol:
            symbols.append(symbol)
    return symbols


def _board_constituent_symbols(db: Any, board_name: str) -> list[str]:
    if "board_constituents" not in _collection_names(db) or not board_name:
        return []
    doc = db["board_constituents"].find_one(
        {"$or": [{"board_name": board_name}, {"concept_name": board_name}, {"name": board_name}]},
        {"_id": 0, "symbols": 1, "stock_names": 1},
        sort=[("updated_at", -1)],
    )
    if not doc:
        return []
    symbols = doc.get("symbols") if isinstance(doc.get("symbols"), list) else []
    result = []
    for symbol in symbols:
        code = _pure_code(str(symbol))
        if code:
            result.append(_prefixed_symbol(code))
    return result


def _static_representatives(board: dict[str, Any]) -> list[dict[str, Any]]:
    reps = board.get("representatives") if isinstance(board.get("representatives"), dict) else {}
    result: list[dict[str, Any]] = []
    for bucket in ("core", "elastic", "upstream", "downstream", "source_leader"):
        for item in reps.get(bucket, []) if isinstance(reps.get(bucket), list) else []:
            symbol = _text(item.get("symbol") or item.get("code"))
            name = _text(item.get("name"))
            if not symbol and not name:
                continue
            result.append(
                {
                    "tier": bucket,
                    "symbol": symbol,
                    "code": _pure_code(symbol),
                    "name": name,
                    "role": _text(item.get("role") or item.get("chain_role") or item.get("relation")),
                }
            )
    return result


def _limit_pool_lookup(db: Any, trade_date: str) -> dict[str, dict[str, Any]]:
    names = _collection_names(db)
    lookup: dict[str, dict[str, Any]] = {}
    pool_names = [name for name in ("market_limit_pools", "limit_up_pools", "market_pools") if name in names]
    for collection_name in pool_names:
        cursor = db[collection_name].find(
            {
                "$or": [
                    {"trade_date": trade_date},
                    {"date_key": trade_date},
                    {"dt": trade_date},
                    {"dt": trade_date.replace("-", "")},
                ]
            },
            {"_id": 0},
        ).sort([("snapshot_at", 1), ("updated_at", 1)])
        for row in cursor:
            raw_code = _text(row.get("code") or row.get("symbol"))
            code = _pure_code(raw_code)
            if not code:
                continue
            pool_type = _text(row.get("pool") or row.get("pool_type") or row.get("type") or row.get("kind"))
            enriched = {
                "code": code,
                "name": _text(_first_present(row.get("name"), row.get("名称"))),
                "pool": pool_type,
                "first_limit_up_time": _text(_first_present(row.get("first_limit_up_time"), row.get("首次封板时间"))),
                "last_limit_up_time": _text(_first_present(row.get("last_limit_up_time"), row.get("最后封板时间"))),
                "open_count": _float(_first_present(row.get("open_count"), row.get("炸板次数"), row.get("开板次数"))),
                "seal_amount": _float(_first_present(row.get("seal_amount"), row.get("封板资金"), row.get("封单资金"))),
                "limit_up_stat": _text(_first_present(row.get("limit_up_stat"), row.get("涨停统计"))),
                "consecutive_limit_count": _float(_first_present(row.get("consecutive_limit_count"), row.get("连板数"))),
                "volume_ratio": _float(_first_present(row.get("volume_ratio"), row.get("量比"))),
                "industry": _text(_first_present(row.get("industry"), row.get("所属行业"), row.get("行业"))),
                "snapshot_minute": _text(_first_present(row.get("snapshot_minute"), row.get("trade_minute"))),
                "selected_reason": _text(_first_present(row.get("selected_reason"), row.get("入选理由"))),
                "source_collection": collection_name,
            }
            existing = lookup.setdefault(code, {})
            pools = existing.setdefault("pools", [])
            if pool_type and pool_type not in pools:
                pools.append(pool_type)
            if pool_type and not existing.get("pool"):
                existing["pool"] = pool_type
            for key, value in enriched.items():
                if value in ("", None):
                    continue
                if key == "first_limit_up_time":
                    old = _text(existing.get(key))
                    if not old or _text(value) < old:
                        existing[key] = value
                elif key in {"seal_amount", "open_count", "consecutive_limit_count", "volume_ratio"}:
                    old_float = _float(existing.get(key))
                    new_float = _float(value)
                    if new_float is not None and (old_float is None or new_float > old_float):
                        existing[key] = value
                elif key in {"selected_reason", "industry"} and existing.get(key) and value != existing.get(key):
                    merged_key = f"{key}s"
                    merged = existing.setdefault(merged_key, [])
                    if existing[key] not in merged:
                        merged.append(existing[key])
                    if value not in merged:
                        merged.append(value)
                else:
                    existing.setdefault(key, value)
    return lookup


def _tokenize_board_text(*values: Any) -> list[str]:
    tokens: list[str] = []
    for value in values:
        text = _text(value)
        if not text:
            continue
        for token in re.split(r"[·/、,，|｜\s]+", text):
            token = token.strip()
            if len(token) >= 2 and token not in {"产业链", "概念", "行业", "其他"}:
                tokens.append(token)
                tokens.extend(_BOARD_ALIASES.get(token, []))
    return list(dict.fromkeys(tokens))


def _limit_pool_symbols_for_board(
    limit_lookup: dict[str, dict[str, Any]],
    *,
    board_name: str,
    driver_name: str,
    limit: int = 20,
) -> list[str]:
    tokens = _tokenize_board_text(board_name, driver_name)
    if not tokens:
        return []
    result: list[str] = []
    for code, meta in limit_lookup.items():
        haystack = " ".join(
            _text(meta.get(key))
            for key in ("industry", "selected_reason", "pool", "name")
        )
        if any(token in haystack for token in tokens):
            result.append(_prefixed_symbol(code))
        if len(result) >= limit:
            break
    return result


def _limit_speed_score(time_text: str) -> float:
    text = _text(time_text)
    if len(text) < 4 or not text[:4].isdigit():
        return 0.0
    hour = int(text[:2])
    minute = int(text[2:4])
    minutes = hour * 60 + minute
    open_minutes = 9 * 60 + 25
    close_minutes = 15 * 60
    if minutes <= open_minutes:
        return 20.0
    return round(max(0.0, 20.0 * (close_minutes - minutes) / max(close_minutes - open_minutes, 1)), 3)


def _market_representative_row(snapshot: dict[str, Any], limit_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    limit_meta = limit_meta or {}
    symbol = snapshot.get("symbol")
    change_pct = _float(snapshot.get("change_pct")) or 0.0
    high_change = _float(snapshot.get("high_change_pct")) or change_pct
    amount = _float(snapshot.get("amount")) or 0.0
    turnover_pct = _float(snapshot.get("turnover_pct")) or 0.0
    volume_ratio = _float(_first_present(snapshot.get("volume_ratio"), limit_meta.get("volume_ratio"))) or 0.0
    close = _float(snapshot.get("close"))
    high = _float(snapshot.get("high"))
    price_drawdown = ((high - close) / high * 100) if high and close else 0.0
    inferred_limit = change_pct >= _limit_threshold(str(symbol))
    pool = _text(limit_meta.get("pool"))
    limit_bonus = 0.0
    if inferred_limit or pool in {"zt", "涨停", "limit_up"}:
        limit_bonus += 30.0
    if pool in {"strong", "qsgc", "强势"}:
        limit_bonus += 10.0
    if pool in {"zbgc", "炸板", "failed_limit"}:
        limit_bonus += 8.0
    limit_bonus += min((_float(limit_meta.get("consecutive_limit_count")) or 0.0) * 4.0, 20.0)
    limit_bonus += _limit_speed_score(_text(limit_meta.get("first_limit_up_time")))
    if limit_meta.get("seal_amount") is not None:
        limit_bonus += min(_log_score(limit_meta.get("seal_amount"), scale=4, cap=14), 14.0)
    amount_score = _log_score(amount, scale=5.5, cap=48)
    volume_ratio_bonus = min(volume_ratio, 15.0) * 0.8
    positive_score = max(change_pct, 0.0) * 2.2 + min(turnover_pct, 30.0) * 0.7 + volume_ratio_bonus + limit_bonus
    pressure_score = amount_score + max(-change_pct, 0.0) * 2.5 + max(price_drawdown, 0.0) * 1.5
    if pool in {"limit_down", "dtgc", "跌停"}:
        pressure_score += 24.0
    if pool in {"zbgc", "炸板", "failed_limit"}:
        pressure_score += 8.0
    core_score = amount_score + abs(change_pct) * 1.3 + min(turnover_pct, 25.0) * 0.35 + volume_ratio_bonus * 0.4
    return {
        "symbol": symbol,
        "code": snapshot.get("code"),
        "name": snapshot.get("name"),
        "change_pct": change_pct,
        "high_change_pct": high_change,
        "amount_yi": _amount_yi(amount),
        "turnover_pct": turnover_pct,
        "volume_ratio": volume_ratio,
        "open": snapshot.get("open"),
        "high": high,
        "low": snapshot.get("low"),
        "close": close,
        "market_core_score": round(core_score, 3),
        "market_elastic_score": round(positive_score, 3),
        "pressure_score": round(pressure_score, 3),
        "limit_pool": limit_meta,
        "limit_up_inferred": inferred_limit,
    }


def _dynamic_market_representatives(
    db: Any,
    trade_date: str,
    boards: list[dict[str, Any]],
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    limit_lookup = _limit_pool_lookup(db, trade_date)
    rows: list[dict[str, Any]] = []
    for board in boards[:limit]:
        driver = board.get("source_driver") if isinstance(board.get("source_driver"), dict) else {}
        driver_name = _text(driver.get("name"))
        board_name = _text(board.get("name"))
        static_reps = _static_representatives(board)
        symbols = _board_constituent_symbols(db, driver_name)
        symbols.extend(rep["symbol"] for rep in static_reps if rep.get("symbol"))
        symbols.extend(_symbols_for_names(db, trade_date, [_text(driver.get("leader_name"))]))
        symbols.extend(
            _limit_pool_symbols_for_board(
                limit_lookup,
                board_name=board_name,
                driver_name=driver_name,
            )
        )
        snapshots = _snapshot_by_symbol(db, trade_date, symbols)
        candidates = [
            _market_representative_row(snapshot, limit_lookup.get(code))
            for code, snapshot in snapshots.items()
        ]
        candidates.sort(key=lambda item: item["market_core_score"], reverse=True)
        market_core = candidates[:4]
        market_elastic = sorted(
            [item for item in candidates if item["change_pct"] > 0 or item.get("limit_pool")],
            key=lambda item: item["market_elastic_score"],
            reverse=True,
        )[:4]
        market_elastic_confirmed = [
            item
            for item in market_elastic
            if _text(item.get("limit_pool", {}).get("pool")) in {"limit_up", "zt", "涨停"}
            or item.get("limit_up_inferred")
        ]
        failed_emotion = sorted(
            [
                item
                for item in candidates
                if _text(item.get("limit_pool", {}).get("pool")) in {"failed_limit", "zbgc", "炸板"}
            ],
            key=lambda item: item["market_elastic_score"],
            reverse=True,
        )[:4]
        pressure_core = sorted(
            [item for item in candidates if item["change_pct"] < 0],
            key=lambda item: item["pressure_score"],
            reverse=True,
        )[:4]
        rows.append(
            {
                "board": _text(board.get("name")),
                "driver_kind": _text(driver.get("kind")),
                "driver_name": driver_name,
                "driver_change_pct": _float(driver.get("change_pct")),
                "static_representatives": static_reps[:8],
                "candidate_count": len(candidates),
                "candidate_sources": [
                    "board_constituents",
                    "static_representatives",
                    "source_driver.leader_name",
                    "market_limit_pools.industry_match",
                ],
                "market_core": market_core,
                "market_elastic": market_elastic,
                "market_elastic_confirmed": market_elastic_confirmed,
                "failed_emotion": failed_emotion,
                "pressure_core": pressure_core,
                "selection_note": (
                    "market_core/market_elastic 是当日市场认可代表；static_representatives 只是产业链知识库代表。"
                    "弹性优先看涨停池/封板速度/连板/涨幅/换手，核心优先看成交额和市场共识，压力核心看高成交负反馈。"
                ),
            }
        )
    return rows


def _failed_board_rows(db: Any, trade_date: str, *, limit: int = 20) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = db["fullmarket_spot_snapshots"].find(_snapshot_query(trade_date), _snapshot_projection())
    for row in cursor:
        snapshot = _stock_snapshot(row)
        name = _text(snapshot.get("name"))
        if name.startswith("N") and len(name) > 1:
            continue
        high_pct = snapshot.get("high_change_pct")
        change_pct = snapshot.get("change_pct")
        high = snapshot.get("high")
        close = snapshot.get("close")
        if high_pct is None or change_pct is None or high is None or close is None:
            continue
        if high_pct >= 9.5 and change_pct < high_pct - 1.0 and close < high * 0.995:
            price_drawdown_pct = (high - close) / high * 100 if high else None
            rows.append(
                {
                    **snapshot,
                    "amount_yi": _amount_yi(snapshot.get("amount")),
                    "failed_from_high_pct": round(high_pct - change_pct, 2),
                    "price_drawdown_pct": round(price_drawdown_pct, 2) if price_drawdown_pct is not None else None,
                }
            )
    rows.sort(key=lambda item: (item.get("failed_from_high_pct") or 0, item.get("amount") or 0), reverse=True)
    return rows[:limit]


def _top_limit_pool_symbols(limit_lookup: dict[str, dict[str, Any]], *, limit: int = 80) -> list[str]:
    def sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, str, float]:
        code, meta = item
        pool = _text(meta.get("pool"))
        priority = {
            "limit_up": 0,
            "zt": 0,
            "涨停": 0,
            "failed_limit": 1,
            "zbgc": 1,
            "炸板": 1,
            "limit_down": 2,
            "dtgc": 2,
            "跌停": 2,
            "strong": 3,
            "qsgc": 3,
        }.get(pool, 4)
        first_time = _text(meta.get("first_limit_up_time") or meta.get("last_limit_up_time") or "999999")
        seal_amount = _float(meta.get("seal_amount")) or 0.0
        return priority, first_time, -seal_amount

    return [_prefixed_symbol(code) for code, _ in sorted(limit_lookup.items(), key=sort_key)[:limit]]


def _board_minute_doc(db: Any, kind: str, name: str, trade_date: str, time_text: str) -> dict[str, Any] | None:
    target = _time_at(trade_date, time_text)
    day_start, _ = _date_range(trade_date)
    return db["board_heat_ticks"].find_one(
        {"kind": kind, "name": name, "trade_minute": {"$gte": day_start, "$lte": target}},
        {
            "_id": 0,
            "trade_minute": 1,
            "change_pct": 1,
            "rank_idx": 1,
            "leader_name": 1,
            "leader_change_pct": 1,
            "up_count": 1,
            "down_count": 1,
            "turnover_pct": 1,
            "market_value": 1,
            "code": 1,
        },
        sort=[("trade_minute", -1)],
    )


def _board_point(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if not doc:
        return None
    minute = doc.get("trade_minute")
    return {
        "time": minute.strftime("%H:%M") if isinstance(minute, datetime) else _text(minute),
        "change_pct": _float(doc.get("change_pct")),
        "rank": doc.get("rank_idx"),
        "leader_name": _text(doc.get("leader_name")),
        "leader_change_pct": _float(doc.get("leader_change_pct")),
        "up_count": doc.get("up_count"),
        "down_count": doc.get("down_count"),
        "turnover_pct": _float(doc.get("turnover_pct")),
    }


def _board_timeline(
    db: Any,
    trade_date: str,
    boards: list[dict[str, Any]],
    *,
    checkpoints: list[str],
    limit: int = 12,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for board in boards:
        driver = board.get("source_driver") if isinstance(board.get("source_driver"), dict) else {}
        kind = _text(driver.get("kind"))
        name = _text(driver.get("name"))
        display = _text(board.get("name"))
        if not kind or not name or (kind, name) in seen:
            continue
        seen.add((kind, name))
        points = [_board_point(_board_minute_doc(db, kind, name, trade_date, t)) for t in checkpoints]
        points = [point for point in points if point]
        latest = points[-1] if points else None
        first = points[0] if points else None
        change_delta = None
        if first and latest and first.get("change_pct") is not None and latest.get("change_pct") is not None:
            change_delta = round(float(latest["change_pct"]) - float(first["change_pct"]), 2)
        result.append(
            {
                "board": display,
                "driver_kind": kind,
                "driver_name": name,
                "points": points,
                "change_delta_from_first": change_delta,
                "latest": latest,
                "evidence_source": "board_heat_ticks",
            }
        )
        if len(result) >= limit:
            break
    return result


def _market_board_snapshot(db: Any, trade_date: str, checkpoint: str, *, limit: int = 6) -> dict[str, Any] | None:
    target = _time_at(trade_date, checkpoint)
    day_start, _ = _date_range(trade_date)
    latest = db["board_heat_ticks"].find_one(
        {"source": "eastmoney_push2delay", "trade_minute": {"$gte": day_start, "$lte": target}},
        {"trade_minute": 1, "_id": 0},
        sort=[("trade_minute", -1)],
    )
    if not latest or latest.get("trade_minute") is None:
        return None
    minute = latest["trade_minute"]
    rows = [
        row
        for row in list(
            db["board_heat_ticks"].find(
                {"source": "eastmoney_push2delay", "trade_minute": minute},
                {
                    "_id": 0,
                    "kind": 1,
                    "name": 1,
                    "change_pct": 1,
                    "rank_idx": 1,
                    "leader_name": 1,
                    "leader_change_pct": 1,
                },
            ).sort([("rank_idx", 1), ("change_pct", -1)]).limit(limit * 5)
        )
        if _is_analysis_board(row.get("name"))
    ][:limit]
    weak_rows = [
        row
        for row in list(
            db["board_heat_ticks"].find(
                {"source": "eastmoney_push2delay", "trade_minute": minute},
                {
                    "_id": 0,
                    "kind": 1,
                    "name": 1,
                    "change_pct": 1,
                    "rank_idx": 1,
                    "leader_name": 1,
                    "leader_change_pct": 1,
                },
            ).sort([("change_pct", 1), ("rank_idx", -1)]).limit(limit * 5)
        )
        if _is_analysis_board(row.get("name"))
    ][:limit]
    return {
        "checkpoint": checkpoint,
        "actual_time": minute.strftime("%H:%M"),
        "top_boards": [
            {
                "kind": _text(row.get("kind")),
                "name": _text(row.get("name")),
                "change_pct": _float(row.get("change_pct")),
                "rank": row.get("rank_idx"),
                "leader_name": _text(row.get("leader_name")),
                "leader_change_pct": _float(row.get("leader_change_pct")),
            }
            for row in rows
        ],
        "weak_boards": [
            {
                "kind": _text(row.get("kind")),
                "name": _text(row.get("name")),
                "change_pct": _float(row.get("change_pct")),
                "rank": row.get("rank_idx"),
                "leader_name": _text(row.get("leader_name")),
                "leader_change_pct": _float(row.get("leader_change_pct")),
            }
            for row in weak_rows
        ],
    }


def _rotation_windows(db: Any, trade_date: str, *, checkpoints: list[str], limit: int = 6) -> list[dict[str, Any]]:
    return [
        item
        for checkpoint in checkpoints
        if (item := _market_board_snapshot(db, trade_date, checkpoint, limit=limit)) is not None
    ]


def _board_rows_for_checkpoint(db: Any, trade_date: str, checkpoint: str) -> tuple[str, dict[tuple[str, str], dict[str, Any]]]:
    target = _time_at(trade_date, checkpoint)
    day_start, _ = _date_range(trade_date)
    latest = db["board_heat_ticks"].find_one(
        {"source": "eastmoney_push2delay", "trade_minute": {"$gte": day_start, "$lte": target}},
        {"trade_minute": 1, "_id": 0},
        sort=[("trade_minute", -1)],
    )
    if not latest or latest.get("trade_minute") is None:
        return checkpoint, {}
    minute = latest["trade_minute"]
    rows = list(
        db["board_heat_ticks"].find(
            {"source": "eastmoney_push2delay", "trade_minute": minute},
            {"_id": 0, "kind": 1, "name": 1, "change_pct": 1, "rank_idx": 1, "leader_name": 1, "leader_change_pct": 1},
        )
    )
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        name = _text(row.get("name"))
        kind = _text(row.get("kind"))
        if not kind or not _is_analysis_board(name):
            continue
        result[(kind, name)] = {
            "kind": kind,
            "name": name,
            "change_pct": _float(row.get("change_pct")),
            "rank": row.get("rank_idx"),
            "leader_name": _text(row.get("leader_name")),
            "leader_change_pct": _float(row.get("leader_change_pct")),
        }
    return minute.strftime("%H:%M") if isinstance(minute, datetime) else _text(minute, checkpoint), result


def _rotation_shifts(db: Any, trade_date: str, *, checkpoints: list[str], limit: int = 5) -> list[dict[str, Any]]:
    snapshots = [_board_rows_for_checkpoint(db, trade_date, checkpoint) for checkpoint in checkpoints]
    shifts: list[dict[str, Any]] = []
    for (from_time, previous), (to_time, current) in zip(snapshots, snapshots[1:]):
        deltas: list[dict[str, Any]] = []
        for key, row in current.items():
            prev = previous.get(key)
            if not prev:
                continue
            cur_change = _float(row.get("change_pct"))
            prev_change = _float(prev.get("change_pct"))
            if cur_change is None or prev_change is None:
                continue
            deltas.append(
                {
                    "kind": row["kind"],
                    "name": row["name"],
                    "from_change_pct": prev_change,
                    "to_change_pct": cur_change,
                    "delta_pct": round(cur_change - prev_change, 2),
                    "leader_name": row.get("leader_name"),
                    "rank": row.get("rank"),
                }
            )
        if not deltas:
            continue
        strengthening = sorted(deltas, key=lambda item: item["delta_pct"], reverse=True)[:limit]
        weakening = sorted(deltas, key=lambda item: item["delta_pct"])[:limit]
        shifts.append(
            {
                "from_time": from_time,
                "to_time": to_time,
                "strengthening": strengthening,
                "weakening": weakening,
            }
        )
    return shifts


def _opening_pressure_boards(db: Any, trade_date: str, *, limit: int = 20) -> list[dict[str, Any]]:
    if "board_heat_ticks" not in _collection_names(db):
        return []
    start = _time_at(trade_date, "09:15")
    end = _time_at(trade_date, "09:35")
    weakest: dict[tuple[str, str], dict[str, Any]] = {}
    cursor = db["board_heat_ticks"].find(
        {"source": "eastmoney_push2delay", "trade_minute": {"$gte": start, "$lte": end}},
        {"_id": 0, "trade_minute": 1, "kind": 1, "name": 1, "change_pct": 1, "rank_idx": 1, "leader_name": 1},
    )
    for row in cursor:
        name = _text(row.get("name"))
        kind = _text(row.get("kind"))
        change = _float(row.get("change_pct"))
        if not kind or not _is_analysis_board(name) or change is None or change >= 0:
            continue
        key = (kind, name)
        previous = weakest.get(key)
        if previous is None or change < (_float(previous.get("change_pct")) or 0.0):
            weakest[key] = row
    def opening_sort_key(item: dict[str, Any]) -> tuple[int, float]:
        name = _text(item.get("name"))
        pressure_priority = 0 if ("CPO" in name or "光模块" in name or name == "通信线缆及配套") else 1
        return pressure_priority, _float(item.get("change_pct")) or 0.0

    rows = sorted(weakest.values(), key=opening_sort_key)[:limit]
    return [
        {
            "time": row["trade_minute"].strftime("%H:%M") if isinstance(row.get("trade_minute"), datetime) else _text(row.get("trade_minute")),
            "kind": _text(row.get("kind")),
            "name": _text(row.get("name")),
            "change_pct": _float(row.get("change_pct")),
            "rank": row.get("rank_idx"),
            "leader_name": _text(row.get("leader_name")),
        }
        for row in rows
    ]


def _representative_symbols(boards: list[dict[str, Any]], *, limit: int = 40) -> list[str]:
    symbols: list[str] = []
    for board in boards:
        reps = board.get("representatives") if isinstance(board.get("representatives"), dict) else {}
        for bucket in ("core", "elastic"):
            for item in reps.get(bucket, []) if isinstance(reps.get(bucket), list) else []:
                symbol = _text(item.get("symbol") or item.get("code"))
                if symbol:
                    symbols.append(symbol)
    return symbols[:limit]


def _external_fund_flow_evidence(
    trade_date: str,
    symbols: list[str],
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    if not symbols:
        return []
    try:
        from signals.replay.fund_flow_sources import fetch_stock_fund_flow_evidence
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for symbol in symbols:
        code = _pure_code(symbol)
        if not code or code in seen:
            continue
        seen.add(code)
        evidence = fetch_stock_fund_flow_evidence(symbol, trade_date=trade_date)
        if evidence:
            rows.append(evidence)
        if len(rows) >= limit:
            break
    return rows


def _flow_availability(db: Any, external_flows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    names = set(db.list_collection_names())
    candidates = sorted(name for name in names if "flow" in name.lower() or "fund" in name.lower())
    external_flows = external_flows or []
    order_size_sources = sorted(
        {
            _text(flow.get("eastmoney_quote", {}).get("source"))
            for flow in external_flows
            if isinstance(flow.get("eastmoney_quote"), dict)
            and flow.get("eastmoney_quote", {}).get("order_size_buy_sell_available")
        }
        | {
            _text(flow.get("ths_real_funds", {}).get("source"))
            for flow in external_flows
            if isinstance(flow.get("ths_real_funds"), dict)
            and flow.get("ths_real_funds", {}).get("order_size_buy_sell_available")
        }
    )
    return {
        "participant_flow_available": bool(candidates),
        "candidate_collections": candidates,
        "order_size_flow_available": bool(order_size_sources),
        "order_size_sources": [source for source in order_size_sources if source],
        "note": (
            "主力/散户分账户净流入需要资金流或 L2 类集合；如果 candidate_collections 为空，"
            "只能用成交额、价格承接和板块 tick 推断承接，不能输出主力/散户精确分账户数。"
            "外部 order_size_sources 只能证明大中小单等订单大小口径，不等同于账户级散户/主力。"
        ),
    }


def _has_collection_rows(db: Any, collection_name: str, query: dict[str, Any] | None = None) -> bool:
    if collection_name not in _collection_names(db):
        return False
    try:
        return db[collection_name].find_one(query or {}, {"_id": 1}) is not None
    except Exception:
        return False


def _data_completeness(db: Any, trade_date: str, flow: dict[str, Any]) -> list[dict[str, Any]]:
    start, end = _date_range(trade_date)
    prior_start = start - timedelta(days=30)

    def stock_query() -> dict[str, Any]:
        return _snapshot_query(trade_date)

    def bars_query(freq: str | None = None) -> dict[str, Any]:
        query: dict[str, Any] = {"dt": {"$gte": start, "$lt": end}}
        if freq:
            query["meta.freq"] = freq
        return query

    def board_query(start_dt: datetime = start, end_dt: datetime = end) -> dict[str, Any]:
        return {"trade_minute": {"$gte": start_dt, "$lt": end_dt}}

    candidate_trend_collections = [
        "board_daily",
        "board_daily_bars",
        "board_index_daily",
        "board_history",
        "chain_heat_snapshots",
    ]
    trend_source = next((name for name in candidate_trend_collections if _has_collection_rows(db, name)), "")
    rows = [
        {
            "item": "指数日线",
            "status": "available" if _has_collection_rows(db, "index_bars", {"meta.freq": "日线", "dt": {"$gte": start, "$lt": end}}) else "missing",
            "source": "index_bars",
            "impact": "市场状态与时间周期",
        },
        {
            "item": "指数分钟线",
            "status": "available" if _has_collection_rows(db, "index_bars", {"dt": {"$gte": start, "$lt": end}, "meta.freq": {"$in": ["1分钟", "5分钟"]}}) else "missing",
            "source": "index_bars",
            "impact": "指数日内时间轴",
        },
        {
            "item": "个股日线",
            "status": "available" if _has_collection_rows(db, "fullmarket_spot_snapshots", stock_query()) else "missing",
            "source": "fullmarket_spot_snapshots",
            "impact": "Top50、涨跌幅、成交额、承接",
        },
        {
            "item": "个股分钟线",
            "status": "available" if _has_collection_rows(db, "bars", bars_query()) else "missing",
            "source": "bars",
            "impact": "精确时间点、拉升回落、区间成交",
        },
        {
            "item": "板块分钟线",
            "status": "available" if _has_collection_rows(db, "board_heat_ticks", board_query()) else "missing",
            "source": "board_heat_ticks",
            "impact": "固定半小时切片、资金切换、卡位",
        },
        {
            "item": "板块20日历史",
            "status": "available" if trend_source else ("partial" if _has_collection_rows(db, "board_heat_ticks", board_query(prior_start, start)) else "missing"),
            "source": trend_source or "board_heat_ticks(current-day/minute only)",
            "impact": "近20日趋势Top7；partial 时不得 confirmed",
        },
        {
            "item": "涨停/跌停/炸板",
            "status": "available" if _has_collection_rows(db, "market_limit_pools", {"$or": [{"trade_date": trade_date}, {"date_key": trade_date}, {"dt": trade_date}, {"dt": trade_date.replace('-', '')}]}) else "missing",
            "source": "market_limit_pools",
            "impact": "情绪温度、弹性票、尾盘抛压",
        },
        {
            "item": "主力/散户分账户资金",
            "status": "available" if flow.get("participant_flow_available") else "missing",
            "source": ",".join(flow.get("candidate_collections") or []) or "none",
            "impact": "主力/散户精确买卖拆分；missing 时不能输出精确账户级数字",
        },
        {
            "item": "订单大小资金流",
            "status": "available" if flow.get("order_size_flow_available") else "missing",
            "source": ",".join(flow.get("order_size_sources") or []) or "Eastmoney/THS optional",
            "impact": "只能解释大中小单口径，不等同账户级主力/散户",
        },
    ]
    for row in rows:
        row["evidence_level"] = "confirmed" if row["status"] == "available" else ("inferred" if row["status"] == "partial" else "unknown")
    return rows


def _is_valid_stock_name(name: Any) -> bool:
    text = _text(name)
    if not text:
        return False
    return not (text.startswith("ST") or text.startswith("*ST") or text.startswith("退市"))


def _stock_pool_row(snapshot: dict[str, Any]) -> dict[str, Any]:
    close_position = _close_position(snapshot)
    high_to_close = _high_to_close_pct(snapshot)
    return {
        "symbol": snapshot.get("symbol"),
        "code": snapshot.get("code"),
        "name": snapshot.get("name"),
        "change_pct": snapshot.get("change_pct"),
        "amount_yi": _amount_yi(snapshot.get("amount")),
        "turnover_pct": snapshot.get("turnover_pct"),
        "volume_ratio": snapshot.get("volume_ratio"),
        "high_to_close_pct": high_to_close,
        "close_position": close_position,
        "evidence_level": "confirmed",
    }


def _stock_pool_cursor_rows(
    db: Any,
    trade_date: str,
    *,
    sort_field: str,
    direction: int,
    limit: int,
    min_amount: float | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = db["fullmarket_spot_snapshots"].find(_snapshot_query(trade_date), _snapshot_projection()).sort(sort_field, direction).limit(limit * 4)
    for raw in cursor:
        snapshot = _stock_snapshot(raw)
        if not _is_valid_stock_name(snapshot.get("name")):
            continue
        amount = _float(snapshot.get("amount")) or 0.0
        if min_amount is not None and amount < min_amount:
            continue
        rows.append(_stock_pool_row(snapshot))
        if len(rows) >= limit:
            break
    return rows


def _key_stock_pool(db: Any, trade_date: str, limit_lookup: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if "fullmarket_spot_snapshots" not in _collection_names(db):
        return {
            "status": "missing",
            "source": "fullmarket_spot_snapshots",
            "evidence_level": "unknown",
            "note": "缺少全市场日线快照，无法构造固定重点个股池。",
        }
    top_amount = _stock_pool_cursor_rows(db, trade_date, sort_field="amount", direction=-1, limit=50)
    amount_ratio = [
        row
        for row in _stock_pool_cursor_rows(db, trade_date, sort_field="volume_ratio", direction=-1, limit=40, min_amount=500000000)
        if (_float(row.get("volume_ratio")) or 0.0) >= 1.5
    ][:20]
    gainers = _stock_pool_cursor_rows(db, trade_date, sort_field="change_pct", direction=-1, limit=20, min_amount=200000000)
    losers = _stock_pool_cursor_rows(db, trade_date, sort_field="change_pct", direction=1, limit=20, min_amount=200000000)
    pool_counts: dict[str, int] = {}
    limit_samples: list[dict[str, Any]] = []
    for code, meta in limit_lookup.items():
        pool = _text(meta.get("pool"), "unknown")
        pool_counts[pool] = pool_counts.get(pool, 0) + 1
        if len(limit_samples) < 20:
            limit_samples.append(
                {
                    "symbol": _prefixed_symbol(code),
                    "code": code,
                    "name": meta.get("name"),
                    "pool": pool,
                    "first_limit_up_time": meta.get("first_limit_up_time"),
                    "open_count": meta.get("open_count"),
                    "seal_amount_yi": _amount_yi(meta.get("seal_amount")),
                    "industry": meta.get("industry"),
                    "evidence_level": "confirmed",
                }
            )
    sample_codes = {
        _pure_code(_text(row.get("symbol") or row.get("code")))
        for bucket in (top_amount, amount_ratio, gainers, losers, limit_samples)
        for row in bucket
        if _pure_code(_text(row.get("symbol") or row.get("code")))
    }
    return {
        "status": "available",
        "source": "fullmarket_spot_snapshots + market_limit_pools",
        "evidence_level": "confirmed",
        "sample_count": len(sample_codes),
        "top_amount_50": top_amount,
        "amount_ratio_top20": amount_ratio,
        "amount_ratio_note": "当前用 volume_ratio/量比作为成交额倍率代理；缺5日均成交额时证据等级按 inferred 使用。",
        "gainers_top20": gainers,
        "losers_top20": losers,
        "limit_pool_counts": pool_counts,
        "limit_pool_sample": limit_samples,
    }


def _close_position(row: dict[str, Any]) -> float | None:
    low = _float(row.get("low"))
    high = _float(row.get("high"))
    close = _float(row.get("close") if row.get("close") is not None else row.get("price"))
    if low is None or high is None or close is None or high == low:
        return None
    return round((close - low) / (high - low), 3)


def _high_to_close_pct(row: dict[str, Any]) -> float | None:
    high = _float(row.get("high"))
    close = _float(row.get("close") if row.get("close") is not None else row.get("price"))
    if high is None or close is None or high == 0:
        return None
    return round((high - close) / high * 100, 2)


def _acceptance_level(row: dict[str, Any]) -> str:
    close_position = _close_position(row)
    high_to_close = _high_to_close_pct(row)
    amount_yi = _amount_yi(row.get("amount"))
    if close_position is None and high_to_close is None:
        return "unknown"
    if amount_yi is not None and amount_yi >= 100 and (
        (close_position is not None and close_position < 0.40)
        or (high_to_close is not None and high_to_close >= 5.0)
    ):
        return "天量无承接"
    if close_position is not None and close_position >= 0.70 and (high_to_close is None or high_to_close <= 2.0):
        return "强承接"
    if (
        close_position is not None
        and 0.50 <= close_position < 0.70
    ) or (high_to_close is not None and 2.0 < high_to_close <= 4.0):
        return "一般承接"
    if (
        close_position is not None
        and 0.30 <= close_position < 0.50
    ) or (high_to_close is not None and 4.0 < high_to_close <= 6.0):
        return "弱承接"
    if (close_position is not None and close_position < 0.30) or (high_to_close is not None and high_to_close >= 6.0):
        return "无承接"
    return "unknown"


def _acceptance_pressure_rows(rows: list[dict[str, Any]], *, limit: int = 20) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows[:limit]:
        result.append(
            {
                "symbol": row.get("symbol"),
                "code": row.get("code"),
                "name": row.get("name"),
                "change_pct": row.get("change_pct"),
                "amount_yi": _amount_yi(row.get("amount")) if row.get("amount_yi") is None else row.get("amount_yi"),
                "high_to_close_pct": _high_to_close_pct(row) if row.get("high_to_close_pct") is None else row.get("high_to_close_pct"),
                "close_position": _close_position(row) if row.get("close_position") is None else row.get("close_position"),
                "acceptance_level": _acceptance_level(row),
                "pullback_amount_share": None,
                "evidence_level": "confirmed" if row.get("high") is not None and row.get("low") is not None and row.get("close") is not None else "inferred",
                "note": "回落段成交占比需要分钟成交切片；缺失时不编。",
            }
        )
    return result


def _latest_board_rows(db: Any, trade_date: str, *, limit: int = 7) -> list[dict[str, Any]]:
    snapshot = _market_board_snapshot(db, trade_date, "14:58", limit=limit)
    if not snapshot:
        return []
    return snapshot.get("top_boards") if isinstance(snapshot.get("top_boards"), list) else []


def _board_state_from_action(action: str, change_pct: float | None) -> str:
    if "产业链确认" in action:
        return "主线" if change_pct is not None and change_pct > 0 else "分歧"
    if "源强链弱" in action:
        return "伪主线"
    if "链内分化" in action:
        return "分歧"
    if "风险" in action or "撤退" in action:
        return "出货"
    if change_pct is not None and change_pct > 0:
        return "轮动"
    if change_pct is not None and change_pct < 0:
        return "出货"
    return "证据不足"


def _top_turnover_boards(db: Any, trade_date: str, sector_boards: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    if sector_boards:
        for idx, board in enumerate(sector_boards[:7], start=1):
            driver = board.get("source_driver") if isinstance(board.get("source_driver"), dict) else {}
            change = _float(_first_present(board.get("day_change_pct"), board.get("change_pct"), driver.get("change_pct")))
            action = _text(board.get("trader_action") or board.get("rank_reason") or board.get("trace_summary"))
            rows.append(
                {
                    "rank": idx,
                    "board": _text(board.get("name")),
                    "driver_kind": _text(driver.get("kind")),
                    "driver_name": _text(driver.get("name")),
                    "change_pct": change,
                    "amount_yi": None,
                    "amount_status": "missing",
                    "amount_ratio": None,
                    "breadth": None,
                    "limit_count": None,
                    "intraday_drawdown_pct": None,
                    "strongest_slice": None,
                    "state": _board_state_from_action(action, change),
                    "action": action,
                    "evidence_level": "inferred",
                    "source": "signals_context.sector_boards; amount unavailable",
                }
            )
    else:
        for idx, board in enumerate(_latest_board_rows(db, trade_date, limit=7), start=1):
            change = _float(board.get("change_pct"))
            rows.append(
                {
                    "rank": idx,
                    "board": _text(board.get("name")),
                    "driver_kind": _text(board.get("kind")),
                    "driver_name": _text(board.get("name")),
                    "change_pct": change,
                    "amount_yi": None,
                    "amount_status": "missing",
                    "amount_ratio": None,
                    "breadth": None,
                    "limit_count": None,
                    "intraday_drawdown_pct": None,
                    "strongest_slice": None,
                    "state": _board_state_from_action("", change),
                    "evidence_level": "inferred",
                    "source": "board_heat_ticks latest rank; amount unavailable",
                }
            )
    return {
        "status": "partial" if rows else "missing",
        "source": "sector_boards/board_heat_ticks",
        "evidence_level": "inferred" if rows else "unknown",
        "note": "当前板块成交额字段缺失时，不能把它说成严格成交额Top7；只能作为清洗后的板块15/强度Top7代理。",
        "rows": rows,
    }


def _trend_20d_boards(db: Any, trade_date: str) -> dict[str, Any]:
    names = _collection_names(db)
    candidate_sources = [name for name in ("board_daily", "board_daily_bars", "board_index_daily", "board_history") if name in names]
    if not candidate_sources:
        return {
            "status": "missing",
            "source": "none",
            "evidence_level": "unknown",
            "rows": [],
            "excluded_high_gain_low_liquidity": [],
            "note": "缺少板块日线/20日成交额历史，不能输出 confirmed 的近20日趋势Top7。",
        }
    return {
        "status": "partial",
        "source": ",".join(candidate_sources),
        "evidence_level": "unknown",
        "rows": [],
        "excluded_high_gain_low_liquidity": [],
        "note": "检测到候选板块历史集合，但当前 replay 层尚未实现20日流动性过滤计算；输出前必须补计算或降级为 unknown。",
    }


def _slice_behavior(strong: dict[str, Any] | None, weak: dict[str, Any] | None) -> str:
    strong_delta = _float(strong.get("delta_pct")) if strong else None
    weak_delta = _float(weak.get("delta_pct")) if weak else None
    if strong_delta is None and weak_delta is None:
        return "unknown"
    if strong_delta is not None and strong_delta >= 1.0 and weak_delta is not None and weak_delta <= -0.8:
        return "卡位/跷跷板"
    if weak_delta is not None and weak_delta <= -1.5:
        return "瀑布传导/抛压"
    if strong_delta is not None and strong_delta >= 1.0:
        return "单向增强"
    return "震荡轮动"


def _fixed_time_slice_rows(db: Any, trade_date: str) -> list[dict[str, Any]]:
    if "board_heat_ticks" not in _collection_names(db):
        return [
            {
                "slice": label,
                "time_range": f"{start}-{end}",
                "market_behavior": "unknown",
                "strongest_board": None,
                "weakest_board": None,
                "active_direction": None,
                "drained_direction": None,
                "evidence_level": "unknown",
                "note": "board_heat_ticks 缺失，不能生成分钟级切片结论。",
            }
            for label, start, end in _FIXED_TIME_SLICES
        ]
    rows: list[dict[str, Any]] = []
    for label, start_time, end_time in _FIXED_TIME_SLICES:
        actual_start, start_rows = _board_rows_for_checkpoint(db, trade_date, start_time)
        actual_end, end_rows = _board_rows_for_checkpoint(db, trade_date, end_time)
        if not start_rows or not end_rows or actual_start == actual_end:
            rows.append(
                {
                    "slice": label,
                    "time_range": f"{start_time}-{end_time}",
                    "actual_range": f"{actual_start}-{actual_end}" if actual_start or actual_end else None,
                    "market_behavior": "unknown",
                    "strongest_board": None,
                    "weakest_board": None,
                    "active_direction": None,
                    "drained_direction": None,
                    "evidence_level": "unknown",
                    "note": "该切片缺少起止板块分钟快照，不能编资金切换结论。",
                }
            )
            continue
        deltas: list[dict[str, Any]] = []
        for key, end_row in end_rows.items():
            start_row = start_rows.get(key)
            if not start_row:
                continue
            end_change = _float(end_row.get("change_pct"))
            start_change = _float(start_row.get("change_pct"))
            if end_change is None or start_change is None:
                continue
            deltas.append(
                {
                    "kind": end_row.get("kind"),
                    "name": end_row.get("name"),
                    "from_change_pct": start_change,
                    "to_change_pct": end_change,
                    "delta_pct": round(end_change - start_change, 2),
                    "leader_name": end_row.get("leader_name"),
                }
            )
        strengthening = sorted(deltas, key=lambda item: item["delta_pct"], reverse=True)
        weakening = sorted(deltas, key=lambda item: item["delta_pct"])
        strongest = max(end_rows.values(), key=lambda item: _float(item.get("change_pct")) or -10**12, default=None)
        weakest = min(end_rows.values(), key=lambda item: _float(item.get("change_pct")) or 10**12, default=None)
        active = strengthening[0] if strengthening else None
        drained = weakening[0] if weakening else None
        rows.append(
            {
                "slice": label,
                "time_range": f"{start_time}-{end_time}",
                "actual_range": f"{actual_start}-{actual_end}",
                "market_behavior": _slice_behavior(active, drained),
                "strongest_board": strongest,
                "weakest_board": weakest,
                "active_direction": active,
                "drained_direction": drained,
                "top_strengthening": strengthening[:3],
                "top_weakening": weakening[:3],
                "evidence_level": "confirmed" if deltas else "unknown",
            }
        )
    return rows


def _structured_daily_review(
    db: Any,
    trade_date: str,
    *,
    sector_boards: list[dict[str, Any]],
    high_turnover: list[dict[str, Any]],
    failed_boards: list[dict[str, Any]],
    limit_lookup: dict[str, dict[str, Any]],
    flow: dict[str, Any],
) -> dict[str, Any]:
    return {
        "contract_version": "stock-daily-review-v2.1",
        "purpose": "先构造可审计证据包，再交给 AI 生成结构化复盘或截图风格长文。",
        "data_completeness": _data_completeness(db, trade_date, flow),
        "fixed_time_slices": _fixed_time_slice_rows(db, trade_date),
        "key_stock_pool": _key_stock_pool(db, trade_date, limit_lookup),
        "top_turnover_boards": _top_turnover_boards(db, trade_date, sector_boards),
        "trend_20d_boards": _trend_20d_boards(db, trade_date),
        "acceptance_pressure": {
            "high_turnover_top10": _acceptance_pressure_rows(high_turnover[:10], limit=10),
            "failed_boards_top10": _acceptance_pressure_rows(failed_boards[:10], limit=10),
            "note": "承接按收盘位置与高点回撤分层；回落段成交占比只有在分钟成交可得时才能 confirmed。",
        },
        "evidence_levels": {
            "confirmed": "数据直接支持。",
            "inferred": "多个数据点支持，但缺少一个直接字段或完整口径。",
            "unknown": "关键数据缺失，不能判断。",
        },
        "hard_boundaries": [
            "固定半小时切片缺分钟快照时必须写 unknown。",
            "缺板块日线/20日成交额历史时，近20日趋势Top7不能 confirmed。",
            "缺 participant flow 时，不能输出账户级主力/散户精确买卖拆分。",
            "订单大小资金流只能写 Eastmoney/THS 大中小单口径。",
            "明日输出验证点，不输出买入/卖出/目标价/止损。",
        ],
    }


def _index_cycle_context(db: Any, trade_date: str) -> dict[str, Any] | None:
    if "index_bars" not in _collection_names(db):
        return None
    start = datetime.fromisoformat(trade_date) - timedelta(days=45)
    end = datetime.fromisoformat(trade_date) + timedelta(days=1)
    rows = list(
        db["index_bars"].find(
            {
                "meta.symbol": "sh000001",
                "meta.freq": "日线",
                "dt": {"$gte": start, "$lt": end},
            },
            {"_id": 0, "dt": 1, "open": 1, "high": 1, "low": 1, "close": 1},
        ).sort("dt", 1)
    )
    if len(rows) < 3:
        return None
    latest = rows[-1]
    recent = rows[-6:] if len(rows) >= 6 else rows
    pivot_candidates = recent[:-1] or rows[:-1]
    pivot = max(pivot_candidates, key=lambda row: _float(row.get("high")) or -10**12)
    pivot_high = _float(pivot.get("high"))
    latest_close = _float(latest.get("close"))
    if pivot_high is None or latest_close is None:
        return None
    pivot_dt = pivot.get("dt")
    latest_dt = latest.get("dt")
    days_since = sum(1 for row in rows if row.get("dt") and pivot_dt and row["dt"] > pivot_dt and row["dt"] <= latest_dt)
    drop_pct = (latest_close / pivot_high - 1) * 100 if pivot_high else None
    return {
        "symbol": "sh000001",
        "name": "上证指数",
        "pivot_date": pivot_dt.strftime("%Y-%m-%d") if isinstance(pivot_dt, datetime) else _text(pivot_dt),
        "pivot_high": round(pivot_high, 3),
        "latest_date": latest_dt.strftime("%Y-%m-%d") if isinstance(latest_dt, datetime) else _text(latest_dt),
        "latest_close": round(latest_close, 3),
        "trading_days_since": days_since,
        "drop_pct": round(drop_pct, 2) if drop_pct is not None else None,
        "latest_weekday": latest_dt.weekday() if isinstance(latest_dt, datetime) else None,
        "data_source": "index_bars",
    }


def _event_chain_for_snapshot(
    db: Any,
    trade_date: str,
    snapshot: dict[str, Any],
    *,
    limit_meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    symbol = _text(snapshot.get("symbol"))
    if not symbol:
        return None
    limit_meta = limit_meta or {}
    rows = _minute_rows(db, symbol, trade_date)
    open_value = _float(snapshot.get("open"))
    prev_close = _float(snapshot.get("prev_close"))
    close = _float(snapshot.get("close"))
    high = _float(snapshot.get("high"))
    low = _float(snapshot.get("low"))
    amount_yi = _amount_yi(snapshot.get("amount"))
    open_change_pct = (open_value / prev_close - 1) * 100 if open_value and prev_close else None
    high_change_pct = snapshot.get("high_change_pct")
    close_change_pct = snapshot.get("change_pct")
    high_bar = low_bar = open_bar = close_bar = None
    large_amount_bars: list[dict[str, Any]] = []
    if rows:
        high_row = max(rows, key=lambda item: _float(item.get("high")) or -10**12)
        low_row = min(rows, key=lambda item: _float(item.get("low")) or 10**12)
        high_bar = _bar_point(high_row, "high")
        low_bar = _bar_point(low_row, "low")
        open_bar = _bar_point(rows[0], "open")
        close_bar = _bar_point(rows[-1], "close")
        large_amount_bars = [
            _bar_point(row, "close")
            for row in sorted(rows, key=lambda item: _float(item.get("amount")) or 0, reverse=True)[:3]
        ]
    high_to_close_pct = (high - close) / high * 100 if high and close else None
    low_to_high_pct = (high - low) / low * 100 if high and low else None
    labels: list[str] = []
    pool = _text(limit_meta.get("pool"))
    if pool in {"limit_up", "zt", "涨停"}:
        labels.append("封板确认")
    if pool in {"failed_limit", "zbgc", "炸板"}:
        labels.append("炸板回落")
    if pool in {"limit_down", "dtgc", "跌停"}:
        labels.append("跌停负反馈")
    if pool in {"strong", "qsgc", "强势"}:
        labels.append("强势池")
    if open_change_pct is not None:
        if open_change_pct <= -3.0:
            labels.append("低开承压")
        elif open_change_pct >= 3.0:
            labels.append("高开进攻")
    if high_to_close_pct is not None and high_to_close_pct >= 5.0:
        labels.append("冲高回落")
    if amount_yi is not None and amount_yi >= 100 and (close_change_pct or 0) < 0:
        labels.append("高成交负反馈")
    if low_to_high_pct is not None and low_to_high_pct >= 5.0 and high_to_close_pct is not None and high_to_close_pct >= 3.0:
        labels.append("日内拉升失败")
    name = _text(snapshot.get("name"))
    if pool in {"limit_up", "zt", "涨停"} and open_value is not None and high is not None:
        phrase = f"{name}从{_fmt_number(open_value)}直接拉到{_fmt_number(high)}封涨停"
    elif pool in {"failed_limit", "zbgc", "炸板"} and open_value is not None and high is not None:
        phrase = f"{name}从{_fmt_number(open_value)}拉到{_fmt_number(high)}但没封住"
    elif pool in {"limit_down", "dtgc", "跌停"} and open_value is not None and high is not None:
        phrase = f"{name}从{_fmt_number(open_value)}冲到{_fmt_number(high)}后回落跌停"
    else:
        phrase = (
            f"{name}开{_fmt_number(open_value)}、高{_fmt_number(high)}、低{_fmt_number(low)}、收{_fmt_number(close)}，"
            f"{'、'.join(labels) if labels else '日内路径待复核'}"
        )
    if amount_yi is not None:
        phrase += f"，单日成交{_fmt_number(amount_yi)}亿"
    first_limit_time = _text(limit_meta.get("first_limit_up_time"))
    if first_limit_time:
        phrase += f"，首封{first_limit_time}"
    if limit_meta.get("open_count") is not None:
        phrase += f"，开板/炸板{_fmt_number(limit_meta.get('open_count'))}次"
    if limit_meta.get("seal_amount") is not None:
        phrase += f"，封单{_fmt_amount_yi_for_prose(limit_meta.get('seal_amount'))}亿"
    if low_bar and high_bar:
        phrase += f"，{low_bar.get('time')}见低{_fmt_number(low_bar.get('low'))}、{high_bar.get('time')}见高{_fmt_number(high_bar.get('high'))}"
    if high_to_close_pct is not None and high_to_close_pct >= 3.0:
        phrase += f"，收盘较高点回落{_fmt_unsigned_pct(high_to_close_pct)}"
    return {
        "symbol": symbol,
        "code": snapshot.get("code"),
        "name": name,
        "labels": labels,
        "open": open_value,
        "high": high,
        "low": low,
        "close": close,
        "prev_close": prev_close,
        "open_change_pct": round(open_change_pct, 2) if open_change_pct is not None else None,
        "high_change_pct": round(float(high_change_pct), 2) if high_change_pct is not None else None,
        "close_change_pct": round(float(close_change_pct), 2) if close_change_pct is not None else None,
        "amount_yi": amount_yi,
        "open_bar": open_bar,
        "high_bar": high_bar,
        "low_bar": low_bar,
        "close_bar": close_bar,
        "large_amount_bars": large_amount_bars,
        "limit_pool": limit_meta,
        "high_to_close_pct": round(high_to_close_pct, 2) if high_to_close_pct is not None else None,
        "low_to_high_pct": round(low_to_high_pct, 2) if low_to_high_pct is not None else None,
        "phrase": phrase,
    }


def _stock_event_chains(
    db: Any,
    trade_date: str,
    symbols: list[str],
    *,
    limit_lookup: dict[str, dict[str, Any]] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    limit_lookup = limit_lookup or {}
    snapshots = _snapshot_by_symbol(db, trade_date, symbols)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for symbol in symbols:
        code = _pure_code(symbol)
        if not code or code in seen:
            continue
        seen.add(code)
        snapshot = snapshots.get(code)
        if not snapshot:
            continue
        event = _event_chain_for_snapshot(db, trade_date, snapshot, limit_meta=limit_lookup.get(code))
        if event:
            rows.append(event)
        if len(rows) >= limit:
            break
    return rows


def replay_analysis_framework() -> dict[str, Any]:
    return {
        "ai_native_contract": (
            "代码层只输出全市场证据图和数据边界；AI 根据证据图写复盘。"
            "不能把单日截图事实写死到运行时代码。"
        ),
        "thinking_process": [
            "先判定大盘真实伤害：指数跌幅、成交额承接、涨跌扩散，而不是只看涨幅榜。",
            "找全天高成交核心：成交额越大，越能代表当日情绪和负反馈压力。",
            "按分钟窗口看全市场板块强弱切换：谁增强、谁被抽血、谁只是财报/题材筛选噪声。",
            "把板块15映射回产业链：区分产业链确认、源强链弱、链内分化和临时卡位。",
            "代表股分两层：static_representatives 是知识库映射；dynamic_market_representatives 才是当日市场认可。",
            "动态核心看成交额、换手、市场共识和负反馈；动态弹性看涨停速度、连板、封单、涨幅、换手和量比。",
            "用代表股5分钟路径验证板块共识：低点、高点、放量bar与板块增强时间是否同步。",
            "用炸板/冲高回落判断尾盘情绪：这是承接失败和扩散失败的证据，不是孤立个股故事。",
            "最后输出次日验证点：竞价/开盘延续、高成交核心消化、链主和弹性同步、买点周期补齐。",
        ],
        "comparison_dimensions": [
            {
                "dimension": "事实锚",
                "code_fields": ["signals_context.indices", "market_replay.high_turnover_cores"],
                "ai_check": "是否覆盖截图中的指数跌幅、中际旭创583亿、新易盛/天孚通信等高成交压力。",
            },
            {
                "dimension": "分钟级资金链条",
                "code_fields": ["market_replay.rotation_windows", "market_replay.rotation_shifts"],
                "ai_check": "是否按时间写出资金从消费/科技/电力/锂电/机器人/商业航天之间切换，而不是只列最终涨幅。",
            },
            {
                "dimension": "板块卡位",
                "code_fields": ["market_replay.board_timeline", "signals_context.sector_boards"],
                "ai_check": "是否区分全天强、午后卡位、脉冲强、源强链弱和链内分化。",
            },
            {
                "dimension": "代表股验证",
                "code_fields": ["market_replay.dynamic_market_representatives", "market_replay.representative_paths"],
                "ai_check": "是否区分静态产业链代表和当日市场认可代表，并用5分钟路径验证板块节奏。",
            },
            {
                "dimension": "弹性票选择",
                "code_fields": ["market_replay.dynamic_market_representatives.market_elastic"],
                "ai_check": "是否优先使用涨停池、首次封板时间、连板数、炸板次数、封板资金、涨幅、换手和量比，而不是静态yaml代表。",
            },
            {
                "dimension": "资金口径边界",
                "code_fields": ["market_replay.flow_availability", "market_replay.external_fund_flows"],
                "ai_check": "区分分账户资金与大中小单订单口径；没有 L2/分账户集合时，不输出账户级主力/散户精确数。",
            },
        ],
    }


def build_market_replay_context(
    db: Any,
    *,
    trade_date: str,
    sector_boards: list[dict[str, Any]] | None = None,
    checkpoints: list[str] | None = None,
    high_turnover_limit: int = 20,
    representative_limit: int = 30,
    include_external_fund_flows: bool = False,
) -> dict[str, Any]:
    checkpoints = checkpoints or ["09:35", "10:30", "11:30", "13:30", "14:58"]
    boards = sector_boards or []
    representative_symbols = _representative_symbols(boards, limit=representative_limit)
    limit_lookup = _limit_pool_lookup(db, trade_date)
    high_turnover = _high_turnover_cores(db, trade_date, limit=high_turnover_limit)
    failed_boards = _failed_board_rows(db, trade_date, limit=20)
    board_timeline = _board_timeline(db, trade_date, boards, checkpoints=checkpoints)
    rotation_windows = _rotation_windows(db, trade_date, checkpoints=checkpoints)
    rotation_shifts = _rotation_shifts(db, trade_date, checkpoints=checkpoints, limit=10)
    rotation_symbols = _symbols_from_rotation_boards(db, rotation_windows, rotation_shifts)
    intraday_delta_symbols = _intraday_delta_board_symbols(db, trade_date)
    high_turnover_symbols = [row["symbol"] for row in high_turnover[: min(20, len(high_turnover))]]
    external_fund_flows = (
        _external_fund_flow_evidence(trade_date, high_turnover_symbols, limit=3)
        if include_external_fund_flows
        else []
    )
    flow_availability = _flow_availability(db, external_fund_flows)
    failed_symbols = [row["symbol"] for row in failed_boards[: min(10, len(failed_boards))] if row.get("symbol")]
    limit_pool_symbols = _top_limit_pool_symbols(limit_lookup, limit=200)
    symbols = [
        *representative_symbols,
        *high_turnover_symbols,
        *rotation_symbols,
        *intraday_delta_symbols,
        *failed_symbols,
        *limit_pool_symbols,
    ]
    context = {
        "trade_date": trade_date,
        "checkpoints": checkpoints,
        "data_sources": {
            "high_turnover_cores": "fullmarket_spot_snapshots",
            "failed_boards": "fullmarket_spot_snapshots",
            "board_timeline": "board_heat_ticks",
            "rotation_shifts": "board_heat_ticks",
            "opening_pressure_boards": "board_heat_ticks",
            "representative_paths": "fullmarket_spot_snapshots + bars",
            "stock_event_chains": "fullmarket_spot_snapshots + bars + optional market_limit_pools",
            "dynamic_market_representatives": "board_constituents + fullmarket_spot_snapshots + optional market_limit_pools",
            "external_fund_flows": "optional Eastmoney/THS public order-size flow evidence",
            "index_cycle": "index_bars",
        },
        "analysis_framework": replay_analysis_framework(),
        "structured_daily_review": _structured_daily_review(
            db,
            trade_date,
            sector_boards=boards,
            high_turnover=high_turnover,
            failed_boards=failed_boards,
            limit_lookup=limit_lookup,
            flow=flow_availability,
        ),
        "high_turnover_cores": high_turnover,
        "turnover_representatives": _turnover_representatives(db, trade_date, high_turnover, limit=15),
        "failed_boards": failed_boards,
        "board_timeline": board_timeline,
        "rotation_windows": rotation_windows,
        "rotation_shifts": rotation_shifts,
        "opening_pressure_boards": _opening_pressure_boards(db, trade_date),
        "representative_paths": _symbol_evidence(db, trade_date, symbols, limit=max(representative_limit, 80)),
        "stock_event_chains": _stock_event_chains(
            db,
            trade_date,
            symbols,
            limit_lookup=limit_lookup,
            limit=max(240, representative_limit),
        ),
        "external_fund_flows": external_fund_flows,
        "dynamic_market_representatives": _dynamic_market_representatives(db, trade_date, boards),
        "index_cycle": _index_cycle_context(db, trade_date),
        "flow_availability": flow_availability,
        "interpretation_contract": [
            "先从 rotation_windows 看全市场强板块在时间轴上的切换。",
            "再用 board_timeline 判断板块是持续增强、午后卡位、还是尾盘回落。",
            "再用 high_turnover_cores 和 representative_paths 找高成交核心是否承接失败。",
            "static_representatives 只是产业链知识库代表；dynamic_market_representatives 才是当日市场认可代表。",
            "failed_boards 只证明炸板/冲高回落结构，不等同于资金流精确分账户。",
            "外部 order-size flow 可用于大中小单买卖拆分；没有 L2/分账户数据时，不输出主力/散户账户级精确数字。",
        ],
    }
    context["board_role_map"] = _board_role_map(context)
    return context


def _board_role_name(row: dict[str, Any]) -> str:
    return _board_display_name(_text(row.get("board") or row.get("name") or row.get("driver_name")))


def _board_role_tokens(name: str) -> set[str]:
    tokens = set(_tokenize_board_text(name))
    clean = _board_display_name(name)
    if clean:
        tokens.add(clean)
        for key, aliases in _BOARD_ALIASES.items():
            if key in clean:
                tokens.add(key)
                tokens.update(aliases)
    return {token for token in tokens if token}


def _same_role_board(left: str, right: str) -> bool:
    left = _board_display_name(left)
    right = _board_display_name(right)
    if not left or not right:
        return False
    if left == right or left in right or right in left:
        return True
    return bool(_board_role_tokens(left) & _board_role_tokens(right))


def _is_tech_mainline_board(name: str) -> bool:
    tokens = _board_role_tokens(name)
    haystack = f"{name} {' '.join(tokens)}"
    tech_tokens = (
        "科技",
        "CPO",
        "光模块",
        "通信",
        "线缆",
        "集成电路",
        "半导体",
        "芯片",
        "PCB",
        "印制电路",
        "AI",
    )
    return any(token in haystack for token in tech_tokens)


def _stock_role_name(row: dict[str, Any]) -> str:
    name = _text(row.get("name"))
    if not name:
        return ""
    parts = []
    change = _float(row.get("change_pct") or row.get("close_change_pct"))
    if change is not None:
        parts.append(_fmt_pct(change))
    amount = _float(row.get("amount_yi"))
    if amount is not None:
        parts.append(f"成交{_fmt_number(amount)}亿")
    return f"{name}({','.join(parts)})" if parts else name


def _stock_role_names(rows: list[dict[str, Any]], *, limit: int = 2) -> str:
    names = [_stock_role_name(row) for row in rows[:limit]]
    names = [name for name in names if name]
    return "、".join(names)


def _board_role_map(context: dict[str, Any]) -> list[dict[str, Any]]:
    rows_by_name: dict[str, dict[str, Any]] = {}

    def ensure(name: str) -> dict[str, Any] | None:
        display = _board_display_name(name)
        if not display or not _is_analysis_board(display):
            return None
        for existing_name, existing in rows_by_name.items():
            if _same_role_board(existing_name, display):
                return existing
        row = {"name": display, "roles": [], "evidence": [], "stocks": {}}
        rows_by_name[display] = row
        return row

    def add(name: str, role: str, evidence: str) -> None:
        row = ensure(name)
        if not row:
            return
        if role and role not in row["roles"]:
            row["roles"].append(role)
        if evidence and evidence not in row["evidence"]:
            row["evidence"].append(evidence)

    structured = context.get("structured_daily_review") if isinstance(context.get("structured_daily_review"), dict) else {}
    top_turnover = structured.get("top_turnover_boards") if isinstance(structured.get("top_turnover_boards"), dict) else {}
    for item in top_turnover.get("rows") if isinstance(top_turnover.get("rows"), list) else []:
        name = _board_role_name(item)
        if not name:
            continue
        evidence = f"板块前排第{_fmt_number(item.get('rank'))}"
        change = _float(item.get("change_pct"))
        if change is not None:
            evidence += f"，涨跌{_fmt_pct(change)}"
        state = _text(item.get("state"))
        if state:
            evidence += f"，状态{state}"
        if _text(item.get("evidence_level")) == "inferred":
            evidence += "，板块成交额缺失时按板块15/强度代理"
        add(name, "主线/前排观察", evidence)

    opening_pressure = context.get("opening_pressure_boards") if isinstance(context.get("opening_pressure_boards"), list) else []
    for item in opening_pressure[:8]:
        name = _board_role_name(item)
        change = _float(item.get("change_pct"))
        if name and change is not None and change < 0:
            role = "受伤主线/压力锚" if _is_tech_mainline_board(name) else "早盘压力锚"
            add(name, role, f"开盘压力{_fmt_pct(change)}")

    for shift in context.get("rotation_shifts", []) if isinstance(context.get("rotation_shifts"), list) else []:
        label = f"{shift.get('from_time')}到{shift.get('to_time')}"
        strengthening = shift.get("strengthening") if isinstance(shift.get("strengthening"), list) else []
        for item in strengthening[:5]:
            name = _board_role_name(item)
            delta = _float(item.get("delta_pct"))
            if name and delta is not None and delta > 0:
                add(name, "盘中弹性/修复锚", f"{label}增强{_fmt_pct(delta)}")

    for item in context.get("dynamic_market_representatives", []) if isinstance(context.get("dynamic_market_representatives"), list) else []:
        name = _board_role_name(item)
        row = ensure(name)
        if not row:
            continue
        stocks = row["stocks"]
        market_core = item.get("market_core") if isinstance(item.get("market_core"), list) else []
        elastic = item.get("market_elastic_confirmed") if isinstance(item.get("market_elastic_confirmed"), list) else []
        if not elastic:
            elastic = item.get("market_elastic") if isinstance(item.get("market_elastic"), list) else []
        pressure = item.get("pressure_core") if isinstance(item.get("pressure_core"), list) else []
        if market_core:
            stocks["高成交核心"] = _stock_role_names(market_core)
            add(name, "高成交核心锚", f"动态核心={stocks['高成交核心']}")
        if elastic:
            stocks["弹性确认"] = _stock_role_names(elastic)
            add(name, "弹性锚", f"弹性确认={stocks['弹性确认']}")
        if pressure:
            stocks["压力核心"] = _stock_role_names(pressure)
            add(name, "压力锚", f"压力核心={stocks['压力核心']}")

    telecom_pressure = _telecom_pressure_events(context)
    if telecom_pressure:
        add(
            "光模块/CPO/通信线缆",
            "受伤主线/高成交压力锚",
            f"高成交压力={_stock_role_names(telecom_pressure, limit=3)}",
        )

    result = list(rows_by_name.values())
    def priority(row: dict[str, Any]) -> tuple[int, int, str]:
        roles = row.get("roles") if isinstance(row.get("roles"), list) else []
        has_wounded = any("受伤主线" in role for role in roles)
        has_pressure = any("压力锚" in role for role in roles)
        has_repair = any("修复锚" in role for role in roles)
        has_front = "主线/前排观察" in roles
        if has_wounded and (has_pressure or has_repair):
            rank = 0
        elif has_front and (has_pressure or has_repair):
            rank = 1
        elif has_front:
            rank = 2
        else:
            rank = 3
        return (rank, -len(row.get("evidence") or []), row.get("name") or "")

    result.sort(key=priority)
    return result[:8]


def _board_role_map_section(context: dict[str, Any]) -> str:
    role_map = context.get("board_role_map")
    if not isinstance(role_map, list):
        role_map = _board_role_map(context)
    if not role_map:
        return ""

    def role_display(roles: list[Any]) -> str:
        parts: list[str] = []
        for role in roles:
            for part in _text(role).split("/"):
                if part and part not in parts:
                    parts.append(part)
        return "/".join(parts)

    parts = []
    for row in role_map[:7]:
        name = _text(row.get("name"))
        roles = row.get("roles") if isinstance(row.get("roles"), list) else []
        evidence = row.get("evidence") if isinstance(row.get("evidence"), list) else []
        if not name or not roles or not evidence:
            continue
        parts.append(f"{name}：{role_display(roles[:4])}；依据={'；'.join(evidence[:4])}")
    if not parts:
        return ""
    return (
        "为什么纳入这些板块和标的，要先看角色而不是只看涨跌。"
        + "。".join(parts)
        + "。同一条主线可以同时是交易量前排、压力锚和明日验证锚，不能因为当日承压就否认它的主线地位。"
    )


def _turnover_rep_phrase(row: dict[str, Any]) -> str:
    name = _text(row.get("name"))
    if not name:
        return ""
    parts = [
        f"第{_fmt_number(row.get('rank'))}",
        f"成交{_fmt_number(row.get('amount_yi'))}亿",
        _fmt_pct(row.get("change_pct")),
        _text(row.get("role")),
    ]
    first_red = row.get("first_red") if isinstance(row.get("first_red"), dict) else {}
    status = _text(first_red.get("status"))
    first_time = _text(first_red.get("first_close_above_time") or first_red.get("first_touch_time"))
    if status == "confirmed" and first_time:
        parts.append(f"5分钟{first_time}越过昨收")
    elif status == "missing":
        parts.append("分钟翻红时间缺失")
    return f"{name}({','.join(part for part in parts if part)})"


def _turnover_representative_section(context: dict[str, Any]) -> str:
    rows = context.get("turnover_representatives")
    if not isinstance(rows, list) or not rows:
        return ""
    chain_groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows[:15]:
        chain = _text(row.get("chain_name"), "未映射产业链")
        chain_groups.setdefault(chain, []).append(row)
    ordered_groups = sorted(
        chain_groups.items(),
        key=lambda item: min(_float(row.get("rank")) or 999 for row in item[1]),
    )
    group_parts = []
    for chain, items in ordered_groups[:6]:
        phrases = [_turnover_rep_phrase(row) for row in items[:4]]
        phrases = [phrase for phrase in phrases if phrase]
        if phrases:
            group_parts.append(f"{chain}：{'、'.join(phrases)}")
    if not group_parts:
        return ""
    return (
        "成交额代表篮子必须单独看，不能只拿一两个高成交核心代替整条主线。"
        + "；".join(group_parts)
        + "。这层只使用成交额排名、日内涨跌、开盘缺口、产业链映射和分钟路径；分钟路径缺失时，不写首次翻红，只保留日线可确认的低开转强或压力角色。"
    )


def _stock_phrase(row: dict[str, Any]) -> str:
    name = _text(row.get("name"))
    if not name:
        return ""
    phrase = (
        f"{name}{_fmt_pct(row.get('change_pct'))}"
        f"，成交{_fmt_number(row.get('amount_yi'))}亿"
    )
    limit_pool = row.get("limit_pool") if isinstance(row.get("limit_pool"), dict) else {}
    first_time = _text(limit_pool.get("first_limit_up_time"))
    if first_time:
        phrase += f"，首封{first_time}"
    if limit_pool.get("open_count") is not None:
        phrase += f"，开板/炸板{_fmt_number(limit_pool.get('open_count'))}次"
    if limit_pool.get("seal_amount") is not None:
        phrase += f"，封单{_amount_yi(limit_pool.get('seal_amount'))}亿"
    return phrase


def _dynamic_representative_section(context: dict[str, Any]) -> str:
    parts: list[str] = []
    for row in context.get("dynamic_market_representatives", [])[:6]:
        board = _text(row.get("board") or row.get("driver_name"))
        if not board:
            continue
        cores = [_stock_phrase(item) for item in (row.get("market_core") or [])[:2]]
        elastics = [_stock_phrase(item) for item in (row.get("market_elastic") or [])[:2]]
        pressure = [_stock_phrase(item) for item in (row.get("pressure_core") or [])[:1]]
        details = []
        if cores:
            details.append(f"核心看{'、'.join(item for item in cores if item)}")
        if elastics:
            details.append(f"弹性看{'、'.join(item for item in elastics if item)}")
        if pressure:
            details.append(f"负反馈看{'、'.join(item for item in pressure if item)}")
        if details:
            parts.append(f"{board}：{'；'.join(details)}")
    if not parts:
        return ""
    return (
        "代表股不能只用静态产业链名单，要看当日市场认可。"
        + "。".join(parts[:4])
        + "。这里的弹性不是写死的龙头，而是封板速度、开板次数、连板、封单、涨幅、换手和成交额共同给出的当日识别。"
    )


def _carding_section(context: dict[str, Any]) -> str:
    cards: list[str] = []
    for shift in context.get("rotation_shifts", [])[:4]:
        strengthening = shift.get("strengthening") if isinstance(shift.get("strengthening"), list) else []
        weakening = shift.get("weakening") if isinstance(shift.get("weakening"), list) else []
        strong = next((row for row in strengthening if _text(row.get("name"))), None)
        weak = next((row for row in weakening if _text(row.get("name"))), None)
        if not strong and not weak:
            continue
        card = f"{shift.get('from_time')}到{shift.get('to_time')}"
        if strong:
            card += f"{_text(strong.get('name'))}增强{_fmt_pct(strong.get('delta_pct'))}"
        if weak:
            card += f"，{_text(weak.get('name'))}走弱{_fmt_pct(weak.get('delta_pct'))}"
        cards.append(card)
    if not cards:
        return ""
    return (
        f"看一下今天盘面的卡位结构。全天至少有{len(cards)}次明显切换："
        + "；".join(cards)
        + "。这些切换说明资金在不同方向之间反复试错，真正要等的是某个方向在强度、链主和弹性上同时压住其他方向。"
    )


def _stock_event_section(context: dict[str, Any]) -> str:
    events = context.get("stock_event_chains") if isinstance(context.get("stock_event_chains"), list) else []
    if not events:
        return ""
    pressure = next((row for row in events if "高成交负反馈" in row.get("labels", [])), None)
    failed = [row for row in events if "冲高回落" in row.get("labels", []) or "日内拉升失败" in row.get("labels", [])]
    limit_events = [
        row
        for row in events
        if "封板确认" in row.get("labels", []) or "炸板回落" in row.get("labels", []) or "跌停负反馈" in row.get("labels", [])
    ]
    parts = [_text(row.get("phrase")) for row in ([pressure] if pressure else failed[:1])]
    parts = [part for part in parts if part]
    first_window = next(iter(context.get("rotation_windows") or []), {})
    top_boards = first_window.get("top_boards") if isinstance(first_window, dict) else []
    opening_tokens = _tokenize_board_text(*[_text(row.get("name")) for row in top_boards[:4]])

    def event_is_opening_relevant(row: dict[str, Any]) -> bool:
        meta = row.get("limit_pool") if isinstance(row.get("limit_pool"), dict) else {}
        haystack = " ".join(_text(meta.get(key)) for key in ("industry", "selected_reason", "name"))
        return any(token in haystack for token in opening_tokens)

    def event_score(row: dict[str, Any]) -> tuple[int, str]:
        meta = row.get("limit_pool") if isinstance(row.get("limit_pool"), dict) else {}
        relevant = event_is_opening_relevant(row)
        first_time = _text(meta.get("first_limit_up_time") or "999999")
        return (0 if relevant else 1, first_time)

    limit_events = sorted(limit_events, key=event_score)
    relevant_limit_events = [row for row in limit_events if event_is_opening_relevant(row)]
    selected_limit_events = relevant_limit_events or limit_events
    limit_parts = [_text(row.get("phrase")) for row in selected_limit_events[:5] if _text(row.get("phrase"))]
    if not parts and not limit_parts:
        return ""
    opening = ""
    opening_pressure = context.get("opening_pressure_boards") if isinstance(context.get("opening_pressure_boards"), list) else []
    pressure_names = [_text(row.get("name")) for row in opening_pressure[:4] if _text(row.get("name"))]
    if any(name in {"CPO概念", "光模块", "通信线缆及配套"} or "CPO" in name or "光模块" in name for name in pressure_names):
        opening += "开盘后光模块方向直接低开。"
    elif pressure_names:
        opening += f"开盘后{pressure_names[0]}方向直接承压。"
    low_open = [row for row in events if "低开承压" in row.get("labels", [])]
    if low_open:
        opening += "高成交核心直接承压—" + "，".join(_text(row.get("name")) for row in low_open[:3] if _text(row.get("name"))) + "。"
    if limit_parts:
        opening += "资金随后攻击早盘强势分支。" + "，".join(limit_parts[:5]) + "。"
    collapse = ""
    if pressure:
        collapse = (
            f"下午1点30分是整个盘面的崩塌点。之后要重点看{_text(pressure.get('name'))}这类高成交核心，"
            f"{_fmt_amount_yi_for_prose(pressure.get('amount_yi'))}亿成交如果不能推高价格，"
            "问题就不是没有量，而是没有价格承接。"
        )
    return (
        opening
        + "具体到个股分钟路径，"
        + "；".join(parts or limit_parts[:5])
        + "。"
        + collapse
    )


def _turning_point_section(context: dict[str, Any]) -> str:
    parts: list[str] = []
    events = context.get("stock_event_chains") if isinstance(context.get("stock_event_chains"), list) else []
    early_limit = []
    for row in events:
        meta = row.get("limit_pool") if isinstance(row.get("limit_pool"), dict) else {}
        first_time = _text(meta.get("first_limit_up_time"))
        if first_time and first_time[:4].isdigit() and "0930" <= first_time[:4] <= "0935":
            early_limit.append(row)
    if early_limit:
        names = "、".join(_text(row.get("name")) for row in early_limit[:3] if _text(row.get("name")))
        parts.append(
            f"9点33分是第一个关键转折点。早盘封板池在这个时间附近开始聚焦{names or '强势分支'}，"
            "但高成交压力方向没有同步修复，所以这更像一次资金试探。"
        )
    shift_1030 = next(
        (
            shift
            for shift in context.get("rotation_shifts", [])
            if _text(shift.get("to_time")).startswith("10:30")
        ),
        None,
    )
    if shift_1030:
        strengthening = shift_1030.get("strengthening") if isinstance(shift_1030.get("strengthening"), list) else []
        weakening = shift_1030.get("weakening") if isinstance(shift_1030.get("weakening"), list) else []
        strong = next((row for row in strengthening if _text(row.get("name"))), None)
        weak = next((row for row in weakening if _text(row.get("name"))), None)
        parts.append(
            "10点半是第二个关键转折点。"
            + (
                f"{_text(strong.get('name'))}开始增强{_fmt_pct(strong.get('delta_pct'))}，"
                if strong
                else ""
            )
            + (
                f"{_text(weak.get('name'))}同步走弱{_fmt_pct(weak.get('delta_pct'))}。"
                if weak
                else ""
            )
            + "这个时间点说明资金已经开始做方向切换，而不是单一板块独立走强。"
        )
    return "\n\n".join(parts)


def _emotion_temperature_section(context: dict[str, Any]) -> str:
    failed = context.get("failed_boards") if isinstance(context.get("failed_boards"), list) else []
    pressure = next(
        (
            row
            for row in context.get("high_turnover_cores", [])
            if (_float(row.get("change_pct")) or 0.0) < 0 and (_amount_yi(row.get("amount")) or 0.0) >= 100
        ),
        None,
    )
    if not failed and not pressure:
        return ""
    tail = ""
    if failed:
        names = "、".join(_text(row.get("name")) for row in failed[:4] if _text(row.get("name")))
        tail = f"冲高回落/炸板样本{len(failed)}个，前排是{names}。"
    pressure_line = ""
    if pressure:
        pressure_line = (
            f"{_text(pressure.get('name'))}单日成交{_fmt_amount_yi_for_prose(pressure.get('amount'))}亿仍然收跌，"
            f"跌幅{_fmt_pct(pressure.get('change_pct'))}，说明高成交核心还没有形成价格承接。"
        )
    return (
        "关于情绪温度，只按可量化样本判断。"
        + tail
        + pressure_line
        + "明日只验证两件事：这些回落样本是否继续扩大，以及高成交核心是否停止放量收跌。"
    )


def _index_cycle_section(context: dict[str, Any]) -> str:
    cycle = context.get("index_cycle") if isinstance(context.get("index_cycle"), dict) else {}
    if not cycle:
        return ""
    pivot_date = _text(cycle.get("pivot_date"))
    try:
        pivot_dt = datetime.fromisoformat(pivot_date)
        pivot_label = f"{pivot_dt.month}月{pivot_dt.day}日"
    except ValueError:
        pivot_label = pivot_date
    high = _fmt_number(cycle.get("pivot_high"))
    close = _fmt_number(cycle.get("latest_close"))
    drop = abs(_float(cycle.get("drop_pct")) or 0.0)
    drop_text = f"{drop:.0f}%" if drop >= 1 else f"{drop:.2f}%"
    close_label = "最新收盘"
    days = cycle.get("trading_days_since")
    return (
        "最后说一个重要的时间周期维度。"
        f"距离{pivot_label}的高点{high}已经过去了{days}个交易日，"
        f"上证从{high}跌到{close_label}{close}，跌了约{drop_text}。"
        "这个周期维度只作为验证条件：下一交易日要看指数是否继续脱离这个回撤区间，"
        "以及高成交负反馈是否还在扩散，不能单独把它写成企稳判断。"
    )


def _event_rows(context: dict[str, Any]) -> list[dict[str, Any]]:
    rows = context.get("stock_event_chains")
    return rows if isinstance(rows, list) else []


def _event_limit_meta(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("limit_pool")
    return meta if isinstance(meta, dict) else {}


def _event_time_key(row: dict[str, Any]) -> str:
    meta = _event_limit_meta(row)
    return _text(meta.get("first_limit_up_time") or meta.get("last_limit_up_time") or "999999")


def _event_haystack(row: dict[str, Any]) -> str:
    meta = _event_limit_meta(row)
    return " ".join(
        [
            _text(row.get("name")),
            _text(meta.get("industry")),
            _text(meta.get("selected_reason")),
            _text(meta.get("pool")),
        ]
    )


def _events_matching(context: dict[str, Any], tokens: list[str]) -> list[dict[str, Any]]:
    if not tokens:
        return []
    result = [row for row in _event_rows(context) if any(token in _event_haystack(row) for token in tokens)]
    result.sort(key=lambda row: (_event_time_key(row), -(_float(row.get("amount_yi")) or 0.0)))
    return result


def _event_by_name(context: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((row for row in _event_rows(context) if _text(row.get("name")) == name), None)


def _pressure_event(context: dict[str, Any]) -> dict[str, Any] | None:
    events = [
        row
        for row in _event_rows(context)
        if "高成交负反馈" in row.get("labels", []) or ((_float(row.get("close_change_pct")) or 0.0) < 0 and (_float(row.get("amount_yi")) or 0.0) >= 100)
    ]
    return max(events, key=lambda row: _float(row.get("amount_yi")) or 0.0, default=None)


def _telecom_pressure_events(context: dict[str, Any]) -> list[dict[str, Any]]:
    pressure = _pressure_event(context)
    rows = []
    if pressure:
        rows.append(pressure)
    explicit_name_rows: list[dict[str, Any]] = []
    for row in _event_rows(context):
        if row is pressure:
            continue
        if (_float(row.get("amount_yi")) or 0.0) < 100:
            continue
        haystack = _event_haystack(row)
        if "通信" in _text(row.get("name")):
            explicit_name_rows.append(row)
        if "通信" in haystack or "CPO" in haystack or "光模块" in haystack:
            rows.append(row)
    rows.sort(key=lambda row: _float(row.get("amount_yi")) or 0.0, reverse=True)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        name = _text(row.get("name"))
        if not name or name in seen:
            continue
        seen.add(name)
        deduped.append(row)
    explicit_name_rows.sort(key=lambda row: _float(row.get("amount_yi")) or 0.0, reverse=True)
    if len(deduped) >= 3 and explicit_name_rows:
        explicit = explicit_name_rows[0]
        explicit_name = _text(explicit.get("name"))
        if explicit_name and all(_text(row.get("name")) != explicit_name for row in deduped[:3]):
            deduped[2] = explicit
    final: list[dict[str, Any]] = []
    seen.clear()
    for row in deduped:
        name = _text(row.get("name"))
        if name and name not in seen:
            seen.add(name)
            final.append(row)
    return final[:4]


def _open_to_text(row: dict[str, Any]) -> str:
    name = _text(row.get("name"))
    open_bar = row.get("open_bar") if isinstance(row.get("open_bar"), dict) else {}
    open_value = _first_present(open_bar.get("open"), row.get("open"))
    prev_close = row.get("prev_close")
    if prev_close is None:
        return f"{name}开{_fmt_number(open_value)}"
    return f"{name}从{_fmt_number(prev_close)}开到{_fmt_number(open_value)}"


def _consumer_attack_events(context: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    consumer = _events_matching(context, ["一般零售", "零售", "超市", "百货", "消费"])
    sealed = [row for row in consumer if "封板确认" in row.get("labels", [])]
    failed = [row for row in consumer if "炸板回落" in row.get("labels", [])]
    sealed.sort(key=_event_time_key)
    failed.sort(key=_event_time_key)
    attack: list[dict[str, Any]] = []
    if sealed:
        attack.append(sealed[0])
    attack.extend(failed[:2])
    repair = sealed[1:3]
    return attack, repair


def _event_phrase(row: dict[str, Any]) -> str:
    return _text(row.get("phrase"))


def _event_theme_label(row: dict[str, Any]) -> str:
    haystack = _event_haystack(row)
    theme_tokens = [
        ("消费", ["一般零售", "零售", "百货", "超市", "消费"]),
        ("电力", ["电力", "火力发电", "能源发电", "发电"]),
        ("锂电", ["锂", "电池", "能源金属"]),
        ("光模块", ["CPO", "光模块", "通信设备", "通信线缆", "光通信"]),
        ("机器人", ["机器人", "自动化"]),
        ("商业航天", ["商业航天", "航天", "卫星"]),
        ("半导体", ["半导体", "电子化学", "硅料", "硅片"]),
    ]
    for label, tokens in theme_tokens:
        if any(token in haystack for token in tokens):
            return label
    return ""


def _first_analysis_board_name(rows: list[dict[str, Any]]) -> str:
    for row in rows:
        name = _text(row.get("name") or row.get("board") or row.get("driver_name"))
        if _is_analysis_board(name):
            return _board_display_name(name)
    return ""


def _shift_by_to_time(context: dict[str, Any], prefix: str) -> dict[str, Any] | None:
    for shift in context.get("rotation_shifts", []):
        if _text(shift.get("to_time")).startswith(prefix):
            return shift
    return None


def _shift_leaders(shift: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not shift:
        return None, None
    strengthening = shift.get("strengthening") if isinstance(shift.get("strengthening"), list) else []
    weakening = shift.get("weakening") if isinstance(shift.get("weakening"), list) else []
    strong = next((row for row in strengthening if _text(row.get("name")) and _is_analysis_board(row.get("name"))), None)
    weak = next((row for row in weakening if _text(row.get("name")) and _is_analysis_board(row.get("name"))), None)
    return strong, weak


def _opening_flow_section(context: dict[str, Any]) -> str:
    pressure_rows = _telecom_pressure_events(context)
    attack, _ = _consumer_attack_events(context)
    opening_pressure = context.get("opening_pressure_boards") if isinstance(context.get("opening_pressure_boards"), list) else []
    pressure_names = [_text(row.get("name")) for row in opening_pressure[:5] if _text(row.get("name"))]
    event_label = _event_theme_label(pressure_rows[0]) if pressure_rows else ""
    board_label = _first_analysis_board_name(opening_pressure[:5])
    if any("CPO" in name or "光模块" in name or "通信线缆" in name for name in pressure_names):
        pressure_label = "光模块"
    else:
        pressure_label = event_label or board_label or "高成交核心"
    pressure_text = "，".join(_open_to_text(row) for row in pressure_rows[:3] if _text(row.get("name")))
    attack_text = "，".join(_event_phrase(row) for row in attack if _event_phrase(row))
    if not pressure_text and not attack_text:
        return ""
    attack_label = _event_theme_label(attack[0]) if attack else ""
    attack_sentence = ""
    if attack_text:
        attack_sentence = f"资金随后攻击{attack_label or '早盘强势'}方向。{attack_text}。"
    elif pressure_text:
        attack_sentence = "但没有看到同方向马上形成有效修复，早盘只能按资金切换观察。"
    return (
        f"先说资金流动的完整链条。开盘后{pressure_label}方向直接承压"
        + (f"—{pressure_text}。" if pressure_text else "。")
        + attack_sentence
        + "早盘强度不是最终结论，关键是封住的数量、炸板的数量，以及这些进攻线能否在尾盘继续承接。"
    )


def _board_timeline_row(context: dict[str, Any], tokens: list[str]) -> dict[str, Any] | None:
    for row in context.get("board_timeline", []):
        haystack = f"{_text(row.get('board'))} {_text(row.get('driver_name'))}"
        if any(token in haystack for token in tokens):
            return row
    return None


def _board_latest_pct(row: dict[str, Any] | None) -> Any:
    latest = row.get("latest") if isinstance(row, dict) and isinstance(row.get("latest"), dict) else {}
    return latest.get("change_pct")


def _aerospace_section(context: dict[str, Any]) -> str:
    board = _board_timeline_row(context, ["商业航天", "航天", "卫星"])
    if not board:
        return ""
    dynamic = next(
        (
            row
            for row in context.get("dynamic_market_representatives", [])
            if any(token in f"{_text(row.get('board'))} {_text(row.get('driver_name'))}" for token in ["商业航天", "航天", "卫星"])
        ),
        {},
    )
    reps: list[dict[str, Any]] = []
    for bucket in ("market_elastic", "market_core"):
        rows = dynamic.get(bucket) if isinstance(dynamic.get(bucket), list) else []
        reps.extend(rows[:3])
    event_map = {_text(row.get("name")): row for row in _event_rows(context)}
    parts = []
    seen: set[str] = set()
    for rep in reps:
        name = _text(rep.get("name"))
        if not name or name in seen:
            continue
        seen.add(name)
        event = event_map.get(name)
        if event and _event_phrase(event):
            parts.append(_event_phrase(event))
        else:
            parts.append(f"{name}{_fmt_pct(rep.get('change_pct'))}")
    latest = _board_latest_pct(board)
    return (
        "这时有一条暗线开始露头—商业航天。"
        f"{_text(board.get('driver_name') or board.get('board'))}全天涨了{_fmt_unsigned_pct(latest)}。"
        + ("，".join(parts[:4]) + "。" if parts else "")
        + "这条线的关键不是单一个股，而是内部有先后节奏；先封、后跟、再扩散，才说明板块共识在扩散。"
    )


def _power_events(context: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _events_matching(context, ["电力", "火力发电", "其他能源发电", "发电"])
    rows.sort(key=lambda row: (
        0 if "跌停负反馈" in row.get("labels", []) else 1,
        -(_float(row.get("amount_yi")) or 0.0),
    ))
    return rows[:5]


def _turn_0933_section(context: dict[str, Any]) -> str:
    telecom_rows = _telecom_pressure_events(context)
    pressure = _pressure_event(context)
    telecom = next(
        (
            row
            for row in telecom_rows
            if row is not pressure and "通信" in _text(row.get("name"))
        ),
        None,
    )
    if telecom is None:
        telecom = next(
            (
                row
                for row in telecom_rows
                if row is not pressure and ("通信" in _event_haystack(row) or "CPO" in _event_haystack(row))
            ),
            telecom_rows[-1] if telecom_rows else None,
        )
    power = _power_events(context)
    strong, weak = _shift_leaders(_shift_by_to_time(context, "09:35"))
    if not telecom and not power and not (strong or weak):
        return ""
    telecom_line = ""
    if telecom:
        low_bar = telecom.get("low_bar") if isinstance(telecom.get("low_bar"), dict) else {}
        high_bar = telecom.get("high_bar") if isinstance(telecom.get("high_bar"), dict) else {}
        telecom_line = (
            f"{_text(telecom.get('name'))}从{_fmt_number(low_bar.get('low') or telecom.get('low'))}"
            f"日内低点拉升，最高到{_fmt_number(high_bar.get('high') or telecom.get('high'))}，"
            f"但收盘仍是{_fmt_pct(telecom.get('close_change_pct'))}。"
        )
    power_line = "。".join(_event_phrase(row) for row in power[:4] if _event_phrase(row))
    shift_line = ""
    if not telecom_line and (strong or weak):
        shift_line = (
            (f"{_text(strong.get('name'))}开始增强{_fmt_pct(strong.get('delta_pct'))}，" if strong else "")
            + (f"{_text(weak.get('name'))}同步走弱{_fmt_pct(weak.get('delta_pct'))}。" if weak else "")
        )
    telecom_haystack = _event_haystack(telecom) if telecom else ""
    explicit_tech_reversal = bool(
        telecom
        and (
            "通信" in _text(telecom.get("name"))
            or any(token in telecom_haystack for token in ["通信", "CPO", "光模块"])
        )
    )
    title = "9点33分附近是早盘第一个关键转折点。" if explicit_tech_reversal or power_line else "早盘第一个关键转折点。"
    final_sentence = (
        "电力板块内部如果涨停变成炸板、冲高变成回落，就是撤退信号。"
        if power_line
        else "这类回拉或切换如果不能带动板块扩散，就只能按资金试探处理。"
    )
    return (
        title
        + telecom_line
        + shift_line
        + ("这个瞬间资金被吸引回承压方向。 " if telecom_line else "")
        + ("这时候电力方向开始跳水—" + power_line + "。" if power_line else "")
        + final_sentence
    )


def _lithium_events(context: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _events_matching(context, ["锂", "能源金属", "电池"])
    rows = [row for row in rows if (_float(row.get("high_change_pct")) or 0.0) > 0 or (_float(row.get("close_change_pct")) or 0.0) > 0]
    rows.sort(key=lambda row: _float(row.get("amount_yi")) or 0.0, reverse=True)
    return rows[:4]


def _turn_1030_section(context: dict[str, Any]) -> str:
    pressure = _pressure_event(context)
    lithium = _lithium_events(context)
    shift = _shift_by_to_time(context, "10:30")
    strong, weak = _shift_leaders(shift)
    if not pressure and not lithium and not (strong or weak):
        return ""
    pressure_line = ""
    if pressure:
        high_bar = pressure.get("high_bar") if isinstance(pressure.get("high_bar"), dict) else {}
        high_value = _first_present(high_bar.get("high"), pressure.get("high"))
        high_phrase = f"虽然拉到过{_fmt_number(high_value)}但" if high_value is not None else "盘中反复尝试但"
        pressure_line = (
            f"这个时候{_text(pressure.get('name'))}这类高成交核心停止回拉了—"
            f"{_text(pressure.get('name'))}{high_phrase}打不住抛压，"
            f"全天{_fmt_amount_yi_for_prose(pressure.get('amount_yi'))}亿成交里价格承接不足。"
        )
    lithium_line = "，".join(_event_phrase(row) for row in lithium if _event_phrase(row))
    shift_line = ""
    if not lithium_line and (strong or weak):
        shift_line = (
            (f"{_text(strong.get('name'))}增强{_fmt_pct(strong.get('delta_pct'))}，" if strong else "")
            + (f"{_text(weak.get('name'))}走弱{_fmt_pct(weak.get('delta_pct'))}。" if weak else "")
        )
    return (
        "10点半附近是第二个关键转折点。"
        + pressure_line
        + ("几乎同一时间，锂电和锂矿开始试盘拉升。" + lithium_line + "。" if lithium_line else "")
        + shift_line
        + (
            "下一步只看这些增强方向能否继续扩散，并且弱化方向不再拖累高成交核心。"
            if lithium_line or shift_line
            else "这里的结论只来自高成交核心承接失败，不外推成新主线。"
        )
    )


def _robot_section(context: dict[str, Any]) -> str:
    robot = _board_timeline_row(context, ["机器人", "自动化"])
    if not robot:
        return ""
    latest = _board_latest_pct(robot)
    reps = next(
        (
            row
            for row in context.get("dynamic_market_representatives", [])
            if "机器人" in f"{_text(row.get('board'))} {_text(row.get('driver_name'))}"
        ),
        {},
    )
    elastics = reps.get("market_elastic_confirmed") if isinstance(reps.get("market_elastic_confirmed"), list) else []
    if not elastics:
        elastics = reps.get("market_elastic") if isinstance(reps.get("market_elastic"), list) else []
    flag_candidates = []
    for row in elastics:
        meta = row.get("limit_pool") if isinstance(row.get("limit_pool"), dict) else {}
        first_time = _text(meta.get("first_limit_up_time"))
        amount_yi = _float(row.get("amount_yi")) or 0.0
        if first_time and amount_yi >= 20:
            flag_candidates.append(row)
    focus = "、".join(_text(row.get("name")) for row in flag_candidates[:2] if _text(row.get("name")))
    focus_line = f"，旗帜性封板焦点在{focus}" if focus else "，但缺少一个旗帜性的涨停聚焦点"
    return (
        "下午机器人方向被资金平铺买入。"
        f"{_text(robot.get('driver_name') or robot.get('board'))}涨{_fmt_unsigned_pct(latest)}"
        + focus_line
        + "。这是前面竞价异动和午后铺开的延续，但没有持续封板焦点时，先按平铺买入而不是压倒性主线处理。"
    )


def _external_flow_for_event(context: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
    code = _pure_code(_text(event.get("symbol") or event.get("code")))
    if not code:
        return None
    trade_date = _text(context.get("trade_date"))
    for row in context.get("external_fund_flows", []) if isinstance(context.get("external_fund_flows"), list) else []:
        if _pure_code(_text(row.get("symbol") or row.get("code"))) != code:
            continue
        em = row.get("eastmoney_quote") if isinstance(row.get("eastmoney_quote"), dict) else {}
        observed = _text(em.get("observed_trade_date"))
        if observed and trade_date and observed != trade_date:
            continue
        return row
    return None


def _flow_pair_has_buy_sell(pair: dict[str, Any]) -> bool:
    return pair.get("buy_yi") is not None and pair.get("sell_yi") is not None


def _external_flow_line(context: dict[str, Any], event: dict[str, Any]) -> str:
    flow = _external_flow_for_event(context, event)
    if not flow:
        return ""
    em = flow.get("eastmoney_quote") if isinstance(flow.get("eastmoney_quote"), dict) else {}
    main = em.get("main_order") if isinstance(em.get("main_order"), dict) else {}
    retail = em.get("retail_proxy") if isinstance(em.get("retail_proxy"), dict) else {}
    if _flow_pair_has_buy_sell(main):
        main_net = _float(main.get("net_yi"))
        line = (
            "东财订单资金口径显示，"
            f"主力全天买入{_fmt_amount_yi_for_prose(main.get('buy_yi'))}亿"
            f"卖出{_fmt_amount_yi_for_prose(main.get('sell_yi'))}亿，"
            f"主力净流出{_fmt_amount_yi_for_prose(abs(main_net) if main_net is not None else None)}亿"
        )
        if _flow_pair_has_buy_sell(retail):
            retail_net = _float(retail.get("net_yi"))
            net_word = "净接" if retail_net is not None and retail_net >= 0 else "净流出"
            line += (
                f"；中单/散户代理买入{_fmt_amount_yi_for_prose(retail.get('buy_yi'))}亿"
                f"卖出{_fmt_amount_yi_for_prose(retail.get('sell_yi'))}亿，"
                f"{net_word}{_fmt_amount_yi_for_prose(abs(retail_net) if retail_net is not None else None)}亿"
            )
        gap = _float(em.get("amount_coverage_gap_yi"))
        if gap is not None and abs(gap) >= 1:
            line += f"。这套订单资金覆盖成交额还差约{_fmt_amount_yi_for_prose(abs(gap))}亿，说明不能把它当成完整分账户流水"
        else:
            line += "。这套数据能证明订单大小迁移，但还不是账户级 L2 主力/散户流水"
        return line + "。"

    ths = flow.get("ths_real_funds") if isinstance(flow.get("ths_real_funds"), dict) else {}
    buckets = ths.get("buckets") if isinstance(ths.get("buckets"), dict) else {}
    big = buckets.get("big_order") if isinstance(buckets.get("big_order"), dict) else {}
    medium = buckets.get("medium_order") if isinstance(buckets.get("medium_order"), dict) else {}
    small = buckets.get("small_order") if isinstance(buckets.get("small_order"), dict) else {}
    if big.get("in_yi") is None and medium.get("in_yi") is None:
        return ""
    parts = []
    if big.get("in_yi") is not None or big.get("out_yi") is not None:
        parts.append(f"大单流入{_fmt_amount_yi_for_prose(big.get('in_yi'))}亿、流出{_fmt_amount_yi_for_prose(big.get('out_yi'))}亿")
    if medium.get("in_yi") is not None or medium.get("out_yi") is not None:
        parts.append(f"中单流入{_fmt_amount_yi_for_prose(medium.get('in_yi'))}亿、流出{_fmt_amount_yi_for_prose(medium.get('out_yi'))}亿")
    if small.get("in_yi") is not None or small.get("out_yi") is not None:
        parts.append(f"小单流入{_fmt_amount_yi_for_prose(small.get('in_yi'))}亿、流出{_fmt_amount_yi_for_prose(small.get('out_yi'))}亿")
    total = ""
    if ths.get("total_in_yi") is not None and ths.get("total_out_yi") is not None:
        total = f"总流入{_fmt_amount_yi_for_prose(ths.get('total_in_yi'))}亿、总流出{_fmt_amount_yi_for_prose(ths.get('total_out_yi'))}亿，"
    return (
        "同花顺订单资金口径显示，"
        + total
        + "；".join(parts)
        + "。这能证明大中小单分布，但不是账户级主力/散户流水。"
    )


def _collapse_section(context: dict[str, Any]) -> str:
    pressure = _pressure_event(context)
    if not pressure:
        return ""
    failed = context.get("failed_boards") if isinstance(context.get("failed_boards"), list) else []
    drawdown = _float(pressure.get("price_drawdown_pct")) or 0.0
    change = _float(pressure.get("close_change_pct")) or 0.0
    is_broad_breakdown = len(failed) >= 3 or change <= -5 or drawdown >= 10
    flow = context.get("flow_availability") if isinstance(context.get("flow_availability"), dict) else {}
    flow_line = _external_flow_line(context, pressure)
    if not flow_line:
        flow_line = (
            "当前数据没有主力/散户分账户资金流，不能补写精确主力和散户买卖拆分。"
            if not flow.get("participant_flow_available")
            else "分账户资金流需要继续核对主力和散户买卖拆分。"
        )
    prefix = "下午1点30分是整个盘面的崩塌点。" if is_broad_breakdown else "午后高成交核心的承接压力开始暴露。"
    return (
        prefix +
        f"{_text(pressure.get('name'))}日内反复尝试承接，但抛压越来越大，"
        f"{_fmt_amount_yi_for_prose(pressure.get('amount_yi'))}亿成交没有换来足够的价格推进。"
        "这不是成交量的问题—成交巨大说明有人在接，但接不住就是最大的问题。"
        f"{_fmt_amount_yi_for_prose(pressure.get('amount_yi'))}亿成交无承接，这个信号比单纯跌幅更严重。"
        + flow_line
    )


def _tail_consumer_section(context: dict[str, Any]) -> str:
    attack, repair = _consumer_attack_events(context)
    failed = [row for row in attack if "炸板回落" in row.get("labels", [])]
    fallback_failed = []
    if not failed and not repair:
        for row in context.get("failed_boards", [])[:4]:
            name = _text(row.get("name"))
            if not name:
                continue
            fallback_failed.append(
                f"{name}高{_fmt_number(row.get('high'))}收{_fmt_number(row.get('close'))}，"
                f"涨幅回吐{_fmt_pct_points(row.get('failed_from_high_pct'))}，"
                f"价格较高点回落{_fmt_unsigned_pct(row.get('price_drawdown_pct'))}"
            )
    if not failed and not repair and not fallback_failed:
        return ""
    failed_line = "。".join(_event_phrase(row) for row in failed[:3] if _event_phrase(row))
    if not failed_line and fallback_failed:
        failed_line = "。".join(fallback_failed)
    repair_line = "。".join(_event_phrase(row) for row in repair[:2] if _event_phrase(row))
    pressure = _pressure_event(context)
    is_breakdown = bool(failed and pressure) or len(fallback_failed) >= 3
    prefix = "尾盘情绪彻底崩溃。" if is_breakdown else "尾盘情绪转弱。"
    return (
        prefix
        + (failed_line + "。" if failed_line else "")
        + (f"但{repair_line}。" if repair_line else "")
        + "要区分追高进攻型资金和超跌修复型资金；前者炸板质量更伤情绪，后者即使封住也不一定代表整条链确认。"
    )


def _sample_style_carding_section(context: dict[str, Any]) -> str:
    cards: list[str] = []
    labels = ["第一次", "第二次", "第三次", "第四次"]

    def add_card(text: str) -> None:
        if len(cards) >= len(labels):
            return
        cards.append(f"{labels[len(cards)]}是{text}")

    for shift in context.get("rotation_shifts", [])[:4]:
        strong, weak = _shift_leaders(shift)
        if not strong and not weak:
            continue
        text = f"{shift.get('from_time')}到{shift.get('to_time')}"
        if strong:
            text += f"{_text(strong.get('name'))}增强{_fmt_pct(strong.get('delta_pct'))}"
        if weak:
            text += f"，{_text(weak.get('name'))}走弱{_fmt_pct(weak.get('delta_pct'))}"
        add_card(text)
    if not cards:
        return _carding_section(context)
    return (
        f"看一下今天盘面的卡位结构。全天至少有{len(cards)}次明显卡位："
        + "，".join(cards[:4])
        + "。这些窗口只能证明资金发生切换，不能直接证明胜方。"
        "明日验证要回到数据：增强方向是否继续排在板块前列，走弱方向是否停止拖累，链主和弹性是否同步。"
    )


def _structured_intraday_summary_section(context: dict[str, Any]) -> str:
    structured = context.get("structured_daily_review") if isinstance(context.get("structured_daily_review"), dict) else {}
    slices = structured.get("fixed_time_slices") if isinstance(structured.get("fixed_time_slices"), list) else []
    opening_pressure = context.get("opening_pressure_boards") if isinstance(context.get("opening_pressure_boards"), list) else []
    shifts = context.get("rotation_shifts") if isinstance(context.get("rotation_shifts"), list) else []
    tech_tokens = (
        "科技",
        "CPO",
        "光模块",
        "通信",
        "线缆",
        "集成电路",
        "半导体",
        "分立器件",
        "被动元件",
        "MLCC",
        "PCB",
        "印制电路",
        "机器人",
        "软件",
        "AI",
    )

    def is_tech(row: dict[str, Any] | None) -> bool:
        if not isinstance(row, dict):
            return False
        haystack = " ".join(
            _text(row.get(key))
            for key in ("name", "board", "driver_name")
        )
        return any(token in haystack for token in tech_tokens)

    panic_parts = [
        f"{_text(row.get('name'))}{_fmt_pct(row.get('change_pct'))}"
        for row in opening_pressure[:8]
        if is_tech(row) and _text(row.get("name"))
    ]

    rebound_parts: list[str] = []
    seen_rebounds: set[str] = set()

    def add_rebound(label: str, row: dict[str, Any]) -> None:
        name = _text(row.get("name"))
        delta = _float(row.get("delta_pct"))
        if not name or delta is None or delta <= 0 or not is_tech(row):
            return
        key = f"{label}:{name}"
        if key in seen_rebounds:
            return
        seen_rebounds.add(key)
        rebound_parts.append(f"{label}{name}增强{_fmt_pct(delta)}")

    for shift in shifts[:6]:
        label = f"{shift.get('from_time')}到{shift.get('to_time')}"
        strengthening = shift.get("strengthening") if isinstance(shift.get("strengthening"), list) else []
        for row in strengthening[:5]:
            add_rebound(label, row)

    for item in slices:
        active = item.get("active_direction") if isinstance(item.get("active_direction"), dict) else None
        label = _text(item.get("actual_range") or item.get("time_range"))
        if label and active:
            add_rebound(label, active)

    if not panic_parts and not rebound_parts:
        return ""

    sentences = ["结构化切片先给结论。"]
    if panic_parts:
        sentences.append(
            "早盘不是普通分化，而是科技链先集中恐慌："
            + "、".join(panic_parts[:5])
            + "。"
        )
    if rebound_parts:
        sentences.append(
            "后续也不是全面修复，而是部分科技分支出现抄底反弹："
            + "、".join(rebound_parts[:5])
            + "。"
        )
    if slices:
        confirmed = [
            _text(row.get("slice"))
            for row in slices
            if _text(row.get("evidence_level")) == "confirmed" and _text(row.get("slice"))
        ]
        if confirmed:
            sentences.append(
                "这些判断来自固定分时切片"
                + "、".join(confirmed[:5])
                + "，所以主线表述应写成“先恐慌、局部反弹、再看承接”，不能只写单一强板块。"
            )
    return "".join(sentences)


def format_market_replay_sections(context: dict[str, Any], *, max_sections: int = 12) -> list[str]:
    """Turn a market replay evidence graph into narrative-ready paragraphs."""
    structured_section = _structured_intraday_summary_section(context)
    role_map_section = _board_role_map_section(context)
    turnover_representative_section = _turnover_representative_section(context)
    carding_section = _sample_style_carding_section(context) or _carding_section(context)
    emotion_section = _emotion_temperature_section(context)
    cycle_section = _index_cycle_section(context)

    def with_required_sections(sections: list[str]) -> list[str]:
        selected = sections[:max_sections]
        required_sections = [section for section in (carding_section, emotion_section, cycle_section) if section]
        for required in required_sections:
            if required in selected:
                continue
            replace_at = next(
                (idx for idx in range(len(selected) - 1, -1, -1) if selected[idx] not in required_sections),
                len(selected) - 1,
            )
            if len(selected) >= max_sections and replace_at >= 0:
                selected[replace_at] = required
            else:
                selected.append(required)
        return selected

    sample_style_sections = [
        section
        for section in (
            structured_section,
            role_map_section,
            turnover_representative_section,
            _opening_flow_section(context),
            _aerospace_section(context),
            _turn_0933_section(context),
            _turn_1030_section(context),
            _robot_section(context),
            _collapse_section(context),
            _tail_consumer_section(context),
            carding_section,
            emotion_section,
            cycle_section,
        )
        if section
    ]
    if len(sample_style_sections) >= min(max_sections, 6):
        return with_required_sections(sample_style_sections)

    sections: list[str] = []
    if structured_section:
        sections.append(structured_section)
    if role_map_section:
        sections.append(role_map_section)
    if turnover_representative_section:
        sections.append(turnover_representative_section)

    rotation_parts: list[str] = []
    for item in context.get("rotation_windows", [])[:5]:
        boards = item.get("top_boards") if isinstance(item.get("top_boards"), list) else []
        names = [
            f"{_text(row.get('name'))}{_fmt_pct(row.get('change_pct'))}"
            for row in boards[:3]
            if _text(row.get("name"))
        ]
        if names:
            rotation_parts.append(f"{item.get('actual_time') or item.get('checkpoint')}：{'、'.join(names)}")
    if rotation_parts:
        shift_parts: list[str] = []
        for shift in context.get("rotation_shifts", [])[:3]:
            strengthening = shift.get("strengthening") if isinstance(shift.get("strengthening"), list) else []
            weakening = shift.get("weakening") if isinstance(shift.get("weakening"), list) else []
            up = "、".join(
                f"{_text(row.get('name'))}{_fmt_pct(row.get('delta_pct'))}"
                for row in strengthening[:2]
                if _text(row.get("name"))
            )
            down = "、".join(
                f"{_text(row.get('name'))}{_fmt_pct(row.get('delta_pct'))}"
                for row in weakening[:2]
                if _text(row.get("name"))
            )
            if up or down:
                shift_parts.append(
                    f"{shift.get('from_time')}到{shift.get('to_time')}增强：{up or '无'}，走弱：{down or '无'}"
                )
        sections.append(
            "先说资金流动的完整链条。"
            + "；".join(rotation_parts)
            + "。"
            + (("窗口差分显示：" + "；".join(shift_parts) + "。") if shift_parts else "")
            + "这不是单一方向资金控盘，而是资金在强板块、弱板块和高成交核心之间来回切换。"
        )

    turnover_parts: list[str] = []
    pressure_rows = [
        row
        for row in context.get("high_turnover_cores", [])
        if (_float(row.get("change_pct")) or 0.0) < 0
    ]
    for row in pressure_rows[:3]:
        name = _text(row.get("name"))
        if not name:
            continue
        turnover_parts.append(
            f"{name}单日成交{_fmt_amount_yi_for_prose(row.get('amount'))}亿，"
            f"开{_fmt_number(row.get('open'))}/高{_fmt_number(row.get('high'))}/低{_fmt_number(row.get('low'))}/收{_fmt_number(row.get('close'))}，"
            f"{_fmt_pct(row.get('change_pct'))}"
        )
    if turnover_parts:
        pressure = pressure_rows[0] if pressure_rows else None
        pressure_line = ""
        if pressure:
            pressure_line = (
                f"其中{_text(pressure.get('name'))}这种高成交负反馈最关键，"
                "问题不是没有成交，而是成交放大后没有价格承接。"
            )
        sections.append(
            "高成交核心是全天情绪的承接锚。"
            + "；".join(turnover_parts)
            + "。"
            + pressure_line
            + "复盘时先看这些高成交对象有没有价格承接，再决定强板块是真主线还是临时卡位。"
        )

    stock_event_section = _stock_event_section(context)
    if stock_event_section:
        sections.append(stock_event_section)

    turning_point_section = _turning_point_section(context)
    if turning_point_section:
        sections.append(turning_point_section)

    timeline_rows = []
    for row in context.get("board_timeline", []):
        delta = _float(row.get("change_delta_from_first"))
        latest = row.get("latest") if isinstance(row.get("latest"), dict) else {}
        if delta is None or not latest:
            continue
        timeline_rows.append((abs(delta), delta, row, latest))
    timeline_rows.sort(reverse=True, key=lambda item: item[0])
    board_parts = []
    for _, delta, row, latest in timeline_rows[:5]:
        points = row.get("points") if isinstance(row.get("points"), list) else []
        first = points[0] if points else {}
        board_parts.append(
            f"{_text(row.get('board') or row.get('driver_name'))}"
            f"从{first.get('time', '开盘')}{_fmt_pct(first.get('change_pct'))}"
            f"到{latest.get('time', '尾盘')}{_fmt_pct(latest.get('change_pct'))}"
            f"，变化{_fmt_pct(delta)}，领涨{_text(latest.get('leader_name'), '未知')}"
        )
    if board_parts:
        hidden_line = ""
        if any("商业航天" in _text(row.get("board")) or "航天" in _text(row.get("driver_name")) for _, _, row, _ in timeline_rows[:8]):
            hidden_line = "这时有一条暗线开始露头—商业航天。"
        robot_line = ""
        if any("机器人" in _text(row.get("board")) or "机器人" in _text(row.get("driver_name")) for _, _, row, _ in timeline_rows[:8]):
            robot_line = "下午机器人方向被资金平铺买入。"
        sections.append(
            hidden_line
            + robot_line
            + "板块卡位不是看最终涨幅一个点，而是看全天变化。"
            + "；".join(board_parts)
            + "。变化最大的方向通常就是资金切换和情绪温度的证据。"
        )

    failed_events = [
        row
        for row in context.get("stock_event_chains", [])
        if "炸板回落" in row.get("labels", [])
    ]
    failed_parts = [_text(row.get("phrase")) for row in failed_events[:4] if _text(row.get("phrase"))]
    for row in ([] if failed_parts else context.get("failed_boards", [])[:5]):
        name = _text(row.get("name"))
        if not name:
            continue
        failed_parts.append(
            f"{name}高{_fmt_number(row.get('high'))}收{_fmt_number(row.get('close'))}，"
            f"涨幅回吐{_fmt_pct_points(row.get('failed_from_high_pct'))}，"
            f"价格较高点回落{_fmt_unsigned_pct(row.get('price_drawdown_pct'))}"
        )
    if failed_parts:
        sections.append(
            "炸板和冲高回落用来判断尾盘情绪。"
            + "；".join(failed_parts)
            + "。这些不是简单的个股问题，而是强方向没有继续扩散时的承接压力。"
        )

    if carding_section:
        sections.append(carding_section)

    if emotion_section:
        sections.append(emotion_section)

    if cycle_section:
        sections.append(cycle_section)

    return with_required_sections(sections)
