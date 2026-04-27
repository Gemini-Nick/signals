from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

import config
from signals.core.stock_names import get_resolver
from signals.data.gateway import get_index_bars, get_kline
from signals.data.models import DataRequest
from signals.core.trade_log import get_trade_log
from signals.services import backtest as backtest_service
from signals.services import cluster as cluster_service
from signals.strategy.snapshot import get_strategy_snapshot

from ..services.engine import get_engine
from ..services.serializers import (
    serialize_index_report,
    serialize_market_context,
    serialize_scored_symbol,
    serialize_signal_change,
)
from .chart import get_chart_data
from .industry import get_industry_detail
from .plan import _serialize_plan
from .stock import analyze_stock

router = APIRouter(prefix="/api/workbench", tags=["workbench"])

UI_FREQS = ["5min", "15min", "30min", "daily", "weekly"]
MINUTE_FREQS = {"5min", "5m", "15min", "15m", "30min", "30m"}
BUY_FREQS = ["daily", "30min", "15min", "5min"]
SECOND_SCREEN_LANES = {
    "quote_lane": {
        "label": "实时观察",
        "cadence": "15-60s",
        "purpose": "关键指数、当前标的和关注池轻量 quote。",
    },
    "signal_lane": {
        "label": "信号确认",
        "cadence": "5m close",
        "purpose": "5m/15m/30m/日/周闭合结构确认。",
    },
    "workbench_lane": {
        "label": "工作台重算",
        "cadence": "10m",
        "purpose": "主观察列表、候选池、风险预警和策略快照。",
    },
    "board_lane": {
        "label": "板块异动",
        "cadence": "20-30m",
        "purpose": "行业/概念排行、leader、产业链承接。",
    },
}
FREQ_ALIASES = {
    "5m": "5min",
    "5min": "5min",
    "15m": "15min",
    "15min": "15min",
    "30m": "30min",
    "30min": "30min",
    "daily": "daily",
    "weekly": "weekly",
    "monthly": "monthly",
}
GATEWAY_FREQS = {
    "5min": "5m",
    "15min": "15m",
    "30min": "30m",
    "daily": "daily",
    "weekly": "weekly",
    "monthly": "monthly",
}
MINGDAO_INDEX_THEMES = {
    "上证指数": ["全市场", "权重", "政策温度"],
    "上证50": ["权重", "大金融", "消费"],
    "沪深300": ["核心资产", "权重", "大金融"],
    "深证成指": ["成长", "先进制造", "消费电子"],
    "创业板指": ["CPO", "电新", "成长链"],
    "科创50": ["芯片", "半导体", "硬科技"],
    "科创综指": ["硬科技", "半导体", "创新成长"],
    "超大盘": ["央国企", "红利", "权重"],
    "中证500": ["中盘成长", "制造业", "弹性成长"],
    "中证1000": ["小盘成长", "主题弹性", "交易活跃"],
    "中证银行": ["大金融", "红利", "顺周期"],
    "国证2000": ["小微盘", "题材弹性", "市场广度"],
    "恒生科技ETF": ["港股科技", "互联网", "风险偏好"],
    "30年国债ETF": ["利率", "避险", "股债跷跷板"],
    "中国石油": ["资源", "央企", "红利"],
}

MINGDAO_MACRO_WATCHLIST = [
    {"name": "上证指数", "symbol": "sh000001", "kind": "index"},
    {"name": "深证成指", "symbol": "sz399001", "kind": "index"},
    {"name": "沪深300", "symbol": "sh000300", "kind": "index"},
    {"name": "创业板指", "symbol": "sz399006", "kind": "index"},
    {"name": "科创50", "symbol": "sh000688", "kind": "index"},
    {"name": "科创综指", "symbol": "sh000680", "kind": "index"},
    {"name": "上证50", "symbol": "sh000016", "kind": "index"},
    {"name": "超大盘", "symbol": "sh000043", "kind": "index"},
    {"name": "中证500", "symbol": "sh000905", "kind": "index"},
    {"name": "中证1000", "symbol": "sh000852", "kind": "index"},
    {"name": "中证银行", "symbol": "sz399986", "kind": "index"},
    {"name": "国证2000", "symbol": "sz399303", "kind": "index"},
    {"name": "恒生科技ETF", "symbol": "SH.513130", "kind": "stock"},
    {"name": "30年国债ETF", "symbol": "SH.511090", "kind": "stock"},
    {"name": "中国石油", "symbol": "SH.601857", "kind": "stock"},
]

for _name, _symbol in config.INDEX_AK_CODES.items():
    if not any(item["name"] == _name for item in MINGDAO_MACRO_WATCHLIST):
        MINGDAO_MACRO_WATCHLIST.append({"name": _name, "symbol": _symbol, "kind": "index"})
BUY_SIGNAL_TOKENS = ("buy", "long", "entry", "候选", "买", "突破", "启动", "三买", "一买", "二买")
SELL_SIGNAL_TOKENS = ("sell", "short", "exit", "预警", "卖", "跌破", "止损", "风险")


def _canonical_freq(freq: str) -> str:
    return FREQ_ALIASES.get(str(freq or "daily").strip().lower(), str(freq or "daily").strip().lower() or "daily")


def _gateway_freq(freq: str) -> str:
    return GATEWAY_FREQS.get(_canonical_freq(freq), _canonical_freq(freq))


def _freq_label(freq: str) -> str:
    return {
        "5min": "5分钟",
        "15min": "15分钟",
        "30min": "30分钟",
        "daily": "日线",
        "weekly": "周线",
        "monthly": "月线",
    }.get(_canonical_freq(freq), str(freq or "daily"))


def _freq_badge(freq: str) -> str:
    return {
        "5min": "5m",
        "15min": "15m",
        "30min": "30m",
        "daily": "D",
        "weekly": "W",
        "monthly": "M",
    }.get(_canonical_freq(freq), str(freq or ""))


def _freq_bucket(freq: Any) -> str:
    value = str(freq or "").strip().lower()
    if value in {"5", "5m", "5min", "5分钟", "5分钟线"}:
        return "5min"
    if value in {"15", "15m", "15min", "15分钟", "15分钟线"}:
        return "15min"
    if value in {"30", "30m", "30min", "30分钟", "30分钟线"}:
        return "30min"
    if value in {"d", "day", "daily", "日", "日线", "1d"}:
        return "daily"
    if value in {"w", "week", "weekly", "周", "周线", "1w"}:
        return "weekly"
    return _canonical_freq(value or "daily")


def _dt_to_unix(value: Any) -> int:
    return int(pd.Timestamp(value).timestamp())


def _float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        parsed = float(value)
        if pd.isna(parsed):
            return default
        return parsed
    except Exception:
        return default


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _serialize_ohlcv_df(df: pd.DataFrame, *, limit: int = 720) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    working = df.copy().sort_index()
    if limit > 0:
        working = working.tail(limit)
    rows: list[dict[str, Any]] = []
    for dt_idx, row in working.iterrows():
        close = _float(row.get("close"))
        if close is None:
            continue
        open_ = _float(row.get("open"), close)
        high = _float(row.get("high"), max(open_, close))
        low = _float(row.get("low"), min(open_, close))
        rows.append({
            "time": _dt_to_unix(dt_idx),
            "open": round(open_, 4),
            "high": round(high, 4),
            "low": round(low, 4),
            "close": round(close, 4),
            "volume": int(_float(row.get("vol") or row.get("volume"), 0) or 0),
        })
    return rows


def _chart_from_df(df: pd.DataFrame, *, symbol: str, freq: str, source: str = "gateway") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "freq": _freq_label(freq),
        "meta": {
            "freq": _canonical_freq(freq),
            "source": source,
            "bars": int(len(df)) if df is not None else 0,
        },
        "ohlcv": _serialize_ohlcv_df(df, limit=900 if _canonical_freq(freq) in {"5min", "15min", "30min"} else 720),
        "signals": [],
        "ma_lines": [],
    }


def _chart_has_ohlcv(chart: dict[str, Any]) -> bool:
    return bool(chart.get("ohlcv"))


def _fallback_chart_when_empty(
    chart: dict[str, Any],
    *,
    symbol: str,
    requested_freq: str,
    loader,
) -> dict[str, Any]:
    """Keep the terminal usable while minute caches are cold or a provider is down."""
    if requested_freq not in MINUTE_FREQS or _chart_has_ohlcv(chart):
        return chart
    fallback_df, fallback_source = loader("daily")
    fallback = _chart_from_df(
        fallback_df,
        symbol=symbol,
        freq="daily",
        source=f"{fallback_source};fallback_from={requested_freq}",
    )
    fallback["meta"] = {
        **fallback.get("meta", {}),
        "requested_freq": requested_freq,
        "fallback_reason": "empty_minute_ohlcv",
    }
    return fallback if _chart_has_ohlcv(fallback) else chart


def _resample_weekly(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    weekly = df.sort_index().resample("W-FRI").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "vol": "sum",
        "amount": "sum",
    })
    weekly = weekly.dropna(subset=["open", "high", "low", "close"], how="any")
    weekly.attrs["data_source"] = "daily_resampled_weekly"
    if not weekly.empty:
        weekly.attrs["as_of"] = str(weekly.index.max().date())
    return weekly


def _stock_df(symbol: str, freq: str) -> tuple[pd.DataFrame, str]:
    canonical = _canonical_freq(freq)
    response = get_kline(DataRequest(
        domain="kline",
        mode="historical",
        market="A",
        symbol=symbol,
        freq=_gateway_freq(canonical),
        purpose="review",
        allow_stale=True,
    ))
    df = response.data if response.data is not None else pd.DataFrame()
    if df is not None and not df.empty:
        return df, response.source
    if canonical == "weekly":
        daily = get_kline(DataRequest(
            domain="kline",
            mode="historical",
            market="A",
            symbol=symbol,
            freq="daily",
            purpose="review",
            allow_stale=True,
        ))
        daily_df = daily.data if daily.data is not None else pd.DataFrame()
        weekly = _resample_weekly(daily_df)
        if not weekly.empty:
            return weekly, "daily_resampled_weekly"
    return pd.DataFrame(), response.source


def _index_df(symbol: str, freq: str) -> tuple[pd.DataFrame, str]:
    response = get_index_bars(DataRequest(
        domain="index",
        mode="historical",
        market="A",
        symbol=symbol,
        freq=_gateway_freq(freq),
        purpose="review",
        allow_stale=True,
    ))
    df = response.data if response.data is not None else pd.DataFrame()
    if df is not None and not df.empty:
        return df, response.source
    if _canonical_freq(freq) == "weekly":
        daily = get_index_bars(DataRequest(
            domain="index",
            mode="historical",
            market="A",
            symbol=symbol,
            freq="daily",
            purpose="review",
            allow_stale=True,
        ))
        daily_df = daily.data if daily.data is not None else pd.DataFrame()
        weekly = _resample_weekly(daily_df)
        if not weekly.empty:
            return weekly, "index_daily_resampled_weekly"
    return pd.DataFrame(), response.source


def _preset_start_date(info: dict[str, Any], today: date) -> Optional[date]:
    if "date" in info:
        try:
            return datetime.strptime(str(info["date"]), "%Y-%m-%d").date()
        except ValueError:
            return None
    offset = info.get("offset")
    if offset == "ytd":
        return date(today.year, 1, 1)
    if isinstance(offset, int):
        return today - timedelta(days=offset)
    return None


def _watchlist_range_columns(today: Optional[date] = None) -> list[dict[str, Any]]:
    today = today or date.today()
    columns: list[dict[str, Any]] = []
    relative: list[tuple[int, date, str, dict[str, Any]]] = []
    absolute: list[tuple[date, str, dict[str, Any]]] = []
    for key, info in config.DATE_PRESETS.items():
        if not isinstance(info, dict):
            continue
        start = _preset_start_date(info, today)
        if not start or start > today:
            continue
        if "date" in info:
            absolute.append((start, key, info))
        else:
            rank = {"ytd": 0, "1w": 1, "1m": 2, "3m": 3}.get(key, 9)
            relative.append((rank, start, key, info))

    relative.sort(key=lambda item: item[0])
    for _, start, key, info in relative:
        columns.append({
            "key": key,
            "label": str(info.get("label") or key),
            "start_date": start.isoformat(),
            "aliases": [key, str(info.get("label") or ""), start.isoformat()],
            "tier": info.get("tier", "relative"),
        })

    absolute.sort(key=lambda item: item[0], reverse=True)

    for start, key, info in absolute:
        mmdd = start.strftime("%m%d")
        label = f"{mmdd}至今"
        columns.append({
            "key": key,
            "label": label,
            "start_date": start.isoformat(),
            "aliases": [key, mmdd, start.isoformat(), str(info.get("label") or "")],
            "tier": info.get("tier", "event"),
        })
    return columns


def _compute_range_returns(df: pd.DataFrame, columns: list[dict[str, Any]]) -> dict[str, Optional[float]]:
    if df is None or df.empty or "close" not in df.columns:
        return {}
    working = df.copy().sort_index()
    closes = pd.to_numeric(working["close"], errors="coerce").dropna()
    if closes.empty:
        return {}
    latest = float(closes.iloc[-1])
    result: dict[str, Optional[float]] = {}
    for column in columns:
        key = str(column.get("key") or "")
        start_date = str(column.get("start_date") or "")
        if not key or not start_date:
            continue
        mask = closes.index >= pd.Timestamp(start_date)
        if not mask.any():
            result[key] = None
            continue
        start_price = float(closes.loc[mask].iloc[0])
        if start_price <= 0:
            result[key] = None
            continue
        result[key] = round((latest - start_price) / start_price * 100, 2)
    return result


def _compute_day_change_pct(df: pd.DataFrame) -> Optional[float]:
    if df is None or df.empty or "close" not in df.columns:
        return None
    closes = pd.to_numeric(df.sort_index()["close"], errors="coerce").dropna()
    if len(closes) < 2:
        return None
    previous = float(closes.iloc[-2])
    latest = float(closes.iloc[-1])
    if previous <= 0:
        return None
    return round((latest - previous) / previous * 100, 2)


def _ma_signal_from_df(df: pd.DataFrame) -> str:
    if df is None or df.empty or "close" not in df.columns:
        return "数据待预热"
    closes = pd.to_numeric(df.sort_index()["close"], errors="coerce").dropna()
    if len(closes) < 22:
        return "数据待预热"
    latest = float(closes.iloc[-1])
    ma5 = float(closes.tail(5).mean())
    ma10 = float(closes.tail(10).mean())
    ma20 = float(closes.tail(20).mean())
    prev_ma20 = float(closes.iloc[-21:-1].tail(20).mean())
    if latest >= ma5 >= ma10 >= ma20 and ma20 >= prev_ma20:
        return "多头上行"
    if latest < ma5 and latest < ma10:
        return "跌破短均"
    if latest >= ma20 and ma20 >= prev_ma20:
        return "站上20日线"
    if abs(latest - ma20) / ma20 <= 0.015:
        return "贴近20日线"
    if ma20 < prev_ma20:
        return "20日线下行"
    return "震荡观察"


def _signal_or_fallback(row: dict[str, Any], df: pd.DataFrame) -> str:
    for key in ("daily_latest_signal", "latest_signal", "signal"):
        value = _text(row.get(key))
        if value and value.lower() not in {"none", "n/a"} and value != "无":
            return value
    f30 = _text(row.get("f30_latest_signal"))
    f15 = _text(row.get("f15_latest_signal"))
    minute_signals = [value for value in (f30, f15) if value and value != "无"]
    if minute_signals:
        return "/".join(minute_signals[:2])
    return _ma_signal_from_df(df)


def _unwrap_response(value: Any) -> Any:
    if isinstance(value, JSONResponse):
        return json.loads(value.body.decode("utf-8"))
    return value


def _ensure_engine():
    engine = get_engine()
    if (
        os.environ.get("SIGNALS_WEB_AUTOSTART_ENGINE", "false").lower() == "true"
        and not engine.is_ready()
        and not engine.state.is_running
    ):
        engine.run_all_async()
    return engine


def _serialize_session(status: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ready": status.get("ready", False),
        "running": status.get("running", False),
        "loading_phase": status.get("loading_phase", ""),
        "label": status.get("session_label", ""),
        "mode": status.get("session_mode", ""),
        "a_live": status.get("a_live", False),
        "hk_live": status.get("hk_live", False),
        "us_live": status.get("us_live", False),
        "active_markets": status.get("active_markets", []),
        "refresh_interval": status.get("refresh_interval", 0),
        "next_check_seconds": status.get("next_check_seconds", 0),
        "next_refresh_at": status.get("next_refresh_at", ""),
        "data_as_of": status.get("data_as_of", ""),
        "error": status.get("error", ""),
    }


def _looks_like_stock(raw: str) -> bool:
    value = raw.strip().upper()
    if not value:
        return False
    if value.startswith(("SH.", "SZ.", "BJ.", "HK.")):
        return True
    return value.isdigit() and len(value) in (5, 6)


def _normalize_stock_symbol(raw: str) -> Tuple[Optional[str], Optional[str]]:
    resolver = get_resolver()
    value = raw.strip().upper()
    if not value:
        return None, None

    if value.startswith(("SH.", "SZ.", "BJ.", "HK.")):
        return value, value.split(".", 1)[1]

    if value.isdigit():
        if len(value) == 5:
            return f"HK.{value}", value
        if len(value) == 6:
            if value.startswith(("5", "6", "9")):
                return f"SH.{value}", value
            if value.startswith(("0", "1", "2", "3")):
                return f"SZ.{value}", value
            if value.startswith(("8", "4")):
                return f"BJ.{value}", value

    code = resolver.get_code(raw.strip())
    if code:
        return code, code.split(".", 1)[1]

    matches = resolver.search(raw.strip())
    if len(matches) == 1:
        code = matches[0][0]
        return code, code.split(".", 1)[1]

    return None, None


def _resolve_target(raw: str, kind: str, engine) -> Dict[str, str]:
    value = raw.strip()
    if not value:
        reports = engine.get_index_reports()
        default_name = reports[0].name if reports else "沪深300"
        return {"kind": "index", "label": default_name}

    forced_kind = kind.lower()
    if value.startswith("industry:"):
        return {"kind": "industry", "label": value.split(":", 1)[1].strip()}
    if value.startswith("concept:"):
        return {"kind": "concept", "label": value.split(":", 1)[1].strip()}

    if forced_kind == "stock":
        symbol, raw_code = _normalize_stock_symbol(value)
        if not symbol:
            raise HTTPException(status_code=404, detail=f"无法识别股票: {value}")
        return {"kind": "stock", "label": symbol, "raw_code": raw_code}

    if forced_kind == "industry":
        return {"kind": "industry", "label": value}

    if forced_kind == "concept":
        return {"kind": "concept", "label": value}

    if forced_kind == "index":
        return {"kind": "index", "label": value}

    reports = engine.get_index_reports()
    for report in reports:
        if value == report.name or value.lower() == report.symbol.lower():
            return {"kind": "index", "label": report.name}

    if _looks_like_stock(value):
        symbol, raw_code = _normalize_stock_symbol(value)
        if symbol:
            return {"kind": "stock", "label": symbol, "raw_code": raw_code}

    ranking = engine.get_industry_ranking_by_name(value)
    if ranking:
        return {"kind": "industry", "label": ranking.name}

    resolved = engine.resolve_sector(value)
    industries = resolved.get("matched_industries") or []
    if len(industries) == 1:
        return {"kind": "industry", "label": industries[0]}
    concepts = resolved.get("matched_concepts") or []
    if len(concepts) == 1:
        concept = concepts[0]
        if isinstance(concept, dict):
            return {"kind": "concept", "label": str(concept.get("name") or concept.get("label") or value)}
        return {"kind": "concept", "label": str(concept)}

    symbol, raw_code = _normalize_stock_symbol(value)
    if symbol:
        return {"kind": "stock", "label": symbol, "raw_code": raw_code}

    raise HTTPException(status_code=404, detail=f"无法识别目标: {value}")


def _resolve_static_index(raw: str) -> Optional[tuple[str, str]]:
    import config

    value = str(raw or "").strip()
    if not value:
        value = "沪深300"
    value_lower = value.lower()
    value_digits = value_lower.replace("sh", "").replace("sz", "")
    for name, symbol in config.INDEX_AK_CODES.items():
        if value == name or value_lower == symbol.lower() or value_digits == symbol.lower().replace("sh", "").replace("sz", ""):
            return name, symbol
    return None


def _top_candidate_symbol(engine) -> str:
    scored = engine.get_scored_symbols()
    if scored:
        return scored[0].symbol
    resolver = get_resolver()
    reports = engine.get_index_reports()
    if reports:
        return resolver.get_code(reports[0].name) or ""
    return ""


def _stock_name(symbol: str, row: Optional[dict[str, Any]] = None) -> str:
    row = row or {}
    explicit = str(row.get("name") or row.get("stock_name") or "").strip()
    if explicit:
        return explicit
    try:
        name = get_resolver().get_name(symbol)
        return "" if name == symbol.split(".")[-1] else name
    except Exception:
        return ""


def _enrich_stock_row(row: dict[str, Any], range_columns: list[dict[str, Any]]) -> dict[str, Any]:
    symbol = str(row.get("symbol") or row.get("code") or row.get("label") or "").strip()
    normalized, raw_code = _normalize_stock_symbol(symbol)
    normalized = normalized or symbol
    df, source = _stock_df(normalized, "daily") if normalized else (pd.DataFrame(), "")
    metadata = dict(row.get("metadata") or {}) if isinstance(row.get("metadata"), dict) else {}
    latest_price = (
        row.get("latest_price")
        or row.get("price")
        or metadata.get("price")
        or (float(df["close"].iloc[-1]) if df is not None and not df.empty and "close" in df.columns else None)
    )
    enriched = dict(row)
    day_change_pct = row.get("day_change_pct") or row.get("daily_change_pct") or row.get("change_pct") or row.get("gain_pct") or _compute_day_change_pct(df)
    latest_signal = _text(row.get("latest_signal") or row.get("signal") or row.get("reason") or row.get("direction"))
    enriched.update({
        "kind": "stock",
        "label": normalized,
        "symbol": normalized,
        "code": normalized,
        "raw_code": raw_code or normalized.split(".")[-1],
        "name": _stock_name(normalized, row),
        "latest_price": latest_price,
        "day_change_pct": day_change_pct,
        "daily_change_pct": day_change_pct,
        "latest_signal": latest_signal or _ma_signal_from_df(df),
        "range_returns": _compute_range_returns(df, range_columns),
        "range_return_source": source,
        "available_freqs": UI_FREQS,
        "target_kind": "stock",
        "target_label": normalized,
        "target_symbol": normalized,
    })
    return enriched


def _enrich_index_row(row: dict[str, Any], range_columns: list[dict[str, Any]]) -> dict[str, Any]:
    symbol = str(row.get("symbol") or row.get("code") or row.get("label") or row.get("name") or "").strip()
    df, source = _index_df(symbol, "daily") if symbol else (pd.DataFrame(), "")
    day_change_pct = row.get("day_change_pct") or row.get("daily_change_pct") or row.get("change_pct") or row.get("gain_pct") or row.get("intraday_change") or _compute_day_change_pct(df)
    enriched = dict(row)
    enriched.update({
        "kind": "index",
        "label": row.get("name") or row.get("label") or symbol,
        "name": row.get("name") or row.get("label") or symbol,
        "code": symbol,
        "latest_price": row.get("latest_price") or (float(df["close"].iloc[-1]) if df is not None and not df.empty and "close" in df.columns else None),
        "day_change_pct": day_change_pct,
        "daily_change_pct": day_change_pct,
        "latest_signal": _signal_or_fallback(row, df),
        "range_returns": _compute_range_returns(df, range_columns),
        "range_return_source": source,
        "available_freqs": ["daily", "weekly", "30min", "15min"],
        "target_kind": "index",
        "target_label": row.get("name") or row.get("label") or symbol,
        "target_symbol": symbol,
    })
    return enriched


def _enrich_cluster_row(row: dict[str, Any], kind: str) -> dict[str, Any]:
    enriched = dict(row)
    label = str(enriched.get("label") or enriched.get("name") or "").strip()
    day_change_pct = enriched.get("day_change_pct") or enriched.get("daily_change_pct") or enriched.get("change_pct") or enriched.get("gain_pct") or enriched.get("strength")
    enriched.update({
        "kind": kind,
        "label": label,
        "name": label,
        "code": str(enriched.get("code") or enriched.get("board_code") or ""),
        "latest_price": enriched.get("latest_price") or enriched.get("value"),
        "day_change_pct": day_change_pct,
        "daily_change_pct": day_change_pct,
        "range_returns": enriched.get("range_returns") or {},
        "target_kind": kind,
        "target_label": label,
        "target_symbol": str(enriched.get("code") or enriched.get("board_code") or label),
    })
    return enriched


def _signal_text(signal: dict[str, Any]) -> str:
    return " ".join(
        str(signal.get(key) or "")
        for key in ("signal_type", "type", "reason", "details", "summary")
    ).lower()


def _is_buy_signal(signal: dict[str, Any]) -> bool:
    text = _signal_text(signal)
    if any(token in text for token in SELL_SIGNAL_TOKENS):
        return False
    return any(token in text for token in BUY_SIGNAL_TOKENS)


def _is_sell_signal(signal: dict[str, Any]) -> bool:
    text = _signal_text(signal)
    return any(token in text for token in SELL_SIGNAL_TOKENS)


def _signal_date(signal: dict[str, Any]) -> str:
    return str(signal.get("signal_date") or signal.get("date_str") or signal.get("updated_at") or "")[:10]


def _load_signal_pool_rows(limit: int = 200) -> list[dict[str, Any]]:
    try:
        from signals.data.gateway import get_signal_pool

        response = get_signal_pool(DataRequest(
            domain="signal",
            mode="historical",
            market="A",
            purpose="review",
            allow_stale=True,
        ))
        rows = response.data or []
        return [dict(item) for item in rows[:limit] if isinstance(item, dict)]
    except Exception:
        return []


def _add_timeframe_signal(target: dict[str, Any], signal: dict[str, Any], *, side: str = "buy") -> None:
    metadata = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
    freq = _freq_bucket(signal.get("freq") or signal.get("timeframe") or metadata.get("freq"))
    if freq not in BUY_FREQS:
        return
    side = "sell" if side == "sell" else "buy"
    stack = target.setdefault("timeframe_signal_stack", {})
    freq_stack = stack.setdefault(freq, {})
    current = freq_stack.get(side)
    next_score = _float(signal.get("total_score") or signal.get("score") or signal.get("confidence"), 0) or 0
    current_score = _float((current or {}).get("score"), -1) if isinstance(current, dict) else -1
    if current and current_score is not None and current_score >= next_score:
        return
    signal_type = _text(signal.get("signal_type") or signal.get("type") or signal.get("reason"))
    if not signal_type:
        signal_type = "卖出预警" if side == "sell" else "买点"
    payload = {
        "freq": freq,
        "badge": _freq_badge(freq),
        "side": side,
        "signal_type": signal_type,
        "score": next_score,
        "confidence": _float(signal.get("confidence")),
        "signal_date": _signal_date(signal),
        "price": _float(signal.get("price")),
    }
    freq_stack[side] = payload
    target.setdefault("timeframe_signals" if side == "buy" else "sell_timeframe_signals", {})[freq] = payload


def _build_focus_stock_rows(
    *,
    buy_rows: list[dict[str, Any]],
    sell_rows: Optional[list[dict[str, Any]]] = None,
    decision_rows: list[dict[str, Any]],
    range_columns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows_by_symbol: dict[str, dict[str, Any]] = {}

    def ensure(row: dict[str, Any]) -> Optional[dict[str, Any]]:
        symbol = str(row.get("symbol") or row.get("code") or row.get("label") or "").strip()
        normalized, raw_code = _normalize_stock_symbol(symbol)
        if not normalized:
            return None
        key = normalized.upper()
        if key not in rows_by_symbol:
            rows_by_symbol[key] = _enrich_stock_row({
                **row,
                "symbol": normalized,
                "raw_code": raw_code,
                "kind": "stock",
            }, range_columns)
            rows_by_symbol[key]["timeframe_signals"] = {}
            rows_by_symbol[key]["sell_timeframe_signals"] = {}
            rows_by_symbol[key]["timeframe_signal_stack"] = {}
            rows_by_symbol[key]["focus_reasons"] = []
            rows_by_symbol[key]["source_tags"] = []
        else:
            rows_by_symbol[key].update({
                "score": max(
                    _float(rows_by_symbol[key].get("score"), 0) or 0,
                    _float(row.get("score") or row.get("total_score") or row.get("fused_total"), 0) or 0,
                )
            })
        reason = _text(row.get("reason") or row.get("summary") or row.get("direction"))
        if reason and reason not in rows_by_symbol[key]["focus_reasons"]:
            rows_by_symbol[key]["focus_reasons"].append(reason)
        source = _text(row.get("source") or row.get("data_source"))
        if source and source not in rows_by_symbol[key]["source_tags"]:
            rows_by_symbol[key]["source_tags"].append(source)
        return rows_by_symbol[key]

    for row in buy_rows:
        item = ensure(dict(row))
        if not item:
            continue
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        signal = {
            "signal_type": row.get("reason") or metadata.get("trigger") or row.get("signal_type"),
            "freq": metadata.get("freq") or row.get("freq"),
            "score": row.get("score"),
            "confidence": row.get("confidence") or metadata.get("confidence"),
            "signal_date": metadata.get("signal_date") or row.get("signal_date"),
            "price": row.get("latest_price") or row.get("price") or metadata.get("price"),
        }
        if _is_buy_signal(signal) or not signal.get("signal_type"):
            _add_timeframe_signal(item, signal, side="buy")
            item["action_status"] = item.get("action_status") or "buy_candidate"

    for row in sell_rows or []:
        item = ensure(dict(row))
        if not item:
            continue
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        signal = {
            "signal_type": row.get("reason") or metadata.get("trigger") or row.get("signal_type") or "卖出预警",
            "freq": metadata.get("freq") or row.get("freq"),
            "score": row.get("score") or row.get("risk_score"),
            "confidence": row.get("confidence") or metadata.get("confidence"),
            "signal_date": metadata.get("signal_date") or row.get("signal_date"),
            "price": row.get("latest_price") or row.get("price") or metadata.get("price"),
        }
        if _is_sell_signal(signal) or signal.get("signal_type"):
            _add_timeframe_signal(item, signal, side="sell")
            item["action_status"] = "exit_review"

    for signal in _load_signal_pool_rows():
        side = "sell" if _is_sell_signal(signal) else "buy" if _is_buy_signal(signal) else ""
        if not side:
            continue
        symbol = _normalize_stock_symbol(str(signal.get("symbol") or ""))[0]
        if not symbol:
            continue
        item = ensure({
            "symbol": symbol,
            "name": signal.get("name"),
            "reason": signal.get("signal_type") or signal.get("type"),
            "score": signal.get("total_score") or signal.get("score") or signal.get("confidence"),
            "price": signal.get("price"),
        })
        if item:
            _add_timeframe_signal(item, signal, side=side)
            if side == "sell":
                item["action_status"] = "exit_review"
            else:
                item["action_status"] = item.get("action_status") or "buy_candidate"

    for row in decision_rows:
        if row.get("symbol"):
            item = ensure(dict(row))
            if item:
                item["decision_status"] = row.get("action") or row.get("action_label")
                if item.get("action_status") != "exit_review":
                    item["action_status"] = "manual_review"

    output = list(rows_by_symbol.values())
    for row in output:
        signals = row.get("timeframe_signals") if isinstance(row.get("timeframe_signals"), dict) else {}
        sell_signals = row.get("sell_timeframe_signals") if isinstance(row.get("sell_timeframe_signals"), dict) else {}
        row["buy_timeframes"] = [
            signals[freq]
            for freq in BUY_FREQS
            if freq in signals
        ]
        row["sell_timeframes"] = [
            sell_signals[freq]
            for freq in BUY_FREQS
            if freq in sell_signals
        ]
        row["signal_stack"] = {
            freq: row.get("timeframe_signal_stack", {}).get(freq)
            for freq in BUY_FREQS
            if isinstance(row.get("timeframe_signal_stack"), dict) and row.get("timeframe_signal_stack", {}).get(freq)
        }
        row["reason"] = " · ".join(row.get("focus_reasons", [])[:2]) or row.get("reason") or row.get("direction") or ""
        if row.get("sell_timeframes") or row.get("buy_timeframes"):
            sell_badges = [f"卖{item.get('badge') or item.get('freq') or ''}" for item in row.get("sell_timeframes", []) if isinstance(item, dict)]
            buy_badges = [item.get("badge") or item.get("freq") or "" for item in row.get("buy_timeframes", []) if isinstance(item, dict)]
            row["latest_signal"] = "/".join([badge for badge in sell_badges + buy_badges if badge])
        elif row.get("reason"):
            row["latest_signal"] = row["reason"]
        if row.get("action_status") == "exit_review":
            trader_action = "减仓/止盈"
            invalidates_when = "重新站回关键均线且卖出信号解除"
        elif any(item.get("badge") == "5m" for item in row.get("buy_timeframes", []) if isinstance(item, dict)):
            trader_action = "可试仓"
            invalidates_when = "5m 买点失效或跌破短线防守位"
        elif row.get("buy_timeframes"):
            trader_action = "等待5m确认"
            invalidates_when = "5m 无法确认或上级周期转弱"
        elif row.get("action_status") == "manual_review":
            trader_action = "观察"
            invalidates_when = "人工复核条件不再成立"
        else:
            trader_action = "观察"
            invalidates_when = "异动消退或跌破对应周期关键位"
        row.update({
            "lane": "signal_lane",
            "second_screen_role": "actionable_focus_stock",
            "trader_action": trader_action,
            "invalidates_when": invalidates_when,
        })
    output.sort(
        key=lambda item: (
            3 if item.get("action_status") == "exit_review" else 2 if item.get("action_status") == "manual_review" else 1 if item.get("buy_timeframes") else 0,
            len(item.get("sell_timeframes") or []) + len(item.get("buy_timeframes") or []),
            _float(item.get("score") or item.get("total_score") or item.get("fused_total"), 0) or 0,
        ),
        reverse=True,
    )
    return output[:24]


def _build_macro_index_rows(
    *,
    reports: list[dict[str, Any]],
    range_columns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    reports_by_name = {str(report.get("name") or report.get("label") or ""): report for report in reports}
    reports_by_symbol = {str(report.get("symbol") or report.get("code") or "").lower(): report for report in reports}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in MINGDAO_MACRO_WATCHLIST:
        name = _text(item.get("name"))
        symbol = _text(item.get("symbol"))
        kind = _text(item.get("kind")) or "index"
        if not name or not symbol:
            continue
        key = f"{kind}:{symbol.lower()}"
        if key in seen:
            continue
        seen.add(key)
        row = dict(reports_by_name.get(name) or reports_by_symbol.get(symbol.lower()) or {
            "name": name,
            "label": name,
            "symbol": symbol,
            "code": symbol,
        })
        row.setdefault("name", name)
        row.setdefault("label", name)
        row.setdefault("symbol", symbol)
        if kind == "stock":
            enriched = _enrich_stock_row({
                **row,
                "name": name,
                "label": symbol,
                "symbol": symbol,
                "kind": "stock",
            }, range_columns)
            if not enriched.get("latest_price"):
                continue
            target_kind = "stock"
            target_label = enriched.get("symbol") or symbol
            target_symbol = enriched.get("symbol") or symbol
        else:
            enriched = _enrich_index_row(row, range_columns)
            if not enriched.get("latest_price"):
                continue
            target_kind = "index"
            target_label = name
            target_symbol = symbol
        enriched.update({
            "group": "macro_indices",
            "lane": "quote_lane",
            "second_screen_role": "market_direction_anchor",
            "action_status": "观察",
            "trader_action": "观察关键指数方向和主题共振",
            "invalidates_when": "指数跌破对应周期防守均线或主题扩散失败",
            "theme_tags": MINGDAO_INDEX_THEMES.get(name, []),
            "latest_signal": (
                _signal_or_fallback(row, _index_df(symbol, "daily")[0] if kind != "stock" else _stock_df(str(target_symbol), "daily")[0])
            ),
            "signal_stack": {
                "daily": row.get("daily_latest_signal") or "",
                "30min": row.get("f30_latest_signal") or "",
                "15min": row.get("f15_latest_signal") or "",
            },
            "target_kind": target_kind,
            "target_label": target_label,
            "target_symbol": target_symbol,
            "target_freq": "daily",
        })
        rows.append(enriched)
    return rows


def _preview_carrier(candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    priority = {
        "core": 5,
        "elastic": 4,
        "semantic_industry_chain": 3,
        "industry_leader": 2,
        "source_leader": 1,
    }
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for item in candidates:
        symbol = _text(item.get("symbol"))
        if not symbol:
            continue
        rep_type = _text(item.get("representative_type")) or _text(item.get("source"))
        ranked.append((priority.get(rep_type, 0), int(item.get("priority") or 0), item))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return ranked[0][2] if ranked else None


def _industry_carrier_candidates(name: str, leader_name: str = "") -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def add(item: dict[str, Any]) -> None:
        symbol = _text(item.get("symbol"))
        raw_code = _text(item.get("raw_code"))
        if not symbol:
            symbol, raw_code = _stock_symbol_from_code_or_name(item.get("code"), item.get("name"))
        if not symbol:
            return
        if any(_text(existing.get("symbol")).upper() == symbol.upper() for existing in candidates):
            return
        candidates.append({
            **item,
            "symbol": symbol,
            "raw_code": raw_code or symbol.split(".", 1)[-1],
            "name": _text(item.get("name")) or _stock_name(symbol),
        })

    if leader_name:
        add({
            "name": leader_name,
            "source": "source_leader",
            "representative_type": "source_leader",
            "relation": name,
            "priority": 32,
        })
    leader = _industry_leader_candidate(name)
    if leader:
        add({**leader, "representative_type": "industry_leader"})
    for item in _preferred_concept_carriers(name, [], [name]):
        add(item)
    for symbol in _industry_constituent_symbols(name):
        add({
            "symbol": symbol,
            "source": "industry_constituents",
            "representative_type": "industry_constituent",
            "relation": name,
            "priority": 8,
        })
    return candidates


def _mapping_chain_from_carrier(name: str, carrier: Optional[dict[str, Any]], *, kind: str) -> dict[str, Any]:
    if not carrier:
        return {
            "query": name,
            "domain": kind,
            "chain_id": None,
            "chain_name": "",
            "node_id": "",
            "node_name": "",
            "layer": "",
            "confidence": 0,
            "evidence_sources": [],
        }
    return {
        "query": name,
        "domain": kind,
        "chain_id": carrier.get("chain_id"),
        "chain_name": carrier.get("chain_name") or "",
        "node_id": carrier.get("node_id") or "",
        "node_name": carrier.get("node_name") or "",
        "layer": carrier.get("layer") or "",
        "stage": carrier.get("stage") or "",
        "confidence": carrier.get("confidence"),
        "evidence_sources": carrier.get("evidence_sources") or [carrier.get("source") or ""],
        "carrier": _representative_payload(carrier),
    }


def _sector_board_preview(row: dict[str, Any], kind: str) -> dict[str, Any]:
    enriched = _enrich_cluster_row(row, kind)
    label = str(enriched.get("label") or enriched.get("name") or "").strip()
    leader = _text(
        enriched.get("leader")
        or enriched.get("leader_name")
        or enriched.get("leading_stock")
        or enriched.get("leading_name")
    )
    candidates: list[dict[str, Any]] = []
    representatives: dict[str, list[dict[str, Any]]] = {"core": [], "elastic": [], "source_leader": []}
    if label:
        if kind == "concept":
            theme_candidates = _concept_theme_candidates(label)
            related = []
            try:
                from signals.layers.industry import _map_concept_to_industries

                for industry in _map_concept_to_industries(label):
                    if industry not in related:
                        related.append(industry)
            except Exception:
                related = []
            candidates = _concept_carrier_candidates(label, theme_candidates, related)
            representatives = _concept_representative_groups(candidates)
        else:
            candidates = _industry_carrier_candidates(label, leader)
    carrier = _cached_daily_carrier(candidates) or _preview_carrier(candidates)
    carrier_payload = _representative_payload(carrier) if carrier else {}
    carrier_range_returns: dict[str, Optional[float]] = {}
    carrier_latest_price: Optional[float] = None
    carrier_day_change: Optional[float] = None
    carrier_range_source = ""
    if carrier_payload.get("symbol"):
        carrier_df, carrier_range_source = _stock_df(str(carrier_payload["symbol"]), "daily")
        carrier_range_returns = _compute_range_returns(carrier_df, _watchlist_range_columns())
        carrier_latest_price = (
            float(carrier_df["close"].iloc[-1])
            if carrier_df is not None and not carrier_df.empty and "close" in carrier_df.columns
            else None
        )
        carrier_day_change = _compute_day_change_pct(carrier_df)
    board_day_change = (
        enriched.get("day_change_pct")
        or enriched.get("daily_change_pct")
        or enriched.get("change_pct")
        or enriched.get("gain_pct")
        or enriched.get("strength")
    )
    board_range_returns = enriched.get("range_returns") or {}
    carrier_name = carrier_payload.get("name") or carrier_payload.get("symbol") or ""
    action_status = "观察" if carrier_payload else "退出复盘"
    explanation_parts = [
        f"{label} 异动" if label else "",
        f"leader {leader}" if leader else "",
        f"承接 {carrier_name}" if carrier_name else "暂无链主承接",
    ]
    enriched.update({
        "group": "sector_boards",
        "domain": "concept" if kind == "concept" else "board",
        "lane": "board_lane",
        "second_screen_role": "hot_sector_explanation",
        "action_status": action_status,
        "trader_action": "观察板块扩散和代表股承接" if carrier_payload else "退出复盘",
        "invalidates_when": "leader 走弱、板块排名回落或链主代表跌破短线防守位",
        "explanation": " · ".join([part for part in explanation_parts if part]),
        "leader": leader,
        "source": enriched.get("source") or enriched.get("data_source") or "",
        "latest_price": enriched.get("latest_price"),
        "day_change_pct": board_day_change,
        "daily_change_pct": board_day_change,
        "range_returns": board_range_returns,
        "range_return_source": enriched.get("range_return_source") or "",
        "range_return_status": "board_kline" if board_range_returns else "board_kline_missing",
        "carrier_latest_price": carrier_latest_price,
        "carrier_day_change_pct": carrier_day_change,
        "carrier_range_returns": carrier_range_returns,
        "carrier_range_return_source": "carrier_stock" if carrier_range_returns else "",
        "carrier_range_return_symbol": carrier_payload.get("symbol") or "",
        "chart_target_status": "carrier_stock" if carrier_payload else "unmapped",
        "latest_signal": enriched.get("latest_signal") or (f"承接{carrier_payload.get('name')}" if carrier_payload.get("name") else "待映射"),
        "target_kind": kind,
        "target_label": label,
        "target_symbol": label,
        "target_freq": "daily",
        "fallback_target": {
            "kind": "stock",
            "label": carrier_payload.get("symbol"),
            "symbol": carrier_payload.get("symbol"),
            "name": carrier_payload.get("name"),
            "reason": "chain_core_representative" if carrier_payload else "",
        } if carrier_payload else {},
        "carrier": carrier_payload,
        "representatives": representatives,
        "mapping_chain": _mapping_chain_from_carrier(label, carrier, kind=kind),
    })
    return enriched


def _build_sector_board_rows(
    *,
    industry_top: list[dict[str, Any]],
    concept_top: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind, source_rows in (("industry", industry_top), ("concept", concept_top)):
        for row in source_rows[:8]:
            item = _sector_board_preview(dict(row), kind)
            label = _text(item.get("label"))
            if not label:
                continue
            key = f"{kind}:{label}"
            if key in seen:
                continue
            seen.add(key)
            rows.append(item)
    rows.sort(key=lambda item: _float(item.get("day_change_pct") or item.get("change_pct") or item.get("gain_pct"), 0) or 0, reverse=True)
    return rows[:16]


def _build_trader_task_queue(
    *,
    decision_rows: list[dict[str, Any]],
    focus_stocks: list[dict[str, Any]],
    sector_boards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []

    def add(task: dict[str, Any]) -> None:
        if not task.get("title"):
            return
        task.setdefault("decision_id", f"task-{len(tasks) + 1}")
        task.setdefault("source", "second_screen")
        task.setdefault("action_label", task.get("trader_action") or task.get("action") or "观察")
        task.setdefault("invalidates_when", "触发条件失效或关键位被破坏")
        tasks.append(task)

    for row in focus_stocks:
        action = _text(row.get("trader_action")) or "观察"
        if action == "观察" and not row.get("latest_signal"):
            continue
        add({
            "decision_id": f"focus:{row.get('symbol') or row.get('label')}",
            "title": f"{action} · {row.get('name') or row.get('symbol')}",
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "action": action,
            "action_label": action,
            "priority": "high" if action in {"减仓/止盈", "可试仓"} else "medium",
            "summary": row.get("reason") or row.get("latest_signal") or "",
            "trigger_reason": row.get("latest_signal") or row.get("reason") or "",
            "chart_target": {"kind": "stock", "label": row.get("symbol"), "freq": "5min"},
            "invalidates_when": row.get("invalidates_when"),
        })

    for row in sector_boards[:6]:
        add({
            "decision_id": f"sector:{row.get('domain')}:{row.get('label')}",
            "title": f"观察 · {row.get('label')}",
            "symbol": row.get("carrier", {}).get("symbol") if isinstance(row.get("carrier"), dict) else "",
            "name": row.get("label"),
            "action": "观察",
            "action_label": "观察",
            "priority": "medium",
            "summary": row.get("explanation") or row.get("latest_signal") or "",
            "trigger_reason": row.get("explanation") or "",
            "chart_target": {
                "kind": row.get("target_kind") or row.get("domain") or "industry",
                "label": row.get("target_label") or row.get("label"),
                "freq": "daily",
                "fallback_target": row.get("fallback_target") or {},
            },
            "invalidates_when": row.get("invalidates_when"),
        })

    for row in decision_rows:
        if not isinstance(row, dict):
            continue
        action = _text(row.get("action_label") or row.get("recommended_action") or row.get("action")) or "观察"
        add({
            **row,
            "action": action,
            "action_label": action,
            "title": _text(row.get("title")) or f"{action} · {_text(row.get('symbol') or row.get('decision_id'))}",
            "trigger_reason": _text(row.get("summary") or row.get("reason") or row.get("recommended_action")),
            "chart_target": row.get("chart_target") or {"kind": "stock", "label": row.get("symbol"), "freq": "daily"},
            "invalidates_when": row.get("invalidates_when") or "复核条件解除或关键位被破坏",
        })

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task in tasks:
        key = _text(task.get("decision_id") or task.get("title"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(task)
    return deduped[:12]


def _build_watchlist_rows(
    *,
    reports: list[dict[str, Any]],
    buy_rows: list[dict[str, Any]],
    sell_rows: list[dict[str, Any]],
    decision_rows: list[dict[str, Any]],
    industry_top: list[dict[str, Any]],
    concept_top: list[dict[str, Any]],
    range_columns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(row: dict[str, Any], kind: str) -> None:
        label = str(row.get("symbol") or row.get("code") or row.get("label") or row.get("name") or "").strip()
        if not label:
            return
        key = f"{kind}:{label}"
        if key in seen:
            return
        seen.add(key)
        rows.append(row)

    for report in reports:
        row = _enrich_index_row(report, range_columns)
        add(row, "index")
    for row in buy_rows:
        enriched = _enrich_stock_row(dict(row), range_columns)
        add(enriched, "stock")
    for row in sell_rows:
        enriched = _enrich_stock_row(dict(row), range_columns)
        add(enriched, "stock")
    for row in decision_rows:
        if row.get("symbol"):
            enriched = _enrich_stock_row(dict(row), range_columns)
            add(enriched, "stock")
    for row in industry_top:
        add(_enrich_cluster_row(dict(row), "industry"), "industry")
    for row in concept_top:
        add(_enrich_cluster_row(dict(row), "concept"), "concept")
    return rows[:60]


def _serialize_trade_record(trade) -> Dict[str, Any]:
    return {
        "id": trade.id,
        "symbol": trade.symbol,
        "name": trade.name,
        "direction": trade.direction,
        "entry_date": trade.entry_date,
        "entry_price": trade.entry_price,
        "entry_signal": trade.entry_signal,
        "exit_date": trade.exit_date,
        "exit_price": trade.exit_price,
        "position_pct": trade.position_pct,
        "pnl_pct": trade.pnl_pct,
        "holding_days": trade.holding_days,
        "total_score": trade.total_score,
        "error_type": trade.error_type,
        "is_open": trade.is_open,
    }


def _trade_context(symbol: Optional[str]) -> Dict[str, Any]:
    log = get_trade_log()
    summary = log.get_summary()
    trades = log.list_trades(status="all", limit=200)
    missed = log.list_missed_signals(limit=50)

    related_trades = []
    related_missed = []
    if symbol:
        symbol_suffix = symbol.split(".", 1)[-1]
        for trade in trades:
            if trade.symbol == symbol or trade.symbol.endswith(symbol_suffix):
                related_trades.append(_serialize_trade_record(trade))
        for item in missed:
            if item.symbol == symbol or item.symbol.endswith(symbol_suffix):
                related_missed.append(
                    {
                        "symbol": item.symbol,
                        "name": item.name,
                        "signal_type": item.signal_type,
                        "signal_date": item.signal_date,
                        "signal_price": item.signal_price,
                        "max_price_after": item.max_price_after,
                        "potential_pnl_pct": item.potential_pnl_pct,
                    }
                )

    return {
        "summary": {
            "total_trades": summary.total_trades,
            "win_rate": summary.win_rate,
            "avg_pnl_pct": summary.avg_pnl_pct,
            "avg_score": summary.avg_score,
            "avg_holding_days": summary.avg_holding_days,
            "error_counts": summary.error_counts,
        },
        "related_trades": related_trades[:12],
        "missed_signals": related_missed[:8],
    }


def _review_context(engine, kind: str, label: str, symbol: Optional[str] = None) -> Dict[str, Any]:
    rv = engine.review_state
    payload: Dict[str, Any] = {
        "completed": rv.completed,
        "is_running": rv.is_running,
        "phase": rv.phase,
        "phase_detail": rv.phase_detail,
        "error": rv.error,
        "start_date": rv.start_date,
        "start_label": rv.start_label,
        "timing": rv.timing,
    }
    if kind == "stock" and symbol:
        timeline = rv.replay_timelines.get(symbol, [])
        payload["timeline"] = [serialize_signal_change(item) for item in timeline]
        for scored in rv.scored_symbols:
            if scored.symbol == symbol:
                payload["reviewed_symbol"] = serialize_scored_symbol(scored)
                break
    elif kind == "index":
        for report in rv.index_reports:
            if report.name == label:
                payload["reviewed_report"] = serialize_index_report(report)
                break
    elif kind == "industry":
        ranking = engine.get_industry_ranking_by_name(label)
        if ranking:
            payload["industry"] = {
                "name": ranking.name,
                "rotation_line": ranking.rotation_line,
                "phase": ranking.rhythm_phase,
                "phase_hint": ranking.rhythm_hint,
                "gain_pct": round(ranking.gain_pct, 2),
                "composite_score": round(ranking.composite_score, 1),
            }
    return payload


def _plan_for_index(engine, name: str) -> Optional[Dict[str, Any]]:
    try:
        from signals.core.planner import generate_plan

        analyzer = engine.get_symbol_analyzer(name, "daily")
        report = next((item for item in engine.get_index_reports() if item.name == name), None)
        if analyzer is None or report is None:
            return None
        plan = generate_plan(analyzer, getattr(report, "ma_context", None))
        plan.name = name
        return _serialize_plan(plan)
    except Exception:
        return None


def _build_shell_payload(engine) -> Dict[str, Any]:
    status = engine.get_status()
    session = _serialize_session(status)
    strategy_snapshot = _safe_strategy_snapshot()
    range_columns = _watchlist_range_columns()
    sync_lanes = _sync_lane_status()
    market_context = serialize_market_context(engine.get_market_context()) if engine.get_market_context() else None
    reports_raw = [
        serialize_index_report(report)
        for report in engine.get_index_reports()
        if getattr(report, "data_available", False)
    ]
    reports = [_enrich_index_row(report, range_columns) for report in reports_raw]
    macro_indices = _build_macro_index_rows(reports=reports_raw, range_columns=range_columns)
    strategy_candidates = [
        dict(item)
        for item in strategy_snapshot.get("candidates", [])
        if isinstance(item, dict)
    ]
    scored_raw = strategy_candidates or [serialize_scored_symbol(item) for item in engine.get_scored_symbols()[:8]]
    scored = [
        _enrich_stock_row(dict(item), range_columns) if item.get("symbol") else dict(item)
        for item in scored_raw
    ]
    sell_warnings = [
        _enrich_stock_row(dict(item), range_columns) if isinstance(item, dict) and item.get("symbol") else dict(item)
        for item in strategy_snapshot.get("warnings", [])
        if isinstance(item, dict)
    ]
    decision_rows_raw = [
        dict(item)
        for item in strategy_snapshot.get("decision_queue", [])
        if isinstance(item, dict)
    ]
    cluster = _unwrap_response(cluster_service.get_latest(top=8))

    snapshot_cluster = _cluster_from_strategy_snapshot(strategy_snapshot)
    industry_top = snapshot_cluster.get("industry_top") or (cluster.get("industry") or {}).get("top") or []
    concept_top = snapshot_cluster.get("concept_top") or (cluster.get("concept") or {}).get("top") or []
    sector_boards = _build_sector_board_rows(
        industry_top=industry_top,
        concept_top=concept_top,
    )
    focus_stocks = _build_focus_stock_rows(
        buy_rows=scored,
        sell_rows=sell_warnings,
        decision_rows=decision_rows_raw,
        range_columns=range_columns,
    )
    for rows, lane in (
        (macro_indices, "quote_lane"),
        (sector_boards, "board_lane"),
        (focus_stocks, "signal_lane"),
    ):
        for row in rows:
            row["lane_status"] = sync_lanes.get(lane, {})
            row["freshness"] = row["lane_status"].get("freshness", "unknown")

    decision_queue = _build_trader_task_queue(
        decision_rows=decision_rows_raw,
        focus_stocks=focus_stocks,
        sector_boards=sector_boards,
    )

    watchlist_directions: List[str] = []
    for report in reports[:5]:
        watchlist_directions.append(report["name"])
    for item in industry_top[:6]:
        label = item.get("label")
        if label and label not in watchlist_directions:
            watchlist_directions.append(label)
    watchlist = _build_watchlist_rows(
        reports=reports,
        buy_rows=scored,
        sell_rows=sell_warnings,
        decision_rows=decision_queue,
        industry_top=industry_top,
        concept_top=concept_top,
        range_columns=range_columns,
    )

    notices = []
    if not session["ready"]:
        notices.append("分析引擎正在启动，首屏数据会逐步填充。")
    if cluster.get("data_warning"):
        notices.append(cluster["data_warning"])

    return {
        "session": session,
        "market": market_context,
        "indices": reports[:8],
        "buy_candidates": scored,
        "sell_warnings": sell_warnings,
        "cluster_summary": {
            "industry_top": industry_top,
            "concept_top": concept_top,
            "market_status": cluster.get("market_status") or {},
            "data_warning": cluster.get("data_warning", ""),
        },
        "watchlist_groups": {
            "macro_indices": macro_indices,
            "sector_boards": sector_boards,
            "focus_stocks": focus_stocks,
        },
        "watchlist": watchlist,
        "watchlist_range_columns": range_columns,
        "sync_lanes": sync_lanes,
        "daily_brief": strategy_snapshot.get("daily_brief", {}),
        "decision_queue": decision_queue,
        "strategy_kpis": strategy_snapshot.get("strategy_kpis", {}),
        "source_confidence": strategy_snapshot.get("source_confidence", {}),
        "watchlist_directions": watchlist_directions[:10],
        "default_target": {
            "kind": "index",
            "label": macro_indices[0]["name"] if macro_indices else "沪深300",
            "freq": "daily",
        },
        "legacy_url": "/legacy",
        "notices": notices,
    }


def _safe_strategy_snapshot() -> Dict[str, Any]:
    try:
        snapshot = get_strategy_snapshot()
        return dict(snapshot) if isinstance(snapshot, dict) else {}
    except Exception as exc:
        return {
            "daily_brief": {"summary": f"strategy_snapshot_error:{exc.__class__.__name__}"},
            "candidates": [],
            "warnings": [],
            "themes": [],
            "decision_queue": [],
            "strategy_kpis": {},
            "source_confidence": {"overall": 0, "sources": []},
        }


def _cluster_from_strategy_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    themes = [
        item for item in snapshot.get("themes", [])
        if isinstance(item, dict)
    ]
    return {
        "industry_top": [
            {
                "label": item.get("name", ""),
                "name": item.get("name", ""),
                "kind": "industry",
                "domain": "board",
                "source": item.get("evidence", [{}])[0].get("source", "strategy_snapshot")
                if isinstance(item.get("evidence"), list) and item.get("evidence")
                else "strategy_snapshot",
                "change_pct": item.get("change_pct", item.get("strength", 0)),
                "leader": item.get("leader", ""),
                "phase": item.get("phase", ""),
            }
            for item in themes
            if item.get("domain") == "board"
        ][:6],
        "concept_top": [
            {
                "label": item.get("name", ""),
                "name": item.get("name", ""),
                "kind": "concept",
                "domain": "concept",
                "source": item.get("evidence", [{}])[0].get("source", "strategy_snapshot")
                if isinstance(item.get("evidence"), list) and item.get("evidence")
                else "strategy_snapshot",
                "change_pct": item.get("change_pct", item.get("strength", 0)),
                "leader": item.get("leader", ""),
                "phase": item.get("phase", ""),
            }
            for item in themes
            if item.get("domain") == "concept"
        ][:6],
    }


def _concept_theme_candidates(name: str) -> list[dict[str, Any]]:
    snapshot = get_strategy_snapshot()
    themes = [
        item for item in snapshot.get("themes", [])
        if isinstance(item, dict) and item.get("domain") == "concept"
    ]
    exact = [item for item in themes if item.get("name") == name]
    if exact:
        return exact
    return [
        item for item in themes
        if name and (name in str(item.get("name", "")) or str(item.get("name", "")) in name)
    ]


def _preferred_concept_carriers(
    concept_name: str,
    theme_candidates: list[dict[str, Any]],
    related_industries: list[str],
) -> list[dict[str, Any]]:
    from signals.core.concept_carriers import preferred_concept_carriers

    return preferred_concept_carriers(
        concept_name,
        aliases=[_text(item.get("name")) for item in theme_candidates],
        related_industries=related_industries,
    )


def _mongo_db():
    from signals.sync.db import get_db

    return get_db()


def _serialize_dt(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat(timespec="seconds")
        except TypeError:
            return value.isoformat()
    return str(value)


def _sync_lane_status() -> dict[str, dict[str, Any]]:
    status = {
        lane: {
            "lane": lane,
            **meta,
            "status": "unknown",
            "freshness": "unknown",
            "last_success_at": "",
            "last_run_at": "",
            "next_due_at": "",
            "degraded_reason": "",
            "modules": [],
        }
        for lane, meta in SECOND_SCREEN_LANES.items()
    }
    try:
        db = _mongo_db()
        docs = list(db["sync_log"].find(
            {"lane": {"$in": list(SECOND_SCREEN_LANES)}},
            {"_id": 0, "module": 1, "market": 1, "lane": 1, "status": 1, "last_run": 1, "next_due_at": 1, "degraded_reason": 1, "error_msg": 1},
        ).sort("last_run", -1).limit(80))
    except Exception:
        return status
    for doc in docs:
        lane = _text(doc.get("lane"))
        if lane not in status:
            continue
        item = status[lane]
        module = _text(doc.get("module"))
        if module and module not in item["modules"]:
            item["modules"].append(module)
        if not item["last_run_at"]:
            item["last_run_at"] = _serialize_dt(doc.get("last_run"))
            item["next_due_at"] = _serialize_dt(doc.get("next_due_at"))
            item["status"] = _text(doc.get("status")) or "unknown"
            item["freshness"] = "fresh" if item["status"] == "ok" else "stale" if item["status"] in {"degraded", "error"} else item["status"]
            item["degraded_reason"] = _text(doc.get("degraded_reason") or doc.get("error_msg"))
        if doc.get("status") == "ok" and not item["last_success_at"]:
            item["last_success_at"] = _serialize_dt(doc.get("last_run"))
    return status


def _stock_symbol_from_code_or_name(code: Any = "", name: Any = "") -> tuple[str, str]:
    for value in (_text(code), _text(name)):
        if not value:
            continue
        normalized, raw_code = _normalize_stock_symbol(value)
        if normalized and raw_code:
            return normalized, raw_code
    return "", ""


def _ensure_daily_bars(symbol: str, raw_code: str) -> bool:
    df, _ = _stock_df(symbol, "daily")
    if df is not None and not df.empty:
        return True
    code = raw_code or symbol.split(".", 1)[-1]
    if not code or not code.isdigit():
        return False
    try:
        from signals.sync.modules.stock_daily import _sync_one_stock

        docs = _sync_one_stock(
            code,
            (datetime.now() - timedelta(days=730)).strftime("%Y%m%d"),
            datetime.now().strftime("%Y%m%d"),
        )
        if not docs:
            return False
        db = _mongo_db()
        existing_dts = {
            item.get("dt")
            for item in db["bars"].find(
                {
                    "meta.symbol": code,
                    "meta.freq": "日线",
                    "dt": {"$in": [doc["dt"] for doc in docs]},
                },
                {"dt": 1},
            )
        }
        new_docs = [doc for doc in docs if doc["dt"] not in existing_dts]
        if new_docs:
            db["bars"].insert_many(new_docs, ordered=False)
        db["sync_log"].update_one(
            {"_id": f"stock_daily:{code}"},
            {"$set": {
                "module": "stock_daily",
                "symbol": code,
                "last_dt": docs[-1]["dt"],
                "last_run": datetime.now(),
                "status": "ok",
                "bar_count": len(docs),
                "written": len(new_docs),
                "source": "concept_carrier_preheat",
            }},
            upsert=True,
        )
        return True
    except Exception:
        return False


def _ensure_minute_bars(symbol: str, raw_code: str, freq: str) -> bool:
    requested = _canonical_freq(freq)
    minute_freq = {
        "5min": "5分钟",
        "15min": "15分钟",
        "30min": "30分钟",
    }.get(requested)
    if not minute_freq:
        return True
    df, _ = _stock_df(symbol, requested)
    if df is not None and not df.empty:
        return True
    code = raw_code or symbol.split(".", 1)[-1]
    if not code or not code.isdigit():
        return False
    try:
        from signals.sync.modules.stock_minute import _sync_one_minute

        docs = _sync_one_minute(code, minute_freq)
        if not docs:
            return False
        db = _mongo_db()
        db["bars"].delete_many({"meta.symbol": code, "meta.freq": minute_freq})
        db["bars"].insert_many(docs, ordered=False)
        db["sync_log"].update_one(
            {"_id": f"stock_minute:{code}:{minute_freq}"},
            {"$set": {
                "module": "stock_minute",
                "symbol": code,
                "last_dt": docs[-1]["dt"],
                "last_run": datetime.now(),
                "status": "ok",
                "bar_count": len(docs),
                "source": docs[-1].get("meta", {}).get("source"),
            }},
            upsert=True,
        )
        return True
    except Exception:
        return False


def _concept_rank_rows(concept_name: str, theme_candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = [concept_name] + [_text(item.get("name")) for item in theme_candidates]
    names = [name for index, name in enumerate(names) if name and name not in names[:index]]
    rows: list[dict[str, Any]] = []
    try:
        db = _mongo_db()
    except Exception:
        return rows
    for collection in ("concept_sina", "concept_em", "concept_ths", "concept_ranking"):
        if collection not in db.list_collection_names():
            continue
        for name in names:
            query = {"$or": [
                {"board_name": {"$regex": name}},
                {"concept": {"$regex": name}},
                {"concept_name": {"$regex": name}},
            ]}
            for row in db[collection].find(query).sort("dt", -1).limit(8):
                item = dict(row)
                item.setdefault("source", collection)
                rows.append(item)
    return rows


def _industry_constituent_symbols(industry_name: str) -> list[str]:
    symbols: list[str] = []
    try:
        db = _mongo_db()
    except Exception:
        return symbols
    query = {"$or": [{"board_name": industry_name}, {"concept_name": industry_name}]}
    for collection in ("board_constituents", "concept_constituents"):
        if collection not in db.list_collection_names():
            continue
        for row in db[collection].find(query).sort("updated_at", -1).limit(4):
            for symbol in row.get("symbols") or []:
                normalized, _ = _normalize_stock_symbol(str(symbol))
                if normalized and normalized not in symbols:
                    symbols.append(normalized)
    return symbols


def _industry_leader_candidate(industry_name: str) -> Optional[dict[str, Any]]:
    try:
        from signals.layers.industry import _INDUSTRY_LEADERS
    except Exception:
        return None
    leader = _INDUSTRY_LEADERS.get(industry_name)
    if not leader:
        return None
    symbol, name = leader
    normalized, raw_code = _normalize_stock_symbol(symbol)
    if not normalized:
        return None
    return {
        "symbol": normalized,
        "raw_code": raw_code or normalized.split(".", 1)[-1],
        "name": name,
        "source": "industry_leader_map",
        "relation": f"{industry_name} 龙头",
        "priority": 64,
    }


def _available_daily_carrier(
    candidates: list[dict[str, Any]],
    *,
    preserve_order: bool = False,
) -> Optional[dict[str, Any]]:
    available: list[dict[str, Any]] = []
    for item in candidates:
        symbol = _text(item.get("symbol"))
        raw_code = _text(item.get("raw_code"))
        if not symbol:
            symbol, raw_code = _stock_symbol_from_code_or_name(item.get("code"), item.get("name"))
        if not symbol:
            continue
        if item.get("source") in {"semantic_preferred_carrier", "semantic_industry_chain", "industry_leader_map"}:
            _ensure_daily_bars(symbol, raw_code)
        df, source = _stock_df(symbol, "daily")
        if df is None or df.empty:
            continue
        available.append({
            **item,
            "symbol": symbol,
            "raw_code": raw_code or symbol.split(".", 1)[-1],
            "name": _text(item.get("name")) or _stock_name(symbol),
            "bar_count": int(len(df)),
            "bar_source": source,
        })
    if not available:
        return None
    if preserve_order:
        return available[0]
    available.sort(key=lambda item: (int(item.get("priority") or 0), int(item.get("bar_count") or 0)), reverse=True)
    return available[0]


def _cached_daily_carrier(
    candidates: list[dict[str, Any]],
    *,
    preserve_order: bool = False,
) -> Optional[dict[str, Any]]:
    available: list[dict[str, Any]] = []
    for item in candidates:
        symbol = _text(item.get("symbol"))
        raw_code = _text(item.get("raw_code"))
        if not symbol:
            symbol, raw_code = _stock_symbol_from_code_or_name(item.get("code"), item.get("name"))
        if not symbol:
            continue
        df, source = _stock_df(symbol, "daily")
        if df is None or df.empty:
            continue
        available.append({
            **item,
            "symbol": symbol,
            "raw_code": raw_code or symbol.split(".", 1)[-1],
            "name": _text(item.get("name")) or _stock_name(symbol),
            "bar_count": int(len(df)),
            "bar_source": source,
        })
    if not available:
        return None
    if preserve_order:
        return available[0]
    available.sort(key=lambda item: (int(item.get("priority") or 0), int(item.get("bar_count") or 0)), reverse=True)
    return available[0]


def _concept_carrier_candidates(
    concept_name: str,
    theme_candidates: list[dict[str, Any]],
    related_industries: list[str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def add(
        symbol: str = "",
        raw_code: str = "",
        name: str = "",
        source: str = "",
        relation: str = "",
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        if not symbol:
            symbol, raw_code = _stock_symbol_from_code_or_name(raw_code, name)
        if not symbol:
            return
        key = symbol.upper()
        rep_type = _text((extra or {}).get("representative_type"))
        if any(
            _text(item.get("symbol")).upper() == key
            and _text(item.get("representative_type")) == rep_type
            for item in candidates
        ):
            return
        candidates.append({
            "symbol": symbol,
            "raw_code": raw_code or symbol.split(".", 1)[-1],
            "name": name or _stock_name(symbol),
            "source": source,
            "relation": relation,
        })
        if extra:
            candidates[-1].update(extra)

    for item in _preferred_concept_carriers(concept_name, theme_candidates, related_industries):
        add(
            symbol=_text(item.get("symbol")),
            name=_text(item.get("name")),
            source=_text(item.get("source")),
            relation=_text(item.get("relation")),
            extra={
                "priority": item.get("priority"),
                "base_priority": item.get("base_priority"),
                "chain_id": item.get("chain_id"),
                "chain_name": item.get("chain_name"),
                "node_id": item.get("node_id"),
                "node_name": item.get("node_name"),
                "layer": item.get("layer"),
                "stage": item.get("stage"),
                "representative_type": item.get("representative_type"),
                "source_note": item.get("source_note"),
                "confidence": item.get("confidence"),
                "hit_terms": item.get("hit_terms"),
                "evidence_sources": item.get("evidence_sources"),
            },
        )

    for row in _concept_rank_rows(concept_name, theme_candidates):
        add(
            raw_code=_text(row.get("leader_code")),
            name=_text(row.get("leader_name") or row.get("leader")),
            source=_text(row.get("source")) or "concept_rank",
            relation=_text(row.get("board_name") or row.get("concept") or row.get("concept_name") or concept_name),
            extra={
                "representative_type": "source_leader",
                "source_rank": row.get("rank"),
                "source_dt": str(row.get("dt") or row.get("date") or ""),
            },
        )
    for theme in theme_candidates:
        add(
            name=_text(theme.get("leader")),
            source="strategy_snapshot",
            relation=_text(theme.get("name")) or concept_name,
            extra={"representative_type": "source_leader"},
        )

    for industry in related_industries:
        leader = _industry_leader_candidate(industry)
        if leader:
            before = len(candidates)
            add(
                symbol=_text(leader.get("symbol")),
                raw_code=_text(leader.get("raw_code")),
                name=_text(leader.get("name")),
                source=_text(leader.get("source")),
                relation=_text(leader.get("relation")),
                extra={"representative_type": "industry_leader"},
            )
            if len(candidates) > before:
                candidates[-1]["priority"] = leader.get("priority")
        for symbol in _industry_constituent_symbols(industry):
            add(
                symbol=symbol,
                source="industry_constituents",
                relation=industry,
                extra={"representative_type": "industry_constituent"},
            )
    return candidates


def _representative_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": item.get("symbol"),
        "raw_code": item.get("raw_code"),
        "name": item.get("name"),
        "relation": item.get("relation"),
        "source": item.get("source"),
        "source_note": item.get("source_note"),
        "priority": item.get("priority"),
        "base_priority": item.get("base_priority"),
        "chain_id": item.get("chain_id"),
        "chain_name": item.get("chain_name"),
        "node_id": item.get("node_id"),
        "node_name": item.get("node_name"),
        "layer": item.get("layer"),
        "stage": item.get("stage"),
        "confidence": item.get("confidence"),
        "hit_terms": item.get("hit_terms") or [],
        "evidence_sources": item.get("evidence_sources") or [],
    }


def _concept_representative_groups(candidates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {"core": [], "elastic": [], "source_leader": []}
    seen: dict[str, set[str]] = {key: set() for key in groups}
    for item in candidates:
        rep_type = _text(item.get("representative_type"))
        if rep_type not in groups:
            if item.get("source") in {"concept_rank", "strategy_snapshot", "concept_sina", "concept_em", "concept_ths"}:
                rep_type = "source_leader"
            else:
                continue
        symbol = _text(item.get("symbol")).upper()
        if not symbol or symbol in seen[rep_type]:
            continue
        seen[rep_type].add(symbol)
        groups[rep_type].append(_representative_payload(item))
    return {key: value[:8] for key, value in groups.items()}


def _ordered_candidate_stocks(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = _concept_representative_groups(candidates)
    ordered: list[dict[str, Any]] = [
        *groups.get("core", []),
        *groups.get("elastic", []),
        *groups.get("source_leader", []),
    ]
    seen = {_text(item.get("symbol")).upper() for item in ordered if item.get("symbol")}
    for item in candidates:
        symbol = _text(item.get("symbol")).upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        ordered.append(_representative_payload(item))
    return ordered[:12]


def _summary_from_index(report: Dict[str, Any], chart: Dict[str, Any]) -> Dict[str, Any]:
    chart_report = chart.get("report") or {}
    ma_context = report.get("ma_context") or {}
    engine = get_engine()
    market_context = engine.get_market_context()
    style_switch = getattr(market_context, "style_switch", None) if market_context else None
    return {
        "title": report.get("name", ""),
        "subtitle": report.get("symbol", ""),
        "latest_price": report.get("latest_price", 0),
        "conclusion": chart_report.get("conclusion") or report.get("summary", ""),
        "daily_trend": report.get("daily_trend", ""),
        "f30_trend": report.get("f30_trend", ""),
        "f15_trend": report.get("f15_trend", ""),
        "latest_signal": chart_report.get("daily_latest_signal") or report.get("daily_latest_signal", ""),
        "key_levels": chart_report.get("key_levels") or ma_context.get("key_levels") or [],
        "style_switch": style_switch.suggestion if style_switch else "",
    }


def _summary_from_static_index(name: str, symbol: str, chart: Dict[str, Any]) -> Dict[str, Any]:
    last_close = chart.get("ohlcv", [{}])[-1].get("close", 0) if chart.get("ohlcv") else 0
    return {
        "title": name,
        "subtitle": symbol,
        "latest_price": last_close,
        "conclusion": "引擎热身中，先使用本地指数K线缓存。",
        "daily_trend": "",
        "f30_trend": "",
        "f15_trend": "",
        "latest_signal": "",
        "key_levels": [],
    }


def _summary_from_industry(name: str, detail: Dict[str, Any], ranking) -> Dict[str, Any]:
    report = detail.get("report") or {}
    info = detail.get("industry_info") or {}
    conclusion = "震荡观察"
    if report.get("has_buy_signal"):
        conclusion = "行业趋势偏强，可结合候选股观察入场。"
    elif report.get("has_sell_signal"):
        conclusion = "行业处于分歧或退潮，优先防守。"
    return {
        "title": name,
        "subtitle": info.get("rotation_line", ""),
        "latest_price": detail.get("ohlcv", [{}])[-1].get("close", 0) if detail.get("ohlcv") else 0,
        "conclusion": conclusion,
        "daily_trend": report.get("daily_trend", ""),
        "f30_trend": "",
        "f15_trend": "",
        "latest_signal": report.get("daily_latest_signal", ""),
        "key_levels": [],
        "gain_pct": info.get("gain_pct", 0),
        "composite_score": info.get("composite_score", 0),
        "phase": info.get("phase", ""),
        "phase_hint": info.get("phase_hint", ""),
        "candidate_count": len(ranking.candidates) if ranking else 0,
    }


def _summary_from_stock(symbol: str, stock: Dict[str, Any], chart: Dict[str, Any]) -> Dict[str, Any]:
    scored = stock.get("scored") or {}
    ma_context = stock.get("ma_context") or {}
    risk = stock.get("risk") or {}
    last_close = chart.get("ohlcv", [{}])[-1].get("close", 0) if chart.get("ohlcv") else 0
    conclusion = scored.get("direction", "")
    if risk.get("description"):
        conclusion = f"{conclusion} · {risk['description']}".strip(" ·")
    return {
        "title": stock.get("name") or symbol,
        "subtitle": symbol,
        "latest_price": last_close,
        "conclusion": conclusion or "等待更多确认",
        "daily_trend": ma_context.get("trend_summary", ""),
        "f30_trend": "",
        "f15_trend": "",
        "latest_signal": chart.get("signals", [{}])[-1].get("type", "") if chart.get("signals") else "",
        "key_levels": ma_context.get("key_levels") or [],
        "score": scored.get("total_score"),
        "fused_total": scored.get("fused_total"),
        "risk_reward": risk.get("risk_reward"),
        "position_pct": risk.get("position_pct"),
    }


async def _build_index_target(engine, name: str, freq: str) -> Dict[str, Any]:
    requested_freq = _canonical_freq(freq)
    report_obj = next((item for item in engine.get_index_reports() if item.name == name), None)
    if report_obj is None:
        static_index = _resolve_static_index(name)
        if static_index is None:
            raise HTTPException(status_code=404, detail=f"未找到指数: {name}")
        return await _build_static_index_target(static_index[0], static_index[1], requested_freq)

    report = serialize_index_report(report_obj)
    if requested_freq in {"daily", "30min", "15min"}:
        chart = _unwrap_response(get_chart_data(name, freq=requested_freq))
    else:
        df, source = _index_df(str(report.get("symbol") or name), requested_freq)
        chart = _chart_from_df(df, symbol=str(report.get("symbol") or name), freq=requested_freq, source=source)
    chart = _fallback_chart_when_empty(
        chart,
        symbol=str(report.get("symbol") or name),
        requested_freq=requested_freq,
        loader=lambda fallback_freq: _index_df(str(report.get("symbol") or name), fallback_freq),
    )
    plan = _plan_for_index(engine, name)
    analysis_target = _top_candidate_symbol(engine)

    return {
        "target": {
            "kind": "index",
            "label": name,
            "symbol": report.get("symbol", ""),
            "requested_freq": requested_freq,
            "effective_freq": chart.get("meta", {}).get("freq", requested_freq),
            "available_freqs": UI_FREQS,
        },
        "chart": chart,
        "summary": _summary_from_index(report, chart),
        "signals": chart.get("signals", []),
        "plan": plan,
        "review": _review_context(engine, "index", name),
        "trade": _trade_context(None),
        "analysis_target": analysis_target,
        "candidate_stocks": [serialize_scored_symbol(item) for item in engine.get_scored_symbols()[:10]],
    }


async def _build_static_index_target(name: str, symbol: str, freq: str) -> Dict[str, Any]:
    requested_freq = _canonical_freq(freq)
    df, source = _index_df(symbol, requested_freq)
    chart = _chart_from_df(df, symbol=symbol, freq=requested_freq, source=source)
    chart = _fallback_chart_when_empty(
        chart,
        symbol=symbol,
        requested_freq=requested_freq,
        loader=lambda fallback_freq: _index_df(symbol, fallback_freq),
    )
    return {
        "target": {
            "kind": "index",
            "label": name,
            "symbol": symbol,
            "requested_freq": requested_freq,
            "effective_freq": chart.get("meta", {}).get("freq", requested_freq),
            "available_freqs": UI_FREQS,
        },
        "chart": chart,
        "summary": _summary_from_static_index(name, symbol, chart),
        "signals": chart.get("signals", []),
        "plan": None,
        "review": {},
        "trade": _trade_context(None),
        "analysis_target": "",
        "candidate_stocks": [],
    }


async def _build_industry_target(engine, name: str, freq: str) -> Dict[str, Any]:
    requested_freq = _canonical_freq(freq)
    ranking = engine.get_industry_ranking_by_name(name)
    candidate_stocks = []
    analysis_target = ""
    if ranking:
        candidate_stocks = [
            {
                "code": candidate.code,
                "name": candidate.name,
                "role": candidate.role,
                "priority": candidate.priority,
                "detail": candidate.detail,
            }
            for candidate in ranking.candidates[:10]
        ]
        if candidate_stocks:
            analysis_target = candidate_stocks[0]["code"]

    leader_name = candidate_stocks[0]["name"] if candidate_stocks else ""
    carrier_candidates = _industry_carrier_candidates(name, leader_name)
    carrier = _preview_carrier(carrier_candidates)

    async def fallback_to_carrier(reason: str) -> Dict[str, Any]:
        fallback = carrier
        if not fallback and candidate_stocks:
            symbol, raw_code = _stock_symbol_from_code_or_name(candidate_stocks[0].get("code"), candidate_stocks[0].get("name"))
            if symbol:
                fallback = {
                    "symbol": symbol,
                    "raw_code": raw_code,
                    "name": candidate_stocks[0].get("name"),
                    "relation": name,
                    "source": "industry_candidates",
                    "representative_type": "source_leader",
                }
        if not fallback:
            raise HTTPException(status_code=404, detail=f"无法获取 {name} K线数据，且未找到可承接代表股")
        _ensure_daily_bars(fallback["symbol"], fallback.get("raw_code", ""))
        _ensure_minute_bars(fallback["symbol"], fallback.get("raw_code", ""), requested_freq)
        payload = await _build_stock_target(fallback["symbol"], fallback.get("raw_code", ""), requested_freq)
        stock_title = payload.get("summary", {}).get("title") or fallback.get("name") or fallback["symbol"]
        mapping_chain = _mapping_chain_from_carrier(name, fallback, kind="industry")
        payload["target"] = {
            **payload.get("target", {}),
            "kind": "industry",
            "label": name,
            "symbol": fallback["symbol"],
            "requested_freq": requested_freq,
            "carrier_kind": "stock",
            "carrier_symbol": fallback["symbol"],
        }
        payload["summary"] = {
            **payload.get("summary", {}),
            "title": name,
            "subtitle": f"行业承接 -> {stock_title}({fallback['symbol']})",
            "conclusion": f"{name} 行业板块 K 线暂不可用，已用代表股 {stock_title} 承接图形复核。",
            "candidate_count": len(candidate_stocks),
            "carrier": _representative_payload(fallback),
            "mapping_chain": mapping_chain,
            "fallback_reason": reason,
        }
        payload["candidate_stocks"] = candidate_stocks or [_representative_payload(item) for item in carrier_candidates[:10]]
        payload["analysis_target"] = fallback["symbol"]
        return payload

    try:
        detail = _unwrap_response(get_industry_detail(name))
    except HTTPException:
        return await fallback_to_carrier("industry_ohlcv_unavailable")
    if not detail.get("ohlcv"):
        return await fallback_to_carrier("industry_ohlcv_empty")

    return {
        "target": {
            "kind": "industry",
            "label": name,
            "symbol": name,
            "requested_freq": requested_freq,
            "effective_freq": "daily",
            "available_freqs": ["daily"],
        },
        "chart": detail,
        "summary": _summary_from_industry(name, detail, ranking),
        "signals": detail.get("signals", []),
        "plan": None,
        "review": _review_context(engine, "industry", name),
        "trade": _trade_context(analysis_target or None),
        "analysis_target": analysis_target,
        "candidate_stocks": candidate_stocks or [_representative_payload(item) for item in carrier_candidates[:10]],
    }


async def _build_concept_target(engine, name: str, freq: str) -> Dict[str, Any]:
    requested_freq = _canonical_freq(freq)
    concept = next((item for item in engine.get_concepts() if getattr(item, "name", "") == name), None)
    theme_candidates = _concept_theme_candidates(name)
    theme = theme_candidates[0] if theme_candidates else {}
    related = list(getattr(concept, "related_industries", []) or [])
    if not related:
        try:
            from signals.layers.industry import _map_concept_to_industries

            for concept_key in [name] + [_text(item.get("name")) for item in theme_candidates]:
                for industry in _map_concept_to_industries(concept_key):
                    if industry not in related:
                        related.append(industry)
        except Exception:
            related = []

    carrier_candidates = _concept_carrier_candidates(name, theme_candidates, related)
    representatives = _concept_representative_groups(carrier_candidates)
    core_candidates = [
        item for item in carrier_candidates
        if item.get("representative_type") == "core"
    ]
    semantic_candidates = [
        item for item in carrier_candidates
        if item.get("source") == "semantic_industry_chain"
    ]
    carrier = (
        _available_daily_carrier(core_candidates, preserve_order=True)
        or _available_daily_carrier(semantic_candidates)
    )
    if carrier:
        _ensure_minute_bars(carrier["symbol"], carrier["raw_code"], requested_freq)
        payload = await _build_stock_target(carrier["symbol"], carrier["raw_code"], requested_freq)
        if not _chart_has_ohlcv(payload.get("chart", {})):
            df, source = _stock_df(carrier["symbol"], requested_freq)
            payload["chart"] = _chart_from_df(df, symbol=carrier["symbol"], freq=requested_freq, source=source)
        relation = _text(carrier.get("relation")) or name
        stock_title = payload.get("summary", {}).get("title") or carrier.get("name") or carrier["symbol"]
        concept_chain = [_text(item.get("name")) for item in theme_candidates]
        concept_chain = [item for item in concept_chain if item]
        if name not in concept_chain:
            concept_chain.insert(0, name)
        chain_name = _text(carrier.get("chain_name"))
        chain_stage = _text(carrier.get("stage"))
        node_id = _text(carrier.get("node_id"))
        node_name = _text(carrier.get("node_name"))
        layer = _text(carrier.get("layer"))
        semantic_path = [item for item in ["/".join(concept_chain[:3]), chain_name, chain_stage, relation] if item]
        mapping_chain = {
            "query": name,
            "concepts": concept_chain[:5],
            "industries": related[:5],
            "chain_id": carrier.get("chain_id"),
            "chain_name": chain_name,
            "node_id": node_id,
            "node_name": node_name,
            "layer": layer,
            "stage": chain_stage,
            "confidence": carrier.get("confidence"),
            "evidence_sources": carrier.get("evidence_sources") or [],
            "industry_chain": {
                "chain_id": carrier.get("chain_id"),
                "chain_name": chain_name,
                "name": chain_name,
                "node_id": node_id,
                "node_name": node_name,
                "layer": layer,
                "stage": chain_stage,
                "confidence": carrier.get("confidence"),
                "hit_terms": carrier.get("hit_terms") or [],
                "evidence_sources": carrier.get("evidence_sources") or [],
            } if chain_name else {},
            "carrier": {
                "symbol": carrier["symbol"],
                "name": stock_title,
                "relation": relation,
                "source": carrier.get("source"),
                "chain_name": chain_name,
                "node_id": node_id,
                "node_name": node_name,
                "layer": layer,
                "stage": chain_stage,
                "representative_type": carrier.get("representative_type"),
                "bar_source": carrier.get("bar_source"),
                "bar_count": carrier.get("bar_count"),
            },
        }
        payload["target"] = {
            **payload.get("target", {}),
            "kind": "concept",
            "label": name,
            "symbol": getattr(concept, "code", "") or name,
            "requested_freq": requested_freq,
            "carrier_kind": "stock",
            "carrier_symbol": carrier["symbol"],
        }
        payload["summary"] = {
            **payload.get("summary", {}),
            "title": name,
            "subtitle": f"{name} -> {' -> '.join(semantic_path)} -> {stock_title}",
            "conclusion": f"{name} 已映射到 {' -> '.join(semantic_path)}，选择 {stock_title}({carrier['symbol']}) 作为图形复核标的。",
            "gain_pct": getattr(concept, "gain_pct", None) or theme.get("change_pct"),
            "composite_score": getattr(concept, "composite_score", None) or theme.get("strength"),
            "carrier": mapping_chain["carrier"],
            "representatives": representatives,
            "mapping_chain": mapping_chain,
        }
        payload["analysis_target"] = carrier["symbol"]
        payload["candidate_stocks"] = _ordered_candidate_stocks(carrier_candidates)
        return payload

    return {
        "target": {
            "kind": "concept",
            "label": name,
            "symbol": getattr(concept, "code", "") or name,
            "requested_freq": requested_freq,
            "effective_freq": "daily",
            "available_freqs": ["daily"],
        },
        "chart": _chart_from_df(pd.DataFrame(), symbol=name, freq="daily", source="concept_unmapped"),
        "summary": {
            "title": name,
            "subtitle": "概念板块",
            "latest_price": 0,
            "conclusion": "暂未找到可映射行业或领涨股，等待概念成分/板块 K 线预热。",
            "key_levels": [],
            "representatives": representatives,
            "mapping_chain": {
                "query": name,
                "concepts": [name],
                "industries": related[:5],
                "chain_id": None,
                "chain_name": "",
                "node_id": "",
                "node_name": "",
                "layer": "",
                "confidence": 0,
                "evidence_sources": [],
            },
        },
        "signals": [],
        "plan": None,
        "review": _review_context(engine, "concept", name),
        "trade": _trade_context(None),
        "analysis_target": "",
        "candidate_stocks": _ordered_candidate_stocks(carrier_candidates),
    }


async def _build_stock_target(symbol: str, raw_code: str, freq: str) -> Dict[str, Any]:
    requested_freq = _canonical_freq(freq)
    if requested_freq in {"daily", "weekly", "monthly"}:
        chart = _unwrap_response(
            await _call_backtest_run(raw_code, requested_freq, lookback=360)
        )
        if isinstance(chart, dict) and chart.get("error"):
            df, source = _stock_df(symbol, requested_freq)
            chart = _chart_from_df(df, symbol=symbol, freq=requested_freq, source=source)
    else:
        df, source = _stock_df(symbol, requested_freq)
        chart = _chart_from_df(df, symbol=symbol, freq=requested_freq, source=source)
    chart = _fallback_chart_when_empty(
        chart,
        symbol=symbol,
        requested_freq=requested_freq,
        loader=lambda fallback_freq: _stock_df(symbol, fallback_freq),
    )
    try:
        stock = _unwrap_response(analyze_stock(symbol))
    except Exception as exc:
        stock = {
            "symbol": symbol,
            "name": _stock_name(symbol),
            "errors": [f"analyze_stock_error:{exc.__class__.__name__}"],
            "ma_context": {},
            "scored": {},
            "risk": {},
            "scenarios": [],
            "layered_position": {},
        }
    engine = _ensure_engine()
    return {
        "target": {
            "kind": "stock",
            "label": stock.get("name") or symbol,
            "symbol": symbol,
            "requested_freq": requested_freq,
            "effective_freq": chart.get("meta", {}).get("freq", requested_freq),
            "available_freqs": UI_FREQS,
        },
        "chart": chart,
        "summary": _summary_from_stock(symbol, stock, chart),
        "signals": chart.get("signals", []),
        "plan": {
            "scenarios": stock.get("scenarios", []),
            "layered_position": stock.get("layered_position", {}),
        },
        "review": _review_context(engine, "stock", symbol, symbol=symbol),
        "trade": _trade_context(symbol),
        "analysis_target": symbol,
        "candidate_stocks": [],
        "stock_analysis": stock,
    }


def _timestamp_range_to_dates(start: Optional[int], end: Optional[int]) -> Tuple[Optional[str], Optional[str]]:
    if not start or not end:
        return None, None
    start_dt = start if start < end else end
    end_dt = end if end > start else start
    from datetime import datetime

    return (
        datetime.fromtimestamp(start_dt).strftime("%Y-%m-%d"),
        datetime.fromtimestamp(end_dt).strftime("%Y-%m-%d"),
    )


def _in_date_range(date_str: str, start: Optional[str], end: Optional[str]) -> bool:
    if not date_str:
        return True
    normalized = date_str[:10]
    if start and normalized < start:
        return False
    if end and normalized > end:
        return False
    return True


def _filter_backtest_payload(payload: Dict[str, Any], start: Optional[str], end: Optional[str]) -> Dict[str, Any]:
    if not start and not end:
        return payload

    signals = [
        item for item in payload.get("signals", [])
        if _in_date_range(item.get("date_str") or item.get("signal_date") or item.get("dt_str", ""), start, end)
    ]
    trades = [
        item for item in payload.get("sim_trades", [])
        if _in_date_range(item.get("entry_date", ""), start, end)
    ]
    filtered = dict(payload)
    filtered["signals"] = signals
    filtered["sim_trades"] = trades
    filtered["range"] = {"start": start, "end": end}
    return filtered


async def _call_backtest_run(code: str, freq: str, lookback: int = 360) -> Any:
    return await backtest_service.backtest_run(
        code=code,
        freq=freq,
        signal_group="all",
        lookback=lookback,
        factor="",
        gap_pct_min=2.0,
        volume_ratio_min=1.5,
        trend_lookback=20,
        bb_period=20,
        squeeze_threshold=0.05,
    )


async def _call_backtest_analyze(code: str, freq: str, lookback: int = 180) -> Any:
    return await backtest_service.backtest_analyze(
        code=code,
        freq=freq,
        signal_group="all",
        lookback=lookback,
        factor="",
        gap_pct_min=2.0,
        volume_ratio_min=1.5,
        trend_lookback=20,
        bb_period=20,
        squeeze_threshold=0.05,
        run_count=3,
        body_ratio=0.5,
        accel_count=3,
        stop_loss=5.0,
        trail_stop=50.0,
        max_hold=20,
        slippage=0.1,
        take_profit=0,
        ma_exit_period=0,
        profit_drawdown=0,
        batch_exit="0",
        batch1_ratio=50,
        batch1_target=5,
        batch2_target=10,
        atr_exit_period=0,
        atr_exit_mult=2.0,
    )


@router.get("/shell")
async def get_workbench_shell():
    engine = _ensure_engine()
    return _build_shell_payload(engine)


@router.get("/cluster")
async def get_workbench_cluster(
    top: int = Query(5, ge=1, le=12),
    direction: str = Query("", description="观察池方向"),
    mode: str = Query("belief", description="belief / panic"),
    scan_top: int = Query(20, ge=1, le=60),
):
    latest = _unwrap_response(cluster_service.get_latest(top=top))
    history = _unwrap_response(cluster_service.get_history())
    scan = None
    if direction.strip():
        scan = _unwrap_response(cluster_service.get_watchlist(direction=direction.strip(), mode=mode, top=scan_top))
    return {
        "latest": latest,
        "history": history,
        "scan": scan,
    }


@router.get("/symbol/{symbol:path}")
async def get_workbench_symbol(
    symbol: str,
    kind: str = Query("auto", description="auto / index / industry / concept / stock"),
    freq: str = Query("daily", description="5min / 15min / 30min / daily / weekly"),
):
    engine = _ensure_engine()
    if not engine.is_ready() and kind in {"auto", "index"}:
        static_index = _resolve_static_index(symbol)
        if static_index is not None:
            return await _build_static_index_target(static_index[0], static_index[1], freq)
        status = engine.get_status()
        return JSONResponse(
            status_code=503,
            content={
                "error": "分析引擎尚未就绪",
                "session": _serialize_session(status),
            },
        )

    resolved = _resolve_target(symbol, kind, engine)
    if resolved["kind"] == "index":
        return await _build_index_target(engine, resolved["label"], freq)
    if resolved["kind"] == "industry":
        return await _build_industry_target(engine, resolved["label"], freq)
    if resolved["kind"] == "concept":
        return await _build_concept_target(engine, resolved["label"], freq)
    return await _build_stock_target(resolved["label"], resolved["raw_code"], freq)


@router.get("/backtest")
async def get_workbench_backtest(
    symbol: str = Query(..., description="股票代码或 Futu symbol"),
    freq: str = Query("daily", description="daily / weekly / monthly"),
    start_ts: Optional[int] = Query(None, description="选区开始秒级时间戳"),
    end_ts: Optional[int] = Query(None, description="选区结束秒级时间戳"),
):
    normalized, raw_code = _normalize_stock_symbol(symbol)
    if not normalized or not raw_code:
        raise HTTPException(status_code=404, detail=f"无法识别股票: {symbol}")

    payload = _unwrap_response(
        await _call_backtest_analyze(
            raw_code,
            freq if freq in {"daily", "weekly", "monthly"} else "daily",
            lookback=360,
        )
    )
    start, end = _timestamp_range_to_dates(start_ts, end_ts)
    filtered = _filter_backtest_payload(payload, start, end)
    filtered["target"] = {
        "symbol": normalized,
        "code": raw_code,
        "requested_freq": freq,
        "effective_freq": freq if freq in {"daily", "weekly", "monthly"} else "daily",
    }
    return filtered
