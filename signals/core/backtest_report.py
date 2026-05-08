# -*- coding: utf-8 -*-
"""HTML/PDF report generation for Signals backtest payloads."""
from __future__ import annotations

import html
import os
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable


def generate_backtest_report(payload: dict[str, Any], output_path: str | os.PathLike[str], title: str = "Signals 回测报告") -> str:
    """Generate a backtest report by output suffix."""
    suffix = Path(output_path).suffix.lower()
    if suffix == ".pdf":
        return generate_pdf_backtest_report(payload, output_path, title=title)
    if suffix in {".html", ".htm"}:
        return generate_html_backtest_report(payload, output_path, title=title)
    raise ValueError(f"不支持的报告后缀: {suffix or '<empty>'}; 请使用 .html 或 .pdf")


def generate_html_backtest_report(payload: dict[str, Any], output_path: str | os.PathLike[str], title: str = "Signals 回测报告") -> str:
    """Generate a self-contained HTML backtest report."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_backtest_report(payload, "html", title=title))
    return str(path)


def generate_pdf_backtest_report(payload: dict[str, Any], output_path: str | os.PathLike[str], title: str = "Signals 回测报告") -> str:
    """Generate a PDF backtest report."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_backtest_report(payload, "pdf", title=title))
    return str(path)


def render_backtest_report(payload: dict[str, Any], report_format: str, title: str = "Signals 回测报告") -> bytes:
    """Render report bytes for API responses."""
    fmt = report_format.lower().lstrip(".")
    if fmt in {"html", "htm"}:
        return _render_html(payload, title).encode("utf-8")
    if fmt == "pdf":
        return _render_pdf(payload, title)
    raise ValueError(f"不支持的报告格式: {report_format}; 请使用 html 或 pdf")


def report_filename(payload: dict[str, Any], report_format: str) -> str:
    """Build a stable download filename for a payload."""
    code = _safe_filename_part(payload.get("code") or payload.get("symbol") or "unknown")
    freq = _safe_filename_part(_freq_slug(payload.get("freq") or "daily"))
    suffix = "pdf" if report_format.lower().lstrip(".") == "pdf" else "html"
    return f"backtest_{code}_{freq}.{suffix}"


def _render_html(payload: dict[str, Any], title: str) -> str:
    code = _h(payload.get("code") or "")
    symbol = _h(payload.get("symbol") or "")
    freq = _h(payload.get("freq") or "")
    source = _h(payload.get("data_source_detail") or payload.get("data_source") or "")
    signals = payload.get("signals") or []
    trades = [t for t in payload.get("sim_trades") or [] if t.get("entry_price") is not None]
    warnings = payload.get("warnings") or []

    kpi_cards = _metric_cards(payload.get("kpi") or {}, payload.get("sim_kpi") or {})
    html_parts = [
        "<!doctype html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{_h(title)} - {code}</title>",
        "<style>",
        _html_css(),
        "</style>",
        "</head>",
        "<body>",
        '<main class="report">',
        '<section class="hero">',
        f"<h1>{_h(title)}</h1>",
        f'<div class="subtitle">{symbol} · {code} · {freq}</div>',
        f'<div class="source">{source}</div>',
        "</section>",
        '<section class="cards">',
        "".join(f'<div class="card"><span>{_h(label)}</span><strong class="{cls}">{_h(value)}</strong></div>' for label, value, cls in kpi_cards),
        "</section>",
        '<section class="panel">',
        "<h2>资金曲线</h2>",
        _equity_svg(payload.get("sim_equity") or []),
        "</section>",
        '<section class="grid">',
        '<div class="panel">',
        "<h2>交易明细</h2>",
        _html_table(
            ["入场", "出场", "信号", "收益", "持仓", "原因"],
            [
                [
                    t.get("entry_date", ""),
                    t.get("exit_date", ""),
                    t.get("signal_type", ""),
                    _fmt_pct(t.get("net_return_pct")),
                    _fmt_days(t.get("holding_days")),
                    t.get("exit_reason", ""),
                ]
                for t in trades[:20]
            ],
        ),
        "</div>",
        '<div class="panel">',
        "<h2>信号类型</h2>",
        _signal_type_table(payload.get("kpi") or {}),
        "</div>",
        "</section>",
        '<section class="panel">',
        "<h2>参数</h2>",
        _config_table(payload.get("sim_config") or {}),
        "</section>",
    ]
    if warnings:
        html_parts.extend([
            '<section class="panel warnings">',
            "<h2>Warnings</h2>",
            "<ul>",
            "".join(f"<li>{_h(w)}</li>" for w in warnings),
            "</ul>",
            "</section>",
        ])
    html_parts.extend([
        '<section class="footer">',
        f"生成时间: {_h(payload.get('generated_at') or '')} · 信号 {len(signals)} 条 · 成交 {len(trades)} 笔",
        "</section>",
        "</main>",
        "</body>",
        "</html>",
    ])
    return "\n".join(html_parts)


def _render_pdf(payload: dict[str, Any], title: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    styles = getSampleStyleSheet()
    base = ParagraphStyle("SignalsBase", parent=styles["Normal"], fontName="STSong-Light", fontSize=9, leading=13)
    title_style = ParagraphStyle(
        "SignalsTitle", parent=base, fontSize=22, leading=28, alignment=TA_CENTER, textColor=colors.HexColor("#172033")
    )
    subtitle_style = ParagraphStyle(
        "SignalsSubtitle", parent=base, fontSize=11, leading=16, alignment=TA_CENTER, textColor=colors.HexColor("#5b6474")
    )
    heading = ParagraphStyle("SignalsHeading", parent=base, fontSize=13, leading=18, textColor=colors.HexColor("#172033"))

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.3 * cm,
        leftMargin=1.3 * cm,
        topMargin=1.2 * cm,
        bottomMargin=1.2 * cm,
        title=title,
    )
    story = [
        Paragraph(_h(title), title_style),
        Spacer(1, 0.15 * cm),
        Paragraph(_header_text(payload), subtitle_style),
        Spacer(1, 0.3 * cm),
    ]

    cards = _metric_cards(payload.get("kpi") or {}, payload.get("sim_kpi") or {})
    story.append(_pdf_table([[label, value] for label, value, _ in cards], col_widths=[3.1 * cm, 2.5 * cm], grid=False))
    story.append(Spacer(1, 0.35 * cm))

    chart = _equity_png(payload.get("sim_equity") or [])
    if chart:
        story.append(Paragraph("资金曲线", heading))
        story.append(Spacer(1, 0.12 * cm))
        story.append(Image(BytesIO(chart), width=17.5 * cm, height=6.2 * cm))
        story.append(Spacer(1, 0.35 * cm))

    trades = [t for t in payload.get("sim_trades") or [] if t.get("entry_price") is not None]
    story.append(Paragraph("交易明细", heading))
    story.append(_pdf_table(
        [["入场", "出场", "信号", "收益", "持仓", "原因"]]
        + [
            [
                _text(t.get("entry_date")),
                _text(t.get("exit_date")),
                _text(t.get("signal_type")),
                _fmt_pct(t.get("net_return_pct")),
                _fmt_days(t.get("holding_days")),
                _text(t.get("exit_reason")),
            ]
            for t in trades[:18]
        ],
        header=True,
    ))
    story.append(Spacer(1, 0.3 * cm))

    story.append(Paragraph("信号类型", heading))
    story.append(_pdf_table(_signal_type_rows(payload.get("kpi") or {}), header=True))
    story.append(Spacer(1, 0.3 * cm))

    config_rows = [["参数", "取值"]] + [[_text(k), _text(v)] for k, v in (payload.get("sim_config") or {}).items()]
    story.append(Paragraph("参数", heading))
    story.append(_pdf_table(config_rows, header=True))

    warnings = payload.get("warnings") or []
    if warnings:
        story.append(Spacer(1, 0.3 * cm))
        story.append(Paragraph("Warnings", heading))
        story.append(_pdf_table([["内容"]] + [[_text(w)] for w in warnings], header=True))

    doc.build(story)
    return buffer.getvalue()


def _metric_cards(kpi: dict[str, Any], sim_kpi: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [
        ("信号数", _text(kpi.get("total", 0)), ""),
        ("T+10 胜率", _fmt_rate(kpi.get("win_rate")), _cls(kpi.get("win_rate"), 50)),
        ("期望收益", _fmt_pct(kpi.get("expectancy")), _cls(kpi.get("expectancy"), 0)),
        ("成交笔数", _text(sim_kpi.get("filled_trades", 0)), ""),
        ("总收益", _fmt_pct(sim_kpi.get("total_return_pct")), _cls(sim_kpi.get("total_return_pct"), 0)),
        ("最大回撤", _fmt_pct(-abs(float(sim_kpi.get("max_drawdown_pct") or 0))), "down"),
        ("Sharpe", _fmt_num(sim_kpi.get("sharpe")), _cls(sim_kpi.get("sharpe"), 1)),
        ("盈亏比", _fmt_num(sim_kpi.get("profit_factor")), _cls(sim_kpi.get("profit_factor"), 1)),
        ("MFE 均值", _fmt_pct(sim_kpi.get("avg_mfe")), "up"),
        ("MAE 均值", _fmt_pct(sim_kpi.get("avg_mae")), "down"),
    ]


def _signal_type_rows(kpi: dict[str, Any]) -> list[list[str]]:
    rows = [["信号类型", "数量", "胜率", "平均T+10"]]
    for sig_type, info in (kpi.get("by_type") or {}).items():
        rows.append([
            _text(sig_type),
            _text(info.get("count", 0)),
            _fmt_rate(info.get("win_rate")),
            _fmt_pct(info.get("avg_return_t10")),
        ])
    if len(rows) == 1:
        rows.append(["-", "0", "-", "-"])
    return rows


def _signal_type_table(kpi: dict[str, Any]) -> str:
    rows = _signal_type_rows(kpi)
    return _html_table(rows[0], rows[1:])


def _config_table(config: dict[str, Any]) -> str:
    return _html_table(["参数", "取值"], [[k, v] for k, v in config.items()])


def _html_table(headers: Iterable[Any], rows: Iterable[Iterable[Any]]) -> str:
    body_rows = list(rows)
    if not body_rows:
        body_rows = [["-" for _ in headers]]
    head = "".join(f"<th>{_h(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{_h(c)}</td>" for c in row) + "</tr>" for row in body_rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _pdf_table(rows: list[list[Any]], col_widths: list[float] | None = None, header: bool = False, grid: bool = True):
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle
    from reportlab.lib.styles import ParagraphStyle

    style = ParagraphStyle("Cell", fontName="STSong-Light", fontSize=8, leading=11)
    data = [[Paragraph(_h(cell), style) for cell in row] for row in rows]
    table = Table(data, colWidths=col_widths, repeatRows=1 if header else 0, hAlign="LEFT")
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if grid:
        commands.append(("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d9dee8")))
    if header:
        commands.extend([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf2f7")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#172033")),
        ])
    else:
        commands.append(("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f7f9fc")))
    table.setStyle(TableStyle(commands))
    return table


def _equity_png(equity: list[dict[str, Any]]) -> bytes:
    import plotly.graph_objects as go

    if len(equity) < 2:
        return b""
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[_axis_time(p.get("time")) for p in equity], y=[p.get("value") for p in equity], mode="lines", line={"color": "#2563eb", "width": 3}))
    fig.update_layout(
        template="plotly_white",
        height=360,
        width=1000,
        margin={"l": 45, "r": 20, "t": 20, "b": 40},
        xaxis_title="日期",
        yaxis_title="资金",
        showlegend=False,
    )
    return fig.to_image(format="png", width=1000, height=360, scale=2)


def _equity_svg(equity: list[dict[str, Any]]) -> str:
    if len(equity) < 2:
        return '<div class="empty">暂无资金曲线</div>'
    points = []
    values = [float(p.get("value") or 0) for p in equity]
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    width, height, pad = 920, 260, 24
    for idx, value in enumerate(values):
        x = pad + idx * (width - 2 * pad) / max(len(values) - 1, 1)
        y = height - pad - (value - lo) * (height - 2 * pad) / span
        points.append(f"{x:.1f},{y:.1f}")
    return (
        f'<svg class="equity" viewBox="0 0 {width} {height}" role="img">'
        f'<polyline points="{" ".join(points)}" fill="none" stroke="#2563eb" stroke-width="3" />'
        f'<text x="{pad}" y="22">{_h(_fmt_num(hi))}</text>'
        f'<text x="{pad}" y="{height - 8}">{_h(_fmt_num(lo))}</text>'
        "</svg>"
    )


def _header_text(payload: dict[str, Any]) -> str:
    source = payload.get("data_source_detail") or payload.get("data_source") or ""
    return " / ".join(_text(x) for x in [payload.get("symbol"), payload.get("code"), payload.get("freq"), source] if x)


def _html_css() -> str:
    return """
body { margin:0; background:#f3f5f9; color:#172033; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
.report { max-width:1120px; margin:0 auto; padding:32px 24px 44px; }
.hero { background:#fff; border:1px solid #d9dee8; padding:28px 32px; border-radius:8px; }
h1 { margin:0; font-size:30px; letter-spacing:0; }
h2 { margin:0 0 14px; font-size:17px; }
.subtitle { margin-top:8px; color:#5b6474; font-size:15px; }
.source { margin-top:4px; color:#7b8494; }
.cards { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:10px; margin:18px 0; }
.card { background:#fff; border:1px solid #d9dee8; border-radius:8px; padding:12px; min-height:58px; }
.card span { display:block; color:#667085; font-size:12px; }
.card strong { display:block; margin-top:4px; font-size:20px; }
.up { color:#c62828; } .down { color:#00897b; }
.panel { background:#fff; border:1px solid #d9dee8; border-radius:8px; padding:18px; margin-bottom:18px; }
.grid { display:grid; grid-template-columns:1.3fr 1fr; gap:18px; }
.equity { width:100%; height:auto; background:#fbfcff; border:1px solid #e7ebf2; border-radius:6px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,td { border-bottom:1px solid #e7ebf2; padding:8px 10px; text-align:left; vertical-align:top; }
th { background:#f7f9fc; color:#344054; }
.warnings { border-color:#f2c94c; }
.footer { color:#7b8494; font-size:12px; text-align:center; margin-top:18px; }
.empty { color:#7b8494; padding:32px; text-align:center; background:#fbfcff; border-radius:6px; }
@media (max-width: 900px) { .cards,.grid { grid-template-columns:1fr; } }
"""


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return _text(value)


def _fmt_rate(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return _text(value)


def _fmt_num(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return _text(value)


def _fmt_days(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{int(value)}D"
    except (TypeError, ValueError):
        return _text(value)


def _cls(value: Any, threshold: float) -> str:
    try:
        return "up" if float(value) >= threshold else "down"
    except (TypeError, ValueError):
        return ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _freq_slug(value: Any) -> str:
    text = _text(value)
    return {
        "日线": "daily",
        "周线": "weekly",
        "月线": "monthly",
        "30分钟": "30m",
    }.get(text, text)


def _safe_filename_part(value: Any) -> str:
    text = _text(value).replace(".", "_")
    safe = []
    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch in {"_", "-"}):
            safe.append(ch)
    return "".join(safe) or "unknown"


def _axis_time(value: Any) -> str:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return _text(value)
    return _text(value)


def _h(value: Any) -> str:
    return html.escape(_text(value), quote=True)
