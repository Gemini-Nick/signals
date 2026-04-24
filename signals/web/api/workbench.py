from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from signals.core.stock_names import get_resolver
from signals.core.trade_log import get_trade_log
from signals.web2.api.backtest import (
    backtest_analyze as web2_backtest_analyze,
    backtest_run as web2_backtest_run,
)
from signals.web2.api.cluster import (
    get_history as web2_cluster_history,
    get_latest as web2_cluster_latest,
    get_watchlist as web2_cluster_watchlist,
)

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


def _unwrap_response(value: Any) -> Any:
    if isinstance(value, JSONResponse):
        return json.loads(value.body.decode("utf-8"))
    return value


def _ensure_engine():
    engine = get_engine()
    if not engine.is_ready() and not engine.state.is_running:
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

    if forced_kind == "stock":
        symbol, raw_code = _normalize_stock_symbol(value)
        if not symbol:
            raise HTTPException(status_code=404, detail=f"无法识别股票: {value}")
        return {"kind": "stock", "label": symbol, "raw_code": raw_code}

    if forced_kind == "industry":
        return {"kind": "industry", "label": value}

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
    market_context = serialize_market_context(engine.get_market_context()) if engine.get_market_context() else None
    reports = [
        serialize_index_report(report)
        for report in engine.get_index_reports()
        if getattr(report, "data_available", False)
    ]
    scored = [serialize_scored_symbol(item) for item in engine.get_scored_symbols()[:8]]
    cluster = _unwrap_response(web2_cluster_latest(top=4))

    industry_top = (cluster.get("industry") or {}).get("top") or []
    concept_top = (cluster.get("concept") or {}).get("top") or []

    watchlist_directions: List[str] = []
    for report in reports[:5]:
        watchlist_directions.append(report["name"])
    for item in industry_top[:6]:
        label = item.get("label")
        if label and label not in watchlist_directions:
            watchlist_directions.append(label)

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
        "cluster_summary": {
            "industry_top": industry_top,
            "concept_top": concept_top,
            "market_status": cluster.get("market_status") or {},
            "data_warning": cluster.get("data_warning", ""),
        },
        "watchlist_directions": watchlist_directions[:10],
        "default_target": {
            "kind": "index",
            "label": reports[0]["name"] if reports else "沪深300",
            "freq": "daily",
        },
        "legacy_url": "/legacy",
        "notices": notices,
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
    report_obj = next((item for item in engine.get_index_reports() if item.name == name), None)
    if report_obj is None:
        raise HTTPException(status_code=404, detail=f"未找到指数: {name}")

    chart = _unwrap_response(get_chart_data(name, freq=freq if freq in {"daily", "30min", "15min"} else "daily"))
    report = serialize_index_report(report_obj)
    plan = _plan_for_index(engine, name)
    analysis_target = _top_candidate_symbol(engine)

    return {
        "target": {
            "kind": "index",
            "label": name,
            "symbol": report.get("symbol", ""),
            "requested_freq": freq,
            "effective_freq": chart.get("meta", {}).get("freq", "daily"),
            "available_freqs": ["daily", "30min", "15min"],
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
    detail = _unwrap_response(get_industry_detail(name))
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

    return {
        "target": {
            "kind": "industry",
            "label": name,
            "symbol": name,
            "requested_freq": freq,
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


async def _build_stock_target(symbol: str, raw_code: str, freq: str) -> Dict[str, Any]:
    effective_freq = freq if freq in {"daily", "weekly", "monthly"} else "daily"
    chart = _unwrap_response(
        await _call_web2_backtest_run(raw_code, effective_freq, lookback=360)
    )
    stock = _unwrap_response(analyze_stock(symbol))
    engine = _ensure_engine()
    return {
        "target": {
            "kind": "stock",
            "label": stock.get("name") or symbol,
            "symbol": symbol,
            "requested_freq": freq,
            "effective_freq": effective_freq,
            "available_freqs": ["daily", "weekly", "monthly"],
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


async def _call_web2_backtest_run(code: str, freq: str, lookback: int = 360) -> Any:
    return await web2_backtest_run(
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


async def _call_web2_backtest_analyze(code: str, freq: str, lookback: int = 180) -> Any:
    return await web2_backtest_analyze(
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
    latest = _unwrap_response(web2_cluster_latest(top=top))
    history = _unwrap_response(web2_cluster_history())
    scan = None
    if direction.strip():
        scan = _unwrap_response(web2_cluster_watchlist(direction=direction.strip(), mode=mode, top=scan_top))
    return {
        "latest": latest,
        "history": history,
        "scan": scan,
    }


@router.get("/symbol/{symbol:path}")
async def get_workbench_symbol(
    symbol: str,
    kind: str = Query("auto", description="auto / index / industry / stock"),
    freq: str = Query("daily", description="daily / 30min / 15min / weekly"),
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
        await _call_web2_backtest_analyze(
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
