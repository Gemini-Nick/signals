# -*- coding: utf-8 -*-
"""Canonical terminal read model for Signals backtest payloads."""
from __future__ import annotations

import math
import statistics
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from signals.core.market_time import timestamp_range_to_dates

TERMINAL_VERSION = "backtest-terminal.v1"


def build_backtest_terminal(
    payload: Mapping[str, Any],
    *,
    scan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the trader-terminal contract from the legacy backtest payload."""
    ohlcv = _as_list(payload.get("ohlcv"))
    signals = _as_list(payload.get("signals"))
    trades = _as_list(payload.get("sim_trades"))
    target = _build_target(payload, ohlcv)
    trade_assumptions = _build_trade_assumptions(payload, target)
    metrics = _build_metrics(payload, ohlcv, signals, trades)
    chart = _build_chart(payload, ohlcv, signals, trades, trade_assumptions)
    panels = _build_panels(payload, target, metrics, trades, signals, chart, trade_assumptions, scan)

    return {
        "version": TERMINAL_VERSION,
        "target": target,
        "market_snapshot": _build_market_snapshot(ohlcv),
        "trade_assumptions": trade_assumptions,
        "metrics": metrics,
        "chart": chart,
        "panels": panels,
    }


def build_scan_terminal(
    scan_result: Mapping[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal terminal contract for parameter scan-only responses."""
    ctx = context or {}
    rows = _as_list(scan_result.get("scan_results"))
    best = _as_record(scan_result.get("best_params"))
    return {
        "version": TERMINAL_VERSION,
        "target": {
            "symbol": _text(ctx.get("symbol")),
            "code": _text(ctx.get("code")),
            "name": _text(ctx.get("name") or ctx.get("code") or ctx.get("symbol")),
            "market": _text(ctx.get("market")),
            "freq": _text(ctx.get("freq")),
            "as_of": _text(ctx.get("as_of") or datetime.now().isoformat(timespec="seconds")),
            "bar_count": _num(ctx.get("bar_count"), 0),
            "data_source": _text(ctx.get("data_source")),
            "freshness": _text(ctx.get("freshness") or "unknown"),
        },
        "market_snapshot": _empty_market_snapshot(),
        "trade_assumptions": _build_trade_assumptions(ctx, _as_record(ctx.get("target"))),
        "metrics": {
            "scan_count": len(rows),
            "best_params": best,
        },
        "chart": {"date_presets": [], "ohlcv": [], "ma_lines": [], "macd": [], "signal_markers": [], "trade_markers": [], "risk_bands": []},
        "panels": {
            "perf": {"groups": [], "summary": []},
            "trades": {"rows": [], "filled_count": 0, "skipped_count": 0},
            "signals": {"rows": [], "count": 0, "groups": []},
            "scan": {"rows": rows, "best_params": best, "heatmap": scan_result.get("heatmap"), "metric": scan_result.get("metric")},
            "risk": {"summary": [], "skip_reasons": {}},
            "config": {"data_health": {}, "warnings": []},
        },
    }


def build_batch_terminal(
    batch_result: Mapping[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the terminal contract for multi-symbol backtest reviews."""
    ctx = context or {}
    stocks = [_as_record(item) for item in _as_list(batch_result.get("stocks"))]
    summary = _as_record(batch_result.get("summary"))
    ok_stocks = [row for row in stocks if _text(row.get("status") or "ok") == "ok"]
    freq = _text(ctx.get("freq_label") or ctx.get("freq"))
    market = _text(ctx.get("market") or _batch_market(stocks))
    benchmark = _batch_benchmark(ctx, market)
    target = {
        "symbol": _text(ctx.get("symbol") or "MULTI"),
        "code": _text(ctx.get("code") or "MULTI"),
        "name": _text(ctx.get("name") or "多标的回测复盘"),
        "market": market,
        "freq": freq,
        "as_of": _latest_text(stocks, "as_of") or _text(ctx.get("as_of") or datetime.now().date().isoformat()),
        "bar_count": int(_num(summary.get("bar_count"), sum(int(_num(row.get("bar_count"), 0) or 0) for row in stocks)) or 0),
        "data_source": _text(ctx.get("data_source") or "batch"),
        "freshness": _text(ctx.get("freshness") or _batch_freshness(stocks)),
        "data_source_detail": _text(ctx.get("data_source_detail") or f"{len(stocks)} symbols batch backtest"),
    }
    trade_assumptions = _build_trade_assumptions({"sim_config": ctx.get("sim_config") or ctx}, target)
    ranking_rows = [_batch_ranking_row(row, idx + 1, benchmark) for idx, row in enumerate(_rank_batch_stocks(stocks))]
    overview_rows = [_batch_interval_row(row) for row in _rank_batch_stocks(stocks)]
    chart_items = [_batch_chart_item(row) for row in _rank_batch_stocks(stocks)]
    script_cards = [_batch_script_card(row, idx + 1, benchmark) for idx, row in enumerate(_rank_batch_stocks(stocks))]
    signal_rows = _batch_signal_rows(stocks)
    metrics = _batch_metrics(summary, ranking_rows, ok_stocks)
    risk_rows = _batch_risk_rows(ranking_rows)

    return {
        "version": TERMINAL_VERSION,
        "mode": "multi",
        "target": target,
        "market_snapshot": _empty_market_snapshot(),
        "trade_assumptions": trade_assumptions,
        "metrics": metrics,
        "chart": {
            "date_presets": _as_list(ctx.get("date_presets")),
            "ohlcv": [],
            "ma_lines": [],
            "macd": [],
            "signal_markers": [],
            "trade_markers": [],
            "risk_bands": [],
            "multi_charts": chart_items,
        },
        "panels": {
            "perf": {
                "groups": [
                    {"title": "批量绩效", "items": metrics["performance"]},
                    {"title": "风险分布", "items": metrics["risk"]},
                    {"title": "交易质量", "items": metrics["trade_quality"]},
                ],
                "summary": ranking_rows,
            },
            "trades": {
                "rows": [],
                "filled_count": int(_num(summary.get("total_trades"), 0) or 0),
                "skipped_count": max(len(stocks) - len(ok_stocks), 0),
            },
            "signals": {
                "rows": signal_rows,
                "count": int(_num(summary.get("total_signals"), 0) or 0),
                "groups": [str(row.get("signal_type")) for row in signal_rows],
            },
            "scan": {"rows": ranking_rows, "best_params": {}, "heatmap": None},
            "risk": {"summary": risk_rows, "rows": risk_rows, "skip_reasons": _batch_skip_reasons(stocks)},
            "config": {
                "data_health": {
                    "symbol_count": len(stocks),
                    "ok_count": len(ok_stocks),
                    "freshness": target["freshness"],
                    "as_of": target["as_of"],
                    "data_source": target["data_source"],
                    "data_source_detail": target["data_source_detail"],
                },
                "warnings": _as_list(batch_result.get("warnings")),
            },
            "ranking": {
                "columns": [
                    "rank", "code", "name", "benchmark_symbol", "benchmark_phase",
                    "strength_grade", "range_return_pct", "max_drawdown_pct",
                    "up_bar_ratio_pct", "relative_excess_pct", "turning_point",
                    "current_character", "trade_difficulty", "review_level",
                    "review_conclusion",
                ],
                "rows": ranking_rows,
            },
            "interval_overview": {"rows": overview_rows},
            "multi_charts": {"items": chart_items},
            "scripts": {"cards": script_cards},
        },
    }


def _rank_batch_stocks(stocks: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(
        stocks,
        key=lambda row: (
            _text(row.get("status") or "ok") == "ok",
            _num(row.get("total_return") or row.get("range_return_pct"), -999) or -999,
            _num(row.get("sharpe"), -999) or -999,
        ),
        reverse=True,
    )


def _batch_market(stocks: list[Mapping[str, Any]]) -> str:
    markets = [_text(row.get("market")) for row in stocks if _text(row.get("market"))]
    if markets:
        return markets[0]
    symbols = [_text(row.get("symbol")) for row in stocks]
    if any(item.startswith("HK.") for item in symbols):
        return "HK"
    return "A" if stocks else ""


def _batch_benchmark(context: Mapping[str, Any], market: str) -> dict[str, Any]:
    symbol = _text(context.get("benchmark_symbol"))
    name = _text(context.get("benchmark_name"))
    if not symbol:
        symbol = "HSI.HK" if market == "HK" else "000852.SH"
    if not name:
        name = "恒生指数" if market == "HK" else "中证1000"
    return {
        "symbol": symbol,
        "name": name,
        "phase": _text(context.get("benchmark_phase") or "指数待校准"),
        "return_pct": _num(context.get("benchmark_return_pct"), 0) or 0,
    }


def _batch_freshness(stocks: list[Mapping[str, Any]]) -> str:
    values = {_text(row.get("freshness")) for row in stocks if _text(row.get("freshness"))}
    if not values:
        return "unknown"
    if values == {"fresh"}:
        return "fresh"
    if "degraded" in values:
        return "degraded"
    if "stale" in values:
        return "stale"
    return "mixed"


def _latest_text(rows: list[Mapping[str, Any]], key: str) -> str:
    values = sorted(_text(row.get(key)) for row in rows if _text(row.get(key)))
    return values[-1] if values else ""


def _batch_ranking_row(row: Mapping[str, Any], rank: int, benchmark: Mapping[str, Any]) -> dict[str, Any]:
    ohlcv = [_as_record(item) for item in _as_list(row.get("ohlcv_tail") or row.get("ohlcv"))]
    interval = _batch_interval_metrics(row, ohlcv)
    range_return = interval["range_return_pct"]
    max_drawdown = interval["max_drawdown_pct"]
    benchmark_return = _num(benchmark.get("return_pct"), 0) or 0
    relative_excess = _round((range_return or 0) - benchmark_return)
    grade = _strength_grade(range_return, relative_excess, max_drawdown, _num(row.get("sharpe"), 0))
    difficulty = _trade_difficulty(max_drawdown, interval["volatility_pct"], interval["median_5d_high_low_pct"])
    review_level = _review_level(grade, range_return, max_drawdown)
    conclusion = _review_conclusion(review_level, grade)
    turning_point = _turning_point(row)
    character = _current_character(range_return, max_drawdown)
    return {
        "rank": rank,
        "code": _text(row.get("code")),
        "symbol": _text(row.get("symbol")),
        "name": _text(row.get("name") or row.get("code")),
        "benchmark_symbol": _text(benchmark.get("symbol")),
        "benchmark_name": _text(benchmark.get("name")),
        "benchmark_phase": _text(benchmark.get("phase")),
        "strength_grade": grade,
        "range_return_pct": range_return,
        "max_drawdown_pct": max_drawdown,
        "max_runup_pct": interval["max_runup_pct"],
        "volatility_pct": interval["volatility_pct"],
        "median_5d_high_low_pct": interval["median_5d_high_low_pct"],
        "up_bar_ratio_pct": interval["up_bar_ratio_pct"],
        "relative_excess_pct": relative_excess,
        "turning_point": turning_point,
        "current_character": character,
        "trade_difficulty": difficulty,
        "review_level": review_level,
        "review_conclusion": conclusion,
        "signal_count": int(_num(row.get("signal_count"), 0) or 0),
        "trade_count": int(_num(row.get("trade_count"), 0) or 0),
        "win_rate": _round(row.get("win_rate")),
        "expectancy_pct": _round(row.get("expectancy")),
        "sharpe": _round(row.get("sharpe"), 2),
        "status": _text(row.get("status") or "ok"),
        "error": _text(row.get("error")),
    }


def _batch_interval_row(row: Mapping[str, Any]) -> dict[str, Any]:
    ohlcv = [_as_record(item) for item in _as_list(row.get("ohlcv_tail") or row.get("ohlcv"))]
    interval = _batch_interval_metrics(row, ohlcv)
    return {
        "code": _text(row.get("code")),
        "symbol": _text(row.get("symbol")),
        "name": _text(row.get("name") or row.get("code")),
        "start_date": _first_date(ohlcv),
        "end_date": _last_date(ohlcv),
        "bar_count": int(_num(row.get("bar_count"), len(ohlcv)) or 0),
        **interval,
    }


def _batch_chart_item(row: Mapping[str, Any]) -> dict[str, Any]:
    ohlcv = [_as_record(item) for item in _as_list(row.get("ohlcv_tail") or row.get("ohlcv"))]
    interval = _batch_interval_metrics(row, ohlcv)
    visible_ohlcv = ohlcv[-520:]
    signals = [_as_record(item) for item in _as_list(row.get("signals") or row.get("signal_rows"))]
    trades = [_as_record(item) for item in _as_list(row.get("sim_trades") or row.get("trades"))]
    return {
        "code": _text(row.get("code")),
        "symbol": _text(row.get("symbol")),
        "name": _text(row.get("name") or row.get("code")),
        "start_date": _first_date(ohlcv),
        "end_date": _last_date(ohlcv),
        "visible_start_date": _first_date(visible_ohlcv),
        "visible_end_date": _last_date(visible_ohlcv),
        "bar_count": int(_num(row.get("bar_count"), len(ohlcv)) or 0),
        "visible_bar_count": len(visible_ohlcv),
        "filled_trade_count": len([trade for trade in trades if _num(trade.get("entry_price")) is not None]),
        "ohlcv": visible_ohlcv,
        "regimes": _chart_regimes(visible_ohlcv),
        "signal_markers": _signal_markers(signals),
        "trade_markers": _trade_markers(trades, visible_ohlcv),
        "range_return_pct": interval["range_return_pct"],
        "max_drawdown_pct": interval["max_drawdown_pct"],
        "max_runup_pct": interval["max_runup_pct"],
        "volatility_pct": interval["volatility_pct"],
        "median_5d_high_low_pct": interval["median_5d_high_low_pct"],
        "up_bar_ratio_pct": interval["up_bar_ratio_pct"],
    }


def _batch_script_card(row: Mapping[str, Any], rank: int, benchmark: Mapping[str, Any]) -> dict[str, Any]:
    ranking = _batch_ranking_row(row, rank, benchmark)
    code = ranking["code"]
    name = ranking["name"]
    return {
        "code": code,
        "symbol": ranking["symbol"],
        "name": name,
        "rank": rank,
        "tone": "up" if (_num(ranking["range_return_pct"], 0) or 0) >= 0 else "down",
        "stats": [
            {"label": "区间表现", "value": ranking["range_return_pct"], "unit": "%"},
            {"label": "5日高低幅", "value": ranking["median_5d_high_low_pct"], "unit": "%"},
            {"label": "锐评档位", "value": ranking["review_level"]},
            {"label": "交易难度", "value": ranking["trade_difficulty"]},
            {"label": "收盘回撤", "value": ranking["max_drawdown_pct"], "unit": "%"},
        ],
        "positioning": f"第{rank}，{ranking['current_character']}，强弱等级{ranking['strength_grade']}。",
        "difficulty": f"{ranking['trade_difficulty']}。5日高低幅中位{_fmt_pct(ranking['median_5d_high_low_pct'])}，最大收盘回撤{_fmt_pct(ranking['max_drawdown_pct'])}。",
        "one_liner": ranking["review_conclusion"],
    }


def _batch_interval_metrics(row: Mapping[str, Any], ohlcv: list[Mapping[str, Any]]) -> dict[str, Any]:
    closes = [_num(item.get("close")) for item in ohlcv]
    closes = [item for item in closes if item is not None]
    opens = [_num(item.get("open")) for item in ohlcv]
    opens = [item for item in opens if item is not None]
    range_return = _num(row.get("range_return_pct") or row.get("total_return"))
    if range_return is None and len(closes) >= 2 and closes[0]:
        range_return = (closes[-1] - closes[0]) / closes[0] * 100
    max_drawdown = _num(row.get("max_drawdown"))
    if max_drawdown is None:
        max_drawdown = _series_max_drawdown_pct(closes)
    max_drawdown = -abs(max_drawdown or 0)
    max_runup = _series_max_runup_pct(closes)
    up_bar_ratio = _up_bar_ratio_pct(ohlcv)
    volatility = _annualized_volatility(ohlcv, row.get("freq"))
    median_5d_high_low = _rolling_high_low_median_pct(ohlcv)
    return {
        "range_return_pct": _round(range_return),
        "max_drawdown_pct": _round(max_drawdown),
        "max_runup_pct": _round(max_runup),
        "volatility_pct": _round(volatility),
        "median_5d_high_low_pct": _round(median_5d_high_low),
        "up_bar_ratio_pct": _round(up_bar_ratio),
    }


def _batch_signal_rows(stocks: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for stock in stocks:
        code = _text(stock.get("code"))
        name = _text(stock.get("name") or code)
        for item in _as_list(stock.get("signal_breakdown")):
            row = _as_record(item)
            signal_type = _text(row.get("signal_type") or row.get("signal_group") or "unknown")
            group = groups.setdefault(signal_type, {
                "signal_type": signal_type,
                "symbol_codes": set(),
                "signal_count": 0,
                "evaluated_count": 0,
                "win_count": 0,
                "trade_count": 0,
                "trade_win_count": 0,
                "t5_values": [],
                "t10_weighted": [],
                "t20_values": [],
                "mfe_values": [],
                "mae_values": [],
                "trade_return_weighted": [],
                "best_symbol": "",
                "best_return_pct": None,
            })
            group["symbol_codes"].add(code)
            signal_count = int(_num(row.get("signal_count"), 0) or 0)
            evaluated_count = int(_num(row.get("evaluated_count"), 0) or 0)
            trade_count = int(_num(row.get("trade_count"), 0) or 0)
            group["signal_count"] += signal_count
            group["evaluated_count"] += evaluated_count
            group["win_count"] += int(_num(row.get("win_count"), 0) or 0)
            group["trade_count"] += trade_count
            group["trade_win_count"] += int(_num(row.get("trade_win_count"), 0) or 0)
            for key, bucket in (
                ("avg_t5_pct", "t5_values"),
                ("avg_t20_pct", "t20_values"),
                ("avg_mfe_pct", "mfe_values"),
                ("avg_mae_pct", "mae_values"),
            ):
                value = _num(row.get(key))
                if value is not None:
                    group[bucket].append(value)
            avg_t10 = _num(row.get("avg_t10_pct"))
            if avg_t10 is not None and evaluated_count > 0:
                group["t10_weighted"].append((avg_t10, evaluated_count))
                best = _num(group["best_return_pct"])
                if best is None or avg_t10 > best:
                    group["best_return_pct"] = avg_t10
                    group["best_symbol"] = " ".join(item for item in [code, name] if item)
            avg_trade = _num(row.get("avg_trade_return_pct"))
            if avg_trade is not None and trade_count > 0:
                group["trade_return_weighted"].append((avg_trade, trade_count))

    rows: list[dict[str, Any]] = []
    for group in groups.values():
        evaluated_count = int(group["evaluated_count"])
        trade_count = int(group["trade_count"])
        rows.append({
            "signal_type": group["signal_type"],
            "symbol_count": len(group["symbol_codes"]),
            "signal_count": int(group["signal_count"]),
            "evaluated_count": evaluated_count,
            "trade_count": trade_count,
            "win_rate": _round(int(group["win_count"]) / evaluated_count * 100) if evaluated_count else 0,
            "trade_win_rate": _round(int(group["trade_win_count"]) / trade_count * 100) if trade_count else 0,
            "avg_t5_pct": _round(_average(group["t5_values"])),
            "avg_t10_pct": _round(_weighted_average(group["t10_weighted"])),
            "avg_t20_pct": _round(_average(group["t20_values"])),
            "avg_mfe_pct": _round(_average(group["mfe_values"])),
            "avg_mae_pct": _round(_average(group["mae_values"])),
            "avg_trade_return_pct": _round(_weighted_average(group["trade_return_weighted"])),
            "best_symbol": group["best_symbol"],
            "best_return_pct": _round(group["best_return_pct"]),
        })
    return sorted(rows, key=lambda row: (_num(row.get("signal_count"), 0) or 0, _num(row.get("avg_t10_pct"), -999) or -999), reverse=True)


def _batch_metrics(
    summary: Mapping[str, Any],
    ranking_rows: list[Mapping[str, Any]],
    ok_stocks: list[Mapping[str, Any]],
) -> dict[str, Any]:
    returns = [_num(row.get("range_return_pct")) for row in ranking_rows]
    returns = [item for item in returns if item is not None]
    drawdowns = [_num(row.get("max_drawdown_pct")) for row in ranking_rows]
    drawdowns = [item for item in drawdowns if item is not None]
    avg_return = _average(returns)
    avg_drawdown = _average(drawdowns)
    total_trades = int(_num(summary.get("total_trades"), 0) or 0)
    total_signals = int(_num(summary.get("total_signals"), 0) or 0)
    win_rate = _num(summary.get("overall_win_rate"), 0) or 0
    expectancy = _num(summary.get("overall_expectancy"), 0) or 0
    flat = {
        "total_return_pct": _round(avg_return),
        "benchmark_return_pct": 0,
        "excess_return_pct": _round(avg_return),
        "annual_return_pct": _round(avg_return),
        "max_drawdown_pct": _round(abs(avg_drawdown)),
        "volatility_pct": _round(_average([_num(row.get("volatility_pct")) for row in ranking_rows])),
        "median_5d_high_low_pct": _round(_median([_num(row.get("median_5d_high_low_pct")) for row in ranking_rows])),
        "sharpe": _round(_average([_num(row.get("sharpe")) for row in ranking_rows]), 2),
        "calmar": _calmar(avg_return, avg_drawdown),
        "filled_trades": total_trades,
        "win_rate": _round(win_rate),
        "profit_factor": 0,
        "expectancy_pct": _round(expectancy),
        "avg_win_pct": 0,
        "avg_loss_pct": 0,
        "avg_holding_days": _round(_average([_num(row.get("avg_hold_days")) for row in ok_stocks]), 1),
        "max_consecutive_losses": 0,
        "exposure_pct": 0,
        "signal_count": total_signals,
        "evaluated_count": total_trades,
        "avg_t5_pct": 0,
        "avg_t10_pct": _round(expectancy),
        "avg_mfe_pct": _round(_average([_num(row.get("max_runup_pct")) for row in ranking_rows])),
        "avg_mae_pct": _round(avg_drawdown),
    }
    flat["performance"] = _metric_group([
        ("symbol_count", "标的数", len(ranking_rows), "", None),
        ("ok_count", "成功标的", int(_num(summary.get("ok_stocks"), len(ok_stocks)) or 0), "", None),
        ("total_return_pct", "平均区间", flat["total_return_pct"], "%", 0),
        ("excess_return_pct", "平均超额", flat["excess_return_pct"], "%", 0),
    ])
    flat["risk"] = _metric_group([
        ("max_drawdown_pct", "平均回撤", flat["max_drawdown_pct"], "%", None, "down"),
        ("volatility_pct", "平均波动", flat["volatility_pct"], "%", None),
        ("sharpe", "平均Sharpe", flat["sharpe"], "", 1),
        ("calmar", "平均Calmar", flat["calmar"], "", 1),
    ])
    flat["trade_quality"] = _metric_group([
        ("signal_count", "总信号", total_signals, "", None),
        ("filled_trades", "总成交", total_trades, "", None),
        ("win_rate", "整体胜率", flat["win_rate"], "%", 50),
        ("expectancy_pct", "整体期望", flat["expectancy_pct"], "%", 0),
    ])
    flat["execution"] = _metric_group([])
    flat["signal_quality"] = _metric_group([])
    return flat


def _batch_risk_rows(ranking_rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "code": row.get("code"),
            "name": row.get("name"),
            "risk": row.get("trade_difficulty"),
            "max_drawdown_pct": row.get("max_drawdown_pct"),
            "volatility_pct": row.get("volatility_pct"),
            "action": "小仓观察" if row.get("strength_grade") in {"A", "B"} else "等待修复",
        }
        for row in ranking_rows
    ]


def _batch_skip_reasons(stocks: list[Mapping[str, Any]]) -> dict[str, int]:
    reasons: dict[str, int] = {}
    for row in stocks:
        reason = _text(row.get("error"))
        if not reason:
            continue
        reasons[reason] = reasons.get(reason, 0) + 1
    return reasons


def _series_max_drawdown_pct(closes: list[float]) -> float:
    if not closes:
        return 0
    peak = closes[0]
    max_drawdown = 0.0
    for close in closes:
        peak = max(peak, close)
        if peak:
            max_drawdown = min(max_drawdown, (close - peak) / peak * 100)
    return max_drawdown


def _series_max_runup_pct(closes: list[float]) -> float:
    if not closes:
        return 0
    trough = closes[0]
    runup = 0.0
    for close in closes:
        trough = min(trough, close)
        if trough:
            runup = max(runup, (close - trough) / trough * 100)
    return runup


def _up_bar_ratio_pct(ohlcv: list[Mapping[str, Any]]) -> float:
    if not ohlcv:
        return 0
    up_count = 0
    valid_count = 0
    for row in ohlcv:
        open_price = _num(row.get("open"))
        close = _num(row.get("close"))
        if open_price is None or close is None:
            continue
        valid_count += 1
        if close >= open_price:
            up_count += 1
    return up_count / valid_count * 100 if valid_count else 0


def _rolling_high_low_median_pct(ohlcv: list[Mapping[str, Any]], window: int = 5) -> float:
    rows = [row for row in ohlcv if _num(row.get("high")) is not None and _num(row.get("low")) is not None]
    if not rows:
        return 0
    window = max(1, window)
    segments = [rows] if len(rows) < window else [rows[index:index + window] for index in range(0, len(rows) - window + 1)]
    values: list[float] = []
    for segment in segments:
        highs = [_num(row.get("high")) for row in segment]
        lows = [_num(row.get("low")) for row in segment]
        highs = [value for value in highs if value is not None]
        lows = [value for value in lows if value is not None]
        if not highs or not lows:
            continue
        high = max(highs)
        low = min(lows)
        if high > 0:
            values.append((high - low) / high * 100)
    return _median(values)


def _chart_regimes(ohlcv: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not ohlcv:
        return []
    window = max(8, len(ohlcv) // 5)
    regimes = []
    for start in range(0, len(ohlcv), window):
        segment = ohlcv[start:start + window]
        closes = [_num(row.get("close")) for row in segment]
        closes = [item for item in closes if item is not None]
        if len(closes) < 2 or not closes[0]:
            continue
        ret = (closes[-1] - closes[0]) / closes[0] * 100
        label = "上涨" if ret >= 3 else "下跌" if ret <= -3 else "震荡"
        regimes.append({
            "start_index": start,
            "end_index": min(start + len(segment) - 1, len(ohlcv) - 1),
            "label": label,
            "tone": "up" if ret >= 0 else "down",
        })
    return regimes


def _strength_grade(ret: float | None, excess: float | None, drawdown: float | None, sharpe: float | None) -> str:
    value = ret or 0
    rel = excess or 0
    dd = abs(drawdown or 0)
    sr = sharpe or 0
    if value >= 25 and rel >= 10 and dd <= 35 and sr >= 0.8:
        return "A"
    if value >= 8 and rel >= 0 and dd <= 30:
        return "B"
    if value >= 0:
        return "C"
    return "D"


def _trade_difficulty(drawdown: float | None, volatility: float | None, high_low_median: float | None = None) -> str:
    dd = abs(drawdown or 0)
    vol = volatility or 0
    swing = high_low_median or 0
    if dd >= 28 or vol >= 55 or swing >= 10:
        return "极高"
    if dd >= 15 or vol >= 35 or swing >= 6:
        return "高"
    return "中"


def _review_level(grade: str, ret: float | None, drawdown: float | None) -> str:
    value = ret or 0
    dd = abs(drawdown or 0)
    if grade == "A" and value >= 20:
        return "人上人"
    if grade in {"B", "C"} and value >= 0:
        return "路边"
    if dd >= 35 or value < -15:
        return "拉完了"
    return "观察"


def _review_conclusion(level: str, grade: str) -> str:
    if level == "人上人":
        return "人上人，不一定最疯，但资金认可度在线。"
    if level == "路边":
        return "路边，看着有动作，实际地位一般。"
    if level == "拉完了":
        return "拉完了，反弹归反弹，别硬讲主线。"
    if grade == "A":
        return "强度在线，但还要看回撤能不能收住。"
    return "先观察，等强弱和回撤给出更干净的信号。"


def _turning_point(row: Mapping[str, Any]) -> str:
    signal_count = int(_num(row.get("signal_count"), 0) or 0)
    if signal_count >= 10:
        return "放量长上影/高开低走"
    if signal_count > 0:
        return "20日线观察"
    return "无明确信号"


def _current_character(ret: float | None, drawdown: float | None) -> str:
    value = ret or 0
    dd = abs(drawdown or 0)
    if value >= 20:
        return "弹性冲浪"
    if value >= 0:
        return "震荡修复"
    if dd >= 25:
        return "弱势反抽"
    return "回撤整理"


def _fmt_pct(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "N/A"
    return f"{number:.2f}%"


def _build_target(payload: Mapping[str, Any], ohlcv: list[Mapping[str, Any]]) -> dict[str, Any]:
    existing = _as_record(payload.get("target"))
    symbol = _text(existing.get("symbol") or payload.get("symbol"))
    code = _text(existing.get("code") or payload.get("code") or _code_from_symbol(symbol))
    market = _text(existing.get("market") or _market_from_symbol(symbol, code))
    return {
        "symbol": symbol,
        "code": code,
        "name": _text(existing.get("name") or payload.get("name") or code or symbol),
        "market": market,
        "freq": _text(existing.get("effective_freq") or existing.get("freq") or payload.get("freq")),
        "as_of": _text(existing.get("as_of") or payload.get("as_of") or _last_date(ohlcv) or payload.get("generated_at")),
        "bar_count": int(_num(existing.get("bar_count") or payload.get("bar_count"), len(ohlcv)) or 0),
        "data_source": _text(existing.get("data_source") or payload.get("data_source")),
        "freshness": _text(existing.get("freshness") or payload.get("freshness") or _freshness(payload)),
        "data_source_detail": _text(existing.get("data_source_detail") or payload.get("data_source_detail")),
        "requested_freq": existing.get("requested_freq"),
        "effective_freq": existing.get("effective_freq"),
    }


def _build_market_snapshot(ohlcv: list[Mapping[str, Any]]) -> dict[str, Any]:
    if not ohlcv:
        return _empty_market_snapshot()
    last = _as_record(ohlcv[-1])
    previous = _as_record(ohlcv[-2]) if len(ohlcv) >= 2 else {}
    close = _num(last.get("close"))
    prev_close = _num(last.get("prev_close") or previous.get("close") or close)
    open_price = _num(last.get("open"))
    high = _num(last.get("high"))
    low = _num(last.get("low"))
    change_pct = _pct_change(close, prev_close)
    amplitude_pct = _safe_div((high or 0) - (low or 0), prev_close) * 100 if prev_close else None
    volume = _num(last.get("volume") or last.get("vol"))
    amount = _num(last.get("amount"))
    if amount is None and close is not None and volume is not None:
        amount = close * volume
    return {
        "last": close,
        "prev_close": prev_close,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "change_pct": _round(change_pct),
        "volume": volume,
        "amount": _round(amount, 2),
        "turnover_rate": _num(last.get("turnover_rate")),
        "amplitude_pct": _round(amplitude_pct),
    }


def _empty_market_snapshot() -> dict[str, Any]:
    return {
        "last": None,
        "prev_close": None,
        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "change_pct": None,
        "volume": None,
        "amount": None,
        "turnover_rate": None,
        "amplitude_pct": None,
    }


def _build_trade_assumptions(payload: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    config = _as_record(payload.get("sim_config"))
    market = _text(target.get("market"))
    return {
        "initial_capital": _first_num(config, ["initial_capital"], 100000),
        "position_size": _first_num(config, ["position_size", "position_size_pct"], 1),
        "commission_pct": _first_num(config, ["commission_pct", "commission"], 0.025),
        "stamp_tax_pct": _first_num(config, ["stamp_tax_pct", "tax_pct", "tax"], 0.05),
        "slippage_pct": _first_num(config, ["slippage_pct", "slippage"], 0.1),
        "lot_size": int(_first_num(config, ["lot_size"], 1 if market == "HK" else 100) or 0),
        "max_hold_days": int(_first_num(config, ["max_hold_days", "max_hold"], 20) or 0),
        "stop_loss_pct": _first_num(config, ["stop_loss_pct"], None),
        "trail_stop_pct": _first_num(config, ["trail_stop_pct"], None),
        "take_profit_pct": _first_num(config, ["take_profit_pct"], 0),
    }


def _build_metrics(
    payload: Mapping[str, Any],
    ohlcv: list[Mapping[str, Any]],
    signals: list[Mapping[str, Any]],
    trades: list[Mapping[str, Any]],
) -> dict[str, Any]:
    kpi = _as_record(payload.get("kpi") or payload.get("forward_kpi"))
    sim = _as_record(payload.get("sim_kpi"))
    filled = [t for t in trades if _num(t.get("entry_price")) is not None]
    returns = [_num(t.get("net_return_pct")) for t in filled]
    returns = [r for r in returns if r is not None]
    losses = [r for r in returns if r < 0]
    wins = [r for r in returns if r > 0]
    total_return_pct = _first_num(sim, ["total_return_pct"], 0) or 0
    benchmark_return_pct = _benchmark_return(ohlcv)
    max_drawdown_pct = _first_num(sim, ["max_drawdown_pct"], 0) or 0
    annual_return_pct = _first_num(sim, ["annual_return_pct"], None)
    if annual_return_pct is None:
        annual_return_pct = _annualized_return(total_return_pct, len(ohlcv), payload.get("freq"))
    volatility_pct = _first_num(sim, ["volatility_pct"], None)
    if volatility_pct is None:
        volatility_pct = _annualized_volatility(ohlcv, payload.get("freq"))
    avg_holding_days = _first_num(sim, ["avg_holding_days", "avg_hold_days"], _average([_num(t.get("holding_days")) for t in filled]))
    signal_count = len(signals)
    evaluated_count = _evaluated_count(signals)
    if signal_count == 0 and "signals" not in payload:
        signal_count = int(_first_num(kpi, ["signal_count", "total"], 0) or 0)
        evaluated_count = int(_first_num(kpi, ["evaluated_count", "evaluated"], 0) or 0)
    avg_t5_pct = _first_num(kpi, ["avg_t5_pct", "avg_return_t5", "return_t5_avg"], _signal_eval_average(signals, "return_t5"))
    avg_t10_pct = _first_num(kpi, ["avg_t10_pct", "avg_return_t10", "return_t10_avg", "expectancy"], _signal_eval_average(signals, "return_t10"))
    avg_mfe_pct = _first_num(kpi, ["avg_mfe_pct", "avg_mfe"], _first_num(sim, ["avg_mfe"], _trade_average(filled, "mfe_pct")))
    avg_mae_pct = _first_num(kpi, ["avg_mae_pct", "avg_mae"], _first_num(sim, ["avg_mae"], _trade_average(filled, "mae_pct")))

    flat = {
        "total_return_pct": _round(total_return_pct),
        "benchmark_return_pct": _round(benchmark_return_pct),
        "excess_return_pct": _round(total_return_pct - benchmark_return_pct),
        "annual_return_pct": _round(annual_return_pct),
        "max_drawdown_pct": _round(max_drawdown_pct),
        "volatility_pct": _round(volatility_pct),
        "sharpe": _first_num(sim, ["sharpe"], 0),
        "calmar": _calmar(annual_return_pct, max_drawdown_pct),
        "filled_trades": int(_first_num(sim, ["filled_trades", "total_trades"], len(filled)) or 0),
        "win_rate": _first_num(sim, ["win_rate"], kpi.get("win_rate", 0)),
        "profit_factor": _first_num(sim, ["profit_factor"], 0),
        "expectancy_pct": _first_num(sim, ["expectancy", "expectancy_pct", "avg_return"], kpi.get("expectancy", 0)),
        "avg_win_pct": _first_num(sim, ["avg_win", "avg_win_pct"], _average(wins)),
        "avg_loss_pct": _first_num(sim, ["avg_loss", "avg_loss_pct"], _average(losses)),
        "avg_holding_days": _round(avg_holding_days, 1),
        "max_consecutive_losses": _max_consecutive_losses(filled),
        "exposure_pct": _round(_exposure_pct(filled, len(ohlcv))),
        "signal_count": signal_count,
        "evaluated_count": evaluated_count,
        "avg_t5_pct": _round(avg_t5_pct),
        "avg_t10_pct": _round(avg_t10_pct),
        "avg_mfe_pct": _round(avg_mfe_pct),
        "avg_mae_pct": _round(avg_mae_pct),
    }
    flat["performance"] = _metric_group([
        ("total_return_pct", "总收益", flat["total_return_pct"], "%", 0),
        ("benchmark_return_pct", "基准收益", flat["benchmark_return_pct"], "%", 0),
        ("excess_return_pct", "超额收益", flat["excess_return_pct"], "%", 0),
        ("annual_return_pct", "年化收益", flat["annual_return_pct"], "%", 0),
    ])
    flat["risk"] = _metric_group([
        ("max_drawdown_pct", "最大回撤", flat["max_drawdown_pct"], "%", None, "down"),
        ("volatility_pct", "波动率", flat["volatility_pct"], "%", None),
        ("sharpe", "Sharpe", flat["sharpe"], "", 1),
        ("calmar", "Calmar", flat["calmar"], "", 1),
    ])
    flat["trade_quality"] = _metric_group([
        ("filled_trades", "成交", flat["filled_trades"], "", None),
        ("win_rate", "胜率", flat["win_rate"], "%", 50),
        ("profit_factor", "盈亏比", flat["profit_factor"], "", 1),
        ("expectancy_pct", "期望", flat["expectancy_pct"], "%", 0),
        ("avg_win_pct", "平均盈利", flat["avg_win_pct"], "%", 0),
        ("avg_loss_pct", "平均亏损", flat["avg_loss_pct"], "%", None, "down"),
    ])
    flat["execution"] = _metric_group([
        ("avg_holding_days", "平均持仓", flat["avg_holding_days"], "D", None),
        ("max_consecutive_losses", "连续亏损", flat["max_consecutive_losses"], "", None, "down"),
        ("exposure_pct", "资金暴露", flat["exposure_pct"], "%", None),
    ])
    flat["signal_quality"] = _metric_group([
        ("signal_count", "信号", flat["signal_count"], "", None),
        ("evaluated_count", "已评估", flat["evaluated_count"], "", None),
        ("avg_t5_pct", "T+5", flat["avg_t5_pct"], "%", 0),
        ("avg_t10_pct", "T+10", flat["avg_t10_pct"], "%", 0),
        ("avg_mfe_pct", "MFE", flat["avg_mfe_pct"], "%", 0),
        ("avg_mae_pct", "MAE", flat["avg_mae_pct"], "%", None, "down"),
    ])
    return flat


def _build_chart(
    payload: Mapping[str, Any],
    ohlcv: list[Mapping[str, Any]],
    signals: list[Mapping[str, Any]],
    trades: list[Mapping[str, Any]],
    assumptions: Mapping[str, Any],
) -> dict[str, Any]:
    latest_close = _num(ohlcv[-1].get("close")) if ohlcv else None
    return {
        "ohlcv": ohlcv,
        "ma_lines": _as_list(payload.get("ma_lines")),
        "macd": _as_list(payload.get("macd")),
        "signal_markers": _signal_markers(signals),
        "entry_exit_markers": _trade_markers(trades, ohlcv),
        "trade_markers": _trade_markers(trades, ohlcv),
        "risk_bands": _risk_bands(latest_close, assumptions),
        "date_presets": _as_list(payload.get("date_presets")),
        "bi_list": _as_list(payload.get("bi_list")),
        "zhongshu": _as_list(payload.get("zhongshu")),
    }


def _build_panels(
    payload: Mapping[str, Any],
    target: Mapping[str, Any],
    metrics: Mapping[str, Any],
    trades: list[Mapping[str, Any]],
    signals: list[Mapping[str, Any]],
    chart: Mapping[str, Any],
    trade_assumptions: Mapping[str, Any],
    scan: Mapping[str, Any] | None,
) -> dict[str, Any]:
    filled = [t for t in trades if _num(t.get("entry_price")) is not None]
    skipped = [t for t in trades if _text(t.get("skip_reason"))]
    return {
        "perf": {
            "summary": [
                metrics.get("total_return_pct"),
                metrics.get("max_drawdown_pct"),
                metrics.get("win_rate"),
                metrics.get("filled_trades"),
                metrics.get("sharpe"),
            ],
            "groups": {
                "performance": metrics.get("performance", []),
                "risk": metrics.get("risk", []),
                "trade_quality": metrics.get("trade_quality", []),
                "execution": metrics.get("execution", []),
                "signal_quality": metrics.get("signal_quality", []),
            },
        },
        "trades": {
            "rows": [_trade_row(t, idx) for idx, t in enumerate(trades)],
            "filled_count": len(filled),
            "skipped_count": len(skipped),
        },
        "signals": {
            "rows": [_signal_row(s, idx) for idx, s in enumerate(signals)],
            "count": len(signals),
            "groups": sorted({str(s.get("group") or "unknown") for s in signals}),
        },
        "scan": _scan_panel(scan or payload.get("scan")),
        "risk": {
            "summary": metrics.get("risk", []),
            "bands": chart.get("risk_bands", []),
            "skip_reasons": _as_record(payload.get("sim_skip_reasons")),
            "assumptions": trade_assumptions,
            "warnings": _as_list(payload.get("warnings")),
        },
        "config": {
            "target": dict(target),
            "simulation": _as_record(payload.get("sim_config")),
            "trade_assumptions": dict(trade_assumptions),
            "data_health": {
                "data_source": target.get("data_source"),
                "data_source_detail": target.get("data_source_detail"),
                "freshness": target.get("freshness"),
                "as_of": target.get("as_of"),
                "bar_count": target.get("bar_count"),
                "partial": payload.get("partial"),
                "derived_from": payload.get("derived_from"),
                "last_upstream_error": payload.get("last_upstream_error"),
            },
            "warnings": _as_list(payload.get("warnings")),
        },
    }


def _signal_markers(signals: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    markers = []
    for idx, signal in enumerate(signals):
        label = _text(signal.get("type") or signal.get("group") or "SIG")
        side = "sell" if _is_sell(label) else "buy"
        markers.append({
            "id": f"signal-{idx}",
            "kind": "signal",
            "source_index": idx,
            "time": _time(signal.get("dt")),
            "date": _text(signal.get("date_str") or signal.get("signal_date") or signal.get("dt_str")),
            "label": label,
            "side": side,
            "price": _num(signal.get("price")),
            "type": signal.get("type"),
            "group": signal.get("group"),
            "confidence": _num(signal.get("confidence")),
            "eval": _as_record(signal.get("eval")),
        })
    return markers


def _trade_markers(trades: list[Mapping[str, Any]], ohlcv: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    time_by_date = _bar_time_by_date(ohlcv)
    markers: list[dict[str, Any]] = []
    for idx, trade in enumerate(trades):
        if _num(trade.get("entry_price")) is None:
            continue
        entry_date = _text(trade.get("entry_date"))
        exit_date = _text(trade.get("exit_date"))
        markers.append({
            "id": f"trade-{idx}-entry",
            "kind": "entry",
            "trade_index": idx,
            "time": time_by_date.get(entry_date),
            "date": entry_date,
            "side": "buy",
            "price": _num(trade.get("entry_price")),
            "label": "ENTRY",
            "signal_type": trade.get("signal_type"),
        })
        markers.append({
            "id": f"trade-{idx}-exit",
            "kind": "exit",
            "trade_index": idx,
            "time": time_by_date.get(exit_date),
            "date": exit_date,
            "side": "sell",
            "price": _num(trade.get("exit_price")),
            "label": _text(trade.get("exit_reason") or "EXIT"),
            "return_pct": _num(trade.get("net_return_pct")),
            "exit_reason": trade.get("exit_reason"),
        })
    return [item for item in markers if item.get("time") is not None and item.get("price") is not None]


def _risk_bands(latest_close: float | None, assumptions: Mapping[str, Any]) -> list[dict[str, Any]]:
    bands = []
    stop_loss = _num(assumptions.get("stop_loss_pct"))
    take_profit = _num(assumptions.get("take_profit_pct"))
    trail_stop = _num(assumptions.get("trail_stop_pct"))
    if stop_loss is not None:
        bands.append({
            "key": "stop_loss",
            "label": "止损",
            "pct": -abs(stop_loss),
            "price": _round(latest_close * (1 - abs(stop_loss) / 100), 4) if latest_close is not None else None,
            "enabled": stop_loss > 0,
        })
    if take_profit is not None:
        bands.append({
            "key": "take_profit",
            "label": "固定止盈",
            "pct": take_profit,
            "price": _round(latest_close * (1 + take_profit / 100), 4) if latest_close is not None else None,
            "enabled": take_profit > 0,
        })
    if trail_stop is not None:
        bands.append({
            "key": "trail_stop",
            "label": "移动止盈回撤",
            "pct": trail_stop,
            "price": None,
            "enabled": trail_stop > 0,
        })
    return bands


def _trade_row(trade: Mapping[str, Any], idx: int) -> dict[str, Any]:
    filled = _num(trade.get("entry_price")) is not None
    return {
        "id": f"trade-{idx}",
        "index": idx,
        "status": "filled" if filled else "skipped",
        "signal_date": trade.get("signal_date"),
        "signal_type": trade.get("signal_type"),
        "signal_group": trade.get("signal_group"),
        "entry_date": trade.get("entry_date"),
        "entry_price": _num(trade.get("entry_price")),
        "exit_date": trade.get("exit_date"),
        "exit_price": _num(trade.get("exit_price")),
        "exit_reason": trade.get("exit_reason"),
        "fill_type": trade.get("fill_type"),
        "holding_days": _num(trade.get("holding_days")),
        "return_pct": _num(trade.get("return_pct")),
        "net_return_pct": _num(trade.get("net_return_pct")),
        "cost_pct": _num(trade.get("cost_pct")),
        "mfe_pct": _num(trade.get("mfe_pct")),
        "mae_pct": _num(trade.get("mae_pct")),
        "skip_reason": trade.get("skip_reason"),
    }


def _signal_row(signal: Mapping[str, Any], idx: int) -> dict[str, Any]:
    ev = _as_record(signal.get("eval"))
    return {
        "id": f"signal-{idx}",
        "index": idx,
        "date": signal.get("date_str") or signal.get("signal_date") or signal.get("dt_str"),
        "time": _time(signal.get("dt")),
        "type": signal.get("type"),
        "group": signal.get("group"),
        "price": _num(signal.get("price")),
        "confidence": _num(signal.get("confidence")),
        "ma_status": signal.get("ma_status"),
        "volume_status": signal.get("volume_status"),
        "return_t5": _num(ev.get("return_t5")),
        "return_t10": _num(ev.get("return_t10")),
        "return_t20": _num(ev.get("return_t20")),
        "mfe_pct": _num(ev.get("mfe")),
        "mae_pct": _num(ev.get("mae")),
        "raw": dict(signal),
    }


def _scan_panel(scan: Any) -> dict[str, Any]:
    data = _as_record(scan)
    return {
        "rows": _as_list(data.get("scan_results")),
        "best_params": _as_record(data.get("best_params")),
        "heatmap": data.get("heatmap"),
        "metric": data.get("metric"),
        "error": data.get("error"),
    }


def _metric_group(items: list[tuple[str, str, Any, str, float | None] | tuple[str, str, Any, str, float | None, str]]) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        key, label, value, unit, threshold, *forced = item
        tone = forced[0] if forced else _tone(value, threshold)
        rows.append({"key": key, "label": label, "value": value, "unit": unit, "tone": tone})
    return rows


def _tone(value: Any, threshold: float | None) -> str:
    number = _num(value)
    if number is None or threshold is None:
        return "neutral"
    return "up" if number >= threshold else "down"


def _benchmark_return(ohlcv: list[Mapping[str, Any]]) -> float:
    if len(ohlcv) < 2:
        return 0
    first = _num(ohlcv[0].get("close"))
    last = _num(ohlcv[-1].get("close"))
    change = _pct_change(last, first)
    return change or 0


def _annualized_return(total_return_pct: float, bar_count: int, freq: Any) -> float:
    if bar_count <= 0 or total_return_pct <= -100:
        return 0
    annual_periods = _annual_periods(freq)
    try:
        return ((1 + total_return_pct / 100) ** (annual_periods / max(bar_count, 1)) - 1) * 100
    except Exception:
        return 0


def _annualized_volatility(ohlcv: list[Mapping[str, Any]], freq: Any) -> float:
    closes = [_num(row.get("close")) for row in ohlcv]
    closes = [item for item in closes if item is not None and item > 0]
    if len(closes) < 3:
        return 0
    returns = []
    for idx in range(1, len(closes)):
        returns.append((closes[idx] - closes[idx - 1]) / closes[idx - 1])
    if len(returns) < 2:
        return 0
    return statistics.stdev(returns) * math.sqrt(_annual_periods(freq)) * 100


def _annual_periods(freq: Any) -> int:
    text = _text(freq).lower()
    if "周" in text or "week" in text:
        return 52
    if "月" in text or "month" in text:
        return 12
    if "30" in text or "min" in text or "分钟" in text:
        return 252 * 8
    return 252


def _calmar(annual_return_pct: float | None, max_drawdown_pct: float | None) -> float:
    annual = _num(annual_return_pct) or 0
    drawdown = abs(_num(max_drawdown_pct) or 0)
    if drawdown <= 0:
        return 0
    return _round(annual / drawdown, 2) or 0


def _max_consecutive_losses(trades: list[Mapping[str, Any]]) -> int:
    max_losses = 0
    current = 0
    sorted_trades = sorted(trades, key=lambda t: _text(t.get("exit_date") or t.get("entry_date")))
    for trade in sorted_trades:
        ret = _num(trade.get("net_return_pct"))
        if ret is not None and ret < 0:
            current += 1
            max_losses = max(max_losses, current)
        else:
            current = 0
    return max_losses


def _exposure_pct(trades: list[Mapping[str, Any]], bar_count: int) -> float:
    if bar_count <= 0:
        return 0
    days = sum((_num(t.get("holding_days")) or 0) for t in trades)
    return min(100, days / bar_count * 100)


def _bar_time_by_date(ohlcv: list[Mapping[str, Any]]) -> dict[str, int]:
    mapping = {}
    for row in ohlcv:
        ts = _time(row.get("time") or row.get("dt") or row.get("timestamp"))
        if ts is None:
            continue
        mapping[_date_from_time(ts)] = ts
    return mapping


def _first_date(ohlcv: list[Mapping[str, Any]]) -> str:
    if not ohlcv:
        return ""
    ts = _time(ohlcv[0].get("time") or ohlcv[0].get("dt") or ohlcv[0].get("timestamp"))
    return _date_from_time(ts) if ts is not None else ""


def _last_date(ohlcv: list[Mapping[str, Any]]) -> str:
    if not ohlcv:
        return ""
    ts = _time(ohlcv[-1].get("time") or ohlcv[-1].get("dt") or ohlcv[-1].get("timestamp"))
    return _date_from_time(ts) if ts is not None else ""


def _date_from_time(ts: int) -> str:
    try:
        value = ts / 1000 if ts > 10_000_000_000 else ts
        start, _ = timestamp_range_to_dates(int(value), int(value), market="A")
        return start or ""
    except Exception:
        return ""


def _time(value: Any) -> int | None:
    number = _num(value)
    if number is None:
        return None
    if number > 10_000_000_000:
        return int(number / 1000)
    return int(number)


def _freshness(payload: Mapping[str, Any]) -> str:
    if payload.get("last_upstream_error"):
        return "degraded"
    if payload.get("partial"):
        return "partial"
    data_source = _text(payload.get("data_source"))
    if "cache" in data_source or "mongodb" in data_source:
        return "cached"
    return "fresh" if data_source else "unknown"


def _code_from_symbol(symbol: str) -> str:
    if "." in symbol:
        return symbol.split(".")[-1]
    return symbol


def _market_from_symbol(symbol: str, code: str) -> str:
    if symbol.startswith("HK.") or len(code) == 5:
        return "HK"
    if symbol.startswith(("SH.", "SZ.")) or len(code) == 6:
        return "A"
    return ""


def _is_sell(label: str) -> bool:
    lowered = label.lower()
    return ("卖" in label and "买" not in label) or "sell" in lowered or "exit" in lowered


def _evaluated_count(signals: list[Mapping[str, Any]]) -> int:
    return sum(1 for signal in signals if _as_record(signal.get("eval")).get("return_t10") is not None)


def _signal_eval_average(signals: list[Mapping[str, Any]], key: str) -> float:
    values = [_num(_as_record(signal.get("eval")).get(key)) for signal in signals]
    return _average(values)


def _trade_average(trades: list[Mapping[str, Any]], key: str) -> float:
    return _average([_num(trade.get(key)) for trade in trades])


def _average(values: list[Any]) -> float:
    numbers = [_num(value) for value in values]
    numbers = [value for value in numbers if value is not None]
    if not numbers:
        return 0
    return sum(numbers) / len(numbers)


def _median(values: list[Any]) -> float:
    numbers = [_num(value) for value in values]
    numbers = [value for value in numbers if value is not None]
    return statistics.median(numbers) if numbers else 0


def _weighted_average(values: list[tuple[Any, Any]]) -> float:
    weighted_sum = 0.0
    weight_sum = 0.0
    for value, weight in values:
        number = _num(value)
        weight_number = _num(weight)
        if number is None or not weight_number:
            continue
        weighted_sum += number * weight_number
        weight_sum += weight_number
    return weighted_sum / weight_sum if weight_sum else 0


def _first_num(record: Mapping[str, Any], keys: list[str], default: Any = None) -> float | None:
    for key in keys:
        value = _num(record.get(key))
        if value is not None:
            return value
    return _num(default)


def _pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in {None, 0}:
        return None
    return (current - previous) / previous * 100


def _safe_div(left: float, right: float | None) -> float:
    if not right:
        return 0
    return left / right


def _round(value: Any, digits: int = 2) -> float | None:
    number = _num(value)
    if number is None:
        return None
    return round(number, digits)


def _num(value: Any, default: Any = None) -> float | None:
    if value is None:
        return _num(default) if default is not None else None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            number = float(value.replace("%", ""))
        except ValueError:
            return _num(default) if default is not None else None
    else:
        return _num(default) if default is not None else None
    if math.isfinite(number):
        return number
    return _num(default) if default is not None else None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_record(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []
