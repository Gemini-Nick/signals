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
        "signals": [
            {"type": "Pattern A", "group": "macd", "date_str": "2026-01-01", "eval": {"return_t10": 3.2, "direction_correct": 1, "mfe": 5.0, "mae": -1.0}},
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
                "holding_days": 2,
                "exit_reason": "data_end",
                "entry_price": 10.1,
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
    assert result["sim_config"]["slippage_pct"] == 0.1
    assert result["sim_kpi"]["filled_trades"] >= 1
