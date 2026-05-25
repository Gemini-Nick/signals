# -*- coding: utf-8 -*-
import asyncio
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient


def _sample_payload():
    equity = [
        {"time": "2026-01-01", "value": 100000},
        {"time": "2026-01-02", "value": 101200},
        {"time": "2026-01-03", "value": 100800},
        {"time": "2026-01-04", "value": 103500},
    ]
    return {
        "symbol": "SZ.002759",
        "code": "002759",
        "freq": "日线",
        "data_source": "bars",
        "data_source_detail": "002759 daily local bars",
        "generated_at": "2026-05-08T10:00:00",
        "ohlcv": [
            {"time": 1767225600, "open": 10.0, "high": 10.3, "low": 9.8, "close": 10.1, "volume": 1000},
            {"time": 1767312000, "open": 10.1, "high": 10.8, "low": 10.0, "close": 10.6, "volume": 1400},
            {"time": 1767398400, "open": 10.6, "high": 11.0, "low": 10.4, "close": 10.8, "volume": 1600},
            {"time": 1767484800, "open": 10.8, "high": 11.2, "low": 10.5, "close": 11.0, "volume": 1800},
        ],
        "signals": [
            {"dt": 1767225600, "type": "Pattern A", "group": "macd", "date_str": "2026-01-01", "price": 10.1, "eval": {"return_t5": 2.1, "return_t10": 3.2, "direction_correct": 1, "mfe": 5.0, "mae": -1.0}},
        ],
        "kpi": {
            "total": 1,
            "win_rate": 100,
            "expectancy": 3.2,
            "avg_return_t10": 3.2,
            "avg_mfe": 5.0,
            "avg_mae": -1.0,
            "by_type": {"Pattern A": {"count": 1, "win_rate": 100, "avg_return_t10": 3.2}},
        },
        "sim_equity": equity,
        "sim_trades": [
            {
                "entry_date": "2026-01-02",
                "exit_date": "2026-01-04",
                "signal_type": "Pattern A",
                "net_return_pct": 3.5,
                "return_pct": 3.9,
                "holding_days": 2,
                "exit_reason": "data_end",
                "entry_price": 10.1,
                "exit_price": 10.8,
            }
        ],
        "sim_kpi": {
            "filled_trades": 1,
            "win_rate": 100,
            "total_return_pct": 3.5,
            "max_drawdown_pct": 0.4,
            "sharpe": 1.5,
            "profit_factor": 999,
            "avg_mfe": 5.0,
            "avg_mae": -1.0,
        },
        "sim_config": {"stop_loss_pct": 5, "max_hold_days": 20, "slippage_pct": 0.1},
        "warnings": ["数据源: local bars"],
}


def test_backtest_terminal_core_contract():
    from signals.core.backtest_terminal import TERMINAL_VERSION, build_backtest_terminal

    terminal = build_backtest_terminal(_sample_payload())

    assert terminal["version"] == TERMINAL_VERSION
    assert terminal["target"]["symbol"] == "SZ.002759"
    assert terminal["target"]["code"] == "002759"
    assert terminal["target"]["market"] == "A"
    assert terminal["market_snapshot"]["last"] == 11.0
    assert terminal["market_snapshot"]["prev_close"] == 10.8
    assert terminal["trade_assumptions"]["initial_capital"] == 100000
    assert terminal["trade_assumptions"]["lot_size"] == 100
    for key in [
        "total_return_pct", "benchmark_return_pct", "excess_return_pct", "annual_return_pct",
        "max_drawdown_pct", "volatility_pct", "sharpe", "calmar", "filled_trades",
        "win_rate", "profit_factor", "expectancy_pct", "avg_win_pct", "avg_loss_pct",
        "avg_holding_days", "max_consecutive_losses", "exposure_pct", "signal_count",
        "evaluated_count", "avg_t5_pct", "avg_t10_pct", "avg_mfe_pct", "avg_mae_pct",
    ]:
        assert key in terminal["metrics"]
    assert terminal["chart"]["ohlcv"]
    assert terminal["chart"]["signal_markers"][0]["kind"] == "signal"
    assert terminal["chart"]["trade_markers"][0]["kind"] == "entry"
    assert set(["perf", "trades", "signals", "scan", "risk", "config"]).issubset(terminal["panels"])


def test_backtest_terminal_empty_signals_and_no_trades():
    from signals.core.backtest_terminal import build_backtest_terminal

    payload = _sample_payload()
    payload["signals"] = []
    payload["sim_trades"] = []
    payload["sim_kpi"] = {"filled_trades": 0, "total_return_pct": 0}

    terminal = build_backtest_terminal(payload)

    assert terminal["metrics"]["signal_count"] == 0
    assert terminal["metrics"]["filled_trades"] == 0
    assert terminal["panels"]["signals"]["rows"] == []
    assert terminal["panels"]["trades"]["rows"] == []
    assert terminal["chart"]["signal_markers"] == []
    assert terminal["chart"]["trade_markers"] == []


def test_batch_terminal_multi_symbol_panels():
    from signals.core.backtest_terminal import build_batch_terminal

    sample_ohlcv = _sample_payload()["ohlcv"]
    batch = {
        "summary": {
            "total_stocks": 2,
            "ok_stocks": 2,
            "total_signals": 7,
            "total_trades": 3,
            "overall_win_rate": 66.7,
            "overall_expectancy": 1.8,
        },
        "stocks": [
            {
                "code": "002759",
                "symbol": "SZ.002759",
                "name": "天赐材料",
                "status": "ok",
                "bar_count": 4,
                "ohlcv_tail": sample_ohlcv,
                "signal_count": 4,
                "trade_count": 2,
                "win_rate": 50,
                "expectancy": 1.2,
                "total_return": 12.5,
                "max_drawdown": 8.4,
                "sharpe": 1.1,
                "signal_breakdown": [
                    {
                        "signal_type": "Pattern A",
                        "signal_count": 3,
                        "evaluated_count": 2,
                        "win_count": 1,
                        "avg_t5_pct": 2.0,
                        "avg_t10_pct": 3.0,
                        "avg_mfe_pct": 5.0,
                        "avg_mae_pct": -1.0,
                        "trade_count": 1,
                        "trade_win_count": 1,
                        "avg_trade_return_pct": 4.0,
                    }
                ],
            },
            {
                "code": "600519",
                "symbol": "SH.600519",
                "name": "贵州茅台",
                "status": "ok",
                "bar_count": 4,
                "ohlcv_tail": sample_ohlcv,
                "signal_count": 3,
                "trade_count": 1,
                "win_rate": 100,
                "expectancy": 2.4,
                "total_return": -3.2,
                "max_drawdown": 12.0,
                "sharpe": 0.4,
                "signal_breakdown": [
                    {
                        "signal_type": "Pattern A",
                        "signal_count": 1,
                        "evaluated_count": 1,
                        "win_count": 0,
                        "avg_t5_pct": -1.0,
                        "avg_t10_pct": -2.0,
                        "avg_mfe_pct": 1.0,
                        "avg_mae_pct": -4.0,
                        "trade_count": 1,
                        "trade_win_count": 0,
                        "avg_trade_return_pct": -3.0,
                    }
                ],
            },
        ],
    }

    terminal = build_batch_terminal(batch, context={"freq": "日线", "market": "A"})

    assert terminal["version"] == "backtest-terminal.v1"
    assert terminal["mode"] == "multi"
    assert terminal["metrics"]["signal_count"] == 7
    assert terminal["metrics"]["filled_trades"] == 3
    assert terminal["panels"]["ranking"]["rows"][0]["code"] == "002759"
    assert terminal["panels"]["interval_overview"]["rows"]
    assert terminal["panels"]["signals"]["rows"][0]["signal_type"] == "Pattern A"
    assert terminal["panels"]["signals"]["rows"][0]["signal_count"] == 4
    assert terminal["panels"]["signals"]["rows"][0]["avg_t10_pct"] == 1.33
    assert terminal["panels"]["multi_charts"]["items"][0]["ohlcv"]
    assert terminal["panels"]["multi_charts"]["items"][0]["max_runup_pct"] is not None
    assert terminal["panels"]["multi_charts"]["items"][0]["bar_count"] == 4
    assert terminal["panels"]["multi_charts"]["items"][0]["median_5d_high_low_pct"] == 12.5
    assert terminal["metrics"]["median_5d_high_low_pct"] == 12.5
    assert terminal["panels"]["scripts"]["cards"][0]["one_liner"]


def test_batch_signal_breakdown_uses_canonical_signal_labels():
    from signals.web2.api.backtest import _batch_signal_breakdown

    signals = [
        {
            "type": "A_零上回踩",
            "group": "macd",
            "eval": {"return_t10": 1.2, "direction_correct": 1, "return_t5": 0.5},
        },
        {
            "type": "Gap_2.1%",
            "group": "gap",
            "eval": {"return_t10": -0.2, "direction_correct": 0, "return_t5": -0.1},
        },
        {
            "type": "Gap_5.8%",
            "group": "gap",
            "eval": {"return_t10": 2.4, "direction_correct": 1, "return_t5": 1.1},
        },
        {
            "type": "二买",
            "group": "czsc",
            "eval": {"return_t10": 3.0, "direction_correct": 1, "return_t5": 1.4},
        },
    ]
    trades = [
        {"signal_type": "A_零上回踩", "signal_group": "macd", "entry_price": 10, "net_return_pct": 2.0},
        {"signal_type": "Gap_2.1%", "signal_group": "gap", "entry_price": 10, "net_return_pct": -1.0},
    ]

    rows = _batch_signal_breakdown("002759", "天赐材料", signals, trades)
    by_type = {row["signal_type"]: row for row in rows}

    assert "MACD · A零上回踩" in by_type
    assert by_type["MACD · A零上回踩"]["trade_count"] == 1
    assert by_type["缺口 · 跳空买点"]["signal_count"] == 2
    assert by_type["缺口 · 跳空买点"]["trade_count"] == 1
    assert by_type["缠论 · 二买"]["signal_count"] == 1


def test_generate_backtest_report_html_and_dispatch(tmp_path):
    from signals.core.backtest_report import generate_backtest_report, generate_html_backtest_report

    payload = _sample_payload()
    html_path = tmp_path / "report.html"
    assert generate_html_backtest_report(payload, html_path) == str(html_path)
    text = html_path.read_text(encoding="utf-8")
    assert "Signals 回测报告" in text
    assert "SZ.002759" in text
    assert "资金曲线" in text

    dispatch_path = tmp_path / "dispatch.htm"
    assert generate_backtest_report(payload, dispatch_path).endswith("dispatch.htm")
    assert dispatch_path.exists()


def test_generate_backtest_report_pdf_and_invalid_suffix(tmp_path):
    from signals.core.backtest_report import generate_backtest_report, generate_pdf_backtest_report

    payload = _sample_payload()
    pdf_path = tmp_path / "report.pdf"
    assert generate_pdf_backtest_report(payload, pdf_path) == str(pdf_path)
    assert pdf_path.read_bytes().startswith(b"%PDF")
    assert pdf_path.stat().st_size > 5000

    try:
        generate_backtest_report(payload, tmp_path / "report.txt")
    except ValueError as exc:
        assert ".html" in str(exc)
        assert ".pdf" in str(exc)
    else:
        raise AssertionError("unsupported suffix should raise ValueError")


def test_backtest_report_api_returns_attachment():
    from signals.web.app import create_app

    client = TestClient(create_app())
    payload = _sample_payload()

    html_resp = client.post("/api/backtest/report?format=html", json=payload)
    assert html_resp.status_code == 200
    assert "text/html" in html_resp.headers["content-type"]
    assert "attachment" in html_resp.headers["content-disposition"]
    assert b"Signals" in html_resp.content

    pdf_resp = client.post("/api/backtest/report?format=pdf", json=payload)
    assert pdf_resp.status_code == 200
    assert pdf_resp.headers["content-type"] == "application/pdf"
    assert pdf_resp.content.startswith(b"%PDF")


def test_workbench_has_report_buttons():
    root = Path(__file__).resolve().parents[1]
    html = (root / "signals/web/static/workbench.html").read_text(encoding="utf-8")
    js = (root / "signals/web/static/js/workbench.js").read_text(encoding="utf-8")

    assert 'id="wb-backtest-html"' in html
    assert 'id="wb-backtest-pdf"' in html
    assert "wbDownloadBacktestReport('html')" in js
    assert "wbDownloadBacktestReport('pdf')" in js


def test_service_analyze_uses_plain_defaults(monkeypatch):
    from signals.services import backtest as backtest_service
    from signals.web2.api import backtest as web2_backtest

    dates = pd.date_range("2026-01-01", periods=40, freq="D")
    df = pd.DataFrame({
        "open": [10 + i * 0.1 for i in range(40)],
        "high": [10.3 + i * 0.1 for i in range(40)],
        "low": [9.8 + i * 0.1 for i in range(40)],
        "close": [10.1 + i * 0.1 for i in range(40)],
        "vol": [1000] * 40,
    }, index=dates)
    df.attrs["data_source"] = "unit"
    df.attrs["data_source_detail"] = "unit bars"

    def fake_fetch_kline(code, market, freq):
        return df.copy()

    def fake_detect_all_signals(*args, **kwargs):
        sig_dt = int(pd.Timestamp(dates[3]).timestamp())
        return ([{
            "dt": sig_dt,
            "date_str": str(dates[3].date()),
            "type": "Pattern A",
            "group": "macd",
            "price": 10.4,
            "confidence": 0.8,
            "eval": {"return_t10": 2.4, "direction_correct": 1, "mfe": 4.0, "mae": -0.8},
        }], [], [], [])

    monkeypatch.setattr(web2_backtest, "_fetch_kline", fake_fetch_kline)
    monkeypatch.setattr(web2_backtest, "_detect_all_signals", fake_detect_all_signals)

    result = asyncio.run(backtest_service.backtest_analyze(code="002759", freq="daily", lookback=180))
    assert result["code"] == "002759"
    assert result["terminal"]["version"] == "backtest-terminal.v1"
    assert result["terminal"]["metrics"]["signal_count"] >= 1
    assert result["sim_config"]["slippage_pct"] == 0.1
    assert result["sim_kpi"]["filled_trades"] >= 1
