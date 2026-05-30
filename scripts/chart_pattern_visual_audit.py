#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate chart screenshots plus a Codex-multimodal review packet.

The deterministic rule output remains the production truth. The generated
request file is an offline review packet: paste each screenshot into Codex
multimodal, collect the fixed JSON response, then pass it back with
``--visual-review`` to get a conflict report.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from PIL import Image, ImageDraw, ImageFont

import config
from signals.core.chart_patterns import classify_latest_chart_pattern
from signals.web.api import workbench

DEFAULT_INDICES = ("上证指数", "上证50", "沪深300", "创业板指", "科创50", "中证500", "中证1000")
DEFAULT_FREQS = ("daily", "weekly")


def _parse_csv(value: str, default: tuple[str, ...]) -> list[str]:
    items = [item.strip() for item in str(value or "").split(",") if item.strip()]
    return items or list(default)


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)


def _rows_from_df(df: pd.DataFrame, limit: int) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    working = df.sort_index().tail(limit).copy()
    rows: list[dict[str, Any]] = []
    for dt, row in working.iterrows():
        close = float(row.get("close") or 0)
        if close <= 0:
            continue
        open_ = float(row.get("open") or close)
        high = float(row.get("high") or max(open_, close))
        low = float(row.get("low") or min(open_, close))
        rows.append({
            "dt": str(pd.Timestamp(dt).date()),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
        })
    return rows


def _price_y(price: float, low: float, high: float, top: int, bottom: int) -> int:
    if high <= low:
        return bottom
    return int(bottom - (price - low) / (high - low) * (bottom - top))


def _ma_values(closes: list[float], period: int) -> list[float | None]:
    values: list[float | None] = []
    for idx in range(len(closes)):
        if idx + 1 < period:
            values.append(None)
        else:
            window = closes[idx + 1 - period:idx + 1]
            values.append(sum(window) / period)
    return values


def _draw_polyline(draw: ImageDraw.ImageDraw, points: list[tuple[int, int]], color: str, width: int = 2) -> None:
    if len(points) >= 2:
        draw.line(points, fill=color, width=width)


def render_chart(rows: list[dict[str, Any]], *, title: str, output_path: Path) -> None:
    width, height = 1280, 760
    left, right, top, bottom = 72, 34, 76, 670
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    try:
        title_font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 28)
        label_font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 18)
    except Exception:
        title_font = label_font = None

    highs = [float(row["high"]) for row in rows]
    lows = [float(row["low"]) for row in rows]
    closes = [float(row["close"]) for row in rows]
    price_low = min(lows)
    price_high = max(highs)
    padding = (price_high - price_low) * 0.08 if price_high > price_low else max(price_high * 0.02, 1)
    price_low -= padding
    price_high += padding

    draw.text((left, 24), title, fill="#111827", font=title_font)
    for i in range(5):
        y = top + (bottom - top) * i // 4
        price = price_high - (price_high - price_low) * i / 4
        draw.line((left, y, width - right, y), fill="#e5e7eb", width=1)
        draw.text((8, y - 10), f"{price:.0f}", fill="#6b7280", font=label_font)

    n = len(rows)
    span = max(1, width - left - right)
    step = span / max(n, 1)
    candle_width = max(3, int(step * 0.55))
    x_centers = [int(left + step * (idx + 0.5)) for idx in range(n)]

    for idx, row in enumerate(rows):
        x = x_centers[idx]
        open_ = float(row["open"])
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        color = "#dc2626" if close >= open_ else "#059669"
        high_y = _price_y(high, price_low, price_high, top, bottom)
        low_y = _price_y(low, price_low, price_high, top, bottom)
        open_y = _price_y(open_, price_low, price_high, top, bottom)
        close_y = _price_y(close, price_low, price_high, top, bottom)
        draw.line((x, high_y, x, low_y), fill=color, width=2)
        y1, y2 = sorted((open_y, close_y))
        draw.rectangle((x - candle_width // 2, y1, x + candle_width // 2, max(y2, y1 + 2)), outline=color, fill="#fee2e2" if close >= open_ else "#d1fae5")

    ma_colors = {5: "#f59e0b", 10: "#2563eb", 20: "#c026d3", 21: "#059669"}
    for period, color in ma_colors.items():
        points: list[tuple[int, int]] = []
        for idx, value in enumerate(_ma_values(closes, period)):
            if value is None:
                continue
            points.append((x_centers[idx], _price_y(value, price_low, price_high, top, bottom)))
        _draw_polyline(draw, points, color, width=2)
        if points:
            draw.text((points[-1][0] + 5, points[-1][1] - 10), f"MA{period}", fill=color, font=label_font)

    if rows:
        draw.text((left, bottom + 18), f"{rows[0]['dt']}  ->  {rows[-1]['dt']}", fill="#374151", font=label_font)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)


def _visual_requests(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    schema = {
        "dominant_pattern": "string",
        "touched_levels": ["string"],
        "channel_state": "ascending_channel | descending_channel | range | none",
        "confidence": "number 0-1",
        "disagrees_with_rule": "boolean",
        "notes": "string",
    }
    requests = []
    for record in records:
        requests.append({
            "name": record["name"],
            "symbol": record["symbol"],
            "freq": record["freq"],
            "image_path": record["image_path"],
            "rule_signal": record["rule_output"].get("primary_chart_signal", {}),
            "instruction": "Inspect the chart image only. Return exactly one JSON object matching expected_json_schema.",
            "expected_json_schema": schema,
        })
    return requests


def _load_visual_review(path: str) -> dict[tuple[str, str], dict[str, Any]]:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("reviews", [])
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        if isinstance(item, dict):
            out[(str(item.get("name") or ""), str(item.get("freq") or ""))] = item
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate deterministic chart-pattern visual audit artifacts.")
    parser.add_argument("--indices", default=",".join(DEFAULT_INDICES), help="Comma-separated index names.")
    parser.add_argument("--freqs", default=",".join(DEFAULT_FREQS), help="Comma-separated freqs: daily,weekly.")
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--visual-review", default="", help="Optional Codex multimodal JSON results to compare.")
    args = parser.parse_args()

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(args.output_dir or f".cache/chart-pattern-visual-audit/{timestamp}")
    visual_reviews = _load_visual_review(args.visual_review)
    records: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    for name in _parse_csv(args.indices, DEFAULT_INDICES):
        symbol = config.INDEX_AK_CODES.get(name) or config.INDEX_FUTU_CODES.get(name) or config.INDEX_US_CODES.get(name)
        if not symbol:
            continue
        for freq in _parse_csv(args.freqs, DEFAULT_FREQS):
            df, source = workbench._index_df(symbol, freq)
            rows = _rows_from_df(df, args.limit)
            if len(rows) < 5:
                continue
            rule_output = classify_latest_chart_pattern(rows, freq)
            image_path = output_dir / "images" / f"{_safe_name(name)}-{symbol}-{freq}.png"
            render_chart(rows, title=f"{name} {symbol} {freq} {source}", output_path=image_path)
            visual_review = visual_reviews.get((name, freq))
            record = {
                "name": name,
                "symbol": symbol,
                "freq": freq,
                "source": source,
                "image_path": str(image_path.resolve()),
                "rule_output": rule_output,
                "codex_visual_review": visual_review,
            }
            records.append(record)
            if visual_review and float(visual_review.get("confidence") or 0) >= 0.75 and visual_review.get("disagrees_with_rule"):
                conflicts.append(record)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "audit_report.json").write_text(
        json.dumps({"records": records, "conflicts": conflicts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "codex_multimodal_requests.json").write_text(
        json.dumps({"requests": _visual_requests(records)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"audit_report={output_dir / 'audit_report.json'}")
    print(f"codex_requests={output_dir / 'codex_multimodal_requests.json'}")
    print(f"records={len(records)} conflicts={len(conflicts)}")


if __name__ == "__main__":
    main()
