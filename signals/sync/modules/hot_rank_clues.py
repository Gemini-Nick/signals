# -*- coding: utf-8 -*-
"""Build hot-rank startup/climb clues from Eastmoney, THS, and Wind exports."""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from pymongo import UpdateOne
from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.sync.task_context import get_task_env

logger = logging.getLogger("signals.sync.hot_rank_clues")

DAILY_FREQS = ("日线", "daily", "D", "1d")
WEEKLY_FREQS = ("周线", "weekly", "W", "1w")
SOURCE_WEIGHTS = {
    "eastmoney": 18.0,
    "ths": 18.0,
    "wind": 28.0,
}
SOURCE_LABELS = {
    "eastmoney": "东财",
    "ths": "同花顺",
    "wind": "Wind",
}
THS_HOT_RANK_URL = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
EASTMONEY_HOT_RANK_URL = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
EASTMONEY_QUOTE_URLS = (
    "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
    "http://push2.eastmoney.com/api/qt/ulist.np/get",
)
EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://guba.eastmoney.com/rank/",
}
THS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://eq.10jqka.com.cn/webpage/ths-hot-list/index.html?showStatusBar=true",
    "source-id": "ths-hot-list",
    "app-key": "ce19ea099b",
}


def _env_text(name: str, default: str = "") -> str:
    return str(get_task_env(name, os.getenv(name, default)) or default).strip()


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(_env_text(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _env_float(name: str, default: float, *, minimum: float = 0.0, maximum: float | None = None) -> float:
    try:
        value = float(_env_text(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _text(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        text = str(value).strip().replace(",", "").replace("%", "")
        if text in {"", "-", "--", "None", "nan"}:
            return default
        return float(text)
    except Exception:
        return default


def _pure_a_code(value: Any) -> str:
    raw = _text(value).upper()
    if not raw:
        return ""
    raw = (
        raw.replace(".SH", "")
        .replace(".SZ", "")
        .replace(".BJ", "")
        .replace("SH.", "")
        .replace("SZ.", "")
        .replace("BJ.", "")
        .replace("SH", "")
        .replace("SZ", "")
        .replace("BJ", "")
    )
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) != 6:
        return ""
    if digits.startswith(("900", "200")):
        return ""
    return digits


def _prefixed_symbol(code: Any) -> str:
    pure = _pure_a_code(code)
    if not pure:
        return ""
    if pure.startswith(("6", "9")):
        return f"SH.{pure}"
    if pure.startswith(("4", "8")):
        return f"BJ.{pure}"
    return f"SZ.{pure}"


def _symbol_query_values(code: str) -> list[str]:
    pure = _pure_a_code(code)
    if not pure:
        return []
    prefixed = _prefixed_symbol(pure)
    market = prefixed.split(".", 1)[0] if "." in prefixed else ""
    values = [pure, prefixed]
    if market:
        values.extend([f"{market.lower()}{pure}", f"{pure}.{market}"])
    return list(dict.fromkeys(values))


def _row_get(row: dict[str, Any], aliases: tuple[str, ...], default: Any = "") -> Any:
    if not isinstance(row, dict):
        return default
    normalized = {str(key).strip().lower(): value for key, value in row.items()}
    for key in aliases:
        if key in row and row[key] not in (None, ""):
            return row[key]
        value = normalized.get(key.lower())
        if value not in (None, ""):
            return value
    return default


def _normalize_hot_rank_record(row: dict[str, Any], *, source: str, position: int) -> dict[str, Any] | None:
    code = _pure_a_code(_row_get(row, ("代码", "股票代码", "证券代码", "code", "symbol", "wind_code")))
    if not code:
        return None
    rank = int(_safe_float(_row_get(row, ("当前排名", "排名", "rank", "order", "hot_rank"), position), float(position)) or position)
    name = _text(_row_get(row, ("股票名称", "名称", "证券简称", "name", "sec_name")))
    return {
        "code": code,
        "symbol": _prefixed_symbol(code),
        "name": name,
        "source": source,
        "rank": rank,
        "hot_score": _safe_float(_row_get(row, ("热度", "人气", "rate", "score", "hot_score"), 0.0)),
        "pct_chg": _safe_float(_row_get(row, ("涨跌幅", "rise_and_fall", "pct_chg"), 0.0)),
        "topic": _text(_row_get(row, ("topic", "tag", "题材", "概念"))),
    }


def _normalize_records(records: list[dict[str, Any]], *, source: str, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(records[:limit], start=1):
        normalized = _normalize_hot_rank_record(row, source=source, position=idx)
        if normalized:
            out.append(normalized)
    return out


def _eastmoney_rank_code(value: Any) -> str:
    text = _text(value).upper()
    if text.startswith(("SZ", "SH", "BJ")):
        return text[2:]
    return _pure_a_code(text)


def _eastmoney_secid(value: Any) -> str:
    text = _text(value).upper()
    code = _eastmoney_rank_code(text)
    if len(code) != 6:
        return ""
    if text.startswith("SH") or code.startswith("6"):
        return f"1.{code}"
    return f"0.{code}"


def _fetch_eastmoney_quote_rows(session: requests.Session, secids: list[str]) -> dict[str, dict[str, Any]]:
    if not secids:
        return {}
    params = {
        "ut": "f057cbcbce2a86e2866ab8877db1d059",
        "fltt": "2",
        "invt": "2",
        "fields": "f14,f3,f12,f2",
        "secids": ",".join(secids),
    }
    last_error: Exception | None = None
    for url in EASTMONEY_QUOTE_URLS:
        try:
            response = session.get(url, params=params, headers=EASTMONEY_HEADERS, timeout=10)
            response.raise_for_status()
            payload = response.json()
            rows = ((payload.get("data") or {}).get("diff") or []) if isinstance(payload, dict) else []
            return {
                _pure_a_code(row.get("f12")): row
                for row in rows
                if isinstance(row, dict) and _pure_a_code(row.get("f12"))
            }
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        logger.warning("eastmoney hot rank quote lookup failed: %s", last_error)
    return {}


def _fetch_eastmoney_hot_rank(limit: int) -> list[dict[str, Any]]:
    payload = {
        "appId": "appId01",
        "globalId": "786e4c21-70dc-435a-93bb-38",
        "marketType": "",
        "pageNo": 1,
        "pageSize": max(limit, 100),
    }
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(EASTMONEY_HOT_RANK_URL, json=payload, headers=EASTMONEY_HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()
        rank_rows = (data.get("data") or []) if isinstance(data, dict) else []
        if not rank_rows:
            return []
        secids = [
            secid
            for row in rank_rows[:limit]
            if isinstance(row, dict)
            for secid in [_eastmoney_secid(row.get("sc"))]
            if secid
        ]
        quotes = _fetch_eastmoney_quote_rows(session, secids)
    records: list[dict[str, Any]] = []
    for position, row in enumerate(rank_rows[:limit], start=1):
        if not isinstance(row, dict):
            continue
        code = _eastmoney_rank_code(row.get("sc"))
        if not code:
            continue
        quote = quotes.get(code) or {}
        records.append({
            "当前排名": row.get("rk") or position,
            "代码": code,
            "股票名称": quote.get("f14") or row.get("name") or "",
            "最新价": quote.get("f2"),
            "涨跌幅": quote.get("f3"),
            "热度": row.get("rc"),
        })
    if not records:
        return []
    return _normalize_records(records, source="eastmoney", limit=limit)


def _extract_ths_rows(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("stock_list", "list", "items", "data"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _fetch_ths_hot_rank(limit: int) -> list[dict[str, Any]]:
    params = {"stock_type": "a", "type": "day", "list_type": "normal"}
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(THS_HOT_RANK_URL, params=params, headers=THS_HEADERS, timeout=10)
        response.raise_for_status()
        rows = _extract_ths_rows(response.json())
    return _normalize_records(rows, source="ths", limit=limit)


def _latest_wind_export_file(directory: Path) -> Path | None:
    if not directory.exists():
        return None
    files = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in {".csv", ".xls", ".xlsx"}
    ]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def _read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        for encoding in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
            try:
                return pd.read_csv(path, encoding=encoding)
            except UnicodeDecodeError:
                continue
        return pd.read_csv(path)
    return pd.read_excel(path)


def _load_wind_export_rows(directory: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    path = _latest_wind_export_file(Path(directory).expanduser())
    if not path:
        return [], ""
    df = _read_table(path)
    if df is None or df.empty:
        return [], str(path)
    rows = _normalize_records(df.head(limit).to_dict("records"), source="wind", limit=limit)
    return rows, str(path)


def _merge_hot_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = row.get("code")
        if not code:
            continue
        item = merged.setdefault(code, {
            "code": code,
            "symbol": row.get("symbol") or _prefixed_symbol(code),
            "name": row.get("name") or "",
            "sources": [],
            "ranks": {},
            "pct_chg": row.get("pct_chg", 0.0),
            "topics": [],
            "source_rows": [],
        })
        source = _text(row.get("source"))
        if source and source not in item["sources"]:
            item["sources"].append(source)
        if source:
            rank = int(row.get("rank") or 999)
            item["ranks"][source] = min(rank, int(item["ranks"].get(source) or rank))
        if row.get("name") and not item.get("name"):
            item["name"] = row.get("name")
        if row.get("topic") and row.get("topic") not in item["topics"]:
            item["topics"].append(row.get("topic"))
        item["source_rows"].append(row)
    out = list(merged.values())
    out.sort(key=lambda item: (-(len(item.get("sources") or [])), min((item.get("ranks") or {"_": 999}).values())))
    return out


def _load_bar_frame(db: Database, code: str, freq_values: tuple[str, ...], *, limit: int) -> pd.DataFrame:
    docs = list(db["bars"].find(
        {
            "meta.symbol": {"$in": _symbol_query_values(code)},
            "meta.freq": {"$in": list(freq_values)},
        },
        {"_id": 0, "dt": 1, "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "meta": 1},
    ).sort("dt", -1).limit(limit * 2))
    rows: list[dict[str, Any]] = []
    for doc in docs:
        dt_value = pd.to_datetime(doc.get("dt"), errors="coerce")
        if pd.isna(dt_value):
            continue
        close = _safe_float(doc.get("close"))
        if close <= 0:
            continue
        rows.append({
            "dt": dt_value,
            "open": _safe_float(doc.get("open"), close),
            "high": max(close, _safe_float(doc.get("high"), close)),
            "low": min(close, _safe_float(doc.get("low"), close)),
            "close": close,
            "vol": _safe_float(doc.get("vol")),
            "amount": _safe_float(doc.get("amount")),
        })
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "vol", "amount"])
    df = pd.DataFrame(rows).dropna(subset=["dt"]).drop_duplicates(subset=["dt"], keep="first")
    return df.sort_values("dt").set_index("dt").tail(limit)


def _weekly_from_daily(daily: pd.DataFrame, *, limit: int = 80) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "vol", "amount"])
    weekly = daily.resample("W-FRI").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "vol": "sum",
        "amount": "sum",
    }).dropna(subset=["open", "high", "low", "close"])
    return weekly.tail(limit)


def _moving_average(df: pd.DataFrame, period: int) -> pd.Series:
    return pd.to_numeric(df["close"], errors="coerce").rolling(period).mean()


def _ma_climb_signal(df: pd.DataFrame, *, period: int, freq_label: str) -> dict[str, Any]:
    if df.empty or len(df) < period + 4:
        return {"ok": False}
    close = pd.to_numeric(df["close"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    ma = _moving_average(df, period)
    latest = float(close.iloc[-1])
    latest_ma = float(ma.iloc[-1])
    prior_ma = float(ma.iloc[-4])
    if latest_ma <= 0 or prior_ma <= 0:
        return {"ok": False}
    distance_pct = (latest - latest_ma) / latest_ma * 100.0
    low_distance_pct = (float(low.iloc[-1]) - latest_ma) / latest_ma * 100.0
    recent_above = int((close.tail(5) >= ma.tail(5)).fillna(False).sum())
    max_distance = 8.0 if freq_label == "日线" else 14.0
    rising = latest_ma >= prior_ma * 1.001
    near_or_orderly = (-2.5 <= low_distance_pct <= 4.0) or (0.0 <= distance_pct <= max_distance * 0.65)
    ok = bool(latest >= latest_ma and rising and distance_pct <= max_distance and recent_above >= 3 and near_or_orderly)
    return {
        "ok": ok,
        "period": period,
        "freq": freq_label,
        "latest_close": round(latest, 3),
        "ma": round(latest_ma, 3),
        "distance_pct": round(distance_pct, 3),
        "low_distance_pct": round(low_distance_pct, 3),
        "recent_above_count": recent_above,
        "rising": rising,
    }


def _just_started_signal(daily: pd.DataFrame) -> dict[str, Any]:
    if daily.empty or len(daily) < 25:
        return {"ok": False}
    close = pd.to_numeric(daily["close"], errors="coerce")
    vol = pd.to_numeric(daily["vol"], errors="coerce").fillna(0.0)
    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    latest = float(close.iloc[-1])
    latest_ma5 = float(ma5.iloc[-1])
    latest_ma10 = float(ma10.iloc[-1])
    if latest_ma5 <= 0 or latest_ma10 <= 0:
        return {"ok": False}
    five_day_gain_pct = (latest / float(close.iloc[-6]) - 1.0) * 100.0 if len(close) >= 6 and close.iloc[-6] else 0.0
    prev_close = float(close.iloc[-2])
    prev_ma5 = float(ma5.iloc[-2])
    prev_ma10 = float(ma10.iloc[-2])
    recent_high = float(close.shift(1).rolling(20).max().iloc[-1])
    base_low = float(close.tail(15).min())
    base_high = float(close.tail(15).max())
    base_range_pct = (base_high / base_low - 1.0) * 100.0 if base_low > 0 else 999.0
    volume_base = vol.shift(1).rolling(20).mean().iloc[-1]
    volume_ratio = float(vol.iloc[-1] / volume_base) if volume_base and volume_base > 0 else 0.0
    ma_turn = bool(ma5.iloc[-1] >= ma5.iloc[-3] and (prev_close <= prev_ma5 or prev_ma5 <= prev_ma10 * 1.003))
    breakout = bool(recent_high > 0 and latest >= recent_high * 0.99)
    orderly_gain = 2.0 <= five_day_gain_pct <= 18.0
    not_extended = latest <= latest_ma10 * 1.18
    ok = bool(latest >= latest_ma5 >= latest_ma10 * 0.995 and ma_turn and orderly_gain and not_extended and (breakout or volume_ratio >= 1.15 or base_range_pct <= 18.0))
    return {
        "ok": ok,
        "latest_close": round(latest, 3),
        "five_day_gain_pct": round(five_day_gain_pct, 3),
        "volume_ratio": round(volume_ratio, 3),
        "base_range_pct": round(base_range_pct, 3),
        "breakout_20d": breakout,
        "ma_turn": ma_turn,
    }


def _rank_quality(ranks: dict[str, int], *, limit: int) -> float:
    values = []
    for rank in ranks.values():
        try:
            rank_value = max(1, int(rank))
        except Exception:
            continue
        values.append(max(0.0, (limit + 1 - min(rank_value, limit)) / limit))
    if not values:
        return 0.0
    return sum(values) / len(values)


def _score_candidate(item: dict[str, Any], tags: list[str], details: dict[str, Any], *, rank_limit: int) -> float:
    source_score = sum(SOURCE_WEIGHTS.get(source, 14.0) for source in item.get("sources") or [])
    source_score = min(46.0, source_score)
    rank_score = _rank_quality(item.get("ranks") or {}, limit=rank_limit) * 14.0
    shape_score = 0.0
    if "just_started" in tags:
        shape_score += 18.0
    if "daily_ma5_climb" in tags:
        shape_score += 17.0
    if "daily_ma10_climb" in tags:
        shape_score += 15.0
    if "weekly_ma5_climb" in tags:
        shape_score += 17.0
    if "weekly_ma10_climb" in tags:
        shape_score += 15.0
    shape_score = min(44.0, shape_score)
    penalty = 0.0
    daily_ma10 = details.get("daily_ma10") or {}
    if _safe_float(daily_ma10.get("distance_pct")) > 13.0:
        penalty += 8.0
    just = details.get("just_started") or {}
    if _safe_float(just.get("five_day_gain_pct")) > 20.0:
        penalty += 8.0
    return round(max(0.0, min(100.0, source_score + rank_score + shape_score - penalty)), 3)


def _tier(score: float) -> str:
    if score >= 85:
        return "S强信号"
    if score >= 72:
        return "A重点"
    return "B观察"


def _reason_summary(tags: list[str], sources: list[str]) -> str:
    source_text = "+".join(SOURCE_LABELS.get(source, source) for source in sources)
    tag_labels = {
        "just_started": "刚启动",
        "daily_ma5_climb": "沿5日线攀爬",
        "daily_ma10_climb": "沿10日线攀爬",
        "weekly_ma5_climb": "沿5周线攀爬",
        "weekly_ma10_climb": "沿10周线攀爬",
    }
    shape_text = "、".join(tag_labels.get(tag, tag) for tag in tags)
    return f"{source_text}热榜 + {shape_text}" if shape_text else f"{source_text}热榜"


def _analyze_candidate(
    item: dict[str, Any],
    *,
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
    min_score: float,
    rank_limit: int,
    now: datetime,
) -> dict[str, Any]:
    tags: list[str] = []
    details: dict[str, Any] = {}
    just_started = _just_started_signal(daily)
    details["just_started"] = just_started
    if just_started.get("ok"):
        tags.append("just_started")
    for label, frame, prefix in (("日线", daily, "daily"), ("周线", weekly, "weekly")):
        for period in (5, 10):
            signal = _ma_climb_signal(frame, period=period, freq_label=label)
            details[f"{prefix}_ma{period}"] = signal
            if signal.get("ok"):
                tags.append(f"{prefix}_ma{period}_climb")
    score = _score_candidate(item, tags, details, rank_limit=rank_limit)
    selected = bool(tags and score >= min_score)
    sources = list(item.get("sources") or [])
    as_of = now.date().isoformat()
    code = _pure_a_code(item.get("code"))
    doc = {
        "_id": code,
        "raw_code": code,
        "code": code,
        "symbol": item.get("symbol") or _prefixed_symbol(code),
        "name": item.get("name") or "",
        "market": "A",
        "source": "hot_rank_clues",
        "sources": sources,
        "source_count": len(sources),
        "ranks": item.get("ranks") or {},
        "topics": (item.get("topics") or [])[:5],
        "pct_chg": _safe_float(item.get("pct_chg")),
        "score": score,
        "tier": _tier(score),
        "strategy_tags": tags,
        "reason_summary": _reason_summary(tags, sources),
        "shape_details": details,
        "selected": selected,
        "active": selected,
        "as_of": as_of,
        "snapshot_at": now,
        "updated_at": now,
        "invalidates_when": "跌破对应5/10日或5/10周均线，或热榜来源跌出Top100且形态失效",
    }
    return doc


def _write_freshness(db: Database, *, now: datetime, docs: list[dict[str, Any]], source_counts: dict[str, int], errors: list[str]) -> None:
    selected_count = sum(1 for doc in docs if doc.get("active"))
    db["data_freshness"].update_one(
        {"domain": "hot_rank_clue", "market": "A", "mode": "realtime", "collection": "hot_rank_clues"},
        {"$set": {
            "domain": "hot_rank_clue",
            "market": "A",
            "mode": "realtime",
            "lane": _env_text("SIGNALS_CURRENT_SYNC_LANE", "launchd"),
            "collection": "hot_rank_clues",
            "freshness": "fresh" if docs else "empty",
            "latest_dt": now.date().isoformat(),
            "as_of": now.date().isoformat(),
            "updated_at": now,
            "stale_reason": "" if docs else "hot_rank_empty",
            "count": len(docs),
            "selected_count": selected_count,
            "source_counts": source_counts,
            "errors": errors[:8],
        }},
        upsert=True,
    )


def sync_hot_rank_clues(db: Database, proxy_url: str = None) -> dict:
    """Fetch hot ranks, classify startup/climb shapes, and cache active clue rows."""
    del proxy_url
    now = naive_market_now("A")
    rank_limit = _env_int("HOT_RANK_SOURCE_LIMIT", 100, minimum=20, maximum=300)
    clue_limit = _env_int("HOT_RANK_CLUE_LIMIT", 40, minimum=1, maximum=120)
    min_score = _env_float("HOT_RANK_MIN_SCORE", 62.0, minimum=0.0, maximum=100.0)
    wind_dir = _env_text("HOT_RANK_WIND_EXPORT_DIR", "data/imports/wind_hot_rank")
    errors: list[str] = []
    source_rows: list[dict[str, Any]] = []
    wind_file = ""

    for source, fetcher in (
        ("eastmoney", _fetch_eastmoney_hot_rank),
        ("ths", _fetch_ths_hot_rank),
    ):
        try:
            rows = fetcher(rank_limit)
            source_rows.extend(rows)
        except Exception as exc:
            logger.warning("hot rank source failed %s: %s", source, exc)
            errors.append(f"{source}:{exc}")
    try:
        wind_rows, wind_file = _load_wind_export_rows(wind_dir, rank_limit)
        source_rows.extend(wind_rows)
    except Exception as exc:
        logger.warning("wind hot rank export failed: %s", exc)
        errors.append(f"wind:{exc}")

    source_counts = {
        source: sum(1 for row in source_rows if row.get("source") == source)
        for source in ("eastmoney", "ths", "wind")
    }
    merged = _merge_hot_rows(source_rows)
    docs: list[dict[str, Any]] = []
    for item in merged:
        code = _pure_a_code(item.get("code"))
        if not code:
            continue
        daily = _load_bar_frame(db, code, DAILY_FREQS, limit=90)
        weekly = _load_bar_frame(db, code, WEEKLY_FREQS, limit=80)
        if weekly.empty:
            weekly = _weekly_from_daily(daily, limit=80)
        if daily.empty:
            continue
        docs.append(_analyze_candidate(
            item,
            daily=daily,
            weekly=weekly,
            min_score=min_score,
            rank_limit=rank_limit,
            now=now,
        ))

    docs.sort(key=lambda doc: (bool(doc.get("active")), _safe_float(doc.get("score")), int(doc.get("source_count") or 0)), reverse=True)
    selected_ids = {doc["_id"] for doc in docs if doc.get("active")}
    selected_sorted = [doc for doc in docs if doc.get("active")][:clue_limit]
    selected_ids = {doc["_id"] for doc in selected_sorted}
    for doc in docs:
        if doc["_id"] not in selected_ids:
            doc["active"] = False
            doc["selected"] = False
    ops = [UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True) for doc in docs]
    if ops:
        db["hot_rank_clues"].bulk_write(ops, ordered=False)
    db["hot_rank_clues"].update_many(
        {"active": True, "_id": {"$nin": list(selected_ids)}},
        {"$set": {"active": False, "selected": False, "updated_at": now, "deactivated_reason": "not_in_latest_hot_rank_selection"}},
    )
    _write_freshness(db, now=now, docs=docs, source_counts=source_counts, errors=errors)

    status = "ok"
    if not source_rows:
        status = "error"
    elif errors:
        status = "partial"
    return {
        "status": status,
        "inserted": len(docs),
        "candidate_count": len(merged),
        "analyzed_count": len(docs),
        "selected_count": len(selected_ids),
        "source_counts": source_counts,
        "wind_export_file": wind_file,
        "errors": errors[:8],
    }
