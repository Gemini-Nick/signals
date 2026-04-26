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
    ytd = config.DATE_PRESETS.get("ytd")
    if isinstance(ytd, dict):
        start = _preset_start_date(ytd, today)
        if start:
            columns.append({
                "key": "ytd",
                "label": "年初至今",
                "start_date": start.isoformat(),
                "aliases": ["ytd", "今年以来", "年初至今"],
            })

    absolute: list[tuple[date, str, dict[str, Any]]] = []
    for key, info in config.DATE_PRESETS.items():
        if key == "ytd" or not isinstance(info, dict) or "date" not in info:
            continue
        start = _preset_start_date(info, today)
        if start and start <= today:
            absolute.append((start, key, info))
    absolute.sort(key=lambda item: item[0], reverse=True)

    for start, key, info in absolute[:3]:
        mmdd = start.strftime("%m%d")
        label = f"{mmdd}至今"
        columns.append({
            "key": key,
            "label": label,
            "start_date": start.isoformat(),
            "aliases": [key, mmdd, start.isoformat(), str(info.get("label") or "")],
        })
    return columns[:4]


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
            if value.startswith("6"):
                return f"SH.{value}", value
            if value.startswith(("0", "3")):
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
    enriched.update({
        "kind": "stock",
        "label": normalized,
        "symbol": normalized,
        "code": normalized,
        "raw_code": raw_code or normalized.split(".")[-1],
        "name": _stock_name(normalized, row),
        "latest_price": latest_price,
        "range_returns": _compute_range_returns(df, range_columns),
        "range_return_source": source,
        "available_freqs": UI_FREQS,
    })
    return enriched


def _enrich_index_row(row: dict[str, Any], range_columns: list[dict[str, Any]]) -> dict[str, Any]:
    symbol = str(row.get("symbol") or row.get("code") or row.get("label") or row.get("name") or "").strip()
    df, source = _index_df(symbol, "daily") if symbol else (pd.DataFrame(), "")
    enriched = dict(row)
    enriched.update({
        "kind": "index",
        "label": row.get("name") or row.get("label") or symbol,
        "name": row.get("name") or row.get("label") or symbol,
        "code": symbol,
        "latest_price": row.get("latest_price") or (float(df["close"].iloc[-1]) if df is not None and not df.empty and "close" in df.columns else None),
        "range_returns": _compute_range_returns(df, range_columns),
        "range_return_source": source,
        "available_freqs": ["daily", "weekly", "30min", "15min"],
    })
    return enriched


def _enrich_cluster_row(row: dict[str, Any], kind: str) -> dict[str, Any]:
    enriched = dict(row)
    label = str(enriched.get("label") or enriched.get("name") or "").strip()
    enriched.update({
        "kind": kind,
        "label": label,
        "name": label,
        "code": str(enriched.get("code") or enriched.get("board_code") or ""),
        "latest_price": enriched.get("latest_price") or enriched.get("value"),
        "day_change_pct": enriched.get("change_pct") or enriched.get("gain_pct") or enriched.get("strength"),
        "range_returns": enriched.get("range_returns") or {},
    })
    return enriched


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
    market_context = serialize_market_context(engine.get_market_context()) if engine.get_market_context() else None
    reports_raw = [
        serialize_index_report(report)
        for report in engine.get_index_reports()
        if getattr(report, "data_available", False)
    ]
    reports = [_enrich_index_row(report, range_columns) for report in reports_raw]
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
    decision_queue = [
        dict(item)
        for item in strategy_snapshot.get("decision_queue", [])
        if isinstance(item, dict)
    ]
    cluster = _unwrap_response(cluster_service.get_latest(top=4))

    snapshot_cluster = _cluster_from_strategy_snapshot(strategy_snapshot)
    industry_top = snapshot_cluster.get("industry_top") or (cluster.get("industry") or {}).get("top") or []
    concept_top = snapshot_cluster.get("concept_top") or (cluster.get("concept") or {}).get("top") or []

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
        "watchlist": watchlist,
        "watchlist_range_columns": range_columns,
        "daily_brief": strategy_snapshot.get("daily_brief", {}),
        "decision_queue": decision_queue,
        "strategy_kpis": strategy_snapshot.get("strategy_kpis", {}),
        "source_confidence": strategy_snapshot.get("source_confidence", {}),
        "watchlist_directions": watchlist_directions[:10],
        "default_target": {
            "kind": "index",
            "label": reports[0]["name"] if reports else "沪深300",
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


def _summary_from_index(report: Dict[str, Any], chart: Dict[str, Any]) -> Dict[str, Any]:
    chart_report = chart.get("report") or {}
    ma_context = report.get("ma_context") or {}
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
        "style_switch": (engine := get_engine()).get_market_context().style_switch.suggestion
        if engine.get_market_context() and getattr(engine.get_market_context(), "style_switch", None)
        else "",
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
        raise HTTPException(status_code=404, detail=f"未找到指数: {name}")

    report = serialize_index_report(report_obj)
    if requested_freq in {"daily", "30min", "15min"}:
        chart = _unwrap_response(get_chart_data(name, freq=requested_freq))
    else:
        df, source = _index_df(str(report.get("symbol") or name), requested_freq)
        chart = _chart_from_df(df, symbol=str(report.get("symbol") or name), freq=requested_freq, source=source)
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

    try:
        detail = _unwrap_response(get_industry_detail(name))
    except HTTPException:
        if analysis_target:
            normalized, raw_code = _normalize_stock_symbol(analysis_target)
            if normalized and raw_code:
                payload = await _build_stock_target(normalized, raw_code, requested_freq)
                payload["target"] = {
                    **payload.get("target", {}),
                    "kind": "industry",
                    "label": name,
                    "symbol": name,
                    "requested_freq": requested_freq,
                }
                payload["summary"] = {
                    **payload.get("summary", {}),
                    "title": name,
                    "subtitle": f"行业K线缺失，显示代表股 {payload.get('summary', {}).get('title', normalized)}",
                    "conclusion": "行业板块 K 线暂不可用，先用代表股走势承接交易复核。",
                    "candidate_count": len(candidate_stocks),
                }
                payload["candidate_stocks"] = candidate_stocks
                payload["analysis_target"] = normalized
                return payload
        raise

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
        "candidate_stocks": candidate_stocks,
    }


async def _build_concept_target(engine, name: str, freq: str) -> Dict[str, Any]:
    requested_freq = _canonical_freq(freq)
    concept = next((item for item in engine.get_concepts() if getattr(item, "name", "") == name), None)
    related = list(getattr(concept, "related_industries", []) or [])
    if not related:
        try:
            from signals.layers.industry import _map_concept_to_industries

            related = _map_concept_to_industries(name)
        except Exception:
            related = []

    for industry_name in related[:3]:
        try:
            payload = await _build_industry_target(engine, industry_name, requested_freq)
        except Exception:
            continue
        payload["target"] = {
            **payload.get("target", {}),
            "kind": "concept",
            "label": name,
            "symbol": getattr(concept, "code", "") or name,
            "requested_freq": requested_freq,
        }
        payload["summary"] = {
            **payload.get("summary", {}),
            "title": name,
            "subtitle": f"概念映射行业 {industry_name}",
            "conclusion": f"概念板块暂用关联行业 {industry_name} 的走势承接复核。",
            "gain_pct": getattr(concept, "gain_pct", None),
            "composite_score": getattr(concept, "composite_score", None),
        }
        return payload

    leading_stock = str(getattr(concept, "leading_stock", "") or "")
    if leading_stock:
        normalized, raw_code = _normalize_stock_symbol(leading_stock)
        if normalized and raw_code:
            payload = await _build_stock_target(normalized, raw_code, requested_freq)
            payload["target"] = {
                **payload.get("target", {}),
                "kind": "concept",
                "label": name,
                "symbol": getattr(concept, "code", "") or name,
                "requested_freq": requested_freq,
            }
            payload["summary"] = {
                **payload.get("summary", {}),
                "title": name,
                "subtitle": f"概念K线缺失，显示领涨股 {leading_stock}",
                "conclusion": "概念板块 K 线暂不可用，先用领涨股走势承接交易复核。",
            }
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
        },
        "signals": [],
        "plan": None,
        "review": _review_context(engine, "concept", name),
        "trade": _trade_context(None),
        "analysis_target": "",
        "candidate_stocks": [],
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
            "effective_freq": requested_freq,
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
