# -*- coding: utf-8 -*-
"""Build the next-session explainable realtime universe for the terminal."""
from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from pymongo.database import Database

from signals.core.market_time import naive_market_now
from signals.core.scorer import FREQ_MULTIPLIER
from signals.core.trading_dates import a_share_realtime_day_key, trading_day
from signals.sync.task_context import get_task_env

logger = logging.getLogger("signals.sync.terminal_pool")

SELL_TOKENS = ("卖", "顶", "风险", "死叉", "减仓", "跌破", "预警")
CHAN_TOKENS = ("一买", "二买", "三买", "一卖", "二卖", "三卖", "背驰", "中枢", "笔", "线段", "趋势")
PATTERN_TOKENS = ("头肩", "双底", "双头", "三角形")
MACD_TOKENS = ("MACD", "零上绿柱扩大", "零下绿柱缩小")
GAP_TOKENS = ("缺口", "突破缺口", "持续缺口", "衰竭缺口", "普通缺口")
ENTRY_FACTOR_TOKENS = (
    "gap",
    "trend_breakout",
    "vol_contraction",
    "candle_run",
    "candle_accel",
    "200d_new_high_breakout",
    "200日新高",
    "新高突破",
    "relative_resilience_refusal_pullback",
    "拒绝回调",
    "相对强度",
)
TECHNICAL_TOKENS = CHAN_TOKENS + PATTERN_TOKENS + MACD_TOKENS + GAP_TOKENS + ENTRY_FACTOR_TOKENS
SIGNAL_TYPE_NORMALIZATIONS = {
    "1买": "一买",
    "2买": "二买",
    "3买": "三买",
    "1卖": "一卖",
    "2卖": "二卖",
    "3卖": "三卖",
}
LEFT_OPPORTUNITY_TOKENS = (
    "一买",
    "背驰买",
    "底背离",
    "MACD绿柱缩小",
    "零下绿柱缩小",
    "头肩底",
    "双底",
    "缺口买:衰竭",
    "vol_contraction",
)
RIGHT_OPPORTUNITY_TOKENS = (
    "二买",
    "三买",
    "趋势买",
    "突破",
    "持续缺口",
    "缺口买:持续",
    "MACD绿柱扩大",
    "零上绿柱扩大",
    "上升三角",
    "trend_breakout",
    "candle_run",
    "candle_accel",
    "200d_new_high_breakout",
    "200日新高",
    "新高突破",
    "relative_resilience_refusal_pullback",
    "拒绝回调",
)
WEAK_CONTEXT_TOKENS = ("缺口买:普通",)
MAINLINE_LENIENT_SECTOR_TOKENS = (
    "科技",
    "半导体",
    "芯片",
    "ai硬件",
    "ai算力",
    "算力",
    "数据中心",
    "cpo",
    "光模块",
    "光连接",
    "光通信",
    "通信网络",
    "f5g",
    "5g",
    "机器人",
    "人形机器人",
    "自动化",
    "执行器",
    "减速器",
    "消费电子",
    "华为链",
    "电新",
    "新能源",
    "锂电",
    "锂资源",
    "电池",
    "储能",
    "光伏",
    "风电",
    "新能车",
    "新能源汽车",
    "电力设备",
    "小金属",
    "稀有金属",
    "稀土",
    "有色金属",
    "工业金属",
    "贵金属",
    "金属新材料",
    "钨",
    "钼",
    "锑",
    "钴",
    "镍",
    "钛",
    "镁",
)
DEFENSIVE_STRICT_SECTOR_TOKENS = (
    "银行",
    "大金融",
    "金融",
    "保险",
    "消费品",
    "大消费",
    "白酒",
    "食品饮料",
    "纺织",
    "轻工",
    "服装",
    "家纺",
    "家电家居",
    "家电/照明",
)
BROAD_MARKET_INDEXES = (
    ("上证指数", "sh000001"),
    ("沪深300", "sh000300"),
    ("创业板指", "sz399006"),
)
BROAD_MARKET_RISE_THRESHOLD = 0.2
BROAD_MARKET_FALL_THRESHOLD = -0.2
BROAD_MARKET_NEUTRAL_THRESHOLD = 0.08
BROAD_MARKET_VOLUME_EXPAND_THRESHOLD = 1.08
BROAD_MARKET_VOLUME_SHRINK_THRESHOLD = 0.85
RIGHT_SIDE_FREQS = {"5分钟", "5min", "5m", "F5", "f5", "15分钟", "15min", "15m", "F15", "f15"}
BUY_FREQ_BONUS = {"30分钟": 120, "30min": 120, "30m": 120, "15分钟": 110, "15min": 110, "15m": 110, "5分钟": 80, "5min": 80, "5m": 80}
ENTRY_30M_FREQS = {"30分钟", "30min", "30m", "F30", "f30"}
ENTRY_UPPER_FREQS = {"日线", "daily", "1d", "D", "d", "周线", "weekly", "1w", "W", "w"}
DEFAULT_CANDIDATE_ANCHOR_FREQS = ENTRY_30M_FREQS | ENTRY_UPPER_FREQS
PRIMARY_MA_PERIODS = (5, 10, 20)
FIBONACCI_MA_PERIODS = (8, 13, 21, 34, 55, 89)
ENTRY_PARTNER_FREQS = ENTRY_UPPER_FREQS | RIGHT_SIDE_FREQS
ENTRY_QUEUE_LANES = {"entry_ready", "entry_waiting_confirm", "entry_waiting_upper_context", "entry_waiting_right_side_confirm"}
RISK_ACTION_STATUSES = {"risk_review", "chain_risk_review", "knowledge_blocked", "knowledge_conflict"}
POOL_RANKING_VERSION = "tech_ma_hot_sector_v10_right_first_left_review"
SETUP_RANK_TIERS = {
    "right_executable": 300,
    "right_attack": 200,
    "right_review": 150,
    "left_review": 100,
    "watch_only": 0,
    "risk_first": -100,
}
FOCUS_REVIEW_GATE_STATUSES = {
    "entry_waiting_30m_confirm",
    "entry_waiting_upper_context",
    "entry_waiting_right_side_confirm",
    "entry_waiting_resonance_confirm",
}
TRADE_STAGE_LABELS = {
    "clue_pool": "线索池",
    "watch_pool": "盯盘池",
    "dip_watch": "低吸观察",
    "left_attack": "低吸进攻",
    "probe_candidate": "试仓候选",
    "attack_entry": "进攻买点",
    "confirmed_entry": "确认买点",
    "skip_now": "暂不参与",
}
TRADE_STAGE_ACTIONS = {
    "clue_pool": "先放线索池",
    "watch_pool": "盯盘等买点",
    "dip_watch": "低吸观察",
    "left_attack": "低吸进攻复核",
    "probe_candidate": "小仓试仓复核",
    "attack_entry": "进攻买点复核",
    "confirmed_entry": "确认买点复核",
    "skip_now": "暂不参与",
}
TRADE_INTENT_LABELS = {
    "clue_only": "线索来源",
    "left_dip": "左侧低吸",
    "left_attack": "低吸进攻",
    "attack_entry": "进攻买点",
    "right_momentum": "右侧动量",
    "probe_candidate": "试仓候选",
    "wait_30m": "等30m买点",
    "wait_big_cycle": "等大周期",
    "confirmed_entry": "确认买点",
    "skip_now": "暂不参与",
}
TRADE_ROLE_LABELS = {
    "mainline_attack": "主线机会",
    "climax_risk": "过热禁追",
    "chain_watch": "产业链观察",
    "holding_chain": "产业链观察",
    "defensive_weight": "防守观察",
    "second_wave": "回踩再起",
    "risk_review": "风险复核",
    "ordinary_watch": "线索观察",
}
TRADE_INTENT_PRIORITY = {
    "confirmed_entry": 100,
    "attack_entry": 92,
    "left_attack": 88,
    "right_momentum": 86,
    "probe_candidate": 78,
    "wait_30m": 62,
    "wait_big_cycle": 54,
    "left_dip": 44,
    "clue_only": 10,
    "skip_now": 0,
}
SETUP_MODE_LABELS = {
    "left_attack": "低吸进攻",
    "right_attack": "右侧进攻",
    "right_review": "右侧复核",
    "watch": "观察",
    "risk_first": "风险优先",
}
TRADE_STAGE_LEGACY_DECISION = {
    "clue_pool": "strategy_candidate",
    "watch_pool": "watch_preheat",
    "dip_watch": "watch_preheat",
    "left_attack": "entry_waiting_confirm",
    "probe_candidate": "watch_preheat",
    "attack_entry": "entry_waiting_confirm",
    "confirmed_entry": "entry_ready",
    "skip_now": "risk_first",
}
MISSING_GATE_LABELS = {
    "risk_signal_present": "有卖点或冲突，先别当机会",
    "missing_buy_technical": "还没有硬技术买点",
    "left_attack_ma_confirmed": "左侧买点叠加10/20日线承接，按低吸进攻复核",
    "30m_missing": "等30m买点",
    "30m_attack_missing": "30m未补齐，按进攻买点小仓复核",
    "30m_stale": "30m信号过期，等新的30m",
    "30m_right_side_missing": "30m还没走出确认买点",
    "daily_or_weekly_missing": "缺日/周大周期位置",
    "daily_or_weekly_stale": "日/周结构过期，等重新确认",
    "partner_period_missing": "缺多周期共振",
    "5m_or_15m_missing": "缺5m/15m下单周期",
    "5m_or_15m_stale": "5m/15m信号过期",
    "5m_or_15m_right_side_missing": "5m/15m还没给下单确认",
    "chain_consensus_climax": "产业链一致高潮，别追",
    "chain_risk_off": "产业链走弱，先不看",
    "chain_block": "产业链风险未解除",
    "defensive_strict_requires_full_confirmation": "防守板块要等完整30m和5m/15m确认",
}
ENTRY_BLOCK_CHAIN_PHASES = {"consensus_climax", "risk_off"}
FREQ_ORDER = {
    "周线": 0,
    "weekly": 0,
    "1w": 0,
    "日线": 1,
    "daily": 1,
    "1d": 1,
    "30分钟": 2,
    "30min": 2,
    "30m": 2,
    "15分钟": 3,
    "15min": 3,
    "15m": 3,
    "5分钟": 4,
    "5min": 4,
    "5m": 4,
}
REASON_WEIGHTS = {
    "user_pinned": 180,
    "technical_trigger": 880,
    "generated_risk_signal": 950,
    "historical_signal_record": 0,
    "custom_signal": 0,
    "chan_signal": 0,
    "knowledge_confirmed": 0,
    "knowledge_conflict": 0,
    "knowledge_watch": 0,
    "chain_membership": 0,
    "chain_context": 0,
    "chain_core_rep": 0,
    "chain_elastic_rep": 0,
    "source_leader": 0,
    "constituent_hot": 0,
    "active_pool_watch": 260,
    "recent_opened": 180,
    "fallback_watch": 160,
    "review_sector_bullish": 420,
    "review_sector_bearish": 0,
}
REVIEW_CLUE_REASON_TYPES = {"review_sector_bullish", "review_sector_bearish"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_signal_type(value: Any) -> str:
    text = _text(value)
    for raw, normalized in SIGNAL_TYPE_NORMALIZATIONS.items():
        text = text.replace(raw, normalized)
    return text


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def _iso_dt(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _text(value)


def _trading_day_age(event_dt: Any, as_of: Any = None) -> int | None:
    event_date = _date_value(event_dt)
    if event_date is None:
        return None
    as_of_date = _date_value(as_of) or trading_day("A")
    if event_date >= as_of_date:
        return 0
    days = 0
    cursor = event_date
    while cursor < as_of_date:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def _reason_event_dt(reason: dict[str, Any]) -> str:
    evidence = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
    value = (
        reason.get("event_dt")
        or reason.get("dt")
        or reason.get("signal_date")
        or reason.get("latest_dt")
        or evidence.get("event_dt")
        or evidence.get("dt")
        or evidence.get("signal_date")
        or evidence.get("latest_dt")
        or reason.get("as_of")
    )
    return _iso_dt(value)


def _reason_age_trading_days(reason: dict[str, Any]) -> int | None:
    return _trading_day_age(_reason_event_dt(reason), reason.get("as_of"))


def _entry_age_limit(freq: Any) -> int:
    text = _text(freq)
    if _is_right_side_freq(text):
        return 0
    if _is_30m_freq(text):
        return 1
    if text in {"日线", "daily", "1d", "D", "d"}:
        return 2
    if text in {"周线", "weekly", "1w", "W", "w"}:
        return 5
    return 3


def _reason_is_current_for_entry(reason: dict[str, Any]) -> bool:
    age = _reason_age_trading_days(reason)
    if age is None:
        return True
    direct_freq = _text(reason.get("freq"))
    freqs = [direct_freq] if direct_freq else list(_reason_freqs(reason))
    limit = min(_entry_age_limit(freq) for freq in freqs) if freqs else 3
    return age <= limit


def _freshness_score_for_reason(reason: dict[str, Any] | None, max_score: float) -> float:
    if not isinstance(reason, dict):
        return 0.0
    age = _reason_age_trading_days(reason)
    if age is None:
        return max_score * 0.6
    direct_freq = _text(reason.get("freq"))
    limit = max(1, _entry_age_limit(direct_freq))
    if age <= 0:
        return max_score
    if age > limit:
        return 0.0
    return max_score * max(0.0, 1.0 - (age / (limit + 1)))


def _freq_sort_key(freq: Any) -> tuple[int, str]:
    text = _text(freq)
    return FREQ_ORDER.get(text, FREQ_ORDER.get(text.lower(), 99)), text


def _is_30m_freq(freq: Any) -> bool:
    return _text(freq) in ENTRY_30M_FREQS


def _is_upper_freq(freq: Any) -> bool:
    return _text(freq) in ENTRY_UPPER_FREQS


def _is_right_side_freq(freq: Any) -> bool:
    return _text(freq) in RIGHT_SIDE_FREQS


def _is_entry_partner_freq(freq: Any) -> bool:
    return _text(freq) in ENTRY_PARTNER_FREQS


def _pure_a_code(symbol: Any) -> str:
    raw = _text(symbol).upper()
    if not raw:
        return ""
    pure = raw.split(".", 1)[-1] if "." in raw else raw
    pure = pure.replace("SH", "").replace("SZ", "").replace("BJ", "")
    if pure.startswith(("900", "200")):
        return ""
    return pure if pure.isdigit() and len(pure) == 6 else ""


def _prefixed_symbol(code: str) -> str:
    if not code:
        return ""
    if code.startswith(("6", "9")):
        return f"SH.{code}"
    if code.startswith(("4", "8")):
        return f"BJ.{code}"
    return f"SZ.{code}"


def _add_stock(stocks: list[str], value: Any, *, index_codes: set[str]) -> None:
    code = _pure_a_code(value)
    if code and code not in index_codes and code not in stocks:
        stocks.append(code)


def _index_codes() -> set[str]:
    import config

    return {
        _pure_a_code(symbol)
        for symbol in getattr(config, "INDEX_AK_CODES", {}).values()
        if _pure_a_code(symbol)
    }


def _date_text(value: Any) -> str:
    parsed = _date_value(value)
    if parsed:
        return parsed.isoformat()
    text = _text(value)
    digits = text.replace("-", "")[:8]
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return ""


def _doc_date_text(doc: dict[str, Any]) -> str:
    for key in ("trade_date", "date_key", "dt", "date", "as_of", "snapshot_at", "updated_at"):
        value = doc.get(key)
        if value:
            parsed = _date_text(value)
            if parsed:
                return parsed
    return ""


def _index_quote_candidates(symbol: str) -> list[str]:
    compact = _text(symbol).lower()
    if len(compact) != 8 or compact[:2] not in {"sh", "sz", "bj"}:
        return [compact] if compact else []
    pure = compact[2:]
    market = compact[:2].upper()
    dotted = f"{market}.{pure}"
    suffix = f"{pure}.{market}"
    return list(dict.fromkeys([dotted, dotted.lower(), compact, compact.upper(), suffix, suffix.lower()]))


def _change_pct_from_doc(doc: dict[str, Any]) -> float | None:
    for key in ("change_pct", "pct_chg", "day_change_pct", "daily_change_pct", "today_change_pct", "gain_pct"):
        if key in doc and doc.get(key) not in (None, ""):
            return _float(doc.get(key))
    close = _float(doc.get("close") or doc.get("price") or doc.get("latest"))
    prev_close = _float(doc.get("prev_close"))
    if close > 0 and prev_close > 0:
        return (close / prev_close - 1.0) * 100.0
    return None


def _index_change_doc_from_quotes(db: Database, symbol: str, trade_date: str) -> dict[str, Any]:
    try:
        doc = db["quote_snapshots"].find_one(
            {"symbol": {"$in": _index_quote_candidates(symbol)}},
            {"_id": 0},
            sort=[("snapshot_at", -1), ("dt", -1)],
        ) or {}
    except Exception:
        return {}
    if not doc or doc.get("is_stale") or _text(doc.get("freshness")).lower() == "stale":
        return {}
    if _doc_date_text(doc) != trade_date:
        return {}
    pct = _change_pct_from_doc(doc)
    if pct is None:
        return {}
    return {"source": "quote_snapshots", "change_pct": pct, "as_of": _doc_date_text(doc)}


def _index_change_doc_from_bars(db: Database, symbol: str, trade_date: str) -> dict[str, Any]:
    candidates = [symbol, symbol.upper(), symbol.lower()]
    for collection in ("index_bars", "bars"):
        try:
            doc = db[collection].find_one(
                {
                    "meta.symbol": {"$in": candidates},
                    "meta.freq": {"$in": ["daily", "日线", "D", "1d"]},
                },
                {"_id": 0},
                sort=[("dt", -1)],
            ) or {}
        except Exception:
            doc = {}
        if not doc or _doc_date_text(doc) != trade_date:
            continue
        pct = _change_pct_from_doc(doc)
        if pct is None:
            continue
        return {"source": collection, "change_pct": pct, "as_of": _doc_date_text(doc)}
    return {}


def _bar_liquidity_value(doc: dict[str, Any], metric: str) -> float:
    if metric == "amount":
        return _float(doc.get("amount"))
    if metric == "volume":
        return _float(doc.get("vol") or doc.get("volume"))
    return 0.0


def _index_volume_doc_from_bars(db: Database, symbol: str, trade_date: str) -> dict[str, Any]:
    candidates = [symbol, symbol.upper(), symbol.lower()]
    for collection in ("index_bars", "bars"):
        try:
            docs = list(db[collection].find(
                {
                    "meta.symbol": {"$in": candidates},
                    "meta.freq": {"$in": ["daily", "日线", "D", "1d"]},
                },
                {"_id": 0, "dt": 1, "amount": 1, "vol": 1, "volume": 1},
            ).sort([("dt", -1)]).limit(25))
        except Exception:
            docs = []
        if not docs:
            continue
        latest = next((doc for doc in docs if _doc_date_text(doc) == trade_date), docs[0])
        latest_date = _doc_date_text(latest)
        for metric in ("amount", "volume"):
            latest_value = _bar_liquidity_value(latest, metric)
            history = [
                _bar_liquidity_value(doc, metric)
                for doc in docs
                if _doc_date_text(doc) < latest_date and _bar_liquidity_value(doc, metric) > 0
            ][:5]
            if latest_value <= 0 or len(history) < 3:
                continue
            avg_value = sum(history) / len(history)
            if avg_value <= 0:
                continue
            return {
                "source": collection,
                "liquidity_metric": metric,
                "liquidity_value": latest_value,
                "liquidity_ratio_5d": round(latest_value / avg_value, 3),
                "liquidity_baseline_days": len(history),
            }
    return {}


def _market_volume_state(value: float) -> str:
    if value >= BROAD_MARKET_VOLUME_EXPAND_THRESHOLD:
        return "expanding"
    if 0 < value <= BROAD_MARKET_VOLUME_SHRINK_THRESHOLD:
        return "shrinking"
    if value > 0:
        return "normal"
    return "unknown"


def _market_volume_label(state: str) -> str:
    return {
        "expanding": "放量",
        "normal": "量能正常",
        "shrinking": "缩量",
        "unknown": "量能未知",
    }.get(state, "量能未知")


def _index_setup_side_from_market(average_change: float, rising_count: int, falling_count: int, index_count: int, volume_state: str) -> str:
    required = max(1, (index_count // 2) + 1)
    if falling_count >= required or average_change <= BROAD_MARKET_FALL_THRESHOLD:
        return "right_sell" if volume_state == "expanding" else "left_sell"
    if rising_count >= required or average_change >= BROAD_MARKET_RISE_THRESHOLD:
        return "right_buy" if volume_state == "expanding" else "left_buy"
    if abs(average_change) < BROAD_MARKET_NEUTRAL_THRESHOLD:
        return "unknown"
    if average_change > 0:
        return "left_buy"
    if average_change < 0:
        return "left_sell"
    return "unknown"


def _index_setup_label(side: str) -> str:
    return {
        "right_buy": "指数右侧买",
        "left_buy": "指数左侧买",
        "left_sell": "指数左侧卖",
        "right_sell": "指数右侧卖",
        "unknown": "指数未知",
    }.get(side, "指数未知")


def _load_broad_market_context(db: Database, trade_date: str) -> dict[str, Any]:
    index_rows: list[dict[str, Any]] = []
    for name, symbol in BROAD_MARKET_INDEXES:
        doc = _index_change_doc_from_quotes(db, symbol, trade_date) or _index_change_doc_from_bars(db, symbol, trade_date)
        if not doc:
            continue
        volume_doc = _index_volume_doc_from_bars(db, symbol, trade_date)
        index_rows.append({
            "name": name,
            "symbol": symbol,
            "change_pct": round(_float(doc.get("change_pct")), 3),
            "source": doc.get("source"),
            "as_of": doc.get("as_of") or trade_date,
            **({key: value for key, value in volume_doc.items() if key != "source"} if volume_doc else {}),
            **({"volume_source": volume_doc.get("source")} if volume_doc else {}),
        })
    if not index_rows:
        return {"is_falling": False, "label": "大盘未知", "index_setup_side": "unknown", "index_setup_label": "指数未知", "volume_state": "unknown", "volume_label": "量能未知", "as_of": trade_date, "index_count": 0, "indexes": []}
    falling = [item for item in index_rows if _float(item.get("change_pct")) <= BROAD_MARKET_FALL_THRESHOLD]
    rising = [item for item in index_rows if _float(item.get("change_pct")) >= BROAD_MARKET_RISE_THRESHOLD]
    average = sum(_float(item.get("change_pct")) for item in index_rows) / len(index_rows)
    volume_ratios = [
        _float(item.get("liquidity_ratio_5d"))
        for item in index_rows
        if _float(item.get("liquidity_ratio_5d")) > 0
    ]
    volume_ratio = sum(volume_ratios) / len(volume_ratios) if volume_ratios else 0.0
    volume_state = _market_volume_state(volume_ratio)
    index_setup_side = _index_setup_side_from_market(average, len(rising), len(falling), len(index_rows), volume_state)
    required = max(1, (len(index_rows) // 2) + 1)
    is_falling = len(falling) >= required or (average <= BROAD_MARKET_FALL_THRESHOLD and bool(falling))
    return {
        "is_falling": is_falling,
        "label": _index_setup_label(index_setup_side),
        "index_setup_side": index_setup_side,
        "index_setup_label": _index_setup_label(index_setup_side),
        "volume_state": volume_state,
        "volume_label": _market_volume_label(volume_state),
        "volume_ratio_5d": round(volume_ratio, 3) if volume_ratio > 0 else 0.0,
        "as_of": trade_date,
        "average_change_pct": round(average, 3),
        "rising_count": len(rising),
        "falling_count": len(falling),
        "index_count": len(index_rows),
        "rise_threshold": BROAD_MARKET_RISE_THRESHOLD,
        "fall_threshold": BROAD_MARKET_FALL_THRESHOLD,
        "indexes": index_rows,
    }


def _attach_broad_market_context(rows: dict[str, dict[str, Any]], context: dict[str, Any]) -> None:
    if not isinstance(context, dict) or not context:
        return
    for row in rows.values():
        row["broad_market_context"] = context


def _signal_text(row: dict[str, Any]) -> str:
    return _normalize_signal_type(" ".join(_text(row.get(key)) for key in ("signal_type", "type", "reason", "summary", "details")))


def _source(row: dict[str, Any]) -> str:
    return _text(row.get("source") or row.get("data_source")).lower()


def _is_historical_signal_source(row: dict[str, Any]) -> bool:
    source = _source(row)
    return source.startswith("sqlite.backtest.signal_records") or source.startswith("historical_signal_record")


def _signal_side(row: dict[str, Any]) -> str:
    status = _text(row.get("pool_status") or row.get("status") or row.get("direction")).lower()
    text = _signal_text(row)
    if status in {"warning", "sell", "risk"} or any(token in text for token in SELL_TOKENS):
        return "sell"
    return "buy"


def _signal_family(row: dict[str, Any]) -> str:
    text = _signal_text(row)
    if any(token in text for token in CHAN_TOKENS):
        return "chan_style"
    return "custom_or_system"


def _reason_type_for_signal(row: dict[str, Any]) -> str:
    source = _source(row)
    side = _signal_side(row)
    if _is_historical_signal_source(row):
        return "historical_signal_record"
    if "czsc" in source or "chan" in source:
        return "technical_trigger"
    if source.startswith("sync.signal_pool.generated") or side == "sell":
        return "generated_risk_signal"
    return "custom_signal"


def _is_generated_daily_signal(row: dict[str, Any]) -> bool:
    source = _source(row)
    signal_type = _text(row.get("signal_type") or row.get("type") or row.get("reason"))
    freq = _text(row.get("freq") or row.get("timeframe"))
    return (
        source.startswith("sync.signal_pool.generated")
        or signal_type.startswith("日线候选")
        or signal_type.startswith("日线预警")
        or (freq == "日线" and source.startswith("sync.signal_pool"))
    )


def _is_hard_screen_signal(row: dict[str, Any]) -> bool:
    source = _source(row)
    if _is_historical_signal_source(row) or source.startswith("sync.signal_pool.generated"):
        return False
    if "czsc" in source or "chan" in source:
        return True
    signal_type = _signal_text(row)
    return any(token in signal_type for token in TECHNICAL_TOKENS)


def _resonance_grade(aligned_freqs: list[str], conflict_freqs: list[str]) -> str:
    if conflict_freqs:
        return "conflict"
    if len(aligned_freqs) >= 3:
        return "strong_resonance"
    if len(aligned_freqs) >= 2:
        return "multi_period"
    return "single_period"


def _screen_resonance_context(signal: dict[str, Any], sibling_signals: list[dict[str, Any]]) -> dict[str, Any]:
    side = _signal_side(signal)
    primary_freq = _text(signal.get("freq") or signal.get("timeframe"))
    aligned_freqs = sorted(
        {_text(item.get("freq") or item.get("timeframe")) for item in sibling_signals if _is_hard_screen_signal(item) and _signal_side(item) == side},
        key=_freq_sort_key,
    )
    conflict_freqs = sorted(
        {_text(item.get("freq") or item.get("timeframe")) for item in sibling_signals if _is_hard_screen_signal(item) and _signal_side(item) != side},
        key=_freq_sort_key,
    )
    aligned_freqs = [item for item in aligned_freqs if item]
    conflict_freqs = [item for item in conflict_freqs if item]
    grade = _resonance_grade(aligned_freqs, conflict_freqs)
    tags: list[str] = []
    if grade == "conflict":
        tags.append("周期冲突")
    if grade in {"multi_period", "strong_resonance"}:
        tags.append("多周期共振")
    if grade == "strong_resonance":
        tags.append("强共振")
    if "周线" in aligned_freqs and "日线" in aligned_freqs:
        tags.append("日周同向")
    if any(freq in {"5分钟", "5min", "5m"} for freq in aligned_freqs):
        tags.append("5m确认")
    if not tags:
        tags.append("硬技术")
    side_text = "买点" if side == "buy" else "风险"
    return {
        "direction": side,
        "primary_freq": primary_freq,
        "aligned_freqs": aligned_freqs or ([primary_freq] if primary_freq else []),
        "conflict_freqs": conflict_freqs,
        "grade": grade,
        "tags": tags[:5],
        "summary": f"{side_text}筛选信号：{','.join(aligned_freqs or [primary_freq])}",
        "latest_dt": _text(signal.get("signal_date") or signal.get("updated_at"))[:10],
    }


def _right_side_confirmed(aligned_freqs: list[str]) -> bool:
    return any(_is_right_side_freq(freq) for freq in aligned_freqs)


def _upper_context_confirmed(aligned_freqs: list[str]) -> bool:
    return any(_is_upper_freq(freq) for freq in aligned_freqs)


def _entry_waiting_label(queue_lane: str) -> str:
    if queue_lane == "entry_waiting_upper_context":
        return "等待日/周确认"
    if queue_lane == "entry_waiting_right_side_confirm":
        return "等待5m/15m确认"
    return "等待共振确认"


def _technical_actionability(side: str, resonance_context: dict[str, Any], freq: str = "") -> tuple[str, str]:
    if side == "sell":
        return "risk_exit_first", "risk_exit_first"
    grade = _text(resonance_context.get("grade"))
    aligned_freqs = [str(item) for item in resonance_context.get("aligned_freqs") or [] if item]
    if not aligned_freqs and freq:
        aligned_freqs = [freq]
    conflict_freqs = [str(item) for item in resonance_context.get("conflict_freqs") or [] if item]
    if grade == "conflict" or conflict_freqs:
        return "review_required", "context_only"
    if len(aligned_freqs) < 2:
        return "observe_only", "context_only"
    if not _upper_context_confirmed(aligned_freqs):
        return "entry_waiting_confirm", "entry_waiting_upper_context"
    if not _right_side_confirmed(aligned_freqs):
        return "entry_waiting_confirm", "entry_waiting_right_side_confirm"
    return "entry_ready", "entry_ready"


def _chain_decision_effect(phase: str) -> str:
    if phase == "accelerating":
        return "confirm"
    if phase in {"consensus_climax", "risk_off"}:
        return "exit_priority"
    return "context_only"


def _is_technical_reason(reason: dict[str, Any]) -> bool:
    rt = _text(reason.get("reason_type"))
    if rt in REVIEW_CLUE_REASON_TYPES:
        return False
    return rt in {"technical_trigger", "technical_signal"}


def _source_doc_id(row: dict[str, Any]) -> str:
    return _text(row.get("_id") or row.get("dedupe_key") or row.get("action_id") or row.get("decision_id"))


def _reason_key(reason: dict[str, Any]) -> str:
    return "|".join([
        _text(reason.get("reason_type")),
        _text(reason.get("source_collection")),
        _text(reason.get("source_doc_id")),
        _text(reason.get("signal_type")),
        _text(reason.get("freq")),
        _text(reason.get("board_or_concept")),
    ])


def _empty_row(code: str, name: str = "") -> dict[str, Any]:
    symbol = _prefixed_symbol(code)
    return {
        "symbol": symbol,
        "code": symbol,
        "raw_code": code,
        "name": name,
        "kind": "stock",
        "score": 0.0,
        "sort_score": 0.0,
        "signal_origin": "",
        "signal_family": "",
        "latest_signal": "",
        "action_status": "watch",
        "trader_action": "观察",
        "next_action": "观察",
        "actionability": "context_only",
        "queue_lane": "context_only",
        "decision_effect": "context_only",
        "source_role": "context",
        "invalidates_when": "入池条件失效或产业链热度回落",
        "technical_evidence": {},
        "resonance_context": {},
        "knowledge_confirmation": {"status": "none"},
        "chain_context": {},
        "inclusion_reasons": [],
        "source_tags": [],
        "target_kind": "stock",
        "target_label": symbol,
        "target_symbol": symbol,
        "target_freq": "30min",
    }


def _add_reason(rows: dict[str, dict[str, Any]], value: Any, reason: dict[str, Any], *, index_codes: set[str], name: str = "") -> None:
    code = _pure_a_code(value)
    if not code or code in index_codes:
        return
    if code not in rows and reason.get("can_create_candidate") is False:
        return
    row = rows.setdefault(code, _empty_row(code, name))
    if name and not row.get("name"):
        row["name"] = name
    reason_type = _text(reason.get("reason_type"))
    base_weight = REASON_WEIGHTS.get(reason_type, 100)
    freq = _text(reason.get("freq"))
    signal_side = _text(reason.get("signal_side"))
    side_bonus = 180 if signal_side == "sell" and reason_type not in REVIEW_CLUE_REASON_TYPES else 0
    decision_effect = _text(reason.get("decision_effect"))
    if not decision_effect:
        decision_effect = "exit_priority" if reason_type == "technical_trigger" and signal_side == "sell" else ("confirm" if reason_type == "technical_trigger" else "context_only")
    source_role = _text(reason.get("source_role")) or ("technical_trigger" if reason_type == "technical_trigger" else "context")
    actionability = _text(reason.get("actionability"))
    queue_lane = _text(reason.get("queue_lane"))
    context_only = decision_effect in {"context_only", "history_pending"} or source_role == "context"
    weight = 0.0 if context_only else base_weight + BUY_FREQ_BONUS.get(freq, 0) + side_bonus + _float(reason.get("score")) * 0.05 + _float(reason.get("heat_score")) * 0.05
    evidence = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
    ma_alignment = reason.get("ma_alignment") if isinstance(reason.get("ma_alignment"), dict) else {}
    if not ma_alignment and isinstance(evidence.get("ma_alignment"), dict):
        ma_alignment = evidence.get("ma_alignment") or {}
    event_dt = (
        _iso_dt(reason.get("event_dt"))
        or _iso_dt(reason.get("dt"))
        or _iso_dt(reason.get("signal_date"))
        or _iso_dt(reason.get("latest_dt"))
        or _iso_dt(evidence.get("event_dt"))
        or _iso_dt(evidence.get("dt"))
        or _iso_dt(evidence.get("signal_date"))
        or _iso_dt(evidence.get("latest_dt"))
    )
    as_of = _text(reason.get("as_of"))
    signal_age = _trading_day_age(event_dt or as_of, as_of)
    normalized = {
        "reason_type": reason_type,
        "weight": round(weight, 3),
        "source_role": source_role,
        "decision_effect": decision_effect,
        "actionability": actionability,
        "queue_lane": queue_lane,
        "source_collection": _text(reason.get("source_collection")),
        "source_doc_id": _text(reason.get("source_doc_id")),
        "signal_type": _normalize_signal_type(reason.get("signal_type")),
        "signal_side": signal_side,
        "signal_family": _text(reason.get("signal_family")),
        "freq": freq,
        "score": _float(reason.get("score")),
        "confidence": _float(reason.get("confidence")),
        "chain_id": _text(reason.get("chain_id")),
        "chain_name": _text(reason.get("chain_name")),
        "node_id": _text(reason.get("node_id")),
        "node_name": _text(reason.get("node_name")),
        "layer": _text(reason.get("layer")),
        "stage": _text(reason.get("stage")),
        "membership_type": _text(reason.get("membership_type")),
        "membership_confidence": _float(reason.get("membership_confidence") or reason.get("confidence")),
        "exposure_score": _float(reason.get("exposure_score")),
        "evidence_sources": [item for item in reason.get("evidence_sources") or [] if _text(item)],
        "source_boards": [
            item for item in (
                reason.get("source_boards")
                if isinstance(reason.get("source_boards"), list)
                else evidence.get("source_boards") if isinstance(evidence.get("source_boards"), list) else []
            )
            if isinstance(item, dict)
        ],
        "board_or_concept": _text(reason.get("board_or_concept")),
        "as_of": as_of,
        "event_dt": event_dt,
        "event_date": event_dt[:10] if event_dt else "",
        "signal_age_trading_days": signal_age,
        "evidence": evidence,
        "ma_alignment": ma_alignment,
        "resonance_context": reason.get("resonance_context") if isinstance(reason.get("resonance_context"), dict) else {},
        "knowledge_status": _text(reason.get("knowledge_status")),
        "knowledge_effect": _text(reason.get("knowledge_effect")),
        "backtest_quality": reason.get("backtest_quality") if isinstance(reason.get("backtest_quality"), dict) else {},
    }
    key = _reason_key(normalized)
    existing_keys = {_reason_key(item) for item in row["inclusion_reasons"]}
    if key not in existing_keys:
        row["inclusion_reasons"].append(normalized)
    row["inclusion_reasons"].sort(key=lambda item: _float(item.get("weight")), reverse=True)
    top = row["inclusion_reasons"][0]
    row["sort_score"] = max(_float(row.get("sort_score")), _float(top.get("weight")))
    row["score"] = max(_float(row.get("score")), _float(reason.get("score")), _float(reason.get("heat_score")))
    if ma_alignment:
        existing_ma = row.get("ma_alignment") if isinstance(row.get("ma_alignment"), dict) else {}
        if _float(ma_alignment.get("score")) >= _float(existing_ma.get("score")):
            row["ma_alignment"] = ma_alignment
    row["signal_origin"] = top["reason_type"]
    row["signal_family"] = top.get("signal_family") or row.get("signal_family") or ""
    if top.get("signal_type"):
        row["latest_signal"] = top["signal_type"]
    if top.get("chain_id"):
        row["chain_context"] = {
            "chain_id": top.get("chain_id"),
            "node_id": top.get("node_id"),
            "board_or_concept": top.get("board_or_concept"),
        }
    source_tag = reason_type
    if source_tag and source_tag not in row["source_tags"]:
        row["source_tags"].append(source_tag)
    if _is_technical_reason(normalized):
        resonance_context = normalized["resonance_context"]
        if not resonance_context and isinstance(normalized["evidence"], dict):
            resonance_context = normalized["evidence"].get("resonance_context") or {}
        actionability, queue_lane = _technical_actionability(normalized["signal_side"], resonance_context, normalized["freq"])
        normalized["actionability"] = normalized.get("actionability") or actionability
        normalized["queue_lane"] = normalized.get("queue_lane") or queue_lane
        normalized["decision_effect"] = "exit_priority" if queue_lane == "risk_exit_first" else ("confirm" if queue_lane in ENTRY_QUEUE_LANES else "context_only")
        row["technical_evidence"] = {
            "source_collection": normalized["source_collection"],
            "source_doc_id": normalized["source_doc_id"],
            "signal_type": normalized["signal_type"],
            "signal_side": normalized["signal_side"],
            "freq": normalized["freq"],
            "score": normalized["score"],
            "confidence": normalized["confidence"],
            "as_of": normalized["as_of"],
            "event_dt": normalized["event_dt"],
            "event_date": normalized["event_date"],
            "signal_age_trading_days": normalized["signal_age_trading_days"],
            "evidence": normalized["evidence"],
            "resonance_context": resonance_context,
            "ma_alignment": normalized["ma_alignment"],
            "actionability": normalized["actionability"],
            "queue_lane": normalized["queue_lane"],
        }
        if resonance_context:
            row["resonance_context"] = resonance_context
        row["actionability"] = normalized["actionability"]
        row["queue_lane"] = normalized["queue_lane"]
        row["source_role"] = "technical_trigger"
        row["decision_effect"] = normalized["decision_effect"]
    if reason_type.startswith("knowledge_"):
        row["knowledge_confirmation"] = {
            "status": normalized.get("knowledge_status") or reason_type.replace("knowledge_", ""),
            "effect": normalized.get("knowledge_effect") or normalized.get("decision_effect") or "context_only",
            "sentiment": _text(reason.get("sentiment")),
            "source_collection": normalized["source_collection"],
            "source_doc_id": normalized["source_doc_id"],
            "as_of": normalized["as_of"],
            "evidence": normalized["evidence"],
        }
    if reason_type in {"chain_membership", "chain_context", "chain_core_rep", "chain_elastic_rep", "source_leader", "constituent_hot"}:
        phase = _text(normalized["evidence"].get("phase") if isinstance(normalized.get("evidence"), dict) else "")
        row["chain_context"] = {
            "chain_id": normalized.get("chain_id"),
            "chain_name": normalized.get("chain_name"),
            "node_id": normalized.get("node_id"),
            "node_name": normalized.get("node_name"),
            "layer": normalized.get("layer"),
            "stage": normalized.get("stage"),
            "membership_type": normalized.get("membership_type"),
            "membership_confidence": normalized.get("membership_confidence"),
            "exposure_score": normalized.get("exposure_score"),
            "evidence_sources": normalized.get("evidence_sources") or [],
            "source_boards": normalized.get("source_boards") or [],
            "board_or_concept": normalized.get("board_or_concept"),
            "phase": phase,
            "effect": normalized.get("decision_effect") or _chain_decision_effect(phase),
            "as_of": normalized.get("as_of"),
            "rank": normalized["evidence"].get("rank") if isinstance(normalized.get("evidence"), dict) else None,
            "heat_score": normalized["evidence"].get("heat_score") if isinstance(normalized.get("evidence"), dict) else None,
            "range_pattern": normalized["evidence"].get("range_pattern") if isinstance(normalized.get("evidence"), dict) else "",
            "evidence": normalized.get("evidence"),
        }
    if top.get("signal_side") == "sell" and top.get("decision_effect") != "context_only":
        row["action_status"] = "risk_review"
        row["trader_action"] = "减仓/止盈" if top.get("queue_lane") == "risk_exit_first" else "风险复核"
        row["next_action"] = row["trader_action"]
        row["queue_lane"] = top.get("queue_lane") or "risk_exit_first"
        row["actionability"] = top.get("actionability") or "risk_exit_first"
        row["invalidates_when"] = "卖出/风险信号解除或重新站回关键周期"
    elif top["reason_type"] in {"technical_trigger", "technical_signal"}:
        actionability = top.get("actionability") or row.get("actionability") or "observe_only"
        if actionability == "entry_ready":
            row["action_status"] = "entry_ready"
            row["trader_action"] = "可试仓"
        elif actionability == "entry_waiting_confirm":
            row["action_status"] = top.get("queue_lane") or "entry_waiting_confirm"
            row["trader_action"] = _entry_waiting_label(row["action_status"])
        elif actionability == "review_required":
            row["action_status"] = "period_conflict_review"
            row["trader_action"] = "周期冲突复核"
        else:
            row["action_status"] = "technical_watch"
            row["trader_action"] = "观察"
        row["next_action"] = row["trader_action"]
        row["queue_lane"] = top.get("queue_lane") or row.get("queue_lane")
        row["actionability"] = actionability
        row["invalidates_when"] = "5m/15m 无法确认或上级周期转弱"
    elif top["reason_type"] == "knowledge_conflict":
        row["action_status"] = "knowledge_conflict"
        row["trader_action"] = "知识库冲突复核"
        row["next_action"] = row["trader_action"]
        row["invalidates_when"] = "技术信号或知识观点解除冲突"
    elif top["reason_type"] in {"knowledge_confirmed", "knowledge_watch"}:
        row["action_status"] = "knowledge_watch"
        row["trader_action"] = "知识库观察"
        row["next_action"] = row["trader_action"]
        row["invalidates_when"] = "知识观点过期或缺少硬技术确认"
    elif top["reason_type"] == "fallback_watch":
        row["action_status"] = "fallback_watch"
        row["trader_action"] = "观察/预热"
        row["next_action"] = row["trader_action"]
        row["queue_lane"] = "fallback_watch"
        row["actionability"] = "observe_only"
        row["invalidates_when"] = "硬技术信号未确认或降级候选过期"
    elif top["reason_type"].startswith("chain_") or top["reason_type"] in {"source_leader", "constituent_hot"}:
        row["action_status"] = "chain_watch"
        row["trader_action"] = "观察产业链共振"
        row["next_action"] = row["trader_action"]
        row["invalidates_when"] = "产业链节点热度转弱或领涨股回落"

    technical = next((item for item in row["inclusion_reasons"] if _is_technical_reason(item)), None)
    if technical:
        if technical.get("signal_side") == "sell":
            row["action_status"] = "risk_review"
            row["trader_action"] = "减仓/止盈" if technical.get("queue_lane") == "risk_exit_first" else "风险复核"
            row["next_action"] = row["trader_action"]
            row["queue_lane"] = technical.get("queue_lane") or "risk_exit_first"
            row["actionability"] = technical.get("actionability") or "risk_exit_first"
            row["invalidates_when"] = "卖出/风险信号解除或重新站回关键周期"
        elif row.get("action_status") in {"watch", "knowledge_watch", "chain_watch", "technical_watch"}:
            actionability = technical.get("actionability") or row.get("actionability") or "observe_only"
            if actionability == "entry_ready":
                row["action_status"] = "entry_ready"
                row["trader_action"] = "可试仓"
            elif actionability == "entry_waiting_confirm":
                row["action_status"] = technical.get("queue_lane") or "entry_waiting_confirm"
                row["trader_action"] = _entry_waiting_label(row["action_status"])
            elif actionability == "review_required":
                row["action_status"] = "period_conflict_review"
                row["trader_action"] = "周期冲突复核"
            else:
                row["action_status"] = "technical_watch"
                row["trader_action"] = "观察"
            row["next_action"] = row["trader_action"]
            row["queue_lane"] = technical.get("queue_lane") or row.get("queue_lane")
            row["actionability"] = actionability
            row["invalidates_when"] = "5m/15m 无法确认或上级周期转弱"
    if (row.get("knowledge_confirmation") or {}).get("status") == "conflict" and row.get("action_status") == "buy_candidate":
        row["action_status"] = "knowledge_conflict"
        row["trader_action"] = "知识库冲突复核"
        row["next_action"] = row["trader_action"]
        row["invalidates_when"] = "知识观点或技术信号解除冲突"
    knowledge_effect = _text((row.get("knowledge_confirmation") or {}).get("effect"))
    chain_effect = _text((row.get("chain_context") or {}).get("effect"))
    if row.get("queue_lane") in ENTRY_QUEUE_LANES and knowledge_effect in {"block", "downgrade", "exit_priority"}:
        row["action_status"] = "knowledge_blocked" if knowledge_effect == "block" else "knowledge_downgraded"
        row["trader_action"] = "知识库提示暂不参与" if knowledge_effect == "block" else "知识库降级复核"
        row["next_action"] = row["trader_action"]
        row["queue_lane"] = "context_only" if knowledge_effect == "block" else "entry_waiting_confirm"
        row["actionability"] = "review_required" if knowledge_effect == "block" else "entry_waiting_confirm"
        row["invalidates_when"] = "知识库风险解除且买点确认重新出现"
    if row.get("queue_lane") in ENTRY_QUEUE_LANES and chain_effect in {"block", "exit_priority"}:
        row["action_status"] = "chain_risk_review"
        row["trader_action"] = "产业链风险复核"
        row["next_action"] = row["trader_action"]
        row["queue_lane"] = "risk_exit_first" if chain_effect == "exit_priority" else "context_only"
        row["actionability"] = "risk_exit_first" if chain_effect == "exit_priority" else "review_required"
        row["invalidates_when"] = "产业链退潮/高潮风险解除且5m/15m重新确认"


def _add_user_pinned(rows: dict[str, dict[str, Any]], index_codes: set[str], now) -> None:
    raw_values = os.getenv("TERMINAL_REALTIME_PRIORITY_CODES", "")
    values = raw_values.replace(";", ",").split(",") if raw_values.strip() else []
    trade_date = a_share_realtime_day_key(now=now)
    for value in values:
        code = _pure_a_code(value)
        _add_reason(rows, code, {
            "reason_type": "user_pinned",
            "source_collection": "config",
            "source_doc_id": "TERMINAL_REALTIME_PRIORITY_CODES",
            "signal_type": "手动关注",
            "signal_side": "neutral",
            "source_role": "context",
            "decision_effect": "context_only",
            "queue_lane": "context_only",
            "as_of": trade_date,
            "evidence": {"raw_value": _text(value)},
        }, index_codes=index_codes)


def _add_signal_rows(
    rows: dict[str, dict[str, Any]],
    db: Database,
    index_codes: set[str],
    *,
    include_generated_daily: bool = False,
    generated_daily_only: bool = False,
) -> None:
    cursor = db["signals"].find({}).sort([("signal_date", -1), ("updated_at", -1)]).limit(500)
    signals = list(cursor)
    by_code: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        code = _pure_a_code(signal.get("symbol"))
        if code:
            by_code.setdefault(code, []).append(signal)
    for signal in signals:
        generated_daily = _is_generated_daily_signal(signal)
        if generated_daily_only and not generated_daily:
            continue
        if generated_daily and not include_generated_daily:
            continue
        signal_type = _text(signal.get("signal_type") or signal.get("type") or signal.get("reason"))
        hard_screen_signal = _is_hard_screen_signal(signal) and not generated_daily
        reason_type = "technical_signal" if hard_screen_signal else _reason_type_for_signal(signal)
        if hard_screen_signal:
            reason_type = "technical_trigger"
        code = _pure_a_code(signal.get("symbol"))
        resonance_context = _screen_resonance_context(signal, by_code.get(code, [])) if hard_screen_signal else {}
        _add_reason(rows, signal.get("symbol"), {
            "reason_type": reason_type,
            "source_collection": "signals",
            "source_doc_id": _source_doc_id(signal),
            "signal_type": signal_type,
            "signal_side": _signal_side(signal),
            "signal_family": _signal_family(signal),
            "freq": _text(signal.get("freq") or signal.get("timeframe")),
            "score": 0 if reason_type == "historical_signal_record" else _float(signal.get("score") or signal.get("total_score")),
            "confidence": _float(signal.get("confidence")),
            "as_of": _text(signal.get("signal_date") or signal.get("updated_at"))[:10],
            "event_dt": _iso_dt(signal.get("signal_date") or signal.get("updated_at")),
            "source_role": "technical_trigger" if hard_screen_signal else "context",
            "decision_effect": "history_pending" if reason_type == "historical_signal_record" else ("confirm" if hard_screen_signal else "context_only"),
            "can_create_candidate": reason_type not in {"historical_signal_record", "custom_signal"},
            "backtest_quality": {
                "status": "not_evaluated",
                "score": 0,
                "source": "sqlite.backtest.signal_records",
            } if reason_type == "historical_signal_record" else {},
            "resonance_context": resonance_context,
            "evidence": {
                "source": _text(signal.get("source")),
                "dedupe_key": _text(signal.get("dedupe_key")),
                "details": signal.get("details_json") if isinstance(signal.get("details_json"), dict) else {},
                "resonance_context": resonance_context,
            },
        }, index_codes=index_codes, name=_text(signal.get("name")))


def _add_technical_signal_rows(rows: dict[str, dict[str, Any]], db: Database, index_codes: set[str]) -> None:
    limit = max(1, int(os.getenv("TERMINAL_POOL_TECHNICAL_SIGNAL_LIMIT", "20000")))
    latest = db["terminal_technical_signals"].find_one(
        {"market": "A", "as_of": {"$exists": True}},
        {"as_of": 1},
        sort=[("as_of", -1), ("updated_at", -1)],
    ) or {}
    query: dict[str, Any] = {"market": "A"}
    if latest.get("as_of"):
        query["as_of"] = latest.get("as_of")
    cursor = db["terminal_technical_signals"].find(
        query,
        {
            "symbol": 1,
            "raw_code": 1,
            "freq": 1,
            "signal_type": 1,
            "signal_side": 1,
            "signal_family": 1,
            "score": 1,
            "total_score": 1,
            "confidence": 1,
            "dt": 1,
            "as_of": 1,
            "updated_at": 1,
            "dedupe_key": 1,
            "ma_alignment": 1,
            "technical_evidence": 1,
            "resonance_context": 1,
            "invalidates_when": 1,
        },
    ).sort([("total_score", -1), ("confidence", -1), ("updated_at", -1)]).limit(limit)
    for signal in cursor:
        evidence = signal.get("technical_evidence") if isinstance(signal.get("technical_evidence"), dict) else {}
        ma_alignment = signal.get("ma_alignment") if isinstance(signal.get("ma_alignment"), dict) else {}
        if not ma_alignment and isinstance(evidence.get("ma_alignment"), dict):
            ma_alignment = evidence.get("ma_alignment") or {}
        resonance_context = signal.get("resonance_context") if isinstance(signal.get("resonance_context"), dict) else {}
        if not resonance_context and isinstance(evidence, dict):
            resonance_context = evidence.get("resonance_context") or {}
        _add_reason(rows, signal.get("symbol") or signal.get("raw_code"), {
            "reason_type": "technical_trigger",
            "source_collection": "terminal_technical_signals",
            "source_doc_id": _source_doc_id(signal),
            "signal_type": _text(signal.get("signal_type")),
            "signal_side": _text(signal.get("signal_side")) or _signal_side(signal),
            "signal_family": _text(signal.get("signal_family")) or "hard_technical",
            "freq": _text(signal.get("freq")),
            "score": _float(signal.get("total_score") or signal.get("score")),
            "confidence": _float(signal.get("confidence")),
            "as_of": _text(signal.get("as_of") or signal.get("updated_at"))[:10],
            "event_dt": _iso_dt(signal.get("dt")),
            "source_role": "technical_trigger",
            "ma_alignment": ma_alignment,
            "resonance_context": resonance_context,
            "evidence": evidence,
        }, index_codes=index_codes)


def _knowledge_status_for(sentiment: str, tech_side: str) -> tuple[str, str]:
    if not tech_side:
        return "knowledge_watch", "watch"
    if tech_side == "buy" and sentiment == "看多":
        return "knowledge_confirmed", "confirmed"
    if tech_side == "sell" and sentiment == "看空":
        return "knowledge_confirmed", "confirmed"
    if tech_side == "buy" and sentiment == "看空":
        return "knowledge_conflict", "conflict"
    if tech_side == "sell" and sentiment == "看多":
        return "knowledge_conflict", "conflict"
    return "knowledge_watch", "neutral"


def _add_knowledge_rows(rows: dict[str, dict[str, Any]], db: Database, index_codes: set[str]) -> None:
    cursor = db["knowledge_market_views"].find(
        {"market": "A", "target_type": "stock"},
        {"symbol": 1, "raw_code": 1, "sentiment": 1, "latest_sentiment": 1, "confidence": 1, "as_of": 1,
         "sources": 1, "catalysts": 1, "view_id": 1, "updated_at": 1},
    ).sort([("as_of", -1), ("updated_at", -1)]).limit(500)
    for view in cursor:
        code = _pure_a_code(view.get("symbol") or view.get("raw_code"))
        if not code:
            continue
        existing = rows.get(code)
        tech_side = ""
        if existing:
            tech_reason = next((item for item in existing.get("inclusion_reasons", []) if item.get("reason_type") == "technical_signal"), None)
            if not tech_reason:
                tech_reason = next((item for item in existing.get("inclusion_reasons", []) if _is_technical_reason(item)), None)
            if tech_reason:
                tech_side = _text(tech_reason.get("signal_side"))
        sentiment = _text(view.get("latest_sentiment") or view.get("sentiment"))
        reason_type, status = _knowledge_status_for(sentiment, tech_side)
        knowledge_effect = _text(view.get("knowledge_effect")) or ("block" if status == "conflict" else ("confirm" if status == "confirmed" else "context_only"))
        sources = view.get("sources") if isinstance(view.get("sources"), list) else []
        _add_reason(rows, code, {
            "reason_type": reason_type,
            "source_collection": "knowledge_market_views",
            "source_doc_id": _text(view.get("view_id")),
            "signal_type": f"知识库{sentiment or '覆盖'}",
            "signal_side": tech_side or "neutral",
            "source_role": "context",
            "decision_effect": knowledge_effect,
            "knowledge_effect": knowledge_effect,
            "can_create_candidate": False,
            "score": 0,
            "confidence": _float(view.get("confidence")),
            "as_of": _text(view.get("as_of") or view.get("updated_at"))[:10],
            "sentiment": sentiment,
            "knowledge_status": status,
            "evidence": {
                "sources": sources[:4],
                "catalysts": view.get("catalysts") if isinstance(view.get("catalysts"), list) else [],
                "policy": "confirm_conflict_degrade_only",
            },
        }, index_codes=index_codes)


BULLISH_WORDS = frozenset({"看好", "关注", "配置", "防御反击", "轮动", "有机会", "走强", "确定性", "低吸", "逢低", "启动", "修复", "主线", "重建", "升级", "机会"})
BEARISH_WORDS = frozenset({"回避", "不追", "高潮", "阴跌", "暂不参与", "结账", "减持", "风险", "见顶", "退潮", "降温", "补涨高潮", "左侧逆势", "分化"})


def _sector_board_map(db: Database) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    try:
        from signals.core.concept_carriers import load_industry_chains
        for chain in load_industry_chains().values():
            name = chain.get("name", "")
            if name:
                index.setdefault(name, []).append(name)
            for alias in chain.get("aliases") or []:
                index.setdefault(alias, []).append(chain["name"])
            for ind in chain.get("industries") or []:
                index.setdefault(ind, []).append(chain["name"])
    except Exception:
        pass
    hard = {
        "银行": ["银行"],
        "周期": ["煤炭", "石油", "有色", "钢铁", "化工"],
        "消费": ["食品饮料", "白酒", "家电", "汽车"],
        "医药": ["医药", "医疗器械", "中药", "创新药"],
        "科技": ["半导体", "机器人", "人工智能", "消费电子", "计算机"],
        "半导体": ["半导体"],
        "机器人": ["机器人"],
        "科创": ["科创50"],
        "创业板": ["创业板"],
        "恒科": ["恒生科技"],
        "煤炭": ["煤炭"],
        "石油": ["石油"],
        "券商": ["证券"],
        "光模块": ["光通信", "光模块"],
        "光通信": ["光通信", "光模块"],
        "中字头": ["中字头"],
        "中船": ["船舶"],
        "AI": ["人工智能"],
        "医药龙头": ["医药"],
    }
    for k, v in hard.items():
        existing = index.get(k, [])
        for b in v:
            if b not in existing:
                existing.append(b)
        index[k] = existing
    return index


def _direction_from_window(window: str) -> str:
    b = sum(1 for w in BULLISH_WORDS if w in window)
    s = sum(1 for w in BEARISH_WORDS if w in window)
    if b > s:
        return "bullish"
    if s > b:
        return "bearish"
    return ""


def _extract_sector_directions(body: str, keyword_index: dict[str, list[str]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for keyword, board_names in keyword_index.items():
        if len(keyword) < 2:
            continue
        pos = body.find(keyword)
        if pos < 0:
            continue
        window = body[max(0, pos - 250):pos + len(keyword) + 250]
        direction = _direction_from_window(window)
        if not direction:
            continue
        dedupe = f"{keyword}:{direction}"
        if dedupe in seen:
            continue
        seen.add(dedupe)
        results.append({
            "keyword": keyword,
            "board_names": board_names,
            "direction": direction,
            "snippet": window[:200],
        })
    return results


def _iter_review_notes(db: Database, since: date | None = None) -> list[dict[str, Any]]:
    from pathlib import Path

    try:
        from signals.sync.modules.knowledge_market_views import _vault_dir as _kvault, _parse_frontmatter
        vault_dir = _kvault()
    except Exception:
        vault_dir = Path.home() / "Desktop" / "知识库"
    inbox_dir = vault_dir / "10 Inbox" / "WeChat"
    if not inbox_dir.exists():
        return []
    keyword_index = _sector_board_map(db)
    notes: list[dict[str, Any]] = []
    for md_file in sorted(inbox_dir.rglob("*.md"), reverse=True):
        try:
            raw = md_file.read_text(encoding="utf-8", errors="ignore")[:80000]
        except Exception:
            continue
        meta, body = _parse_frontmatter(raw)
        title = meta.get("title", "")
        author = (meta.get("author_focus") or "").lower()
        if not author:
            combined = title + body[:4000]
            if "胖哥" in combined:
                author = "pangge"
            elif "道长" in combined:
                author = "daozhang"
        if author not in ("daozhang", "pangge"):
            continue
        created = meta.get("created_at", "")
        if since and created:
            try:
                note_date = datetime.fromisoformat(created.replace("Z", "+00:00")).date()
                if note_date < since:
                    continue
            except Exception:
                pass
        sectors = _extract_sector_directions(body, keyword_index)
        if not sectors:
            continue
        notes.append({
            "title": title,
            "author": author,
            "date": created[:10] if created else "",
            "sectors": sectors,
        })
    return notes


def _add_review_clue_rows(rows: dict[str, dict[str, Any]], db: Database, index_codes: set[str]) -> None:
    notes = _iter_review_notes(db)
    if not notes:
        return
    for note in notes:
        author = note["author"]
        author_label = "道长" if author == "daozhang" else "胖哥"
        for sector in note["sectors"]:
            if sector["direction"] != "bullish":
                continue
            for board_name in sector["board_names"]:
                doc = db["board_constituents"].find_one(
                    {"$or": [{"_id": board_name}, {"board_name": board_name}, {"name": board_name}]},
                    {"symbols": 1, "stock_names": 1},
                    sort=[("updated_at", -1)],
                ) or {}
                symbols = list(doc.get("symbols") or [])
                stock_names = dict(doc.get("stock_names") or {})
                if not symbols:
                    doc2 = db["concept_constituents"].find_one(
                        {"$or": [{"concept_name": board_name}, {"board_name": board_name}, {"name": board_name}]},
                        {"symbols": 1, "stock_names": 1},
                        sort=[("updated_at", -1)],
                    ) or {}
                    symbols = list(doc2.get("symbols") or [])
                    stock_names = dict(doc2.get("stock_names") or {})
                for symbol in symbols[:8]:
                    name = stock_names.get(symbol, "")
                    _add_reason(rows, symbol, {
                        "reason_type": "review_sector_bullish",
                        "source_collection": "review_sector_clues",
                        "source_doc_id": note.get("title", ""),
                        "signal_type": f"{author_label}看好{board_name}",
                        "signal_side": "buy",
                        "source_role": "review_clue",
                        "decision_effect": "context_only",
                        "can_create_candidate": True,
                        "board_or_concept": board_name,
                        "evidence": {
                            "author": author,
                            "review_date": note.get("date", ""),
                            "sector_keyword": sector.get("keyword", ""),
                            "snippet": sector.get("snippet", ""),
                        },
                    }, index_codes=index_codes, name=name)


def _has_clue_source(row: dict[str, Any]) -> bool:
    for reason in row.get("inclusion_reasons") or []:
        if not isinstance(reason, dict):
            continue
        rt = _text(reason.get("reason_type"))
        if rt in REVIEW_CLUE_REASON_TYPES or rt in {"user_pinned", "chain_core_rep", "chain_elastic_rep", "source_leader", "knowledge_confirmed", "knowledge_watch", "fallback_watch"}:
            return True
    return False


def _entry_gate_passed(row: dict[str, Any]) -> bool:
    passed, _, _, _, _ = _entry_gate(row)
    return passed


def _gate_progress(row: dict[str, Any]) -> int:
    buy_reasons = _buy_technical_reasons(row)
    risk_reasons = _risk_reasons(row)
    if risk_reasons:
        return 0
    chain_blocker = _chain_entry_blocker(row)
    if chain_blocker:
        return 0
    if not buy_reasons:
        return 0
    freqs: set[str] = set()
    for reason in buy_reasons:
        freqs.update(_reason_freqs(reason))
    score = 0
    if any(_is_30m_freq(f) for f in freqs):
        score += 2
    if any(_is_upper_freq(f) for f in freqs):
        score += 2
    if any(_is_right_side_freq(f) for f in freqs):
        score += 2
    if any(_is_entry_partner_freq(f) for f in freqs):
        score += 1
    current_buy = [r for r in buy_reasons if _reason_is_current_for_entry(r)]
    if current_buy:
        score += 1
    return score


def _clue_quality_score(row: dict[str, Any]) -> float:
    source_score = 0.0
    for reason in row.get("inclusion_reasons") or []:
        if not isinstance(reason, dict):
            continue
        rt = _text(reason.get("reason_type"))
        if rt == "review_sector_bullish":
            source_score = max(source_score, 50.0)
        elif rt == "user_pinned":
            source_score = max(source_score, 30.0)
        elif rt in {"chain_core_rep", "source_leader"}:
            source_score = max(source_score, 25.0)
        elif rt == "knowledge_confirmed":
            source_score = max(source_score, 15.0)
    tech_proximity = float(_gate_progress(row)) * 8.0
    theme_bonus = _mainline_alignment_score(row)
    return round(source_score + tech_proximity + theme_bonus, 3)


def _latest_strategy_snapshot(db: Database) -> dict[str, Any]:
    doc = db["strategy_snapshots"].find_one(
        {"snapshot": {"$exists": True}},
        {"snapshot": 1, "as_of": 1, "updated_at": 1, "_id": 1},
        sort=[("updated_at", -1), ("as_of", -1)],
    ) or {}
    snapshot = doc.get("snapshot") or {}
    snapshot["_source_doc_id"] = _text(doc.get("_id"))
    snapshot["_as_of"] = _text(doc.get("as_of") or doc.get("updated_at"))[:10]
    return snapshot


def _add_strategy_rows(rows: dict[str, dict[str, Any]], db: Database, index_codes: set[str]) -> None:
    snapshot = _latest_strategy_snapshot(db)
    source_doc_id = _text(snapshot.get("_source_doc_id")) or "latest"
    as_of = _text(snapshot.get("_as_of"))
    for key in ("warnings", "candidates", "decision_queue"):
        for item in snapshot.get(key) or []:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            signal_type = _text(item.get("reason") or metadata.get("trigger") or item.get("signal_type"))
            if not signal_type:
                continue
            source = _text(metadata.get("source") or item.get("source"))
            probe = {**item, "source": source, "signal_type": signal_type, "pool_status": "warning" if key == "warnings" else item.get("status")}
            signal_side = _signal_side(probe)
            reason_type = "generated_risk_signal" if signal_side == "sell" or key == "warnings" else "fallback_watch"
            _add_reason(rows, item.get("symbol") or item.get("code"), {
                "reason_type": reason_type,
                "source_collection": "strategy_snapshots",
                "source_doc_id": source_doc_id,
                "signal_type": signal_type,
                "signal_side": signal_side,
                "signal_family": "fallback_candidate" if reason_type == "fallback_watch" else _signal_family(probe),
                "freq": _text(metadata.get("freq") or item.get("freq")),
                "score": _float(item.get("score") or metadata.get("score")),
                "confidence": _float(item.get("confidence") or metadata.get("confidence")),
                "board_or_concept": _text(metadata.get("theme")),
                "as_of": as_of,
                "source_role": "fallback" if reason_type == "fallback_watch" else "technical_trigger",
                "decision_effect": "fallback_watch" if reason_type == "fallback_watch" else "exit_priority",
                "actionability": "observe_only" if reason_type == "fallback_watch" else "risk_exit_first",
                "queue_lane": "fallback_watch" if reason_type == "fallback_watch" else "risk_exit_first",
                "evidence": metadata.get("evidence") if isinstance(metadata.get("evidence"), dict) else {"source": source},
            }, index_codes=index_codes, name=_text(item.get("name")))


def _latest_chain_rows(db: Database, limit: int = 24) -> list[dict[str, Any]]:
    latest = db["chain_heat_snapshots"].find_one({"market": "A"}, {"trade_minute": 1}, sort=[("trade_minute", -1)])
    if not latest or latest.get("trade_minute") is None:
        return []
    return list(db["chain_heat_snapshots"].find(
        {"market": "A", "trade_minute": latest["trade_minute"]},
        {"_id": 0},
    ).sort("rank", 1).limit(limit))


def _constituents_for_domain(db: Database, domain: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    kind = _text(domain.get("kind"))
    name = _text(domain.get("name"))
    if not kind or not name:
        return [], {}
    if kind == "concept":
        doc = db["concept_constituents"].find_one(
            {"$or": [{"concept_name": name}, {"board_name": name}, {"name": name}]},
            {"symbols": 1, "stock_names": 1},
            sort=[("updated_at", -1)],
        ) or {}
    else:
        doc = db["board_constituents"].find_one(
            {"$or": [{"board_name": name}, {"name": name}]},
            {"symbols": 1, "stock_names": 1},
            sort=[("updated_at", -1)],
        ) or {}
    return list(doc.get("symbols") or []), dict(doc.get("stock_names") or {})


def _add_chain_rows(rows: dict[str, dict[str, Any]], db: Database, index_codes: set[str]) -> None:
    added_constituents = 0
    for chain in _latest_chain_rows(db):
        chain_key = f"{chain.get('chain_id')}:{chain.get('node_id')}:{chain.get('trade_minute')}"
        board_or_concept = ""
        integrated = chain.get("integrated_domains") if isinstance(chain.get("integrated_domains"), list) else []
        if integrated:
            board_or_concept = _text(integrated[0].get("name"))
        for rep in chain.get("representatives") or []:
            if not isinstance(rep, dict):
                continue
            rep_type = "chain_core_rep" if _text(rep.get("representative_type")) == "core" else "chain_elastic_rep"
            phase = _text(chain.get("phase"))
            chain_effect = _chain_decision_effect(phase)
            _add_reason(rows, rep.get("symbol"), {
                "reason_type": rep_type,
                "source_collection": "chain_heat_snapshots",
                "source_doc_id": chain_key,
                "signal_type": _text(chain.get("trading_signal")),
                "signal_side": "neutral",
                "source_role": "context",
                "decision_effect": chain_effect,
                "can_create_candidate": True,
                "score": _float(chain.get("heat_score")),
                "confidence": _float(rep.get("priority")),
                "chain_id": _text(chain.get("chain_id")),
                "node_id": _text(chain.get("node_id")),
                "board_or_concept": board_or_concept,
                "as_of": _text(chain.get("trade_minute")),
                "evidence": {
                    "phase": phase,
                    "range_pattern": chain.get("range_pattern"),
                    "change_pct": chain.get("change_pct"),
                    "up_count": chain.get("up_count"),
                    "down_count": chain.get("down_count"),
                    "rank": chain.get("rank"),
                    "leader_change_pct": chain.get("leader_change_pct"),
                    "heat_score": chain.get("heat_score"),
                    "momentum_5m": chain.get("momentum_5m"),
                    "momentum_15m": chain.get("momentum_15m"),
                    "momentum_30m": chain.get("momentum_30m"),
                    "mapping_confidence": chain.get("mapping_confidence"),
                    "integrated_count": chain.get("integrated_count"),
                },
            }, index_codes=index_codes, name=_text(rep.get("name")))
        for domain in integrated[:6]:
            if added_constituents >= 36:
                break
            leader_symbol = _text(domain.get("leader_symbol"))
            if leader_symbol:
                _add_reason(rows, leader_symbol, {
                    "reason_type": "source_leader",
                    "source_collection": "chain_heat_snapshots",
                    "source_doc_id": chain_key,
                    "signal_type": _text(chain.get("trading_signal")),
                    "signal_side": "neutral",
                    "source_role": "context",
                    "decision_effect": _chain_decision_effect(_text(chain.get("phase"))),
                    "can_create_candidate": True,
                    "score": _float(domain.get("leader_change_pct")),
                    "confidence": _float(domain.get("mapping_confidence")),
                    "chain_id": _text(chain.get("chain_id")),
                    "node_id": _text(chain.get("node_id")),
                    "board_or_concept": _text(domain.get("name")),
                    "as_of": _text(chain.get("trade_minute")),
                    "evidence": {
                        "phase": chain.get("phase"),
                        "range_pattern": chain.get("range_pattern"),
                        "rank": chain.get("rank"),
                        "heat_score": chain.get("heat_score"),
                        "leader_change_pct": chain.get("leader_change_pct"),
                        "momentum_5m": chain.get("momentum_5m"),
                        "momentum_15m": chain.get("momentum_15m"),
                        "momentum_30m": chain.get("momentum_30m"),
                    },
                }, index_codes=index_codes, name=_text(domain.get("leader_name")))
            symbols, stock_names = _constituents_for_domain(db, domain)
            for symbol in symbols[:2]:
                if added_constituents >= 36:
                    break
                code = _pure_a_code(symbol)
                _add_reason(rows, code, {
                    "reason_type": "constituent_hot",
                    "source_collection": "board_constituents" if domain.get("kind") == "industry" else "concept_constituents",
                    "source_doc_id": _text(domain.get("name")),
                    "signal_type": _text(chain.get("trading_signal")),
                    "signal_side": "neutral",
                    "source_role": "context",
                    "decision_effect": _chain_decision_effect(_text(chain.get("phase"))),
                    "can_create_candidate": False,
                    "heat_score": _float(chain.get("heat_score")),
                    "confidence": _float(domain.get("mapping_confidence")),
                    "chain_id": _text(chain.get("chain_id")),
                    "node_id": _text(chain.get("node_id")),
                    "board_or_concept": _text(domain.get("name")),
                    "as_of": _text(chain.get("trade_minute")),
                    "evidence": {
                        "phase": chain.get("phase"),
                        "range_pattern": chain.get("range_pattern"),
                        "rank": chain.get("rank"),
                        "heat_score": chain.get("heat_score"),
                        "momentum_5m": chain.get("momentum_5m"),
                        "momentum_15m": chain.get("momentum_15m"),
                        "momentum_30m": chain.get("momentum_30m"),
                    },
                }, index_codes=index_codes, name=stock_names.get(code, ""))
                added_constituents += 1


def _latest_chain_membership_trade_date(db: Database) -> str:
    doc = db["security_chain_memberships"].find_one(
        {"trade_date": {"$exists": True}},
        {"trade_date": 1},
        sort=[("trade_date", -1), ("updated_at", -1)],
    ) or {}
    return _text(doc.get("trade_date"))


def _latest_chain_rollups(db: Database, limit: int = 48) -> list[dict[str, Any]]:
    latest = db["chain_node_security_rollups"].find_one(
        {"trade_date": {"$exists": True}},
        {"trade_date": 1},
        sort=[("trade_date", -1), ("updated_at", -1)],
    ) or {}
    trade_date = _text(latest.get("trade_date"))
    if not trade_date:
        return []
    return list(db["chain_node_security_rollups"].find(
        {"trade_date": trade_date, "market": "A"},
        {"_id": 0},
    ).sort([("heat_score", -1), ("covered_security_count", -1), ("avg_confidence", -1)]).limit(limit))


def _latest_chain_heat_by_node(db: Database) -> dict[tuple[str, str], dict[str, Any]]:
    latest = db["chain_heat_snapshots"].find_one({"market": "A"}, {"trade_minute": 1}, sort=[("trade_minute", -1)]) or {}
    trade_minute = latest.get("trade_minute")
    if not trade_minute:
        return {}
    return {
        (_text(row.get("chain_id")), _text(row.get("node_id"))): row
        for row in db["chain_heat_snapshots"].find(
            {"market": "A", "trade_minute": trade_minute},
            {"_id": 0},
        )
    }


def _chain_phase_from_rollup(rollup: dict[str, Any], heat_by_node: dict[tuple[str, str], dict[str, Any]]) -> str:
    heat = heat_by_node.get((_text(rollup.get("chain_id")), _text(rollup.get("node_id"))), {})
    return _text(heat.get("phase") or rollup.get("phase"))


def _chain_effect_from_phase(phase: str) -> str:
    if phase == "consensus_climax":
        return "exit_priority"
    if phase == "risk_off":
        return "exit_priority"
    if phase == "accelerating":
        return "confirm"
    return "context_only"


def _membership_reason_from_security(
    *,
    security: dict[str, Any],
    rollup: dict[str, Any],
    phase: str,
    can_create_candidate: bool,
) -> dict[str, Any]:
    evidence_sources = [item for item in security.get("evidence_sources") or [] if _text(item)]
    signal_type = {
        "accelerating": "产业链主线加速",
        "warming": "产业链升温",
        "consensus_climax": "产业链一致高潮",
        "cooling": "产业链退潮观察",
        "diverging": "产业链分化观察",
        "risk_off": "产业链风险退潮",
    }.get(phase, "产业链归属")
    effect = _chain_effect_from_phase(phase)
    return {
        "reason_type": "chain_membership",
        "source_collection": "security_chain_memberships",
        "source_doc_id": _text(security.get("security_id")) or _text(security.get("symbol")),
        "signal_type": signal_type,
        "signal_side": "neutral",
        "source_role": "context",
        "decision_effect": effect,
        "can_create_candidate": can_create_candidate,
        "score": _float(rollup.get("heat_score")),
        "confidence": _float(security.get("confidence")),
        "membership_confidence": _float(security.get("confidence")),
        "exposure_score": _float(security.get("exposure_score")),
        "chain_id": _text(rollup.get("chain_id")),
        "chain_name": _text(rollup.get("chain_name")),
        "node_id": _text(rollup.get("node_id")),
        "node_name": _text(rollup.get("node_name")),
        "layer": _text(rollup.get("layer")),
        "stage": _text(rollup.get("stage")),
        "membership_type": _text(security.get("membership_type")),
        "board_or_concept": _text(rollup.get("node_name") or rollup.get("chain_name")),
        "evidence_sources": evidence_sources,
        "as_of": _text(rollup.get("trade_date")),
        "evidence": {
            "phase": phase,
            "rank": rollup.get("rank") or rollup.get("coverage_rank"),
            "heat_score": rollup.get("heat_score"),
            "covered_security_count": rollup.get("covered_security_count"),
            "primary_security_count": rollup.get("primary_security_count"),
            "membership_type": security.get("membership_type"),
            "confidence": security.get("confidence"),
            "exposure_score": security.get("exposure_score"),
            "evidence_sources": evidence_sources,
            "source_boards": [
                item for item in security.get("source_boards") or []
                if isinstance(item, dict)
            ],
            "source_policy": "postmarket_chain_rebuild",
        },
        "source_boards": [
            item for item in security.get("source_boards") or []
            if isinstance(item, dict)
        ],
    }


def _add_chain_membership_rows(rows: dict[str, dict[str, Any]], db: Database, index_codes: set[str]) -> None:
    heat_by_node = _latest_chain_heat_by_node(db)
    added = 0
    for rollup in _latest_chain_rollups(db, limit=48):
        phase = _chain_phase_from_rollup(rollup, heat_by_node)
        # Do not create a broad clue from cold taxonomy-only rollups. Existing
        # technical rows are still enriched by `_attach_membership_context`.
        hot_or_actionable = phase in {"accelerating", "warming", "consensus_climax", "cooling", "diverging", "risk_off"} or _float(rollup.get("heat_score")) > 0
        if not hot_or_actionable:
            continue
        per_rollup_limit = 6 if phase in {"accelerating", "warming"} else 4
        for security in (rollup.get("top_securities") or [])[:per_rollup_limit]:
            if not isinstance(security, dict):
                continue
            reason = _membership_reason_from_security(
                security=security,
                rollup=rollup,
                phase=phase,
                can_create_candidate=True,
            )
            _add_reason(rows, security.get("symbol") or security.get("raw_code"), reason, index_codes=index_codes, name=_text(security.get("name")))
            added += 1
            if added >= 160:
                return


def _attach_membership_context(rows: dict[str, dict[str, Any]], db: Database, index_codes: set[str]) -> None:
    if not rows:
        return
    trade_date = _latest_chain_membership_trade_date(db)
    if not trade_date:
        return
    codes = sorted(rows)
    cursor = db["security_chain_memberships"].find(
        {"trade_date": trade_date, "raw_code": {"$in": codes}},
        {"_id": 0},
    ).sort([("is_primary_chain", -1), ("exposure_score", -1), ("confidence", -1)])
    best_by_code: dict[str, dict[str, Any]] = {}
    for row in cursor:
        code = _text(row.get("raw_code"))
        if code and code not in best_by_code:
            best_by_code[code] = row
    heat_by_node = _latest_chain_heat_by_node(db)
    for code, membership in best_by_code.items():
        phase = _text((heat_by_node.get((_text(membership.get("chain_id")), _text(membership.get("node_id")))) or {}).get("phase")) or "mapped"
        rollup = {
            "trade_date": trade_date,
            "chain_id": membership.get("chain_id"),
            "chain_name": membership.get("chain_name"),
            "node_id": membership.get("node_id"),
            "node_name": membership.get("node_name"),
            "layer": membership.get("layer"),
            "stage": membership.get("stage"),
            "phase": phase,
            "heat_score": 0,
            "covered_security_count": 0,
        }
        security = {
            "security_id": membership.get("security_id"),
            "symbol": membership.get("symbol"),
            "raw_code": membership.get("raw_code"),
            "name": membership.get("name"),
            "membership_type": membership.get("membership_type"),
            "confidence": membership.get("confidence"),
            "exposure_score": membership.get("exposure_score"),
            "evidence_sources": membership.get("evidence_sources") or [],
        }
        reason = _membership_reason_from_security(
            security=security,
            rollup=rollup,
            phase=phase,
            can_create_candidate=False,
        )
        _add_reason(rows, code, reason, index_codes=index_codes, name=_text(membership.get("name")))


def _add_active_pool(rows: dict[str, dict[str, Any]], db: Database, index_codes: set[str]) -> None:
    doc = db["market_pools"].find_one({"pool": "active"}, {"symbols": 1, "items": 1, "dt": 1, "updated_at": 1}, sort=[("dt", -1), ("updated_at", -1)]) or {}
    as_of = _text(doc.get("dt") or doc.get("updated_at"))[:10]
    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        _add_reason(rows, item.get("symbol") or item.get("code"), {
            "reason_type": "active_pool_watch",
            "source_collection": "market_pools",
            "source_doc_id": "active",
            "signal_type": "活跃池观察",
            "signal_side": "buy",
            "as_of": as_of,
            "evidence": {"sources": item.get("sources") or []},
        }, index_codes=index_codes)
    for symbol in doc.get("symbols") or []:
        _add_reason(rows, symbol, {
            "reason_type": "active_pool_watch",
            "source_collection": "market_pools",
            "source_doc_id": "active",
            "signal_type": "活跃池观察",
            "signal_side": "buy",
            "as_of": as_of,
        }, index_codes=index_codes)


def _add_fallback_watch_rows(rows: dict[str, dict[str, Any]], db: Database, index_codes: set[str], *, limit: int, now) -> int:
    if limit <= 0:
        return 0
    before = len(rows)
    trade_date = a_share_realtime_day_key(now=now)
    snapshot = _latest_strategy_snapshot(db)
    source_doc_id = _text(snapshot.get("_source_doc_id")) or "latest"
    as_of = _text(snapshot.get("_as_of")) or trade_date
    for item in snapshot.get("candidates") or []:
        if len(rows) - before >= limit:
            break
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        signal_type = _text(item.get("reason") or metadata.get("trigger") or item.get("latest_signal") or "买点候选")
        _add_reason(rows, item.get("symbol") or item.get("code"), {
            "reason_type": "fallback_watch",
            "source_collection": "strategy_snapshots",
            "source_doc_id": source_doc_id,
            "signal_type": signal_type,
            "signal_side": "buy",
            "signal_family": "fallback_candidate",
            "freq": _text(metadata.get("freq") or item.get("freq")),
            "score": _float(item.get("score") or metadata.get("score")),
            "confidence": _float(item.get("confidence") or metadata.get("confidence")),
            "board_or_concept": _text(metadata.get("theme")),
            "as_of": as_of,
            "source_role": "fallback",
            "decision_effect": "fallback_watch",
            "actionability": "observe_only",
            "queue_lane": "fallback_watch",
            "evidence": metadata.get("evidence") if isinstance(metadata.get("evidence"), dict) else {"policy": "strict_pool_fallback"},
        }, index_codes=index_codes, name=_text(item.get("name")))

    if len(rows) - before >= limit:
        return len(rows) - before

    doc = db["market_pools"].find_one(
        {"pool": "active"},
        {"symbols": 1, "items": 1, "dt": 1, "updated_at": 1},
        sort=[("dt", -1), ("updated_at", -1)],
    ) or {}
    active_as_of = _text(doc.get("dt") or doc.get("updated_at"))[:10] or trade_date
    active_items = [item for item in doc.get("items") or [] if isinstance(item, dict)]
    if not active_items:
        active_items = [{"symbol": symbol} for symbol in doc.get("symbols") or []]
    for item in active_items:
        if len(rows) - before >= limit:
            break
        _add_reason(rows, item.get("symbol") or item.get("code"), {
            "reason_type": "fallback_watch",
            "source_collection": "market_pools",
            "source_doc_id": "active",
            "signal_type": "活跃池降级观察",
            "signal_side": "buy",
            "signal_family": "fallback_active_pool",
            "score": _float(item.get("score") or item.get("total_score")),
            "confidence": _float(item.get("confidence")),
            "as_of": active_as_of,
            "source_role": "fallback",
            "decision_effect": "fallback_watch",
            "actionability": "observe_only",
            "queue_lane": "fallback_watch",
            "evidence": {"sources": item.get("sources") or [], "policy": "strict_pool_fallback"},
        }, index_codes=index_codes, name=_text(item.get("name") or item.get("stock_name")))
    return len(rows) - before


def _add_recent_opened(rows: dict[str, dict[str, Any]], db: Database, index_codes: set[str]) -> None:
    cursor = db["sync_log"].find(
        {"module": {"$in": ["stock_minute", "stock_daily"]}, "status": "ok", "symbol": {"$exists": True}},
        {"symbol": 1, "module": 1, "last_run": 1},
    ).sort("last_run", -1).limit(120)
    for doc in cursor:
        _add_reason(rows, doc.get("symbol"), {
            "reason_type": "recent_opened",
            "source_collection": "sync_log",
            "source_doc_id": _text(doc.get("module")),
            "signal_type": "近期终端/同步观察",
            "signal_side": "buy",
            "as_of": _text(doc.get("last_run")),
        }, index_codes=index_codes)


def _top_heat_names(db: Database, kind: str, limit: int) -> list[str]:
    docs = list(db["board_heat_ticks"].find(
        {"kind": kind},
        {"name": 1, "trade_minute": 1, "rank_idx": 1},
    ).sort([("trade_minute", -1), ("rank_idx", 1)]).limit(limit * 4))
    names: list[str] = []
    for doc in docs:
        name = _text(doc.get("name"))
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


def _reason_freqs(reason: dict[str, Any]) -> set[str]:
    freqs = {_text(reason.get("freq"))}
    context = reason.get("resonance_context") if isinstance(reason.get("resonance_context"), dict) else {}
    for key in ("aligned_freqs", "conflict_freqs"):
        for freq in context.get(key) or []:
            if _text(freq):
                freqs.add(_text(freq))
    evidence = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
    nested = evidence.get("resonance_context") if isinstance(evidence.get("resonance_context"), dict) else {}
    for key in ("aligned_freqs", "conflict_freqs"):
        for freq in nested.get(key) or []:
            if _text(freq):
                freqs.add(_text(freq))
    return {freq for freq in freqs if freq}


def _buy_technical_reasons(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        reason
        for reason in row.get("inclusion_reasons") or []
        if isinstance(reason, dict) and _is_technical_reason(reason) and _text(reason.get("signal_side")) == "buy"
    ]


def _current_buy_technical_reasons(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [reason for reason in _buy_technical_reasons(row) if _reason_is_current_for_entry(reason)]


def _is_fresh_buy_technical_reason(reason: dict[str, Any]) -> bool:
    return (
        isinstance(reason, dict)
        and _is_technical_reason(reason)
        and _text(reason.get("signal_side")) == "buy"
        and _reason_is_current_for_entry(reason)
    )


def _is_default_candidate_anchor_reason(reason: dict[str, Any]) -> bool:
    return _is_fresh_buy_technical_reason(reason) and _text(reason.get("freq")) in DEFAULT_CANDIDATE_ANCHOR_FREQS


def _is_strong_30m_anchor_reason(reason: dict[str, Any]) -> bool:
    if not _is_default_candidate_anchor_reason(reason) or not _is_30m_freq(reason.get("freq")):
        return False
    text = _reason_signal_text(reason)
    weak_tokens = ("MACD绿柱扩大", "MACD绿柱缩小", "零上绿柱扩大", "零下绿柱缩小", "缺口买:普通")
    if any(token in text for token in weak_tokens):
        return False
    strong_tokens = (
        "一买",
        "二买",
        "三买",
        "趋势买",
        "背驰买",
        "底背离",
        "形态",
        "头肩底",
        "双底",
        "上升三角",
        "突破",
        "缺口买:持续",
        "缺口买:突破",
        "trend_breakout",
        "candle_run",
        "candle_accel",
        "200d_new_high_breakout",
        "200日新高",
        "新高突破",
    )
    return any(token in text for token in strong_tokens)


def _has_default_candidate_anchor(row: dict[str, Any]) -> bool:
    anchors = [
        reason
        for reason in row.get("inclusion_reasons") or []
        if _is_default_candidate_anchor_reason(reason)
    ]
    if any(_is_upper_freq(reason.get("freq")) for reason in anchors):
        return True
    return any(_is_strong_30m_anchor_reason(reason) for reason in anchors)


def _is_st_stock(row: dict[str, Any]) -> bool:
    text = f"{_text(row.get('name'))} {_text(row.get('symbol'))} {_text(row.get('code'))}".upper()
    return " ST" in f" {text}" or "*ST" in text


def _default_opportunity_candidate_rows(
    rows: dict[str, dict[str, Any]],
    *,
    include_st: bool = False,
) -> dict[str, dict[str, Any]]:
    """Default opportunity candidates need a fresh 30m/daily/weekly buy anchor.

    Fresh 5m/15m signals are kept only as execution-period evidence for anchored
    symbols; stale buy technical reasons are dropped so old context cannot lift a
    row back into the visible pools.
    """
    filtered: dict[str, dict[str, Any]] = {}
    for code, row in rows.items():
        if not include_st and _is_st_stock(row):
            continue
        if not _has_default_candidate_anchor(row):
            continue
        kept_reasons = []
        for reason in row.get("inclusion_reasons") or []:
            if _is_technical_reason(reason) and _text(reason.get("signal_side")) == "buy":
                if not _is_fresh_buy_technical_reason(reason):
                    continue
            kept_reasons.append(reason)
        cloned = dict(row)
        cloned["inclusion_reasons"] = kept_reasons
        filtered[code] = cloned
    return filtered


def _timeframe_priority_for_freq(freq: Any) -> float:
    text = _text(freq)
    if text in {"日线", "daily", "1d", "D", "d"}:
        return 34.0
    if _is_30m_freq(text):
        return 28.0
    if text in {"周线", "weekly", "1w", "W", "w"}:
        return 20.0
    if _is_right_side_freq(text):
        return 12.0
    return 0.0


def _reason_timeframe_bucket(freq: Any) -> str:
    text = _text(freq)
    if text in {"日线", "daily", "1d", "D", "d"}:
        return "daily"
    if _is_30m_freq(text):
        return "30m"
    if text in {"周线", "weekly", "1w", "W", "w"}:
        return "weekly"
    if _is_right_side_freq(text):
        return "execution"
    return ""


def _current_reason_freqs(reasons: list[dict[str, Any]]) -> set[str]:
    freqs: set[str] = set()
    for reason in reasons:
        freq = _text(reason.get("freq"))
        if freq:
            freqs.add(freq)
    return {freq for freq in freqs if freq}


def _timeframe_priority_score(reasons: list[dict[str, Any]]) -> float:
    return max((_timeframe_priority_for_freq(freq) for freq in _current_reason_freqs(reasons)), default=0.0)


def _multi_period_score(reasons: list[dict[str, Any]]) -> float:
    buckets = {
        bucket
        for bucket in (_reason_timeframe_bucket(freq) for freq in _current_reason_freqs(reasons))
        if bucket
    }
    if len(buckets) < 2:
        return 0.0
    return min(24.0, (len(buckets) - 1) * 8.0)


def _indicator_breadth_score(reasons: list[dict[str, Any]]) -> float:
    labels = {
        _reason_signal_text(reason)
        for reason in reasons
        if _reason_signal_text(reason)
    }
    families = {
        _technical_signal_family(reason)
        for reason in reasons
        if _technical_signal_family(reason)
    }
    family_bonus = max(0, len(families) - 1) * 2.0
    return min(24.0, len(labels) * 4.0 + family_bonus)


def _truthy_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return _text(value).lower() in {"1", "true", "yes", "on", "y", "站上", "是"}


def _reason_ma_alignment(reason: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(reason, dict):
        return {}
    if isinstance(reason.get("ma_alignment"), dict):
        return reason.get("ma_alignment") or {}
    evidence = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
    if isinstance(evidence.get("ma_alignment"), dict):
        return evidence.get("ma_alignment") or {}
    technical = reason.get("technical_evidence") if isinstance(reason.get("technical_evidence"), dict) else {}
    if isinstance(technical.get("ma_alignment"), dict):
        return technical.get("ma_alignment") or {}
    return {}


def _best_ma_alignment(
    row: dict[str, Any],
    reasons: list[dict[str, Any]] | None = None,
    *,
    include_row_level: bool = True,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    if include_row_level and isinstance(row.get("ma_alignment"), dict):
        candidates.append(row["ma_alignment"])
    technical = row.get("technical_evidence") if isinstance(row.get("technical_evidence"), dict) else {}
    if include_row_level and isinstance(technical.get("ma_alignment"), dict):
        candidates.append(technical["ma_alignment"])
    reason_list = _buy_technical_reasons(row) if reasons is None else reasons
    for reason in reason_list:
        ma = _reason_ma_alignment(reason)
        if ma:
            candidates.append(ma)
    return max(candidates, key=lambda item: _float(item.get("score")), default={})


def _ma_alignment_score_from_alignment(ma: dict[str, Any]) -> float:
    if not isinstance(ma, dict) or not ma:
        return 0.0
    score = 0.0
    explicit = _float(ma.get("score"))
    weights = {5: 6.0, 10: 11.0, 20: 18.0}
    reclaim_weights = {5: 3.0, 10: 5.0, 20: 8.0}
    has_primary_field = False
    for period, weight in weights.items():
        has_primary_field = has_primary_field or any(key in ma for key in (f"above_ma{period}", f"near_ma{period}", f"reclaim_ma{period}"))
        if _truthy_bool(ma.get(f"above_ma{period}")):
            score += weight
        elif _truthy_bool(ma.get(f"near_ma{period}")):
            score += weight * 0.45
        if _truthy_bool(ma.get(f"reclaim_ma{period}")):
            score += reclaim_weights[period]
    if _text(ma.get("ma_stack")) == "bullish":
        score += 5.0
    elif _text(ma.get("ma_stack")) == "bearish":
        score -= 4.0
    direction = _text(ma.get("ma20_direction")).lower()
    if direction in {"向上", "up", "upward", "rising"}:
        score += 3.0
    elif direction in {"向下", "down", "downward", "falling"}:
        score -= 3.0
    if not has_primary_field and explicit:
        score = explicit
    return round(max(0.0, min(45.0, score)), 3)


def _fib_ma_support_score_from_alignment(ma: dict[str, Any]) -> float:
    if not isinstance(ma, dict) or not ma:
        return 0.0
    explicit = _float(ma.get("fib_support_score"))
    weights = {8: 2.0, 13: 2.5, 21: 3.5, 34: 4.0, 55: 5.0, 89: 6.0}
    score = 0.0
    has_field = False
    for period, weight in weights.items():
        has_field = has_field or any(key in ma for key in (f"above_ma{period}", f"near_ma{period}", f"reclaim_ma{period}"))
        if _truthy_bool(ma.get(f"near_ma{period}")):
            score += weight
        elif _truthy_bool(ma.get(f"above_ma{period}")):
            score += weight * 0.55
        if _truthy_bool(ma.get(f"reclaim_ma{period}")):
            score += min(4.0, weight)
    if explicit:
        score = max(score, explicit)
    if not has_field and not explicit:
        return 0.0
    return round(min(18.0, score), 3)


def _ma_alignment_score(
    row: dict[str, Any],
    reasons: list[dict[str, Any]] | None = None,
    *,
    include_row_level: bool = True,
) -> float:
    return _ma_alignment_score_from_alignment(_best_ma_alignment(row, reasons, include_row_level=include_row_level))


def _fib_ma_support_score(
    row: dict[str, Any],
    reasons: list[dict[str, Any]] | None = None,
    *,
    include_row_level: bool = True,
) -> float:
    return _fib_ma_support_score_from_alignment(_best_ma_alignment(row, reasons, include_row_level=include_row_level))


def _ma_left_attack_confirmed(ma: dict[str, Any]) -> bool:
    if not ma:
        return False
    primary_support = any(
        _truthy_bool(ma.get(key))
        for key in ("above_ma20", "near_ma20", "reclaim_ma20", "above_ma10", "near_ma10", "reclaim_ma10")
    )
    fib_key_support = any(
        _truthy_bool(ma.get(key))
        for period in (21, 34, 55)
        for key in (f"near_ma{period}", f"reclaim_ma{period}")
    )
    return primary_support or fib_key_support


def _ma_right_attack_confirmed(ma: dict[str, Any]) -> bool:
    if not ma:
        return False
    above_count = sum(1 for period in (5, 10, 20) if _truthy_bool(ma.get(f"above_ma{period}")))
    return above_count >= 2 or _float(ma.get("above_count")) >= 2


def _buy_point_quality_for_reason(reason: dict[str, Any]) -> float:
    text = _reason_signal_text(reason)
    side = _technical_opportunity_side(reason)
    if side not in {"left", "right"}:
        return 0.0
    if "200日新高" in text or "200d_new_high_breakout" in text or "新高突破" in text:
        freshness = _freshness_score_for_reason(reason, 5.0)
        signal_score = min(4.0, max(0.0, _float(reason.get("score"))) * 0.02)
        confidence = min(1.0, _float(reason.get("confidence"))) * 2.0
        return round(min(18.0, 4.0 + freshness + signal_score + confidence), 3)
    elif "二买" in text:
        base = 28.0
    elif "三买" in text:
        base = 26.0
    elif "一买" in text:
        base = 25.0
    elif "背驰买" in text or "底背离" in text:
        base = 21.0
    elif "趋势买" in text:
        base = 19.0
    elif "突破" in text or "trend_breakout" in text:
        base = 18.0
    elif "MACD绿柱扩大" in text or "零上绿柱扩大" in text:
        base = 14.0
    elif "MACD绿柱缩小" in text or "零下绿柱缩小" in text:
        base = 12.0
    elif "缺口买" in text or "缺口" in text:
        base = 10.0
    else:
        base = 8.0
    freq = _text(reason.get("freq"))
    freq_bonus = 0.0
    if freq in {"日线", "daily", "1d", "D", "d"}:
        freq_bonus = 8.0
    elif _is_30m_freq(freq):
        freq_bonus = 6.0
    elif freq in {"周线", "weekly", "1w", "W", "w"}:
        freq_bonus = 4.0
    elif _is_right_side_freq(freq):
        freq_bonus = 3.0
    side_bonus = 4.0 if side == "right" and (_is_30m_freq(freq) or _is_right_side_freq(freq)) else 3.0 if side == "left" else 0.0
    freshness = _freshness_score_for_reason(reason, 5.0)
    signal_score = min(8.0, max(0.0, _float(reason.get("score"))) * 0.04)
    confidence = min(1.0, _float(reason.get("confidence"))) * 3.0
    return round(min(55.0, base + freq_bonus + side_bonus + freshness + signal_score + confidence), 3)


def _buy_point_quality(row: dict[str, Any], buy_reasons: list[dict[str, Any]] | None = None) -> float:
    reasons = _buy_technical_reasons(row) if buy_reasons is None else buy_reasons
    values = [_buy_point_quality_for_reason(reason) for reason in reasons]
    quality = max(values, default=0.0)
    sides = {_technical_opportunity_side(reason) for reason in reasons}
    if {"left", "right"} <= sides:
        quality += 4.0
    return round(min(60.0, quality), 3)


def _new_high_breakout_score_for_reason(reason: dict[str, Any]) -> float:
    text = _reason_signal_text(reason)
    if not any(token in text for token in ("200日新高", "200d_new_high_breakout", "新高突破")):
        return 0.0
    evidence = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
    entry_factor = evidence.get("entry_factor") if isinstance(evidence.get("entry_factor"), dict) else {}
    breakout_pct = max(0.0, _float(entry_factor.get("breakout_pct")))
    five_day_gain_pct = max(0.0, _float(entry_factor.get("five_day_gain_pct")))
    volume_ratio = max(0.0, _float(entry_factor.get("volume_ratio")))
    if not entry_factor:
        return 5.0
    score = 4.0
    score += min(4.0, breakout_pct * 0.5)
    score += min(5.0, five_day_gain_pct * 0.12)
    score += min(3.0, max(0.0, volume_ratio - 1.0) * 2.0)
    return round(min(16.0, score), 3)


def _new_high_breakout_score(row: dict[str, Any], buy_reasons: list[dict[str, Any]] | None = None) -> float:
    reasons = _buy_technical_reasons(row) if buy_reasons is None else buy_reasons
    return max((_new_high_breakout_score_for_reason(reason) for reason in reasons), default=0.0)


def _refusal_pullback_score_for_reason(reason: dict[str, Any]) -> float:
    text = _reason_signal_text(reason)
    if not any(token in text for token in ("relative_resilience_refusal_pullback", "拒绝回调", "相对强度")):
        return 0.0
    evidence = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
    entry_factor = evidence.get("entry_factor") if isinstance(evidence.get("entry_factor"), dict) else {}
    if not entry_factor:
        return 4.0
    max_drawdown_pct = max(0.0, _float(entry_factor.get("max_drawdown_pct")))
    max_close_drawdown_pct = max(0.0, _float(entry_factor.get("max_close_drawdown_pct")))
    high_proximity_pct = max(0.0, _float(entry_factor.get("high_proximity_pct")))
    strong_close_days = max(0.0, _float(entry_factor.get("strong_close_days")))
    twenty_day_gain_pct = max(0.0, _float(entry_factor.get("twenty_day_gain_pct")))
    recent_volume_ratio = max(0.0, _float(entry_factor.get("recent_volume_ratio")))
    score = 4.0
    score += max(0.0, 3.5 - max_drawdown_pct) * 1.2
    score += max(0.0, 2.0 - max_close_drawdown_pct)
    score += min(4.0, max(0.0, high_proximity_pct - 97.0) * 0.8)
    score += min(4.0, strong_close_days * 1.2)
    score += min(3.0, twenty_day_gain_pct * 0.12)
    if 0 < recent_volume_ratio <= 1.35:
        score += 2.0
    return round(min(18.0, score), 3)


def _refusal_pullback_score(row: dict[str, Any], buy_reasons: list[dict[str, Any]] | None = None) -> float:
    reasons = _buy_technical_reasons(row) if buy_reasons is None else buy_reasons
    return max((_refusal_pullback_score_for_reason(reason) for reason in reasons), default=0.0)


def _risk_reasons(row: dict[str, Any]) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    for reason in row.get("inclusion_reasons") or []:
        if not isinstance(reason, dict):
            continue
        signal_side = _text(reason.get("signal_side"))
        reason_type = _text(reason.get("reason_type"))
        decision_effect = _text(reason.get("decision_effect"))
        knowledge_effect = _text(reason.get("knowledge_effect"))
        if signal_side == "sell" or decision_effect in {"exit_priority", "block"} or knowledge_effect in {"block", "exit_priority"}:
            reasons.append(reason)
            continue
        if reason_type in {"generated_risk_signal", "knowledge_conflict", "review_sector_bearish"}:
            reasons.append(reason)
    return reasons


def _source_collections(row: dict[str, Any]) -> list[str]:
    values = []
    for reason in row.get("inclusion_reasons") or []:
        if not isinstance(reason, dict):
            continue
        source = _text(reason.get("source_collection"))
        if source and source not in values:
            values.append(source)
    return values


def _scorer_freq_multiplier(freq: Any) -> float:
    text = _text(freq)
    aliases = {
        "weekly": "周线",
        "1w": "周线",
        "w": "周线",
        "daily": "日线",
        "1d": "日线",
        "d": "日线",
        "30min": "30分钟",
        "30m": "30分钟",
        "f30": "30分钟",
        "15min": "15分钟",
        "15m": "15分钟",
        "f15": "15分钟",
        "5min": "5分钟",
        "5m": "5分钟",
        "f5": "5分钟",
    }
    key = aliases.get(text.lower(), text)
    return float(FREQ_MULTIPLIER.get(key, 1.0))


def _buy_reason_priority(reason: dict[str, Any]) -> tuple[float, float, float, float]:
    direct_freq = _text(reason.get("freq"))
    freqs = {direct_freq} if direct_freq else _reason_freqs(reason)
    context_freqs = _reason_freqs(reason)
    readiness = 0.0
    if any(_is_30m_freq(freq) for freq in freqs):
        readiness += 1000.0
    elif any(_is_30m_freq(freq) for freq in context_freqs):
        readiness += 120.0
    if any(_is_right_side_freq(freq) for freq in freqs):
        readiness += 700.0
    elif any(_is_right_side_freq(freq) for freq in context_freqs):
        readiness += 90.0
    if any(_is_upper_freq(freq) for freq in freqs):
        readiness += 600.0
    elif any(_is_upper_freq(freq) for freq in context_freqs):
        readiness += 80.0
    upper_quality = sum(_scorer_freq_multiplier(freq) for freq in context_freqs if _is_upper_freq(freq))
    return (
        readiness,
        upper_quality,
        _float(reason.get("score")),
        _float(reason.get("confidence")),
    )


def _reason_has_freq(reason: dict[str, Any], predicate) -> bool:
    return any(predicate(freq) for freq in _reason_freqs(reason))


def _has_current_reason_for_freq(reasons: list[dict[str, Any]], predicate) -> bool:
    direct = [reason for reason in reasons if predicate(reason.get("freq"))]
    if direct:
        return any(_reason_is_current_for_entry(reason) for reason in direct)
    return any(
        _reason_has_freq(reason, predicate) and _reason_is_current_for_entry(reason)
        for reason in reasons
    )


def _has_direct_reason_for_freq(reasons: list[dict[str, Any]], predicate, *, side: str | None = None) -> bool:
    for reason in reasons:
        if not predicate(reason.get("freq")):
            continue
        if side and _technical_opportunity_side(reason) != side:
            continue
        return True
    return False


def _has_current_direct_reason_for_freq(reasons: list[dict[str, Any]], predicate, *, side: str | None = None) -> bool:
    for reason in reasons:
        if not predicate(reason.get("freq")) or not _reason_is_current_for_entry(reason):
            continue
        if side and _technical_opportunity_side(reason) != side:
            continue
        return True
    return False


def _has_reason_context_for_freq(reasons: list[dict[str, Any]], predicate, *, side: str | None = None) -> bool:
    for reason in reasons:
        if not _reason_has_freq(reason, predicate):
            continue
        if side and _technical_opportunity_side(reason) != side:
            continue
        return True
    return False


def _has_current_reason_context_for_freq(reasons: list[dict[str, Any]], predicate, *, side: str | None = None) -> bool:
    for reason in reasons:
        if not _reason_has_freq(reason, predicate) or not _reason_is_current_for_entry(reason):
            continue
        if side and _technical_opportunity_side(reason) != side:
            continue
        return True
    return False


def _current_direct_reasons_for_freq(
    reasons: list[dict[str, Any]],
    predicate,
    *,
    side: str | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for reason in reasons:
        if not predicate(reason.get("freq")) or not _reason_is_current_for_entry(reason):
            continue
        if side and _technical_opportunity_side(reason) != side:
            continue
        out.append(reason)
    return out


def _is_left_attack_reason(reason: dict[str, Any]) -> bool:
    text = _reason_signal_text(reason)
    if _technical_opportunity_side(reason) != "left":
        return False
    if any(token in text for token in ("一买", "背驰买", "底背离", "MACD绿柱缩小", "零下绿柱缩小")):
        return True
    return "二买" in text and _is_upper_freq(reason.get("freq"))


def _is_right_attack_reason(reason: dict[str, Any]) -> bool:
    text = _reason_signal_text(reason)
    if _technical_opportunity_side(reason) != "right":
        return False
    if not (_is_30m_freq(reason.get("freq")) or _is_right_side_freq(reason.get("freq"))):
        return False
    return any(token in text for token in (
        "二买",
        "三买",
        "趋势买",
        "突破",
        "MACD绿柱扩大",
        "零上绿柱扩大",
        "trend_breakout",
        "candle_run",
        "candle_accel",
        "200d_new_high_breakout",
        "200日新高",
        "新高突破",
    ))


def _has_left_attack_setup(row: dict[str, Any], buy_reasons: list[dict[str, Any]]) -> bool:
    current_left = [
        reason
        for reason in buy_reasons
        if _reason_is_current_for_entry(reason) and _is_left_attack_reason(reason)
    ]
    if not current_left:
        return False
    return _ma_left_attack_confirmed(_best_ma_alignment(row, _current_buy_technical_reasons(row), include_row_level=False))


def _has_right_attack_setup(row: dict[str, Any], buy_reasons: list[dict[str, Any]]) -> bool:
    current_right = [
        reason
        for reason in buy_reasons
        if _reason_is_current_for_entry(reason) and _is_right_attack_reason(reason)
    ]
    if not current_right:
        return False
    return _ma_right_attack_confirmed(_best_ma_alignment(row, _current_buy_technical_reasons(row), include_row_level=False))


def _has_attack_entry_setup(row: dict[str, Any], buy_reasons: list[dict[str, Any]]) -> bool:
    """Allow hot right-side setups into focus without waiting for a 30m print."""
    if not _has_right_attack_setup(row, buy_reasons):
        return False
    upper_reasons = [
        reason
        for reason in buy_reasons
        if _reason_has_freq(reason, _is_upper_freq) and _reason_is_current_for_entry(reason)
    ]
    right_side_reasons = _current_direct_reasons_for_freq(buy_reasons, _is_right_side_freq, side="right")
    if not upper_reasons or not right_side_reasons:
        return False
    right_freqs = {_text(reason.get("freq")) for reason in right_side_reasons}
    has_5m = any(freq in {"5分钟", "5min", "5m", "F5", "f5"} for freq in right_freqs)
    has_15m = any(freq in {"15分钟", "15min", "15m", "F15", "f15"} for freq in right_freqs)
    strong_pair = has_5m and has_15m
    strong_score = max((_float(reason.get("score")) for reason in right_side_reasons), default=0.0) >= 90.0
    theme_bonus = _sector_policy(row).get("policy") in {"mainline_lenient", "defensive_lenient"}
    return strong_pair or (strong_score and theme_bonus)


def _knowledge_confirms_left(row: dict[str, Any]) -> bool:
    knowledge = row.get("knowledge_confirmation") if isinstance(row.get("knowledge_confirmation"), dict) else {}
    if _text(knowledge.get("status")) == "confirmed" or _text(knowledge.get("effect")) == "confirm":
        return True
    for reason in row.get("inclusion_reasons") or []:
        if not isinstance(reason, dict):
            continue
        if _text(reason.get("reason_type")) == "knowledge_confirmed":
            return True
        if _text(reason.get("knowledge_status")) == "confirmed" or _text(reason.get("knowledge_effect")) == "confirm":
            return True
    return False


def _left_allowed_reason(row: dict[str, Any]) -> str:
    policy = _sector_policy(row).get("policy")
    if policy == "mainline_lenient":
        return "mainline_lenient"
    if policy == "defensive_lenient" and _broad_market_is_falling(row):
        return "defensive_lenient_broad_market_falling"
    if _knowledge_confirms_left(row):
        return "knowledge_confirmed"
    return ""


def _left_attack_allowed_in_focus(row: dict[str, Any], entry_gate_status: str) -> bool:
    return entry_gate_status == "left_attack_confirmed" and bool(_left_allowed_reason(row))


def _entry_allowed_in_focus(row: dict[str, Any], entry_gate_status: str) -> bool:
    if entry_gate_status == "entry_attack_confirmed":
        return _focus_mainline_rank_tier(row) > 0
    return True


def _focus_mainline_rank_tier(row: dict[str, Any]) -> int:
    policy = _sector_policy(row).get("policy")
    if _knowledge_confirms_left(row):
        return 300
    if policy == "mainline_lenient":
        return 240
    if policy == "defensive_lenient":
        return 160
    return 0


def _right_review_allowed_in_focus(row: dict[str, Any], entry_gate_status: str, top_buy: dict[str, Any] | None) -> bool:
    if entry_gate_status not in FOCUS_REVIEW_GATE_STATUSES or not top_buy:
        return False
    if _focus_mainline_rank_tier(row) <= 0:
        return False
    if _sector_policy(row).get("policy") == "defensive_strict":
        return False
    buy_reasons = _current_buy_technical_reasons(row)
    if not any(_technical_opportunity_side(reason) == "right" for reason in buy_reasons):
        return False
    timeframe_sides = _timeframe_signal_sides(row)
    upper_right = _side_is_confirming(_text(timeframe_sides.get("upper", {}).get("side")))
    trade_right = _side_is_confirming(_text(timeframe_sides.get("trade", {}).get("side")))
    execution_right = _side_is_confirming(_text(timeframe_sides.get("execution", {}).get("side")))
    ma = _ma_alignment_score(row, buy_reasons, include_row_level=False)
    fib = _fib_ma_support_score(row, buy_reasons, include_row_level=False)
    quality = _buy_point_quality(row, buy_reasons)
    has_support = ma >= 20.0 or fib >= 8.0
    if trade_right and (execution_right or has_support or quality >= 32.0):
        return True
    if upper_right and execution_right and (has_support or quality >= 28.0):
        return True
    if upper_right and ma >= 30.0 and fib >= 8.0 and quality >= 20.0:
        return True
    return False


def _market_setup_bias(pool_type: str, entry_gate_status: str, top_risk: dict[str, Any] | None) -> str:
    del top_risk
    if pool_type == "risk" or entry_gate_status.startswith("blocked_by"):
        return "risk_first"
    if entry_gate_status == "entry_confirmed":
        return "right_executable"
    if entry_gate_status == "entry_attack_confirmed":
        return "right_attack"
    if pool_type == "focus" and entry_gate_status in FOCUS_REVIEW_GATE_STATUSES:
        return "right_review"
    if entry_gate_status == "left_attack_confirmed":
        return "left_review"
    return "watch_only"


def _chain_entry_blocker(row: dict[str, Any]) -> str:
    chain = row.get("chain_context") if isinstance(row.get("chain_context"), dict) else {}
    phase = _text(chain.get("phase"))
    effect = _text(chain.get("effect"))
    if effect == "block":
        return "chain_block"
    if effect == "exit_priority":
        return "chain_risk_off"
    if phase in ENTRY_BLOCK_CHAIN_PHASES:
        return f"chain_{phase}"
    return ""


def _entry_gate(row: dict[str, Any]) -> tuple[bool, str, list[str], dict[str, Any] | None, dict[str, Any] | None]:
    buy_reasons = _buy_technical_reasons(row)
    risk_reasons = _risk_reasons(row)
    current_buy_reasons = [reason for reason in buy_reasons if _reason_is_current_for_entry(reason)]
    top_buy = max(current_buy_reasons, key=_buy_reason_priority, default=None) or max(buy_reasons, key=_buy_reason_priority, default=None)
    top_risk = max(risk_reasons, key=lambda item: (_float(item.get("weight")), abs(_float(item.get("score")))), default=None)
    chain_blocker = _chain_entry_blocker(row)
    if not top_buy:
        if top_risk:
            return False, "blocked_by_risk", ["risk_signal_present"], None, top_risk
        if chain_blocker:
            return False, "blocked_by_chain_context", [chain_blocker], None, None
        return False, "watch_only_not_hard_buy", ["missing_buy_technical"], None, None
    freqs: set[str] = set()
    conflict_freqs: set[str] = set()
    for reason in buy_reasons:
        freqs.update(_reason_freqs(reason))
        context = reason.get("resonance_context") if isinstance(reason.get("resonance_context"), dict) else {}
        conflict_freqs.update(_text(freq) for freq in context.get("conflict_freqs") or [] if _text(freq))
    if conflict_freqs:
        top_risk = top_risk or {
            "reason_type": "period_conflict",
            "signal_type": "周期冲突",
            "signal_side": "sell",
            "freq": ",".join(sorted(conflict_freqs, key=_freq_sort_key)),
            "score": 0,
            "confidence": 0,
            "decision_effect": "mark_only",
        }
    if chain_blocker:
        top_risk = top_risk or {
            "reason_type": "chain_context",
            "signal_type": "产业链风险",
            "signal_side": "sell",
            "score": 0,
            "confidence": 0,
            "decision_effect": "mark_only",
            "details": chain_blocker,
        }
    has_30m = _has_direct_reason_for_freq(buy_reasons, _is_30m_freq)
    has_upper = any(_is_upper_freq(freq) for freq in freqs)
    has_right_side = _has_direct_reason_for_freq(buy_reasons, _is_right_side_freq, side="right")
    has_partner = any(_is_entry_partner_freq(freq) for freq in freqs)
    fresh_30m = _has_current_direct_reason_for_freq(buy_reasons, _is_30m_freq)
    fresh_30m_right = _has_current_direct_reason_for_freq(buy_reasons, _is_30m_freq, side="right")
    fresh_upper = _has_current_reason_for_freq(buy_reasons, _is_upper_freq)
    fresh_right_side = _has_current_direct_reason_for_freq(buy_reasons, _is_right_side_freq, side="right")
    fresh_right_side_confirmed = fresh_right_side
    if (
        has_30m
        and fresh_30m
        and fresh_30m_right
        and has_upper
        and fresh_upper
        and has_partner
        and has_right_side
        and fresh_right_side
        and fresh_right_side_confirmed
    ):
        return True, "entry_confirmed", [], top_buy, top_risk
    defensive_strict = _sector_policy(row).get("policy") == "defensive_strict"
    if _has_left_attack_setup(row, current_buy_reasons):
        if defensive_strict:
            return False, "entry_waiting_defensive_confirmation", ["defensive_strict_requires_full_confirmation"], top_buy, top_risk
        return True, "left_attack_confirmed", ["left_attack_ma_confirmed"], top_buy, top_risk
    if not has_30m:
        if fresh_upper and fresh_right_side and _has_attack_entry_setup(row, buy_reasons):
            if defensive_strict:
                return False, "entry_waiting_defensive_confirmation", ["defensive_strict_requires_full_confirmation"], top_buy, top_risk
            return True, "entry_attack_confirmed", ["30m_attack_missing"], top_buy, top_risk
        return False, "entry_waiting_30m_confirm", ["30m_missing"], top_buy, top_risk
    if not fresh_30m:
        return False, "entry_waiting_30m_confirm", ["30m_stale"], top_buy, top_risk
    if not fresh_30m_right:
        return False, "entry_waiting_30m_confirm", ["30m_right_side_missing"], top_buy, top_risk
    if not has_upper:
        return False, "entry_waiting_upper_context", ["daily_or_weekly_missing"], top_buy, top_risk
    if not fresh_upper:
        return False, "entry_waiting_upper_context", ["daily_or_weekly_stale"], top_buy, top_risk
    if not has_partner:
        return False, "entry_waiting_resonance_confirm", ["partner_period_missing"], top_buy, top_risk
    if not has_right_side:
        return False, "entry_waiting_right_side_confirm", ["5m_or_15m_missing"], top_buy, top_risk
    if not fresh_right_side:
        return False, "entry_waiting_right_side_confirm", ["5m_or_15m_stale"], top_buy, top_risk
    if not fresh_right_side_confirmed:
        return False, "entry_waiting_right_side_confirm", ["5m_or_15m_right_side_missing"], top_buy, top_risk
    return True, "entry_confirmed", [], top_buy, top_risk


def _freq_severity(freqs: set[str]) -> float:
    if any(freq in {"周线", "weekly", "1w", "W", "w"} for freq in freqs):
        return 30.0
    if any(freq in {"日线", "daily", "1d", "D", "d"} for freq in freqs):
        return 24.0
    if any(_is_30m_freq(freq) for freq in freqs):
        return 18.0
    if any(freq in {"15分钟", "15min", "15m", "F15", "f15"} for freq in freqs):
        return 14.0
    return 8.0


def _context_adjust(row: dict[str, Any]) -> float:
    adjust = 0.0
    knowledge = row.get("knowledge_confirmation") if isinstance(row.get("knowledge_confirmation"), dict) else {}
    if knowledge.get("status") == "confirmed":
        adjust += 8.0
    if knowledge.get("status") == "conflict" or knowledge.get("effect") == "block":
        adjust -= 40.0
    chain = row.get("chain_context") if isinstance(row.get("chain_context"), dict) else {}
    phase = _text(chain.get("phase"))
    if chain.get("effect") == "confirm":
        adjust += 18.0
    elif phase == "warming":
        adjust += 2.0
    if chain.get("effect") in {"block", "exit_priority"}:
        adjust -= 25.0
    return adjust


def _chain_alignment_score(row: dict[str, Any]) -> float:
    chain = row.get("chain_context") if isinstance(row.get("chain_context"), dict) else {}
    if not chain:
        return 0.0
    evidence = chain.get("evidence") if isinstance(chain.get("evidence"), dict) else {}
    phase = _text(chain.get("phase") or evidence.get("phase"))
    if phase == "accelerating":
        base = 20.0
    elif phase == "warming":
        base = 6.0
    elif phase in {"consensus_climax", "risk_off"}:
        base = -18.0
    elif phase in {"diverging", "cooling"}:
        base = -14.0
    else:
        base = 0.0
    rank = _float(evidence.get("rank") or chain.get("rank"), 999.0)
    rank_bonus = max(0.0, min(10.0, (31.0 - rank) * 0.4)) if rank > 0 else 0.0
    role_bonus = 0.0
    tags = set(row.get("source_tags") or [])
    if "chain_core_rep" in tags or "source_leader" in tags:
        role_bonus = 5.0
    elif "chain_elastic_rep" in tags or "constituent_hot" in tags:
        role_bonus = 3.0
    return round(base + rank_bonus + role_bonus, 3)


def _mainline_confirmation_reason(row: dict[str, Any]) -> str:
    if _knowledge_confirms_left(row):
        return "knowledge_confirmed"
    chain = row.get("chain_context") if isinstance(row.get("chain_context"), dict) else {}
    evidence = chain.get("evidence") if isinstance(chain.get("evidence"), dict) else {}
    phase = _text(chain.get("phase") or evidence.get("phase"))
    effect = _text(chain.get("effect") or evidence.get("effect"))
    if effect == "confirm" and phase != "warming":
        return "chain_confirmed"
    if phase == "accelerating":
        return "chain_accelerating"
    return ""


def _mainline_status(row: dict[str, Any]) -> str:
    if _mainline_confirmation_reason(row):
        return "confirmed"
    chain = row.get("chain_context") if isinstance(row.get("chain_context"), dict) else {}
    evidence = chain.get("evidence") if isinstance(chain.get("evidence"), dict) else {}
    phase = _text(chain.get("phase") or evidence.get("phase"))
    if phase == "warming":
        return "rebound_rotation"
    if phase in {"cooling", "diverging"}:
        return "cooling"
    if phase in {"consensus_climax", "risk_off"}:
        return "risk"
    return "none"


def _append_policy_value(values: list[str], value: Any) -> None:
    text = _text(value)
    if text and text not in values:
        values.append(text)


def _append_policy_fields(values: list[str], source: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        _append_policy_value(values, source.get(key))


def _append_source_boards(*, concept_values: list[str], industry_values: list[str], boards: Any) -> None:
    if not isinstance(boards, list):
        return
    for board in boards:
        if not isinstance(board, dict):
            continue
        name = _text(board.get("name") or board.get("board_name") or board.get("concept_name"))
        kind = _text(board.get("kind")).lower()
        source = _text(board.get("source_board_id") or board.get("collection")).lower()
        if kind == "concept" or ":concept:" in source or "concept" in source:
            _append_policy_value(concept_values, name)
        elif kind == "industry" or ":industry:" in source or "industry" in source:
            _append_policy_value(industry_values, name)


def _sector_policy_texts(row: dict[str, Any]) -> dict[str, str]:
    concept_values: list[str] = []
    chain_values: list[str] = []
    industry_values: list[str] = []
    fallback_values: list[str] = []
    concept_keys = ("concept", "theme", "board_or_concept", "rotation_line", "exposure_bucket")
    chain_keys = ("chain_name", "primary_chain", "chain", "node_name", "chain_node", "node", "board_or_concept")
    industry_keys = ("sector", "industry", "industry_name", "domain", "domain_name")

    _append_policy_fields(concept_values, row, ("concept", "theme", "rotation_line", "exposure_bucket"))
    _append_policy_fields(industry_values, row, industry_keys)
    _append_policy_fields(fallback_values, row, concept_keys + chain_keys + industry_keys)

    chain = row.get("chain_context") if isinstance(row.get("chain_context"), dict) else {}
    evidence = chain.get("evidence") if isinstance(chain.get("evidence"), dict) else {}
    if chain:
        _append_policy_fields(chain_values, chain, chain_keys)
        _append_source_boards(
            concept_values=concept_values,
            industry_values=industry_values,
            boards=chain.get("source_boards") or evidence.get("source_boards"),
        )
    if evidence:
        _append_policy_fields(chain_values, evidence, chain_keys)
        _append_source_boards(
            concept_values=concept_values,
            industry_values=industry_values,
            boards=evidence.get("source_boards"),
        )

    for reason in row.get("inclusion_reasons") or []:
        if not isinstance(reason, dict):
            continue
        source_collection = _text(reason.get("source_collection")).lower()
        reason_type = _text(reason.get("reason_type"))
        nested = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
        _append_source_boards(
            concept_values=concept_values,
            industry_values=industry_values,
            boards=reason.get("source_boards") or nested.get("source_boards"),
        )
        if "concept" in source_collection:
            _append_policy_fields(concept_values, reason, concept_keys + ("source_doc_id",))
            _append_policy_fields(concept_values, nested, concept_keys + ("source_doc_id",))
        elif "industry" in source_collection or "board_constituents" in source_collection:
            _append_policy_fields(industry_values, reason, industry_keys + ("board_or_concept", "source_doc_id"))
            _append_policy_fields(industry_values, nested, industry_keys + ("board_or_concept", "source_doc_id"))
        elif reason_type in {"chain_membership", "chain_context", "chain_core_rep", "chain_elastic_rep", "source_leader", "constituent_hot"}:
            _append_policy_fields(chain_values, reason, chain_keys)
            _append_policy_fields(chain_values, nested, chain_keys)
        else:
            _append_policy_fields(fallback_values, reason, concept_keys + chain_keys + industry_keys)
            _append_policy_fields(fallback_values, nested, concept_keys + chain_keys + industry_keys)

    return {
        "concept": " ".join(concept_values).lower(),
        "chain": " ".join(chain_values).lower(),
        "industry": " ".join(industry_values).lower(),
        "fallback": " ".join(fallback_values).lower(),
        "all": " ".join(dict.fromkeys(concept_values + chain_values + industry_values + fallback_values)).lower(),
    }


def _first_matching_token(text: str, tokens: tuple[str, ...]) -> str:
    for token in tokens:
        if token.lower() in text:
            return token
    return ""


def _broad_market_is_falling(row: dict[str, Any]) -> bool:
    contexts = [
        row.get("broad_market_context"),
        row.get("market_context"),
        row.get("market_regime"),
    ]
    for context in contexts:
        if not isinstance(context, dict):
            continue
        for key in ("is_falling", "broad_market_falling", "market_falling", "index_falling"):
            if context.get(key) is True:
                return True
        for key in ("average_change_pct", "avg_change_pct", "day_change_pct", "change_pct"):
            if key in context and context.get(key) not in (None, ""):
                if _float(context.get(key), 999.0) <= BROAD_MARKET_FALL_THRESHOLD:
                    return True
        indexes = context.get("indexes") if isinstance(context.get("indexes"), list) else []
        changes = [
            _float(item.get("change_pct"), 999.0)
            for item in indexes
            if isinstance(item, dict) and item.get("change_pct") not in (None, "")
        ]
        if changes:
            required = max(1, (len(changes) // 2) + 1)
            if sum(1 for value in changes if value <= BROAD_MARKET_FALL_THRESHOLD) >= required:
                return True
        label = " ".join(
            _text(context.get(key)).lower()
            for key in ("label", "summary", "overall_direction", "market_line", "phase", "regime")
        )
        if any(token in label for token in ("大盘下跌", "下跌", "走弱", "偏空", "回撤", "risk_off", "bear")):
            return True
    return False


def _index_setup_side(row: dict[str, Any]) -> str:
    context = row.get("broad_market_context") if isinstance(row.get("broad_market_context"), dict) else {}
    side = _text(context.get("index_setup_side"))
    if side in {"left_buy", "right_buy", "left_sell", "right_sell"}:
        return side
    if _broad_market_is_falling(row):
        return "left_sell"
    return "unknown"


def _stock_setup_side(entry_gate_status: str, top_buy: dict[str, Any] | None, top_risk: dict[str, Any] | None) -> str:
    if top_risk and not top_buy:
        return "sell"
    if entry_gate_status.startswith("blocked_by"):
        return "sell" if top_risk else "context"
    if entry_gate_status in {"entry_confirmed", "entry_attack_confirmed"}:
        return "right_buy"
    if entry_gate_status == "left_attack_confirmed":
        return "left_buy"
    if top_buy:
        side = _technical_opportunity_side(top_buy)
        if side == "right":
            return "right_buy"
        if side == "left":
            return "left_buy"
        if side == "sell":
            return "sell"
    if top_risk:
        return "sell"
    return "context"


def _stock_setup_label(side: str) -> str:
    return {
        "right_buy": "个股右侧买",
        "left_buy": "个股左侧买",
        "sell": "个股卖侧",
        "context": "个股观察",
    }.get(side, "个股观察")


def _market_setup_alignment(row: dict[str, Any], entry_gate_status: str, top_buy: dict[str, Any] | None, top_risk: dict[str, Any] | None) -> dict[str, Any]:
    index_side = _index_setup_side(row)
    stock_side = _stock_setup_side(entry_gate_status, top_buy, top_risk)
    context = row.get("broad_market_context") if isinstance(row.get("broad_market_context"), dict) else {}
    if stock_side == "sell":
        alignment, policy, score = "stock_sell", "risk_first", -40.0
    elif index_side == "right_sell" and stock_side.endswith("_buy"):
        alignment, policy, score = "index_right_sell_stock_buy", "mark_index_risk", -28.0
    elif index_side == "left_sell" and stock_side == "right_buy":
        if entry_gate_status == "entry_confirmed":
            alignment, policy, score = "index_left_sell_stock_right_buy", "allow_focus_cautious", -8.0
        else:
            alignment, policy, score = "index_left_sell_stock_right_buy", "mark_index_caution", -14.0
    elif index_side == "left_sell" and stock_side == "left_buy":
        if _sector_policy(row).get("policy") == "defensive_lenient":
            alignment, policy, score = "index_left_sell_defensive_left_buy", "allow_focus_cautious", 2.0
        else:
            alignment, policy, score = "index_left_sell_stock_left_buy", "mark_index_caution", -18.0
    elif index_side == "right_buy" and stock_side == "right_buy":
        alignment, policy, score = "aligned_right_buy", "allow_focus", 20.0
    elif index_side == "right_buy" and stock_side == "left_buy":
        alignment, policy, score = "index_right_buy_stock_left_buy", "allow_focus", 8.0
    elif index_side == "left_buy" and stock_side == "right_buy":
        alignment, policy, score = "index_left_buy_stock_right_buy", "allow_focus", 10.0
    elif index_side == "left_buy" and stock_side == "left_buy":
        alignment, policy, score = "aligned_left_buy", "mark_index_caution", 4.0
    else:
        alignment, policy, score = "mixed", "neutral", 0.0
    volume_state = _text(context.get("volume_state")) or "unknown"
    volume_ratio = _float(context.get("volume_ratio_5d"))
    if policy in {"allow_focus", "allow_focus_cautious"} and volume_state == "shrinking":
        score -= 4.0
    return {
        "index_setup_side": index_side,
        "index_setup_label": _index_setup_label(index_side),
        "stock_setup_side": stock_side,
        "stock_setup_label": _stock_setup_label(stock_side),
        "setup_alignment": alignment,
        "alignment_policy": policy,
        "alignment_score": round(score, 3),
        "market_volume_state": volume_state,
        "market_volume_label": _market_volume_label(volume_state),
        "market_volume_ratio": round(volume_ratio, 3) if volume_ratio > 0 else 0.0,
    }


def _market_alignment_components(row: dict[str, Any], entry_gate_status: str, top_buy: dict[str, Any] | None, top_risk: dict[str, Any] | None) -> dict[str, float]:
    score = _float(_market_setup_alignment(row, entry_gate_status, top_buy, top_risk).get("alignment_score"))
    return {"market_alignment": score} if score else {}


def _alignment_allows_focus(row: dict[str, Any], entry_gate_status: str, top_buy: dict[str, Any] | None, top_risk: dict[str, Any] | None) -> bool:
    policy = _market_setup_alignment(row, entry_gate_status, top_buy, top_risk).get("alignment_policy")
    return policy != "risk_first"


def _sector_policy(row: dict[str, Any]) -> dict[str, Any]:
    texts = _sector_policy_texts(row)
    concept_attack_token = _first_matching_token(texts["concept"], MAINLINE_LENIENT_SECTOR_TOKENS)
    chain_attack_token = _first_matching_token(texts["chain"], MAINLINE_LENIENT_SECTOR_TOKENS)
    industry_attack_token = _first_matching_token(texts["industry"], MAINLINE_LENIENT_SECTOR_TOKENS)
    fallback_attack_token = _first_matching_token(texts["fallback"], MAINLINE_LENIENT_SECTOR_TOKENS)
    attack_token = concept_attack_token or chain_attack_token or industry_attack_token or fallback_attack_token
    attack_source = (
        "概念/题材" if concept_attack_token else
        "产业链" if chain_attack_token else
        "行业" if industry_attack_token else
        "线索"
    )
    defensive_token = (
        _first_matching_token(texts["industry"], DEFENSIVE_STRICT_SECTOR_TOKENS)
        or _first_matching_token(texts["chain"], DEFENSIVE_STRICT_SECTOR_TOKENS)
        or _first_matching_token(texts["fallback"], DEFENSIVE_STRICT_SECTOR_TOKENS)
    )
    raw_mainline = _chain_alignment_score(row)
    chain = row.get("chain_context") if isinstance(row.get("chain_context"), dict) else {}
    evidence = chain.get("evidence") if isinstance(chain.get("evidence"), dict) else {}
    phase = _text(chain.get("phase") or evidence.get("phase"))
    effect = _text(chain.get("effect"))
    is_current_mainline = raw_mainline >= 12.0 or phase in {"accelerating", "warming"} or effect == "confirm"
    if attack_token and is_current_mainline:
        overlay = "，覆盖防守行业" if defensive_token and attack_source != "行业" else ""
        return {
            "policy": "mainline_lenient",
            "label": "主线宽松",
            "matched_token": attack_token,
            "source": attack_source,
            "reason": f"{attack_source}{attack_token}处于当年/当前主线{overlay}，买点和均线共振可优先",
        }
    if defensive_token and not attack_token:
        if _broad_market_is_falling(row):
            return {
                "policy": "defensive_lenient",
                "label": "防守放宽",
                "matched_token": defensive_token,
                "source": "行业/产业链",
                "reason": f"{defensive_token}遇到大盘下跌，允许按避险进攻放宽",
            }
        return {
            "policy": "defensive_strict",
            "label": "防守严格",
            "matched_token": defensive_token,
            "source": "行业/产业链",
            "reason": f"{defensive_token}按防守板块处理，必须等更完整买点确认",
        }
    return {"policy": "neutral", "label": "中性", "matched_token": "", "source": "", "reason": ""}


def _sector_policy_components(row: dict[str, Any]) -> dict[str, float]:
    policy = _sector_policy(row).get("policy")
    if policy == "mainline_lenient":
        return {"mainline_lenient_policy": 18.0}
    if policy == "defensive_lenient":
        return {"defensive_lenient_policy": 12.0}
    if policy == "defensive_strict":
        return {"defensive_strict_policy": -24.0}
    return {}


def _mainline_alignment_score(row: dict[str, Any]) -> float:
    raw = _chain_alignment_score(row)
    policy = _sector_policy(row).get("policy")
    if policy == "mainline_lenient" and raw > 0:
        return round(min(36.0, raw + 6.0), 3)
    if policy == "defensive_lenient":
        return round(min(14.0, raw * 0.5 if raw > 0 else 6.0), 3)
    if policy == "defensive_strict":
        return round(min(0.0, raw) - 6.0, 3)
    return raw


def _mainline_alignment_level(score: float) -> str:
    if score >= 24:
        return "strong_mainline"
    if score >= 12:
        return "mainline_confirm"
    if score < 0:
        return "mainline_block"
    return "neutral"


def _row_buy_freqs(row: dict[str, Any], top_buy: dict[str, Any]) -> set[str]:
    freqs = {_text(top_buy.get("freq"))}
    for reason in _current_buy_technical_reasons(row):
        freqs.add(_text(reason.get("freq")))
    return {freq for freq in freqs if freq}


def _entry_components(row: dict[str, Any], top_buy: dict[str, Any]) -> dict[str, float]:
    buy_reasons = _current_buy_technical_reasons(row)
    freqs = _row_buy_freqs(row, top_buy)
    context = top_buy.get("resonance_context") if isinstance(top_buy.get("resonance_context"), dict) else {}
    grade = _text(context.get("grade"))
    resonance = 34.0 if grade == "strong_resonance" else 24.0 if grade == "multi_period" else 12.0
    upper_score = min(30.0, sum(_scorer_freq_multiplier(freq) * 8.0 for freq in freqs if _is_upper_freq(freq)))
    right_side = 30.0 if any(_is_right_side_freq(freq) for freq in freqs) else 0.0
    trigger_30m = 32.0 if any(_is_30m_freq(freq) for freq in freqs) else 0.0
    components = {
        "entry_readiness": 36.0,
        "timeframe_priority": _timeframe_priority_score(buy_reasons),
        "multi_period_bonus": _multi_period_score(buy_reasons),
        "indicator_breadth": _indicator_breadth_score(buy_reasons),
        "buy_point_quality": _buy_point_quality(row, buy_reasons),
        "breakout_momentum": _new_high_breakout_score(row, buy_reasons),
        "relative_resilience": _refusal_pullback_score(row, buy_reasons),
        "ma_alignment": _ma_alignment_score(row, buy_reasons, include_row_level=False),
        "fib_ma_support": _fib_ma_support_score(row, buy_reasons, include_row_level=False),
        "upper_timeframe_quality": upper_score,
        "trigger_30m": trigger_30m,
        "right_side_confirmation": right_side,
        "technical_score": max(0.0, _float(top_buy.get("score"))) * 0.16,
        "resonance": resonance,
        "confidence": min(1.0, _float(top_buy.get("confidence"))) * 18.0,
        "freshness": _freshness_score_for_reason(top_buy, 10.0),
        "mainline_alignment": _mainline_alignment_score(row),
        "context_adjust": _context_adjust(row),
    }
    components.update(_sector_policy_components(row))
    return {key: round(value, 3) for key, value in components.items()}


def _focus_review_components(row: dict[str, Any], top_buy: dict[str, Any]) -> dict[str, float]:
    components = _entry_components(row, top_buy)
    components["entry_readiness"] = 18.0
    components["focus_review"] = 12.0
    return {key: round(value, 3) for key, value in components.items()}


def _rank_reason(score_components: dict[str, float]) -> str:
    labels = {
        "entry_readiness": "买点确认",
        "focus_review": "买点复核",
        "buy_point_quality": "买点质量",
        "breakout_momentum": "新高动量",
        "relative_resilience": "拒绝回调",
        "ma_alignment": "均线确认",
        "fib_ma_support": "Fibonacci均线",
        "upper_timeframe_quality": "周/日线",
        "trigger_30m": "30m触发",
        "right_side_confirmation": "5m/15m确认",
        "technical_score": "技术分",
        "resonance": "共振",
        "confidence": "置信度",
        "freshness": "新鲜度",
        "mainline_alignment": "主线",
        "market_alignment": "市场共振",
        "mainline_lenient_policy": "主线宽松",
        "defensive_lenient_policy": "防守放宽",
        "defensive_strict_policy": "防守严格",
        "context_adjust": "观点修正",
        "sell_strength": "风险强度",
        "timeframe_severity": "级别严重度",
        "chain_or_knowledge_risk": "知识/产业链风险",
        "source_priority": "来源优先级",
        "entry_proximity": "接近买点",
        "heat_or_theme": "热度/主题",
        "hot_sector": "热门板块",
        "timeframe_priority": "周期优先级",
        "multi_period_bonus": "多周期",
        "indicator_breadth": "指标数量",
        "clue_quality": "线索质量",
    }
    ordered = sorted(score_components.items(), key=lambda item: abs(_float(item[1])), reverse=True)
    selected = ordered[:5]
    if "upper_timeframe_quality" in score_components and not any(key == "upper_timeframe_quality" for key, _ in selected):
        selected.append(("upper_timeframe_quality", score_components["upper_timeframe_quality"]))
    if _float(score_components.get("market_alignment")) and not any(key == "market_alignment" for key, _ in selected):
        selected.append(("market_alignment", score_components["market_alignment"]))
    if _float(score_components.get("relative_resilience")) and not any(key == "relative_resilience" for key, _ in selected):
        selected.append(("relative_resilience", score_components["relative_resilience"]))
    if _float(score_components.get("fib_ma_support")) and not any(key == "fib_ma_support" for key, _ in selected):
        selected.append(("fib_ma_support", score_components["fib_ma_support"]))
    parts = []
    for key, value in selected:
        numeric = _float(value)
        if numeric == 0:
            continue
        label = labels.get(key, key)
        parts.append(f"{label}{numeric:+.1f}")
    return " · ".join(parts)


def _risk_components(row: dict[str, Any], top_risk: dict[str, Any]) -> dict[str, float]:
    freqs = _reason_freqs(top_risk)
    components = {
        "sell_strength": abs(_float(top_risk.get("score"))) * 0.50,
        "timeframe_severity": _freq_severity(freqs),
        "freshness": _freshness_score_for_reason(top_risk, 16.0),
        "chain_or_knowledge_risk": 18.0 if _text(top_risk.get("reason_type")) in {"knowledge_conflict", "chain_context", "chain_core_rep", "chain_elastic_rep"} else 0.0,
    }
    return {key: round(value, 3) for key, value in components.items()}


def _watch_components(row: dict[str, Any], gate_status: str) -> dict[str, float]:
    del gate_status
    buy_reasons = _current_buy_technical_reasons(row)
    hot_sector = max(0.0, min(36.0, _mainline_alignment_score(row)))
    components = {
        "timeframe_priority": _timeframe_priority_score(buy_reasons),
        "multi_period_bonus": _multi_period_score(buy_reasons),
        "indicator_breadth": _indicator_breadth_score(buy_reasons),
        "buy_point_quality": _buy_point_quality(row, buy_reasons),
        "breakout_momentum": _new_high_breakout_score(row, buy_reasons),
        "relative_resilience": _refusal_pullback_score(row, buy_reasons),
        "ma_alignment": _ma_alignment_score(row, buy_reasons, include_row_level=False),
        "fib_ma_support": _fib_ma_support_score(row, buy_reasons, include_row_level=False),
        "hot_sector": hot_sector,
    }
    return {key: round(value, 3) for key, value in components.items()}


def _reason_signal_text(reason: dict[str, Any]) -> str:
    evidence = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
    details = evidence.get("details")
    return _normalize_signal_type(" ".join([
        _text(reason.get("signal_type")),
        _text(reason.get("reason_type")),
        _text(details if isinstance(details, str) else ""),
        " ".join(_text(value) for value in details.values()) if isinstance(details, dict) else "",
    ]))


def _technical_signal_family(reason: dict[str, Any]) -> str:
    text = _reason_signal_text(reason)
    family = _text(reason.get("signal_family"))
    if text.startswith("形态:") or any(token in text for token in PATTERN_TOKENS):
        return "pattern"
    if "MACD" in text or "绿柱" in text:
        return "macd"
    if "缺口" in text:
        return "gap"
    if any(token in text for token in ENTRY_FACTOR_TOKENS):
        return "entry_factor"
    if any(token in text for token in ("一买", "二买", "三买", "一卖", "二卖", "三卖", "背驰", "中枢", "笔", "线段", "趋势")):
        return "chan"
    if family and family != "hard_technical":
        return family
    return family or "technical"


def _technical_opportunity_side(reason: dict[str, Any]) -> str:
    text = _reason_signal_text(reason)
    signal_side = _text(reason.get("signal_side"))
    freq = _text(reason.get("freq"))
    if signal_side == "sell":
        return "sell"
    if any(token in text for token in WEAK_CONTEXT_TOKENS):
        return "context"
    if "二买" in text and _is_upper_freq(freq):
        return "left"
    if any(token in text for token in RIGHT_OPPORTUNITY_TOKENS):
        return "right"
    if any(token in text for token in LEFT_OPPORTUNITY_TOKENS):
        return "left"
    if signal_side == "buy":
        return "right" if _is_right_side_freq(freq) else "left"
    if any(token in text for token in SELL_TOKENS):
        return "sell"
    return "context"


def _technical_signal_group_item(reason: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": _normalize_signal_type(reason.get("signal_type") or reason.get("reason_type")),
        "family": _technical_signal_family(reason),
        "freq": _text(reason.get("freq")),
        "event_date": _text(reason.get("event_date")) or _reason_event_dt(reason)[:10],
        "score": round(_float(reason.get("score")), 3),
        "confidence": round(_float(reason.get("confidence")), 3),
        "source_collection": _text(reason.get("source_collection")),
    }


def _technical_signal_groups(row: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {"left": [], "right": [], "sell": [], "context": []}
    seen: set[str] = set()
    for reason in row.get("inclusion_reasons") or []:
        if not isinstance(reason, dict) or not _is_technical_reason(reason):
            continue
        if _text(reason.get("signal_side")) == "buy" and not _reason_is_current_for_entry(reason):
            continue
        side = _technical_opportunity_side(reason)
        item = _technical_signal_group_item(reason)
        key = "|".join([side, item["label"], item["freq"], item["event_date"], item["source_collection"]])
        if key in seen:
            continue
        seen.add(key)
        groups.setdefault(side, []).append(item)
    for key in list(groups):
        groups[key] = groups[key][:6]
    return groups


def _technical_signal_reason_labels(groups: dict[str, list[dict[str, Any]]], side: str) -> list[str]:
    labels: list[str] = []
    for item in groups.get(side, []):
        label = _text(item.get("label"))
        freq = _text(item.get("freq"))
        text = f"{freq} {label}".strip()
        if text and text not in labels:
            labels.append(text)
    return labels[:5]


def _timeframe_bucket(freq: Any) -> str:
    if _is_upper_freq(freq):
        return "upper"
    if _is_30m_freq(freq):
        return "trade"
    if _is_right_side_freq(freq):
        return "execution"
    return "other"


def _bucket_side(left_items: list[dict[str, Any]], right_items: list[dict[str, Any]]) -> str:
    if left_items and right_items:
        return "mixed"
    if right_items:
        return "right"
    if left_items:
        return "left"
    return "none"


def _timeframe_signal_sides(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {
        "upper": {"label": "日/周", "left": [], "right": []},
        "trade": {"label": "30m", "left": [], "right": []},
        "execution": {"label": "5m/15m", "left": [], "right": []},
    }
    seen: set[str] = set()
    for reason in _current_buy_technical_reasons(row):
        side = _technical_opportunity_side(reason)
        if side not in {"left", "right"}:
            continue
        bucket = _timeframe_bucket(reason.get("freq"))
        if bucket not in buckets:
            continue
        item = _technical_signal_group_item(reason)
        key = "|".join([bucket, side, item["label"], item["freq"], item["event_date"]])
        if key in seen:
            continue
        seen.add(key)
        buckets[bucket][side].append(item)
    for bucket in buckets.values():
        bucket["left"] = bucket["left"][:4]
        bucket["right"] = bucket["right"][:4]
        bucket["side"] = _bucket_side(bucket["left"], bucket["right"])
    return buckets


def _side_is_confirming(side: str) -> bool:
    return side in {"right", "mixed"}


def _missing_condition(blocked_by: list[str], fallback: str = "等新的技术确认") -> str:
    labels = [MISSING_GATE_LABELS.get(_text(code), _text(code)) for code in blocked_by if _text(code)]
    return " / ".join(labels) or fallback


def _stage_label(stage: str) -> str:
    return TRADE_STAGE_LABELS.get(stage, "盯盘池")


def _trade_stage_from_context(
    *,
    pool_type: str,
    entry_gate_status: str,
    blocked_by: list[str],
    top_buy: dict[str, Any] | None,
    top_risk: dict[str, Any] | None,
    timeframe_sides: dict[str, dict[str, Any]],
) -> str:
    if entry_gate_status == "clue_pool":
        return "clue_pool"
    if entry_gate_status == "left_attack_confirmed":
        return "left_attack"
    if entry_gate_status == "entry_attack_confirmed":
        return "attack_entry"
    del top_risk
    if pool_type == "risk" or entry_gate_status.startswith("blocked_by"):
        return "skip_now"
    trade_side = _text(timeframe_sides.get("trade", {}).get("side"))
    execution_side = _text(timeframe_sides.get("execution", {}).get("side"))
    upper_side = _text(timeframe_sides.get("upper", {}).get("side"))
    has_trade = trade_side != "none"
    has_execution = execution_side != "none"
    if entry_gate_status == "entry_confirmed" and _side_is_confirming(trade_side) and _side_is_confirming(execution_side):
        return "confirmed_entry"
    if has_trade and (entry_gate_status == "entry_waiting_right_side_confirm" or has_execution):
        return "probe_candidate"
    if trade_side == "left" or execution_side == "left" or upper_side == "left":
        return "dip_watch"
    if top_buy:
        return "watch_pool"
    if any(code == "missing_buy_technical" for code in blocked_by):
        return "clue_pool"
    return "watch_pool"


def _timeframe_reads(timeframe_sides: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        "big_cycle": {
            "label": "大周期",
            "freqs": "日/周",
            "side": _text(timeframe_sides.get("upper", {}).get("side")) or "none",
            "status": "有方向" if _text(timeframe_sides.get("upper", {}).get("side")) != "none" else "待确认",
            "evidence": timeframe_sides.get("upper", {}),
        },
        "trade_cycle": {
            "label": "交易周期",
            "freqs": "30m",
            "side": _text(timeframe_sides.get("trade", {}).get("side")) or "none",
            "status": "买点出现" if _side_is_confirming(_text(timeframe_sides.get("trade", {}).get("side"))) else "等买点",
            "evidence": timeframe_sides.get("trade", {}),
        },
        "order_cycle": {
            "label": "下单周期",
            "freqs": "5m/15m",
            "side": _text(timeframe_sides.get("execution", {}).get("side")) or "none",
            "status": "可复核" if _side_is_confirming(_text(timeframe_sides.get("execution", {}).get("side"))) else "等确认",
            "evidence": timeframe_sides.get("execution", {}),
        },
    }


def _chain_position(row: dict[str, Any]) -> dict[str, Any]:
    chain = row.get("chain_context") if isinstance(row.get("chain_context"), dict) else {}
    evidence = chain.get("evidence") if isinstance(chain.get("evidence"), dict) else {}
    position = {
        "chain_id": _text(chain.get("chain_id")),
        "node_id": _text(chain.get("node_id")),
        "chain": _text(chain.get("chain_name") or evidence.get("chain_name")),
        "node": _text(chain.get("node_name") or evidence.get("node_name")),
        "board_or_concept": _text(chain.get("board_or_concept") or evidence.get("board_or_concept")),
        "layer": _text(chain.get("layer") or evidence.get("layer")),
        "stage": _text(chain.get("stage") or evidence.get("stage")),
        "phase": _text(chain.get("phase") or evidence.get("phase")),
        "membership_type": _text(chain.get("membership_type") or evidence.get("membership_type")),
        "membership_confidence": _float(chain.get("membership_confidence") or evidence.get("confidence")),
        "exposure_score": _float(chain.get("exposure_score") or evidence.get("exposure_score")),
        "evidence_sources": chain.get("evidence_sources") or evidence.get("evidence_sources") or [],
        "role": "链主" if "chain_core_rep" in row.get("source_tags", []) or "source_leader" in row.get("source_tags", []) else "弹性/成分" if any(tag in row.get("source_tags", []) for tag in ("chain_elastic_rep", "constituent_hot")) else "",
    }
    if any(position.get(key) for key in ("chain", "node", "board_or_concept", "role")):
        return position
    symbol = _text(row.get("symbol") or row.get("code") or row.get("raw_code"))
    if not symbol:
        return position
    try:
        from signals.core.chain_map import get_all_chain_positions

        positions = get_all_chain_positions(symbol)
    except Exception:
        positions = []
    if not positions:
        return position
    primary = positions[0]
    return {
        **position,
        "chain": _text(getattr(primary, "chain_name", "")),
        "node": _text(getattr(primary, "role", "")),
        "layer": _text(getattr(primary, "position", "")),
        "stage": _text(getattr(primary, "position", "")),
        "role": _text(getattr(primary, "role", "")),
        "source": "industry_chains.yaml",
        "source_note": "代表标的静态映射",
        "confidence": "representative_only",
        "related_chains": list(getattr(primary, "related_chains", []) or [])[:3],
    }


def _bucket_side_labels(bucket: dict[str, Any]) -> str:
    if not isinstance(bucket, dict):
        return ""
    labels: list[str] = []
    for side_key in ("right", "left", "sell", "context"):
        for item in bucket.get(side_key) or []:
            if not isinstance(item, dict):
                continue
            freq = _text(item.get("freq"))
            label = _text(item.get("label"))
            if freq or label:
                labels.append(f"{freq} {label}".strip())
    return "、".join(labels[:3])


def _trade_intent_from_context(
    *,
    pool_type: str,
    entry_gate_status: str,
    trade_stage: str,
    timeframe_sides: dict[str, dict[str, Any]],
    blocked_by: list[str],
    top_buy: dict[str, Any] | None,
    top_risk: dict[str, Any] | None,
) -> str:
    del top_risk
    if pool_type == "risk" or entry_gate_status.startswith("blocked_by"):
        return "skip_now"
    if entry_gate_status == "left_attack_confirmed":
        return "left_attack"
    if entry_gate_status == "entry_attack_confirmed":
        return "attack_entry"
    if entry_gate_status == "entry_confirmed" or trade_stage == "confirmed_entry":
        return "confirmed_entry"
    trade_side = _text(timeframe_sides.get("trade", {}).get("side"))
    execution_side = _text(timeframe_sides.get("execution", {}).get("side"))
    upper_side = _text(timeframe_sides.get("upper", {}).get("side"))
    if not top_buy or any(code == "missing_buy_technical" for code in blocked_by):
        return "clue_only"
    if trade_side == "left" or upper_side == "left":
        return "left_dip"
    if entry_gate_status == "entry_waiting_right_side_confirm" or trade_stage == "probe_candidate":
        return "probe_candidate"
    if trade_side in {"right", "mixed"} or execution_side in {"right", "mixed"}:
        return "right_momentum"
    if upper_side in {"right", "mixed"}:
        return "wait_30m"
    if entry_gate_status == "entry_waiting_upper_context":
        return "wait_big_cycle"
    if entry_gate_status == "entry_waiting_30m_confirm":
        return "wait_30m"
    return "wait_30m"


def _trade_intent_label(intent: str) -> str:
    return TRADE_INTENT_LABELS.get(intent, "盯盘池")


def _setup_mode_from_context(
    *,
    pool_type: str,
    entry_gate_status: str,
    top_risk: dict[str, Any] | None,
) -> str:
    del top_risk
    if pool_type == "risk" or entry_gate_status.startswith("blocked_by"):
        return "risk_first"
    if entry_gate_status == "left_attack_confirmed":
        return "left_attack"
    if entry_gate_status in {"entry_attack_confirmed", "entry_confirmed"}:
        return "right_attack"
    if pool_type == "focus" and entry_gate_status in FOCUS_REVIEW_GATE_STATUSES:
        return "right_review"
    return "watch"


def _setup_explanation(intent: str, missing_condition: str) -> str:
    if intent == "confirmed_entry":
        return "30m买点和5m/15m下单周期都已确认，复核位置、止损和仓位。"
    if intent == "attack_entry":
        return "日/周背景和5m/15m右侧已经给出，30m未补齐，按进攻买点小仓复核。"
    if intent == "left_attack":
        return "左侧买点叠加10/20日线承接，按低吸进攻复核，不按追涨买点处理。"
    if intent == "right_momentum":
        return "已有右侧动量或大周期走强，但30m交易买点还没补齐。"
    if intent == "probe_candidate":
        return "30m或上级结构有动作，离买点近，等5m/15m下单确认。"
    if intent == "left_dip":
        return "偏左侧低吸，只能观察承接，不能当追涨买点。"
    if intent == "wait_big_cycle":
        return "短周期有动作，但日/周大周期还没站住。"
    if intent == "clue_only":
        return "只有来源或主题线索，还没有硬技术买点。"
    if intent == "skip_now":
        return "已有卖点、冲突、过期或产业链高潮，暂不参与。"
    return missing_condition or "盯盘等买点。"


def _entry_logic_summary(
    *,
    timeframe_sides: dict[str, dict[str, Any]],
    missing_condition: str,
    chain_position: dict[str, Any],
) -> str:
    trade = _bucket_side_labels(timeframe_sides.get("trade", {})) or "30m未确认"
    execution = _bucket_side_labels(timeframe_sides.get("execution", {})) or "5m/15m未确认"
    upper = _bucket_side_labels(timeframe_sides.get("upper", {})) or "日/周未确认"
    chain = _text(chain_position.get("chain") or chain_position.get("board_or_concept") or chain_position.get("node"))
    parts = [f"30m: {trade}", f"5m/15m: {execution}", f"日/周: {upper}"]
    if chain:
        parts.append(f"产业链: {chain}")
    if missing_condition and missing_condition != "买点路径已走通":
        parts.append(f"还差: {missing_condition}")
    return "；".join(parts)


def _chain_brief(chain_position: dict[str, Any]) -> str:
    values = [
        _text(chain_position.get("chain") or chain_position.get("board_or_concept")),
        _text(chain_position.get("node") or chain_position.get("role")),
    ]
    deduped: list[str] = []
    for value in values:
        if value and value not in deduped:
            deduped.append(value)
    return " · ".join(deduped[:2])


def _trade_role_from_context(
    row: dict[str, Any],
    *,
    chain_position: dict[str, Any],
    trade_stage: str,
    pool_type: str,
    blocked_by: list[str],
) -> str:
    chain_context = row.get("chain_context") if isinstance(row.get("chain_context"), dict) else {}
    phase = _text(row.get("chain_phase") or chain_position.get("phase"))
    chain_evidence = chain_context.get("evidence") if isinstance(chain_context.get("evidence"), dict) else {}
    exposure_bucket = _text(
        row.get("exposure_bucket")
        or chain_position.get("exposure_bucket")
        or chain_context.get("exposure_bucket")
        or chain_evidence.get("exposure_bucket")
    )
    has_chain_context = any(_text(chain_position.get(key) or chain_context.get(key)) for key in ("chain", "chain_name", "node", "node_name", "board_or_concept"))
    if pool_type == "risk" or trade_stage == "skip_now":
        if phase == "consensus_climax" or "chain_consensus_climax" in blocked_by:
            return "climax_risk"
        return "risk_review"
    if phase == "consensus_climax" or "chain_consensus_climax" in blocked_by:
        return "climax_risk"
    if exposure_bucket in {"defensive", "防守", "稳仓", "高股息", "低波"}:
        return "defensive_weight"
    if phase in {"cooling", "diverging"} or trade_stage in {"dip_watch", "left_attack", "probe_candidate"}:
        return "second_wave"
    if phase in {"accelerating", "warming"} or _float(row.get("theme_rank_bonus")) >= 12:
        return "mainline_attack"
    if trade_stage == "confirmed_entry":
        return "mainline_attack"
    if has_chain_context:
        return "chain_watch"
    return "ordinary_watch"


def _trader_read_summary(
    row: dict[str, Any],
    *,
    trade_role: str,
    chain_position: dict[str, Any],
    trade_stage: str,
    missing_condition: str,
) -> str:
    chain = _chain_brief(chain_position)
    prefix = TRADE_ROLE_LABELS.get(trade_role, "观察")
    if trade_role == "climax_risk":
        return f"{chain or '主线'}：一致高潮，确认买点不再推追高，等分歧后的承接。"
    if trade_role in {"chain_watch", "holding_chain"}:
        return f"{chain or '产业链'}：只说明已进入东财/同花顺板块图谱，不代表真实持仓；等30m承接和5m/15m下单确认。"
    if trade_role == "defensive_weight":
        return f"{chain or '防守观察'}：偏稳仓/防守，不和进攻票混排，回踩爬起再看仓位。"
    if trade_role == "second_wave" and trade_stage != "left_attack":
        return f"{chain or '回踩再起'}：先当回踩后二次启动观察，等重新放量和右侧确认。"
    if trade_role == "risk_review":
        return f"{chain or prefix}：先复核风险，卖点/冲突解除前不当机会处理。"
    if trade_stage == "attack_entry":
        return f"{chain or prefix}：进攻买点，日/周和5m/15m已确认，30m未补齐，按小仓和快复核处理。"
    if trade_stage == "left_attack":
        return f"{chain or prefix}：低吸进攻，左侧买点叠加10/20日线承接，先复核关键均线和止损。"
    if trade_stage == "confirmed_entry":
        return f"{chain or prefix}：买点路径已走通，先复核位置、止损和仓位。"
    return f"{chain or prefix}：处在观察到买点之间，{missing_condition or '等新的技术确认'}。"


def _evidence_summary(
    row: dict[str, Any],
    *,
    timeframe_sides: dict[str, dict[str, Any]],
    chain_position: dict[str, Any],
    missing_condition: str,
) -> str:
    technical = _entry_logic_summary(
        timeframe_sides=timeframe_sides,
        missing_condition=missing_condition,
        chain_position=chain_position,
    )
    sources = "/".join(_source_collections(row)[:2])
    chain = _chain_brief(chain_position)
    parts = []
    if chain:
        parts.append(f"产业链: {chain}")
    if chain_position.get("source_note"):
        parts.append(f"产业链来源: {chain_position.get('source_note')}")
    if sources:
        parts.append(f"来源: {sources}")
    if technical:
        parts.append(f"技术: {technical}")
    return "；".join(parts)


def _promotion_path_for_trade_stage(
    row: dict[str, Any],
    *,
    trade_stage: str,
    timeframe_sides: dict[str, dict[str, Any]],
    blocked_by: list[str],
) -> list[dict[str, Any]]:
    source_detail = "/".join(_source_collections(row)[:2])
    upper_side = _text(timeframe_sides.get("upper", {}).get("side"))
    trade_side = _text(timeframe_sides.get("trade", {}).get("side"))
    execution_side = _text(timeframe_sides.get("execution", {}).get("side"))
    blocked = trade_stage == "skip_now"
    return [
        {"key": "source", "status": "passed" if source_detail else "context", "detail": source_detail},
        {"key": "theme_alignment", "status": "passed" if row.get("theme_rank_bonus", 0) else "context", "detail": _text(row.get("theme_alignment_level"))},
        {"key": "upper_context", "status": "waiting" if "daily_or_weekly_missing" in blocked_by else "passed" if upper_side != "none" else "context", "detail": f"大周期 {upper_side or 'none'}"},
        {"key": "trigger_30m", "status": "waiting" if any(code.startswith("30m") for code in blocked_by) else "passed" if _side_is_confirming(trade_side) else "waiting", "detail": f"交易周期 {trade_side or 'none'}"},
        {"key": "right_side", "status": "waiting" if any(code.startswith("5m_or_15m") for code in blocked_by) else "passed" if _side_is_confirming(execution_side) else "waiting", "detail": f"下单周期 {execution_side or 'none'}"},
        {"key": "risk_clear", "status": "blocked" if blocked else "passed", "detail": _missing_condition(blocked_by, "无主要冲突") if blocked else "无主要冲突"},
    ]


def _opportunity_side_from_groups(
    *,
    pool_type: str,
    entry_gate_status: str,
    groups: dict[str, list[dict[str, Any]]],
    top_risk: dict[str, Any] | None,
) -> str:
    del top_risk
    if pool_type == "risk":
        return "risk"
    if entry_gate_status == "entry_confirmed" and groups.get("right"):
        return "right"
    if groups.get("left"):
        return "left"
    if groups.get("right"):
        return "right"
    return "context"


def _opportunity_side_label(side: str) -> str:
    labels = {
        "left": "低吸观察",
        "right": "确认买点",
        "risk": "暂不参与",
        "context": "线索池",
    }
    return labels.get(side, "线索池")


def _risk_level_for_reason(reason: dict[str, Any] | None) -> str:
    if not isinstance(reason, dict) or not reason:
        return "none"
    decision_effect = _text(reason.get("decision_effect"))
    knowledge_effect = _text(reason.get("knowledge_effect"))
    signal_side = _text(reason.get("signal_side"))
    score = abs(_float(reason.get("score")))
    weight = _float(reason.get("weight"))
    if decision_effect in {"block", "exit_priority"} or knowledge_effect in {"block", "exit_priority"}:
        return "high"
    if signal_side == "sell" and (score >= 80.0 or weight >= 180.0):
        return "high"
    return "medium"


def _left_setup_reason_codes(row: dict[str, Any], top_buy: dict[str, Any] | None) -> list[str]:
    codes: list[str] = []
    buy_reasons = _buy_technical_reasons(row)
    for reason in buy_reasons:
        text = _reason_signal_text(reason)
        freq = _text(reason.get("freq"))
        if any(token in text for token in ("一买", "背驰买", "底背离", "MACD绿柱缩小", "衰竭缺口")):
            codes.append("divergence_or_exhaustion")
        if _is_upper_freq(freq):
            codes.append("upper_timeframe_left_context")
    if any(tag in row.get("source_tags", []) for tag in ("knowledge_confirmed", "knowledge_watch", "chain_context", "chain_core_rep", "chain_elastic_rep")):
        codes.append("theme_or_knowledge_context")
    if top_buy and _reason_age_trading_days(top_buy) not in (None, 0):
        codes.append("not_same_day_trigger")
    return list(dict.fromkeys(codes))[:4]


def _right_confirm_reason_codes(row: dict[str, Any]) -> list[str]:
    buy_reasons = _buy_technical_reasons(row)
    codes: list[str] = []
    if _has_current_reason_for_freq(buy_reasons, _is_30m_freq):
        codes.append("trigger_30m")
    if _has_current_reason_for_freq(buy_reasons, _is_right_side_freq):
        codes.append("right_5m_15m")
    if _has_current_reason_for_freq(buy_reasons, _is_upper_freq):
        codes.append("daily_weekly_context")
    if any("突破" in _reason_signal_text(reason) or "三买" in _reason_signal_text(reason) or "趋势买" in _reason_signal_text(reason) for reason in buy_reasons):
        codes.append("breakout_or_trend_confirm")
    return list(dict.fromkeys(codes))[:4]


def _strategy_semantics(
    row: dict[str, Any],
    *,
    pool_type: str,
    entry_gate_status: str,
    trade_stage: str,
    top_buy: dict[str, Any] | None,
    top_risk: dict[str, Any] | None,
) -> dict[str, Any]:
    left_reasons = _left_setup_reason_codes(row, top_buy)
    right_reasons = _right_confirm_reason_codes(row)
    label = _stage_label(trade_stage)
    if trade_stage == "skip_now":
        intervention_side = "risk_exit"
        lineage = ["pangge", "system"]
    elif trade_stage == "confirmed_entry":
        intervention_side = "hybrid" if left_reasons else "right_confirmed"
        lineage = ["pangge", "system"] + (["daozhang"] if left_reasons else [])
    elif trade_stage == "attack_entry":
        intervention_side = "right_confirmed"
        lineage = ["pangge", "system"]
    elif trade_stage == "left_attack":
        intervention_side = "left_attack"
        lineage = ["daozhang", "system"]
    elif trade_stage in {"dip_watch", "probe_candidate"}:
        intervention_side = "left_setup"
        lineage = ["daozhang", "system"]
    elif trade_stage == "watch_pool":
        intervention_side = "left_setup" if left_reasons or top_buy else "context"
        lineage = ["daozhang", "system"] if left_reasons or top_buy else ["system"]
    else:
        intervention_side = "context"
        lineage = ["system"]
    semantics = {
        "trade_stage": trade_stage,
        "stage_label": label,
        "intervention_side": intervention_side,
        "intervention_label": label,
        "strategy_lineage": list(dict.fromkeys(lineage)),
        "left_setup_reasons": left_reasons,
        "right_confirm_reasons": right_reasons,
        "risk_policy": "暂不参与" if intervention_side == "risk_exit" else "先看位置和买点",
    }
    return semantics


def _finalize_pool_row(
    row: dict[str, Any],
    *,
    pool_type: str,
    rank_score: float,
    score_components: dict[str, float],
    entry_gate_status: str,
    blocked_by: list[str],
    top_buy: dict[str, Any] | None,
    top_risk: dict[str, Any] | None,
) -> dict[str, Any]:
    row["pool_type"] = pool_type
    row["rank_score"] = round(rank_score, 3)
    row["sort_score"] = row["rank_score"]
    row["score_components"] = score_components
    row["rank_reason"] = _rank_reason(score_components)
    row["entry_gate_status"] = entry_gate_status
    row["blocked_by"] = blocked_by
    row["missing_gates"] = blocked_by
    row["top_buy_reason"] = top_buy or {}
    row["top_risk_reason"] = top_risk or {}
    row["source_collections"] = _source_collections(row)
    row["coverage_status"] = "required_freqs_present" if entry_gate_status == "entry_confirmed" else entry_gate_status
    technical_groups = _technical_signal_groups(row)
    opportunity_side = _opportunity_side_from_groups(
        pool_type=pool_type,
        entry_gate_status=entry_gate_status,
        groups=technical_groups,
        top_risk=top_risk,
    )
    row["opportunity_side"] = opportunity_side
    row["opportunity_label"] = _opportunity_side_label(opportunity_side)
    row["technical_signal_groups"] = {key: value for key, value in technical_groups.items() if value}
    row["left_signal_reasons"] = _technical_signal_reason_labels(technical_groups, "left")
    row["right_signal_reasons"] = _technical_signal_reason_labels(technical_groups, "right")
    risk_signal_reasons = _technical_signal_reason_labels(technical_groups, "sell")
    risk_marker = ""
    risk_marker_reason_type = ""
    if isinstance(top_risk, dict) and top_risk:
        risk_marker = _text(top_risk.get("signal_type") or top_risk.get("reason_type"))
        risk_marker_reason_type = _text(top_risk.get("reason_type"))
        if risk_marker and risk_marker not in risk_signal_reasons:
            risk_signal_reasons.append(risk_marker)
    row["risk_signal_reasons"] = risk_signal_reasons[:4]
    row["risk_marked"] = bool(top_risk)
    row["risk_marker"] = risk_marker
    row["risk_marker_reason_type"] = risk_marker_reason_type
    row["risk_level"] = _risk_level_for_reason(top_risk)
    best_ma = _best_ma_alignment(row, _current_buy_technical_reasons(row), include_row_level=False)
    if best_ma:
        row["ma_alignment"] = best_ma
    else:
        row.pop("ma_alignment", None)
    timeframe_sides = _timeframe_signal_sides(row)
    row["trade_timeframe"] = "30m"
    row["timeframe_signal_sides"] = timeframe_sides
    row["upper_timeframe_side"] = timeframe_sides["upper"]["side"]
    row["trade_timeframe_side"] = timeframe_sides["trade"]["side"]
    row["execution_timeframe_side"] = timeframe_sides["execution"]["side"]
    row["chain_phase"] = _text((row.get("chain_context") or {}).get("phase")) if isinstance(row.get("chain_context"), dict) else ""
    theme_rank_bonus = _mainline_alignment_score(row)
    row["theme_rank_bonus"] = theme_rank_bonus
    row["theme_alignment_level"] = _mainline_alignment_level(theme_rank_bonus)
    sector_policy = _sector_policy(row)
    row["sector_policy"] = sector_policy.get("policy")
    row["sector_policy_label"] = sector_policy.get("label")
    row["sector_policy_reason"] = sector_policy.get("reason")
    row["sector_policy_matched_token"] = sector_policy.get("matched_token")
    row["sector_policy_source"] = sector_policy.get("source")
    row["mainline_status"] = _mainline_status(row)
    row["mainline_confirmation_reason"] = _mainline_confirmation_reason(row)
    row["mainline_rank_tier"] = _focus_mainline_rank_tier(row)
    if isinstance(row.get("broad_market_context"), dict):
        row["broad_market_label"] = row["broad_market_context"].get("label")
        row["market_volume_label"] = row["broad_market_context"].get("volume_label")
        row["market_volume_state"] = row["broad_market_context"].get("volume_state")
        row["market_volume_ratio"] = row["broad_market_context"].get("volume_ratio_5d")
    row.update(_market_setup_alignment(row, entry_gate_status, top_buy, top_risk))
    setup_bias = _market_setup_bias(pool_type, entry_gate_status, top_risk)
    row["market_setup_bias"] = setup_bias
    row["setup_rank_tier"] = SETUP_RANK_TIERS.get(setup_bias, 0)
    row["left_allowed_reason"] = _left_allowed_reason(row) if setup_bias == "left_review" else ""
    trade_stage = _trade_stage_from_context(
        pool_type=pool_type,
        entry_gate_status=entry_gate_status,
        blocked_by=blocked_by,
        top_buy=top_buy,
        top_risk=top_risk,
        timeframe_sides=timeframe_sides,
    )
    if pool_type == "focus" and entry_gate_status == "entry_confirmed":
        trade_stage = "confirmed_entry"
    elif pool_type == "risk":
        trade_stage = "skip_now"
    stage_label = _stage_label(trade_stage)
    row["trade_stage"] = trade_stage
    row["stage_label"] = stage_label
    row["current_position"] = stage_label
    row["decision_stage"] = TRADE_STAGE_LEGACY_DECISION.get(trade_stage, "watch_preheat")
    row["timeframe_reads"] = _timeframe_reads(timeframe_sides)
    chain_position = _chain_position(row)
    row["chain_position"] = chain_position
    row["missing_condition"] = _missing_condition(blocked_by, "买点路径已走通" if trade_stage == "confirmed_entry" else "等新的技术确认")
    row["primary_blocker"] = row["missing_condition"]
    row["recommended_action"] = TRADE_STAGE_ACTIONS.get(trade_stage, "盯盘等买点")
    row["entry_reason"] = " / ".join(_technical_signal_reason_labels(technical_groups, "right") + _technical_signal_reason_labels(technical_groups, "left")) or row.get("reason") or stage_label
    row["invalidation"] = row.get("invalidates_when") or "触发条件失效或关键位被破坏"
    trade_intent = _trade_intent_from_context(
        pool_type=pool_type,
        entry_gate_status=entry_gate_status,
        trade_stage=trade_stage,
        timeframe_sides=timeframe_sides,
        blocked_by=blocked_by,
        top_buy=top_buy,
        top_risk=top_risk,
    )
    row["trade_intent"] = trade_intent
    row["trade_intent_label"] = _trade_intent_label(trade_intent)
    setup_mode = _setup_mode_from_context(
        pool_type=pool_type,
        entry_gate_status=entry_gate_status,
        top_risk=top_risk,
    )
    row["setup_mode"] = setup_mode
    row["setup_mode_label"] = SETUP_MODE_LABELS.get(setup_mode, "观察")
    row["setup_side_label"] = row["trade_intent_label"]
    row["watch_sort_priority"] = TRADE_INTENT_PRIORITY.get(trade_intent, 0)
    row["setup_explanation"] = _setup_explanation(trade_intent, row["missing_condition"])
    row["entry_logic_summary"] = _entry_logic_summary(
        timeframe_sides=timeframe_sides,
        missing_condition=row["missing_condition"],
        chain_position=chain_position,
    )
    row["promotion_path"] = _promotion_path_for_trade_stage(row, trade_stage=trade_stage, timeframe_sides=timeframe_sides, blocked_by=blocked_by)
    semantics = _strategy_semantics(
        row,
        pool_type=pool_type,
        entry_gate_status=entry_gate_status,
        trade_stage=trade_stage,
        top_buy=top_buy,
        top_risk=top_risk,
    )
    row["strategy_semantics"] = semantics
    row["intervention_side"] = semantics["intervention_side"]
    row["intervention_label"] = semantics["intervention_label"]
    row["strategy_lineage"] = semantics["strategy_lineage"]
    row["left_setup_reasons"] = semantics["left_setup_reasons"]
    row["right_confirm_reasons"] = semantics["right_confirm_reasons"]
    row["opportunity_label"] = stage_label
    primary_reason = top_risk if pool_type == "risk" else (top_buy or top_risk)
    if isinstance(primary_reason, dict) and primary_reason:
        row["event_latest_dt"] = _reason_event_dt(primary_reason)
        age = _reason_age_trading_days(primary_reason)
        if age is not None:
            row["signal_age_trading_days"] = age
            row["stale_context"] = not _reason_is_current_for_entry(primary_reason)
    stale_reasons = [
        reason
        for reason in row.get("inclusion_reasons") or []
        if isinstance(reason, dict) and _is_technical_reason(reason) and not _reason_is_current_for_entry(reason)
    ]
    if stale_reasons:
        row["stale_signal_count"] = len(stale_reasons)
    if pool_type == "focus":
        attack_entry = entry_gate_status == "entry_attack_confirmed"
        left_attack = entry_gate_status == "left_attack_confirmed"
        right_review = entry_gate_status in FOCUS_REVIEW_GATE_STATUSES
        row["action_status"] = "left_attack" if left_attack else "attack_entry" if attack_entry else entry_gate_status if right_review else "entry_ready"
        row["trader_action"] = "低吸进攻复核" if left_attack else "进攻买点复核" if attack_entry else "右侧买点复核" if right_review else "确认买点复核"
        row["next_action"] = row["trader_action"]
        row["queue_lane"] = "left_review" if left_attack else "entry_waiting_confirm" if (attack_entry or right_review) else "entry_ready"
        row["actionability"] = "left_review" if left_attack else "entry_waiting_confirm" if (attack_entry or right_review) else "entry_ready"
        row["decision_effect"] = "left_review" if left_attack else "attack_confirm" if attack_entry else "right_review" if right_review else "confirm"
        row["signal_origin"] = _text((top_buy or {}).get("reason_type")) or row.get("signal_origin")
        row["latest_signal"] = _text((top_buy or {}).get("signal_type")) or row.get("latest_signal")
        if left_attack:
            row["invalidates_when"] = "跌回10/20日线、左侧买点失效或产业链风险升温"
        elif attack_entry:
            row["invalidates_when"] = "5m/15m转弱、30m迟迟不补、日/周转冲突或产业链高潮"
        elif right_review:
            row["invalidates_when"] = "缺口周期迟迟不补、右侧动量转弱或产业链风险升温"
        else:
            row["invalidates_when"] = "30m买点失效、下单周期转弱、日/周冲突或产业链高潮"
    elif pool_type == "risk":
        row["action_status"] = "skip_now"
        row["trader_action"] = "暂不参与"
        row["next_action"] = row["trader_action"]
        row["queue_lane"] = "risk_exit_first"
        row["actionability"] = "risk_exit_first"
        row["decision_effect"] = "exit_priority"
        row["signal_origin"] = _text((top_risk or {}).get("reason_type")) or row.get("signal_origin")
        row["latest_signal"] = _text((top_risk or {}).get("signal_type")) or row.get("latest_signal")
        row["invalidates_when"] = "卖点/冲突解除，并重新走出30m和5m/15m买点"
    else:
        if trade_stage == "skip_now":
            row["action_status"] = "skip_now"
            row["trader_action"] = "暂不参与"
            row["queue_lane"] = "context_only"
        elif entry_gate_status == "entry_waiting_30m_confirm":
            row["action_status"] = "entry_waiting_30m_confirm"
            row["trader_action"] = "盯盘等30m买点"
            row["queue_lane"] = "watch_preheat"
        elif entry_gate_status == "entry_waiting_upper_context":
            row["action_status"] = "entry_waiting_upper_context"
            row["trader_action"] = "盯盘等大周期"
            row["queue_lane"] = "watch_preheat"
        elif entry_gate_status == "entry_waiting_right_side_confirm":
            row["action_status"] = "entry_waiting_right_side_confirm"
            row["trader_action"] = "等下单周期确认"
            row["queue_lane"] = "watch_preheat"
        elif entry_gate_status == "entry_waiting_defensive_confirmation":
            row["action_status"] = "entry_waiting_defensive_confirmation"
            row["trader_action"] = "防守板块等完整确认"
            row["queue_lane"] = "watch_preheat"
        elif entry_gate_status == "entry_waiting_resonance_confirm":
            row["action_status"] = "entry_waiting_resonance_confirm"
            row["trader_action"] = "盯盘等共振"
            row["queue_lane"] = "watch_preheat"
        elif entry_gate_status.startswith("blocked_by"):
            row["action_status"] = "skip_now"
            row["trader_action"] = "暂不参与"
            row["queue_lane"] = "context_only"
        elif trade_stage == "clue_pool":
            row["action_status"] = "clue_pool"
            row["trader_action"] = "线索先放着"
            row["queue_lane"] = "watch_preheat"
        elif trade_stage == "dip_watch":
            row["action_status"] = "dip_watch"
            row["trader_action"] = "低吸观察"
            row["queue_lane"] = "watch_preheat"
        elif trade_stage == "left_attack":
            row["action_status"] = "left_attack"
            row["trader_action"] = "低吸进攻复核"
            row["queue_lane"] = "left_review"
        elif trade_stage == "probe_candidate":
            row["action_status"] = "probe_candidate"
            row["trader_action"] = "试仓候选"
            row["queue_lane"] = "entry_waiting_confirm"
        else:
            row["action_status"] = row.get("action_status") if row.get("action_status") != "risk_review" else "watch"
            row["trader_action"] = row.get("trader_action") or "盯盘观察"
            row["queue_lane"] = row.get("queue_lane") if row.get("queue_lane") != "risk_exit_first" else "watch_preheat"
        row["next_action"] = row["trader_action"]
        row["actionability"] = "observe_only"
        row["decision_effect"] = "context_only"
        row["invalidates_when"] = row.get("invalidates_when") or "买点没走出来、信号过期或关键位被破坏"
    row["recommended_action"] = row["trader_action"]
    row["invalidation"] = row["invalidates_when"]
    trade_role = _trade_role_from_context(
        row,
        chain_position=chain_position,
        trade_stage=trade_stage,
        pool_type=pool_type,
        blocked_by=blocked_by,
    )
    row["trade_role"] = trade_role
    row["trade_role_label"] = TRADE_ROLE_LABELS.get(trade_role, "观察")
    row["trade_identity"] = trade_role
    row["trade_identity_label"] = row["trade_role_label"]
    row["primary_chain"] = _text(chain_position.get("chain"))
    row["chain_node"] = _text(chain_position.get("node"))
    row["chain_phase"] = _text(chain_position.get("phase") or row.get("chain_phase"))
    row["membership_confidence"] = _float(chain_position.get("membership_confidence"))
    row["evidence_sources"] = [item for item in chain_position.get("evidence_sources") or [] if _text(item)]
    row["can_trade_now"] = bool(
        pool_type == "focus"
        and entry_gate_status in {"entry_confirmed", "entry_attack_confirmed"}
        and trade_role not in {"climax_risk", "risk_review"}
    )
    row["trader_read"] = _trader_read_summary(
        row,
        trade_role=trade_role,
        chain_position=chain_position,
        trade_stage=trade_stage,
        missing_condition=row["missing_condition"],
    )
    row["ai_trade_summary"] = row["trader_read"]
    row["evidence_summary"] = _evidence_summary(
        row,
        timeframe_sides=timeframe_sides,
        chain_position=chain_position,
        missing_condition=row["missing_condition"],
    )
    row["reason"] = " · ".join(
        _text(reason.get("signal_type") or reason.get("reason_type"))
        for reason in (top_buy, top_risk)
        if isinstance(reason, dict) and _text(reason.get("signal_type") or reason.get("reason_type"))
    ) or row.get("reason")
    return row


def _assign_pool_ranks(rows: list[dict[str, Any]]) -> None:
    for index, row in enumerate(rows, start=1):
        row["rank"] = index


def _slim_ma_alignment_for_pool(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    keys = (
        "latest_close",
        "ma5",
        "ma8",
        "ma10",
        "ma13",
        "ma20",
        "ma21",
        "ma34",
        "ma55",
        "ma89",
        "above_ma5",
        "above_ma8",
        "above_ma10",
        "above_ma13",
        "above_ma20",
        "above_ma21",
        "above_ma34",
        "above_ma55",
        "above_ma89",
        "near_ma5",
        "near_ma8",
        "near_ma10",
        "near_ma13",
        "near_ma20",
        "near_ma21",
        "near_ma34",
        "near_ma55",
        "near_ma89",
        "reclaim_ma5",
        "reclaim_ma8",
        "reclaim_ma10",
        "reclaim_ma13",
        "reclaim_ma20",
        "reclaim_ma21",
        "reclaim_ma34",
        "reclaim_ma55",
        "reclaim_ma89",
        "distance_ma5_pct",
        "distance_ma8_pct",
        "distance_ma10_pct",
        "distance_ma13_pct",
        "distance_ma20_pct",
        "distance_ma21_pct",
        "distance_ma34_pct",
        "distance_ma55_pct",
        "distance_ma89_pct",
        "ma_stack",
        "ma20_direction",
        "above_count",
        "reclaim_count",
        "fib_above_count",
        "fib_reclaim_count",
        "fib_support_score",
        "score",
        "summary",
        "tags",
    )
    return {key: value.get(key) for key in keys if value.get(key) not in (None, "", [], {})}


def _slim_resonance_for_pool(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    keys = ("direction", "primary_freq", "aligned_freqs", "conflict_freqs", "grade", "tags", "summary", "latest_dt")
    return {key: value.get(key) for key in keys if value.get(key) not in (None, "", [], {})}


def _slim_evidence_for_pool(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    keep_keys = (
        "source",
        "dedupe_key",
        "direction",
        "phase",
        "rank",
        "heat_score",
        "range_pattern",
        "leader_change_pct",
        "momentum_5m",
        "momentum_15m",
        "momentum_30m",
        "mapping_confidence",
        "integrated_count",
        "covered_security_count",
        "primary_security_count",
        "membership_type",
        "confidence",
        "exposure_score",
        "source_policy",
        "author",
        "review_date",
        "sector_keyword",
        "snippet",
        "sentiment",
    )
    out = {key: value.get(key) for key in keep_keys if value.get(key) not in (None, "", [], {})}
    details = value.get("details")
    if isinstance(details, str) and details:
        out["details"] = details[:240]
    elif isinstance(details, dict):
        detail_keys = ("signal", "reason", "summary", "pattern", "freq", "price", "dt")
        out["details"] = {key: details.get(key) for key in detail_keys if details.get(key) not in (None, "", [], {})}
    if isinstance(value.get("sources"), list):
        out["sources"] = value["sources"][:4]
    if isinstance(value.get("catalysts"), list):
        out["catalysts"] = value["catalysts"][:4]
    if isinstance(value.get("evidence_sources"), list):
        out["evidence_sources"] = value["evidence_sources"][:4]
    entry_factor = value.get("entry_factor")
    if isinstance(entry_factor, dict):
        entry_factor_keys = (
            "group",
            "type",
            "price",
            "today_high",
            "previous_high",
            "breakout_pct",
            "five_day_gain_pct",
            "volume_ratio",
            "max_drawdown_pct",
            "max_close_drawdown_pct",
            "three_day_change_pct",
            "twenty_day_gain_pct",
            "high_proximity_pct",
            "close_position_avg",
            "strong_close_days",
            "recent_volume_ratio",
            "score",
            "confidence",
            "date",
            "date_str",
        )
        out["entry_factor"] = {
            key: entry_factor.get(key)
            for key in entry_factor_keys
            if entry_factor.get(key) not in (None, "", [], {})
        }
    resonance = _slim_resonance_for_pool(value.get("resonance_context"))
    if resonance:
        out["resonance_context"] = resonance
    ma_alignment = _slim_ma_alignment_for_pool(value.get("ma_alignment"))
    if ma_alignment:
        out["ma_alignment"] = ma_alignment
    return out


def _slim_reason_for_pool(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out = dict(value)
    out["evidence"] = _slim_evidence_for_pool(value.get("evidence"))
    resonance = _slim_resonance_for_pool(value.get("resonance_context"))
    if resonance:
        out["resonance_context"] = resonance
    elif "resonance_context" in out:
        out.pop("resonance_context", None)
    ma_alignment = _slim_ma_alignment_for_pool(value.get("ma_alignment"))
    if ma_alignment:
        out["ma_alignment"] = ma_alignment
    elif "ma_alignment" in out:
        out.pop("ma_alignment", None)
    return {key: item for key, item in out.items() if item not in (None, "", [], {})}


def _slim_context_for_pool(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out = dict(value)
    if isinstance(out.get("evidence"), dict):
        out["evidence"] = _slim_evidence_for_pool(out.get("evidence"))
    return {key: item for key, item in out.items() if item not in (None, "", [], {})}


def _slim_pool_row_for_storage(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["inclusion_reasons"] = [
        slim
        for slim in (_slim_reason_for_pool(reason) for reason in row.get("inclusion_reasons") or [])
        if slim
    ][:12]
    for key in ("top_buy_reason", "top_risk_reason", "technical_evidence"):
        slim = _slim_reason_for_pool(row.get(key))
        if slim:
            out[key] = slim
        else:
            out.pop(key, None)
    ma_alignment = _slim_ma_alignment_for_pool(row.get("ma_alignment"))
    if ma_alignment:
        out["ma_alignment"] = ma_alignment
    else:
        out.pop("ma_alignment", None)
    resonance = _slim_resonance_for_pool(row.get("resonance_context"))
    if resonance:
        out["resonance_context"] = resonance
    elif "resonance_context" in out:
        out.pop("resonance_context", None)
    for key in ("knowledge_confirmation", "chain_context", "chain_position"):
        context = _slim_context_for_pool(row.get(key))
        if context:
            out[key] = context
        else:
            out.pop(key, None)
    return out


def _slim_skipped_row_for_storage(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "symbol",
        "code",
        "raw_code",
        "name",
        "rank",
        "pool_type",
        "rank_score",
        "score",
        "signal_origin",
        "latest_signal",
        "rank_reason",
        "setup_mode",
        "setup_mode_label",
        "trade_stage",
        "trade_intent",
        "trade_intent_label",
        "watch_sort_priority",
        "source_tags",
    )
    out = {key: row.get(key) for key in keys if row.get(key) not in (None, "", [], {})}
    reasons = []
    for reason in row.get("inclusion_reasons") or []:
        if not isinstance(reason, dict):
            continue
        reasons.append({
            key: reason.get(key)
            for key in ("reason_type", "source_collection", "signal_type", "signal_side", "freq")
            if reason.get(key) not in (None, "", [], {})
        })
        if len(reasons) >= 4:
            break
    if reasons:
        out["inclusion_reasons"] = reasons
    return out


def _prepare_pool_rows(rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = list(rows.values())
    for row in ordered:
        row["inclusion_reasons"] = sorted(row.get("inclusion_reasons") or [], key=lambda item: _float(item.get("weight")), reverse=True)[:12]
        row["exit_condition"] = row.get("invalidates_when")
        row["next_action"] = row.get("next_action") or row.get("trader_action") or "观察"
        row["queue_lane"] = row.get("queue_lane") or "context_only"
        row["actionability"] = row.get("actionability") or "context_only"
        row["decision_effects"] = [
            _text(item.get("decision_effect"))
            for item in row["inclusion_reasons"]
            if _text(item.get("decision_effect"))
        ][:6]
        if not isinstance(row.get("technical_evidence"), dict) or not row.get("technical_evidence"):
            row["technical_evidence"] = {"status": "missing", "note": "watch_only_not_buy_candidate"}
        if not isinstance(row.get("knowledge_confirmation"), dict) or not row.get("knowledge_confirmation"):
            row["knowledge_confirmation"] = {"status": "none"}
    return ordered


def _split_pool_rows(
    rows: dict[str, dict[str, Any]],
    *,
    focus_limit: int,
    risk_limit: int,
    watch_limit: int,
) -> dict[str, Any]:
    focus: list[dict[str, Any]] = []
    risk: list[dict[str, Any]] = []
    watch: list[dict[str, Any]] = []
    prepared = _prepare_pool_rows(rows)
    for row in prepared:
        entry_ok, gate_status, blocked_by, top_buy, top_risk = _entry_gate(row)
        if top_risk and not top_buy:
            components = _risk_components(row, top_risk)
            risk.append(_finalize_pool_row(
                row,
                pool_type="risk",
                rank_score=sum(components.values()),
                score_components=components,
                entry_gate_status=gate_status,
                blocked_by=blocked_by,
                top_buy=top_buy,
                top_risk=top_risk,
            ))
        elif entry_ok and top_buy and _entry_allowed_in_focus(row, gate_status) and _alignment_allows_focus(row, gate_status, top_buy, top_risk) and not (
            gate_status == "left_attack_confirmed"
            and not _left_attack_allowed_in_focus(row, gate_status)
        ):
            components = _entry_components(row, top_buy)
            components.update(_market_alignment_components(row, gate_status, top_buy, top_risk))
            focus.append(_finalize_pool_row(
                row,
                pool_type="focus",
                rank_score=sum(components.values()),
                score_components=components,
                entry_gate_status=gate_status,
                blocked_by=blocked_by,
                top_buy=top_buy,
                top_risk=top_risk,
            ))
        elif top_buy and _right_review_allowed_in_focus(row, gate_status, top_buy) and _alignment_allows_focus(row, gate_status, top_buy, top_risk):
            components = _focus_review_components(row, top_buy)
            components.update(_market_alignment_components(row, gate_status, top_buy, top_risk))
            focus.append(_finalize_pool_row(
                row,
                pool_type="focus",
                rank_score=sum(components.values()),
                score_components=components,
                entry_gate_status=gate_status,
                blocked_by=blocked_by,
                top_buy=top_buy,
                top_risk=top_risk,
            ))
        else:
            components = _watch_components(row, gate_status)
            components.update(_market_alignment_components(row, gate_status, top_buy, top_risk))
            watch.append(_finalize_pool_row(
                row,
                pool_type="watch",
                rank_score=sum(components.values()),
                score_components=components,
                entry_gate_status=gate_status,
                blocked_by=blocked_by,
                top_buy=top_buy,
                top_risk=top_risk,
            ))
    focus.sort(
        key=lambda item: (
            _float(item.get("setup_rank_tier")),
            _float(item.get("mainline_rank_tier")),
            _float(item.get("rank_score")),
            _float(item.get("score")),
        ),
        reverse=True,
    )
    risk.sort(key=lambda item: (_float(item.get("rank_score")), _float(item.get("score"))), reverse=True)
    for bucket in (focus, risk):
        _assign_pool_ranks(bucket)
    watch.sort(
        key=lambda item: (
            _float(item.get("rank_score")),
            _float((item.get("score_components") or {}).get("timeframe_priority")),
            _float((item.get("score_components") or {}).get("multi_period_bonus")),
            _float((item.get("score_components") or {}).get("indicator_breadth")),
            _float((item.get("score_components") or {}).get("buy_point_quality")),
            _float((item.get("score_components") or {}).get("ma_alignment")),
            _float((item.get("score_components") or {}).get("fib_ma_support")),
            _float((item.get("score_components") or {}).get("hot_sector")),
            _float(item.get("score")),
        ),
        reverse=True,
    )
    _assign_pool_ranks(watch)
    return {
        "focus": focus[:focus_limit],
        "risk": risk[:risk_limit],
        "watch": watch[:watch_limit],
        "skipped": {
            "focus": focus[focus_limit:],
            "risk": risk[risk_limit:],
            "watch": watch[watch_limit:],
        },
        "pool_counts": {
            "focus": len(focus),
            "risk": len(risk),
            "watch": len(watch),
            "total": len(prepared),
        },
    }


def _backfill_watch_from_clue_candidates(
    watch_stocks: list[dict[str, Any]],
    clue_candidates: list[dict[str, Any]],
    *,
    clue_symbols: set[str],
    watch_limit: int,
) -> int:
    """Use clue overflow as observe-only watch candidates without loosening focus."""
    if len(watch_stocks) >= watch_limit:
        return 0
    watch_symbols = {row.get("symbol") for row in watch_stocks if row.get("symbol")}
    added = 0
    for source_row in clue_candidates:
        symbol = source_row.get("symbol")
        if not symbol or symbol in clue_symbols or symbol in watch_symbols:
            continue
        row = dict(source_row)
        row["inclusion_reasons"] = sorted(
            row.get("inclusion_reasons") or [],
            key=lambda item: _float(item.get("weight")),
            reverse=True,
        )[:12]
        entry_ok, gate_status, blocked_by, top_buy, top_risk = _entry_gate(row)
        if entry_ok or top_risk:
            continue
        components = _watch_components(row, gate_status)
        components["clue_quality"] = _clue_quality_score(row)
        finalized = _finalize_pool_row(
            row,
            pool_type="watch",
            rank_score=sum(components.values()),
            score_components=components,
            entry_gate_status=gate_status,
            blocked_by=blocked_by,
            top_buy=top_buy,
            top_risk=top_risk,
        )
        finalized["clue_quality_score"] = components["clue_quality"]
        finalized["watch_backfill_source"] = "clue_overflow"
        finalized["promotion_gates"] = blocked_by
        watch_stocks.append(finalized)
        watch_symbols.add(symbol)
        added += 1
        if len(watch_stocks) >= watch_limit:
            break
    return added


def _candidate_meta(rows: dict[str, dict[str, Any]]) -> dict[str, dict[str, int]]:
    by_source: Counter[str] = Counter()
    by_side: Counter[str] = Counter()
    by_freq: Counter[str] = Counter()
    for row in rows.values():
        for reason in row.get("inclusion_reasons") or []:
            if not isinstance(reason, dict):
                continue
            by_source[_text(reason.get("source_collection")) or "unknown"] += 1
            by_side[_text(reason.get("signal_side")) or "unknown"] += 1
            by_freq[_text(reason.get("freq")) or "unknown"] += 1
    return {
        "candidate_counts_by_source": dict(by_source),
        "candidate_counts_by_side": dict(by_side),
        "candidate_counts_by_freq": dict(by_freq),
    }


def _selected_rows(rows: dict[str, dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = _prepare_pool_rows(rows)
    for row in ordered:
        row["reason"] = " · ".join(
            _text(item.get("signal_type") or item.get("reason_type"))
            for item in row["inclusion_reasons"][:2]
            if _text(item.get("signal_type") or item.get("reason_type"))
        )
    ordered.sort(key=lambda item: (_float(item.get("sort_score")), _float(item.get("score"))), reverse=True)
    return ordered[:limit], ordered[limit:]


def sync_terminal_realtime_pool(db: Database, proxy_url: str = None) -> dict:
    """Build terminal_stock_pool and mirror the selected codes to legacy terminal_realtime_pool."""
    import config

    now = naive_market_now("A")
    trade_date = a_share_realtime_day_key(now=now)
    trade_dt = datetime.strptime(trade_date, "%Y-%m-%d")
    stock_limit = int(os.getenv("TERMINAL_REALTIME_STOCK_LIMIT", "24"))
    risk_limit = int(os.getenv("TERMINAL_RISK_STOCK_LIMIT", "72"))
    watch_limit = int(os.getenv("TERMINAL_WATCH_STOCK_LIMIT", "48"))
    clue_limit = int(os.getenv("TERMINAL_CLUE_STOCK_LIMIT", "24"))
    strict_sources = str(get_task_env("TERMINAL_POOL_STRICT_SOURCES", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}
    include_st = os.getenv("TERMINAL_POOL_INCLUDE_ST", "false").strip().lower() in {"1", "true", "yes", "on"}
    include_legacy_daily = os.getenv("TERMINAL_POOL_INCLUDE_LEGACY_DAILY", "false").strip().lower() in {"1", "true", "yes", "on"}
    fallback_min = max(0, int(os.getenv("TERMINAL_POOL_FALLBACK_MIN_STOCKS", "12")))
    index_codes = _index_codes()
    rows: dict[str, dict[str, Any]] = {}

    _add_user_pinned(rows, index_codes, now)
    _add_technical_signal_rows(rows, db, index_codes)
    _add_signal_rows(rows, db, index_codes, include_generated_daily=False)
    _add_chain_membership_rows(rows, db, index_codes)
    _add_knowledge_rows(rows, db, index_codes)
    _add_review_clue_rows(rows, db, index_codes)
    _attach_membership_context(rows, db, index_codes)
    strict_candidate_count = len(rows)
    fallback_count = 0
    fallback_enabled = False
    if not strict_sources:
        if include_legacy_daily:
            _add_signal_rows(rows, db, index_codes, include_generated_daily=True, generated_daily_only=True)
        _add_strategy_rows(rows, db, index_codes)
        _add_active_pool(rows, db, index_codes)
        _add_recent_opened(rows, db, index_codes)
    elif strict_candidate_count < fallback_min:
        fallback_enabled = True
        fallback_count = _add_fallback_watch_rows(
            rows,
            db,
            index_codes,
            limit=fallback_min - strict_candidate_count,
            now=now,
        )

    broad_market_context = _load_broad_market_context(db, trade_date)
    _attach_broad_market_context(rows, broad_market_context)
    raw_candidate_count = len(rows)
    opportunity_rows = _default_opportunity_candidate_rows(rows, include_st=include_st)
    default_candidate_symbols = {row.get("symbol") for row in opportunity_rows.values() if row.get("symbol")}
    strict_candidate_count = len(opportunity_rows)

    split = _split_pool_rows(opportunity_rows, focus_limit=stock_limit, risk_limit=risk_limit, watch_limit=watch_limit)
    focus_stocks = split["focus"]
    risk_stocks = split["risk"]
    watch_stocks = split["watch"]
    skipped_by_pool = split["skipped"]
    all_processed_symbols = {row.get("symbol") for row in (focus_stocks + risk_stocks + watch_stocks) if row.get("symbol")}
    clue_candidates = []
    for row in rows.values():
        if row.get("symbol") in all_processed_symbols or row.get("symbol") in default_candidate_symbols:
            continue
        if not include_st and _is_st_stock(row):
            continue
        if _risk_reasons(row):
            continue
        if _chain_entry_blocker(row):
            continue
        if _entry_gate_passed(row):
            continue
        if not _has_clue_source(row):
            continue
        clue_candidates.append(row)
    clue_candidates.sort(key=_clue_quality_score, reverse=True)
    clue_stocks = []
    for row in clue_candidates[:clue_limit]:
        row["inclusion_reasons"] = sorted(row.get("inclusion_reasons") or [], key=lambda item: _float(item.get("weight")), reverse=True)[:12]
        entry_ok, gate_status, blocked_by, top_buy, top_risk = _entry_gate(row)
        row = _finalize_pool_row(
            row,
            pool_type="watch",
            rank_score=_clue_quality_score(row),
            score_components={"clue_quality": _clue_quality_score(row)},
            entry_gate_status="clue_pool",
            blocked_by=[],
            top_buy=top_buy,
            top_risk=top_risk,
        )
        row["clue_quality_score"] = _clue_quality_score(row)
        row["clue_sources"] = [
            f"{'道长' if _text(r.get('evidence', {}).get('author')) == 'daozhang' else '胖哥' if _text(r.get('evidence', {}).get('author')) == 'pangge' else ''}:{_text(r.get('board_or_concept'))}"
            for r in row.get("inclusion_reasons", [])
            if isinstance(r, dict) and r.get("reason_type") == "review_sector_bullish" and r.get("board_or_concept")
        ]
        row["promotion_gates"] = blocked_by
        clue_stocks.append(row)
    for idx, row in enumerate(clue_stocks, start=1):
        row["rank"] = idx
    clue_symbols = {row.get("symbol") for row in clue_stocks}
    if clue_symbols:
        watch_stocks = [row for row in watch_stocks if row.get("symbol") not in clue_symbols]
    if len(watch_stocks) < watch_limit:
        watch_symbols = {row.get("symbol") for row in watch_stocks if row.get("symbol")}
        for row in skipped_by_pool.get("watch") or []:
            symbol = row.get("symbol")
            if not symbol or symbol in clue_symbols or symbol in watch_symbols:
                continue
            watch_stocks.append(row)
            watch_symbols.add(symbol)
            if len(watch_stocks) >= watch_limit:
                break
    watch_backfill_count = _backfill_watch_from_clue_candidates(
        watch_stocks,
        clue_candidates,
        clue_symbols=clue_symbols,
        watch_limit=watch_limit,
    )
    _assign_pool_ranks(watch_stocks)
    skipped = (skipped_by_pool.get("focus") or []) + (skipped_by_pool.get("risk") or []) + (skipped_by_pool.get("watch") or [])
    stored_focus_stocks = [_slim_pool_row_for_storage(row) for row in focus_stocks]
    stored_risk_stocks = [_slim_pool_row_for_storage(row) for row in risk_stocks]
    stored_watch_stocks = [_slim_pool_row_for_storage(row) for row in watch_stocks]
    stored_clue_stocks = [_slim_pool_row_for_storage(row) for row in clue_stocks]
    stored_skipped = [_slim_skipped_row_for_storage(row) for row in skipped[:60]]
    stored_skipped_by_pool = {
        key: [_slim_skipped_row_for_storage(row) for row in value[:15]]
        for key, value in skipped_by_pool.items()
    }
    candidate_meta = _candidate_meta(opportunity_rows)
    focus_reason_counts = Counter(
        reason.get("reason_type")
        for row in focus_stocks
        for reason in row.get("inclusion_reasons", [])
        if reason.get("reason_type")
    )
    pool_counts = dict(split["pool_counts"])
    pool_counts.update({
        "focus_selected": len(focus_stocks),
        "risk_selected": len(risk_stocks),
        "watch_selected": len(watch_stocks),
        "clue_selected": len(clue_stocks),
        "watch_backfilled": watch_backfill_count,
    })
    technical_freshness = db["data_freshness"].find_one(
        {"domain": "technical_signal", "market": "A", "collection": "terminal_technical_signals", "coverage_by_freq": {"$exists": True}},
        {"coverage_by_freq": 1, "required_freqs": 1, "optional_freqs": 1, "is_full_market_complete": 1, "coverage_status": 1},
        sort=[("updated_at", -1)],
    ) or {}
    pool_doc = {
        "pool": "terminal_stock_pool",
        "market": "A",
        "dt": trade_dt,
        "trade_date": trade_date,
        "updated_at": now,
        "stock_limit": stock_limit,
        "risk_limit": risk_limit,
        "watch_limit": watch_limit,
        "clue_limit": clue_limit,
        "stocks": stored_focus_stocks,
        "focus_stocks": stored_focus_stocks,
        "risk_stocks": stored_risk_stocks,
        "watch_stocks": stored_watch_stocks,
        "clue_stocks": stored_clue_stocks,
        "skipped_stocks": stored_skipped,
        "skipped_by_pool": stored_skipped_by_pool,
        "skipped_count": len(skipped),
        "candidate_count": len(opportunity_rows),
        "raw_candidate_count": raw_candidate_count,
        "strict_candidate_count": strict_candidate_count,
        "fallback_count": fallback_count,
        "fallback_enabled": fallback_enabled,
        "pool_counts": pool_counts,
        "broad_market_context": broad_market_context,
        "reason_counts": dict(focus_reason_counts),
        **candidate_meta,
        "coverage_by_freq": technical_freshness.get("coverage_by_freq") or {},
        "required_freqs": technical_freshness.get("required_freqs") or ["日线", "周线", "30分钟"],
        "optional_freqs": technical_freshness.get("optional_freqs") or ["15分钟", "5分钟"],
        "is_full_market_complete": bool(technical_freshness.get("is_full_market_complete")),
        "coverage_status": _text(technical_freshness.get("coverage_status")) or "unknown",
        "ranking_version": POOL_RANKING_VERSION,
        "source": "whitebox_pool_builder",
        "source_policy": "postmarket_strict_with_fallback_watch" if strict_sources and fallback_enabled else ("postmarket_strict_technical_knowledge_chain" if strict_sources else "runtime_watch_and_signal_blend"),
        "selection_policy": "strict_fresh_30m_daily_weekly_anchor__multi_indicator_resonance__ma_fib_support__clue_source_only",
    }
    db["terminal_stock_pool"].update_one(
        {"pool": "terminal_stock_pool", "market": "A"},
        {"$set": pool_doc},
        upsert=True,
    )
    try:
        from signals.notify.intraday_pool_alerts import process_terminal_stock_pool_alerts

        alert_result = process_terminal_stock_pool_alerts(db, pool_doc)
    except Exception as exc:
        logger.debug("terminal stock pool alert skipped: %s", exc)
        alert_result = {"status": "error", "reason": str(exc), "sent": 0}

    legacy_doc = {
        "pool": "terminal_realtime",
        "market": "A",
        "dt": pool_doc["dt"],
        "trade_date": trade_date,
        "updated_at": now,
        "stocks": [row["raw_code"] for row in (stored_focus_stocks + stored_risk_stocks + stored_watch_stocks + stored_clue_stocks)[: max(stock_limit, 72)]],
        "indices": list(getattr(config, "INDEX_AK_CODES", {}).values()),
        "industries": _top_heat_names(db, "industry", 20),
        "concepts": _top_heat_names(db, "concept", 20),
        "stock_limit": stock_limit,
        "source": "terminal_stock_pool_mirror",
    }
    db["terminal_realtime_pool"].update_one(
        {"pool": "terminal_realtime", "market": "A"},
        {"$set": legacy_doc},
        upsert=True,
    )

    db["data_freshness"].update_one(
        {"domain": "terminal_pool", "market": "A", "mode": "realtime", "collection": "terminal_stock_pool"},
        {"$set": {
            "domain": "terminal_pool",
            "market": "A",
            "mode": "realtime",
            "lane": "workbench_lane",
            "collection": "terminal_stock_pool",
            "freshness": "fresh" if focus_stocks else "empty",
            "latest_dt": trade_date,
            "as_of": trade_date,
            "date_key": trade_date.replace("-", ""),
            "updated_at": now,
            "stale_reason": "" if focus_stocks else "terminal_focus_stock_pool_empty",
            "count": len(focus_stocks),
            "candidate_count": len(opportunity_rows),
            "raw_candidate_count": raw_candidate_count,
            "strict_candidate_count": strict_candidate_count,
            "fallback_count": fallback_count,
            "skipped_count": len(skipped),
            "pool_counts": pool_counts,
            "broad_market_context": broad_market_context,
            "reason_counts": dict(focus_reason_counts),
            **candidate_meta,
            "coverage_by_freq": pool_doc["coverage_by_freq"],
            "coverage_status": pool_doc["coverage_status"],
            "is_full_market_complete": pool_doc["is_full_market_complete"],
            "selection_policy": pool_doc["selection_policy"],
            "ranking_version": POOL_RANKING_VERSION,
        }},
        upsert=True,
    )
    logger.info(
        "terminal stock pool: focus=%d risk=%d watch=%d clue=%d candidates=%d skipped=%d",
        len(focus_stocks),
        len(risk_stocks),
        len(watch_stocks),
        len(clue_stocks),
        len(opportunity_rows),
        len(skipped),
    )
    return {
        "inserted": len(focus_stocks),
        "stocks": len(focus_stocks),
        "focus_stocks": len(focus_stocks),
        "risk_stocks": len(risk_stocks),
        "watch_stocks": len(watch_stocks),
        "clue_stocks": len(clue_stocks),
        "candidates": len(opportunity_rows),
        "raw_candidates": raw_candidate_count,
        "strict_candidates": strict_candidate_count,
        "fallback_candidates": fallback_count,
        "skipped": len(skipped),
        "indices": len(legacy_doc["indices"]),
        "industries": len(legacy_doc["industries"]),
        "concepts": len(legacy_doc["concepts"]),
        "pool_counts": pool_counts,
        "broad_market_context": broad_market_context,
        "reason_counts": dict(focus_reason_counts),
        **candidate_meta,
        "coverage_status": pool_doc["coverage_status"],
        "is_full_market_complete": pool_doc["is_full_market_complete"],
        "ranking_version": POOL_RANKING_VERSION,
        "alerts": alert_result,
    }
