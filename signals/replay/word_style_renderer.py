# -*- coding: utf-8 -*-
"""Render a Word-style daily replay note from Signals evidence.

The reference Word document defines shape and density only. This module does
not read reference prose; it renders from ``signals_context`` and
``market_replay`` evidence so missing local data remains visible.
"""
from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any


_WEEKDAY_CN = "一二三四五六日"


def _text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _fmt_num(value: Any, digits: int = 2) -> str:
    number = _float(value)
    if number is None:
        return "unknown"
    if abs(number) < 0.5 * (10 ** -digits):
        number = 0.0
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def _fmt_pct(value: Any) -> str:
    number = _float(value)
    if number is None:
        return "unknown"
    sign = "+" if number > 0 else ""
    return f"{sign}{number:.2f}%"


def _fmt_yi(value: Any) -> str:
    number = _float(value)
    if number is None:
        return "unknown"
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _trade_date_text(trade_date: str) -> tuple[str, str]:
    try:
        dt = datetime.fromisoformat(trade_date)
    except (TypeError, ValueError):
        return trade_date or "unknown", "周unknown"
    return f"{dt.year}年{dt.month}月{dt.day}日", f"周{_WEEKDAY_CN[dt.weekday()]}"


def _short_date(trade_date: str) -> str:
    try:
        dt = datetime.fromisoformat(trade_date)
    except (TypeError, ValueError):
        return trade_date or "unknown"
    return f"{dt.month}月{dt.day}日"


def _parse_date(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    safe_rows = rows or [["unknown" for _ in headers]]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in safe_rows:
        cells = [_text(cell, "unknown").replace("|", "/") for cell in row]
        if len(cells) < len(headers):
            cells.extend("unknown" for _ in range(len(headers) - len(cells)))
        lines.append("| " + " | ".join(cells[: len(headers)]) + " |")
    return lines


def _status_map(market_replay: dict[str, Any]) -> dict[str, dict[str, Any]]:
    structured = market_replay.get("structured_daily_review") if isinstance(market_replay.get("structured_daily_review"), dict) else {}
    rows = _as_list(structured.get("data_completeness"))
    return {_text(row.get("item")): row for row in rows if _text(row.get("item"))}


def _status_text(statuses: dict[str, dict[str, Any]], item: str) -> str:
    row = statuses.get(item, {})
    status = _text(row.get("status"), "unknown")
    source = _text(row.get("source"), "unknown")
    impact = _text(row.get("impact"), "待确认")
    return f"{status}（{source}；{impact}）"


def _board_rankings(market_replay: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ranking = market_replay.get("daily_board_rankings") if isinstance(market_replay.get("daily_board_rankings"), dict) else {}
    strong = _as_list(ranking.get("rows"))
    weak = _as_list(ranking.get("weak_rows"))
    if strong:
        return strong, weak
    structured = market_replay.get("structured_daily_review") if isinstance(market_replay.get("structured_daily_review"), dict) else {}
    top_proxy = structured.get("top_turnover_boards") if isinstance(structured.get("top_turnover_boards"), dict) else {}
    proxy_rows = _as_list(top_proxy.get("rows"))
    converted = [
        {
            "name": _text(row.get("board") or row.get("driver_name")),
            "kind": _text(row.get("driver_kind"), "board_proxy"),
            "change_pct": row.get("change_pct"),
            "amount_yi": row.get("amount_yi"),
            "leader_name": "unknown",
            "source": _text(row.get("source"), "structured_daily_review.top_turnover_boards"),
            "evidence_level": _text(row.get("evidence_level"), "inferred"),
        }
        for row in proxy_rows
        if _text(row.get("board") or row.get("driver_name"))
    ]
    return converted, sorted(converted, key=lambda item: _float(item.get("change_pct")) or 0.0)


def _board_name(row: dict[str, Any]) -> str:
    name = _text(row.get("name") or row.get("board") or row.get("driver_name"), "unknown")
    kind = _text(row.get("kind") or row.get("driver_kind"))
    return f"{name}（{kind}）" if kind else name


def _trend_board_map(market_replay: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    structured = market_replay.get("structured_daily_review") if isinstance(market_replay.get("structured_daily_review"), dict) else {}
    trend = structured.get("trend_20d_boards") if isinstance(structured.get("trend_20d_boards"), dict) else {}
    if trend.get("status") != "available":
        return {}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _as_list(trend.get("rows")):
        name = _text(row.get("name"))
        kind = _text(row.get("kind"))
        if name:
            result[(kind, name)] = row
            result[("", name)] = row
    return result


def _trend_for_board(row: dict[str, Any], trend_map: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    name = _text(row.get("name") or row.get("board") or row.get("driver_name"))
    kind = _text(row.get("kind") or row.get("driver_kind"))
    return trend_map.get((kind, name)) or trend_map.get(("", name)) or {}


def _direction_candidate_lines(
    strong_boards: list[dict[str, Any]],
    weak_boards: list[dict[str, Any]],
    trend_map: dict[tuple[str, str], dict[str, Any]],
) -> list[str]:
    lines = ["方向定性"]
    trend_rows = list({id(row): row for row in trend_map.values()}.values())
    continuation = next(
        (
            row
            for row in sorted(
                trend_rows,
                key=lambda item: (
                    _float(item.get("change_20d_pct")) or -10**12,
                    _float(item.get("change_5d_pct")) or -10**12,
                ),
                reverse=True,
            )
            if (_float(row.get("change_pct")) or 0.0) > 0 and (_float(row.get("change_20d_pct")) or 0.0) >= 15
        ),
        None,
    )
    burst = next(
        (
            row
            for row in strong_boards
            if not _trend_for_board(row, trend_map)
            or (_float(_trend_for_board(row, trend_map).get("change_20d_pct")) or 0.0) < 5
        ),
        strong_boards[0] if strong_boards else None,
    )
    retreat = next(
        (
            row
            for row in weak_boards
            if (_float(row.get("change_pct")) or 0.0) <= -2
        ),
        weak_boards[0] if weak_boards else None,
    )
    if continuation:
        lines.append(
            (
                f"- {_board_name(continuation)}——强趋势延续，"
                f"5日{_fmt_pct(continuation.get('change_5d_pct'))}、20日{_fmt_pct(continuation.get('change_20d_pct'))}；"
                "但这只证明日度趋势，内部扩散仍需看上涨家数、领涨股和次日承接。"
            )
        )
    if burst:
        trend = _trend_for_board(burst, trend_map)
        trend_text = (
            f"20日{_fmt_pct(trend.get('change_20d_pct'))}"
            if trend
            else "20日趋势证据不足"
        )
        lines.append(
            (
                f"- {_board_name(burst)}——当日放量/强度爆发候选，涨跌幅{_fmt_pct(burst.get('change_pct'))}、{trend_text}；"
                "次日要验证成交维持和领涨股扩散。"
            )
        )
    if retreat:
        trend = _trend_for_board(retreat, trend_map)
        trend_text = (
            f"，前期20日{_fmt_pct(trend.get('change_20d_pct'))}"
            if trend
            else ""
        )
        lines.append(
            (
                f"- {_board_name(retreat)}——退潮/风险方向，当日{_fmt_pct(retreat.get('change_pct'))}{trend_text}；"
                "若次日仍弱于市场宽度，继续按风险方向处理。"
            )
        )
    if len(lines) == 1:
        lines.append("- 趋势、爆发和退潮方向均缺少足够证据，维持 unknown。")
    return lines


def _index_brief(indices: list[dict[str, Any]]) -> str:
    if not indices:
        return "指数日线缺失，无法还原四大指数表现。"
    parts = [f"{_text(row.get('name'))}{_fmt_pct(row.get('change_pct'))}" for row in indices[:4]]
    return "、".join(parts)


def _turnover_sentence(indices: list[dict[str, Any]]) -> str:
    market_rows = [
        row
        for row in indices
        if _text(row.get("name")) in {"上证指数", "深证成指"}
    ]
    total_yi = sum(_float(row.get("amount_yi")) or 0.0 for row in (market_rows or indices))
    if total_yi <= 0:
        return "全市场总成交 unknown"
    if total_yi >= 10000:
        return f"全市场总成交约{total_yi / 10000:.2f}万亿"
    return f"全市场总成交约{total_yi:.0f}亿"


def _market_state_sentence(indices: list[dict[str, Any]], breadth: dict[str, Any]) -> str:
    down = _float(breadth.get("down"))
    up = _float(breadth.get("up"))
    index_changes = [(row, _float(row.get("change_pct"))) for row in indices]
    valid = [(row, change) for row, change in index_changes if change is not None]
    if not valid:
        return "市场状态定性：unknown。"
    strongest, strongest_change = max(valid, key=lambda item: item[1])
    rest = [change for row, change in valid if row is not strongest]
    split = bool(rest and strongest_change - max(rest) >= 2.0)
    width_weak = bool(down is not None and up is not None and down > up)
    if split and width_weak:
        return f"市场状态定性：极度分化，{_text(strongest.get('name'))}显著强于其余指数，个股宽度偏弱。"
    if split:
        return f"市场状态定性：指数分化，{_text(strongest.get('name'))}明显领涨。"
    if width_weak:
        return "市场状态定性：指数修复但个股宽度偏弱。"
    return "市场状态定性：指数和个股宽度同步修复。"


def _ma_state_text(row: dict[str, Any]) -> str:
    close = _float(row.get("close"))
    if close is None:
        return "unknown"
    above = []
    below = []
    for label, key in (("MA5", "ma5"), ("MA10", "ma10"), ("MA20", "ma20")):
        value = _float(row.get(key))
        if value is None:
            continue
        if close >= value:
            above.append(label)
        else:
            below.append(label)
    parts = []
    if above:
        parts.append("收在" + "/".join(above) + "上方")
    if below:
        parts.append("收在" + "/".join(below) + "下方")
    return "，".join(parts) if parts else "MA 数据不足"


def _technical_key_level(row: dict[str, Any]) -> str:
    close = _float(row.get("close"))
    levels = []
    for label, key in (("MA5", "ma5"), ("MA10", "ma10"), ("MA20", "ma20")):
        value = _float(row.get(key))
        if close is None or value is None:
            continue
        relation = "压力" if close < value else "支撑"
        levels.append(f"{label}={_fmt_num(value)}为{relation}")
    bias = _float(row.get("bias20_pct"))
    if bias is not None:
        levels.append(f"MA20乖离{_fmt_pct(bias)}")
    return "；".join(levels[:4]) if levels else "关键位 unknown"


def _technical_lines(market_replay: dict[str, Any], cycle: dict[str, Any]) -> list[str]:
    technical = market_replay.get("major_index_technical") if isinstance(market_replay.get("major_index_technical"), dict) else {}
    rows = _as_list(technical.get("rows"))
    if not rows:
        return _table(
            ["项目", "状态", "证据"],
            [
                ["均线", "unknown", "Signals 当前 evidence package 未返回均线表；不从 Word 补。"],
                ["MACD/RSI", "unknown", "缺指标快照；不输出背离/金叉等结论。"],
                [
                    "时间周期",
                    "confirmed" if cycle else "unknown",
                    (
                        f"上证距{cycle.get('pivot_date')}高点{_fmt_num(cycle.get('pivot_high'))}已{cycle.get('trading_days_since')}个交易日，累计{_fmt_pct(cycle.get('drop_pct'))}"
                        if cycle
                        else "index_cycle 缺失"
                    ),
                ],
            ],
        )
    lines = _table(
        ["指数", "均线", "MACD", "RSI(6)", "关键位"],
        [
            [
                _text(row.get("name")),
                _ma_state_text(row),
                f"DIF={_fmt_num(row.get('macd_dif'))}，DEA={_fmt_num(row.get('macd_dea'))}，柱={_fmt_num(row.get('macd_bar'))}",
                _fmt_num(row.get("rsi6")),
                _technical_key_level(row),
            ]
            for row in rows
        ],
    )
    if cycle:
        lines.append(
            f"时间周期：上证距{cycle.get('pivot_date')}高点{_fmt_num(cycle.get('pivot_high'))}已{cycle.get('trading_days_since')}个交易日，累计{_fmt_pct(cycle.get('drop_pct'))}。"
        )
    note = _text(technical.get("note"), "由本地指数日线计算").rstrip("。")
    lines.append(f"技术指标来源：{_text(technical.get('source'), 'index_bars:日线')}；{note}。")
    return lines


def _index_intraday_lines(market_replay: dict[str, Any]) -> list[str]:
    intraday = market_replay.get("major_index_intraday") if isinstance(market_replay.get("major_index_intraday"), dict) else {}
    rows = _as_list(intraday.get("rows"))
    if not rows:
        return ["- 指数5分钟线：missing；共同低点窗口 unknown。"]
    window = intraday.get("common_low_window") if isinstance(intraday.get("common_low_window"), dict) else {}
    cluster = intraday.get("dominant_low_cluster") if isinstance(intraday.get("dominant_low_cluster"), dict) else {}
    window_text = (
        f"{_text(window.get('start'))}-{_text(window.get('end'))}"
        if window
        else "unknown"
    )
    cluster_text = (
        f"{_text(cluster.get('start'))}-{_text(cluster.get('end'))}（{_text(cluster.get('count'))}/{_text(cluster.get('total'))}）"
        if cluster
        else "unknown"
    )
    lines = [
        (
            f"- 指数5分钟线：available（{_text(intraday.get('source'), 'index_bars:5分钟')}）；"
            f"主要共同拐点集中在 {cluster_text}，全部低点范围 {window_text}。"
        ),
    ]
    lines.extend(
        _table(
            ["指数", "低点时间", "低点", "高点时间", "最新5分钟价", "低点后修复"],
            [
                [
                    _text(row.get("name")),
                    _text((row.get("low_bar") or {}).get("time")),
                    _fmt_num((row.get("low_bar") or {}).get("low")),
                    _text((row.get("high_bar") or {}).get("time")),
                    _fmt_num((row.get("close_bar") or {}).get("close")),
                    _fmt_pct(row.get("low_to_close_pct")),
                ]
                for row in rows
            ],
        )
    )
    return lines


def _fixed_time_slice_lines(market_replay: dict[str, Any]) -> list[str]:
    structured = market_replay.get("structured_daily_review") if isinstance(market_replay.get("structured_daily_review"), dict) else {}
    rows = _as_list(structured.get("fixed_time_slices"))
    if not rows:
        return ["- 固定半小时时间轴：unknown。"]
    lines = ["固定半小时时间轴"]
    lines.extend(
        _table(
            ["切片", "时间", "市场行为", "增强方向", "承压方向", "证据等级"],
            [
                [
                    _text(row.get("slice")),
                    _text(row.get("actual_range") or row.get("time_range")),
                    _text(row.get("market_behavior"), "unknown"),
                    _text((row.get("active_direction") or {}).get("name"), "unknown"),
                    _text((row.get("drained_direction") or {}).get("name"), "unknown"),
                    _text(row.get("evidence_level"), "unknown"),
                ]
                for row in rows
            ],
        )
    )
    return lines


def _pressure_stock(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    negative = [row for row in rows if (_float(row.get("change_pct")) or 0.0) < 0]
    candidates = negative or rows
    return max(candidates, key=lambda item: _float(item.get("amount_yi")) or 0.0)


def _stock_key(row: dict[str, Any]) -> str:
    return _text(row.get("code") or row.get("symbol") or row.get("name"))


def _stock_display(row: dict[str, Any]) -> str:
    name = _text(row.get("name"), "unknown")
    symbol = _text(row.get("symbol") or row.get("code"))
    if "." in symbol:
        prefix, code = symbol.split(".", 1)
        display_symbol = f"{prefix.lower()}{code}"
    else:
        code = symbol
        if code.startswith(("6", "9")):
            display_symbol = f"sh{code}"
        elif code.startswith(("0", "2", "3")):
            display_symbol = f"sz{code}"
        elif code:
            display_symbol = code.lower()
        else:
            display_symbol = ""
    return f"{name}({display_symbol})" if display_symbol else name


def _event_chain_map(market_replay: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in _as_list(market_replay.get("stock_event_chains")):
        for key in (_text(row.get("code")), _text(row.get("symbol")), _text(row.get("name"))):
            if key:
                result[key] = row
    return result


def _stock_daily_replay_map(market_replay: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in _as_list(market_replay.get("stock_daily_replays")):
        for key in (_text(row.get("code")), _text(row.get("symbol")), _text(row.get("name"))):
            if key:
                result[key] = row
    return result


def _stock_daily_replay_for(row: dict[str, Any], replay_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return (
        replay_map.get(_text(row.get("code")))
        or replay_map.get(_text(row.get("symbol")))
        or replay_map.get(_text(row.get("name")))
        or {}
    )


def _daily_replay_lines(replay: dict[str, Any]) -> list[str]:
    rows = _as_list(replay.get("rows"))
    if not rows:
        return ["逐日回放：日线历史 unknown。"]
    display_rows = rows[-8:]
    return _table(
        ["日期", "涨跌幅", "成交额(亿)", "换手率", "盘中最低", "收盘", "关键事件"],
        [
            [
                _text(row.get("date")),
                _fmt_pct(row.get("change_pct")),
                _fmt_yi(row.get("amount_yi")),
                _fmt_pct(row.get("turnover_pct")),
                _fmt_num(row.get("low")),
                _fmt_num(row.get("close")),
                _text(row.get("event"), "普通交易日"),
            ]
            for row in display_rows
        ],
    )


def _calibration_lines(market_replay: dict[str, Any]) -> list[str]:
    rows = _as_list(market_replay.get("previous_validation_calibration"))
    if not rows:
        return ["昨日观察点暂无可校准样本。"]
    lines: list[str] = []
    for row in rows:
        lines.append(
            (
                f"- {_text(row.get('object'), '观察对象unknown')}：触发条件={_text(row.get('trigger'), 'unknown')}；"
                f"结果={_text(row.get('result'), 'unknown')}；"
                f"校准={_text(row.get('calibration'), '待复核')}。"
            )
        )
    return lines


def _dynamic_representative_stocks(market_replay: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    bucket_labels = [
        ("pressure_core", "压力核心"),
        ("failed_emotion", "失败对照"),
        ("market_core", "主线容量/动态核心"),
        ("market_elastic_confirmed", "封板弹性"),
        ("market_elastic", "强势弹性"),
    ]
    for board in _as_list(market_replay.get("dynamic_market_representatives")):
        board_name = _text(board.get("board") or board.get("driver_name"))
        for bucket, label in bucket_labels:
            for row in _as_list(board.get(bucket))[:1]:
                key = _stock_key(row)
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                result.append({**row, "_word_type": label, "_board": board_name})
    return result


def _failed_sample_text(failed_samples: list[dict[str, Any]]) -> str:
    if not failed_samples:
        return "本方向无有效同链失败对比样本。"
    row = failed_samples[0]
    prefix = "同链失败/弱化样本" if row.get("_same_chain_sample") else "失败/弱化样本"
    chain = _text(row.get("chain_name"))
    node = _text(row.get("node_name"))
    chain_text = f"，归属{chain}/{node}" if chain and node else (f"，归属{chain}" if chain else "")
    raw_reasons = row.get("_weakness_reasons")
    reasons = ""
    if isinstance(raw_reasons, list):
        reasons = "、".join(_text(item) for item in raw_reasons if _text(item))
    reason_text = f"；弱化证据={reasons}" if reasons else ""
    return (
        f"{prefix}：{_stock_display(row)}{chain_text}，涨跌{_fmt_pct(row.get('change_pct'))}、"
        f"成交{_fmt_yi(row.get('amount_yi'))}亿{reason_text}；对照点是收盘承接或封板质量弱于成功样本。"
    )


def _latest_replay_row(replay: dict[str, Any]) -> dict[str, Any]:
    rows = _as_list(replay.get("rows"))
    return rows[-1] if rows else {}


def _same_chain_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_chain_id = _text(left.get("chain_id"))
    right_chain_id = _text(right.get("chain_id"))
    if left_chain_id and right_chain_id:
        return left_chain_id == right_chain_id
    left_chain = _text(left.get("chain_name"))
    right_chain = _text(right.get("chain_name"))
    return bool(left_chain and right_chain and left_chain == right_chain)


def _same_node_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_node_id = _text(left.get("node_id"))
    right_node_id = _text(right.get("node_id"))
    if left_node_id and right_node_id:
        return left_node_id == right_node_id
    left_node = _text(left.get("node_name"))
    right_node = _text(right.get("node_name"))
    return bool(left_node and right_node and left_node == right_node)


def _weakness_sample_from_replay(replay: dict[str, Any]) -> dict[str, Any] | None:
    latest = _latest_replay_row(replay)
    if not latest:
        return None
    change = _float(latest.get("change_pct"))
    amount_yi = _float(latest.get("amount_yi"))
    high = _float(latest.get("high"))
    low = _float(latest.get("low"))
    close = _float(latest.get("close"))
    high_to_close = (high - close) / high * 100 if high and close is not None else 0.0
    close_position = (close - low) / (high - low) if high is not None and low is not None and close is not None and high > low else 1.0
    event = _text(latest.get("event"))
    reasons: list[str] = []
    if change is not None and change <= -2.0:
        reasons.append("收跌")
    if "大幅回撤" in event:
        reasons.append("大幅回撤")
    if "冲高回落" in event:
        reasons.append("冲高回落")
    elif high_to_close >= 5.0:
        reasons.append("冲高回落")
    if close_position <= 0.25 and (amount_yi or 0.0) >= 10.0:
        reasons.append("低位收盘")
    if not reasons:
        return None
    return {
        "symbol": _text(replay.get("symbol")),
        "code": _text(replay.get("code")),
        "name": _text(replay.get("name")),
        "change_pct": change,
        "amount_yi": amount_yi,
        "chain_id": _text(replay.get("chain_id")),
        "chain_name": _text(replay.get("chain_name")),
        "node_id": _text(replay.get("node_id")),
        "node_name": _text(replay.get("node_name")),
        "high_to_close_pct": round(high_to_close, 2),
        "close_position": round(close_position, 3),
        "_weakness_reasons": reasons,
        "_same_chain_sample": True,
    }


def _failed_samples_for_trend(
    row: dict[str, Any],
    replay: dict[str, Any],
    daily_replays: dict[str, dict[str, Any]],
    fallback: list[dict[str, Any]],
    *,
    exclude_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    target = {**row, **replay}
    if not (_text(target.get("chain_id")) or _text(target.get("chain_name"))):
        return fallback[:1]
    exclude = set(exclude_keys or set())
    exclude.add(_stock_key(row))
    candidates: list[tuple[int, float, dict[str, Any]]] = []
    seen_replays: set[str] = set()
    for candidate_replay in daily_replays.values():
        key = _stock_key(candidate_replay)
        if not key or key in seen_replays or key in exclude:
            continue
        seen_replays.add(key)
        if not _same_chain_match(target, candidate_replay):
            continue
        sample = _weakness_sample_from_replay(candidate_replay)
        if not sample:
            continue
        change = _float(sample.get("change_pct")) or 0.0
        amount_yi = _float(sample.get("amount_yi")) or 0.0
        drawdown = _float(sample.get("high_to_close_pct")) or 0.0
        close_position = _float(sample.get("close_position"))
        low_close_score = max(0.25 - (close_position if close_position is not None else 1.0), 0.0) * 12.0
        same_node = _same_node_match(target, sample)
        node_score = 8.0 if same_node else 0.0
        score = max(-change, 0.0) * 3.0 + max(drawdown - 3.0, 0.0) * 2.0 + min(amount_yi, 100.0) * 0.2 + low_close_score + node_score
        candidates.append((1 if same_node else 0, score, sample))
    if not candidates:
        return []
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [candidates[0][2]]


def _chain_pressure_pool(
    trend_rows: list[dict[str, Any]],
    daily_replays: dict[str, dict[str, Any]],
    *,
    limit: int = 16,
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    trend_keys = {_stock_key(row) for row in trend_rows if _stock_key(row)}
    for row in trend_rows:
        replay = _stock_daily_replay_for(row, daily_replays)
        target = {**row, **replay}
        if _text(target.get("chain_id")) or _text(target.get("chain_name")):
            targets.append(target)
    if not targets:
        return []

    candidates: list[tuple[float, dict[str, Any]]] = []
    seen: set[str] = set()
    for replay in daily_replays.values():
        key = _stock_key(replay)
        if not key or key in seen or key in trend_keys:
            continue
        seen.add(key)
        sample = _weakness_sample_from_replay(replay)
        if not sample or _is_unsuitable_note_stock(sample):
            continue
        if not any(_same_chain_match(target, sample) for target in targets):
            continue
        amount_yi = _float(sample.get("amount_yi")) or 0.0
        change = _float(sample.get("change_pct")) or 0.0
        drawdown = _float(sample.get("high_to_close_pct")) or 0.0
        same_node = any(_same_node_match(target, sample) for target in targets)
        score = min(amount_yi, 150.0) * 1.2 + max(-change, 0.0) * 2.0 + max(drawdown - 3.0, 0.0) + (10.0 if same_node else 0.0)
        candidates.append((score, {**sample, "_same_node_pool": same_node}))
    candidates.sort(
        key=lambda item: (
            _float(item[1].get("amount_yi")) or 0.0,
            item[0],
            max(-(_float(item[1].get("change_pct")) or 0.0), 0.0),
        ),
        reverse=True,
    )
    return [sample for _score, sample in candidates[:limit]]


def _stock_structure(row: dict[str, Any], events: dict[str, dict[str, Any]]) -> str:
    event = events.get(_text(row.get("code"))) or events.get(_text(row.get("symbol"))) or events.get(_text(row.get("name")))
    if event and _text(event.get("phrase")):
        return _text(event.get("phrase"))
    return (
        f"开{_fmt_num(row.get('open'))}、高{_fmt_num(row.get('high'))}、"
        f"低{_fmt_num(row.get('low'))}、收{_fmt_num(row.get('close'))}"
    )


def _dedupe_stocks(*buckets: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bucket in buckets:
        for row in bucket:
            key = _stock_key(row)
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(row)
            if len(result) >= limit:
                return result
    return result


def _is_new_or_low_comparable(row: dict[str, Any]) -> bool:
    name = _text(row.get("name"))
    code = _text(row.get("code") or row.get("symbol")).split(".")[-1]
    if name.startswith(("N", "C")):
        return True
    return code.startswith(("8", "4", "920"))


def _is_unsuitable_note_stock(row: dict[str, Any]) -> bool:
    name = _text(row.get("name"))
    if name.startswith(("ST", "*ST")) or "退" in name:
        return True
    return _is_new_or_low_comparable(row)


def _trend_review_score(row: dict[str, Any], replay: dict[str, Any]) -> float:
    total = max(_float(replay.get("total_change_pct")) or 0.0, 0.0)
    amount = _float(row.get("amount_yi")) or 0.0
    change = max(_float(row.get("change_pct")) or 0.0, 0.0)
    replay_rows = _as_list(replay.get("rows"))
    row_count = _float(replay.get("row_count")) or len(replay_rows)
    start = _parse_date(replay.get("acceleration_date"))
    end = _parse_date(replay.get("end_date"))
    days_since_acceleration = (end - start).days if start and end else 999
    trend_quality = min(total, 110.0) * 0.75 - max(total - 125.0, 0.0) * 0.65
    current_strength = min(change, 20.0) * 0.8
    amount_score = min(amount, 60.0) * 0.25
    completeness = min(row_count, 12.0) * 1.2
    recency = max(0.0, 45.0 - days_since_acceleration * 2.2)
    acceleration = 18.0 if _text(replay.get("acceleration_date")) else 0.0
    return trend_quality + current_strength + amount_score + completeness + recency + acceleration


def _trend_group_label(row: dict[str, Any], replay: dict[str, Any]) -> str:
    chain = _text(replay.get("chain_name") or row.get("chain_name"))
    node = _text(replay.get("node_name") or row.get("node_name"))
    industry = _text(
        replay.get("industry")
        or row.get("industry")
        or row.get("_board")
        or (replay.get("limit_pool") or {}).get("industry")
        or (row.get("limit_pool") or {}).get("industry")
    )
    acceleration_date = _text(replay.get("acceleration_date"))
    if chain and node and acceleration_date:
        return f"{chain}/{node}|{acceleration_date}"
    if chain and acceleration_date:
        return f"{chain}|{acceleration_date}"
    if industry and acceleration_date:
        return f"{industry}|{acceleration_date}"
    return industry or acceleration_date


def _chain_linkage_text(row: dict[str, Any], replay: dict[str, Any]) -> str:
    chain = _text(replay.get("chain_name") or row.get("chain_name"))
    node = _text(replay.get("node_name") or row.get("node_name"))
    source = _text(replay.get("chain_source") or row.get("chain_source"))
    evidence_date = _text(replay.get("chain_evidence_date") or row.get("chain_evidence_date"))
    industry = _text(
        replay.get("industry")
        or row.get("industry")
        or row.get("_board")
        or (replay.get("limit_pool") or {}).get("industry")
        or (row.get("limit_pool") or {}).get("industry")
    )
    if chain:
        label = f"{chain}/{node}" if node else chain
        source_text = "、".join(item for item in (source, evidence_date) if item)
        suffix = f"；映射来源={source_text}" if source_text else ""
        return f"归属{label}{suffix}。强弱仍只按当日涨跌、成交额、涨停池和日线回放确认。"
    if industry:
        return f"涨停池行业={industry}；缺少产业链节点映射，强弱只按当日证据确认。"
    return "缺少产业链/板块映射；当前只列为日度强势样本。"


def _is_failed_limit_candidate(row: dict[str, Any], replay: dict[str, Any]) -> bool:
    limit_pool = row.get("limit_pool") if isinstance(row.get("limit_pool"), dict) else {}
    replay_pool = replay.get("limit_pool") if isinstance(replay.get("limit_pool"), dict) else {}
    pools = [
        _text(limit_pool.get("pool")),
        _text(replay_pool.get("pool")),
        *[_text(item) for item in (limit_pool.get("pools") or [])],
        *[_text(item) for item in (replay_pool.get("pools") or [])],
    ]
    return any(pool in {"failed_limit", "zbgc", "炸板"} for pool in pools)


def _trend_candidates(
    rows: list[dict[str, Any]],
    fallback: list[dict[str, Any]],
    daily_replays: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    scored: list[tuple[float, dict[str, Any], dict[str, Any], str, bool]] = []
    filtered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = _stock_key(row)
        if not key or key in seen or _is_unsuitable_note_stock(row):
            continue
        if (_float(row.get("amount_yi")) or 0.0) < 5.0:
            continue
        if (_float(row.get("change_pct")) or 0.0) < 9.0:
            continue
        seen.add(key)
        replay = _stock_daily_replay_for(row, daily_replays)
        if replay and _as_list(replay.get("rows")) and (_float(replay.get("total_change_pct")) or 0.0) >= 5.0:
            scored.append(
                (
                    _trend_review_score(row, replay),
                    row,
                    replay,
                    _trend_group_label(row, replay),
                    _is_failed_limit_candidate(row, replay),
                )
            )
        filtered.append(row)
    if scored:
        scored.sort(
            key=lambda item: (
                item[0],
                _float(item[1].get("amount_yi")) or 0.0,
                _float(item[1].get("change_pct")) or 0.0,
            ),
            reverse=True,
        )
        selected: list[dict[str, Any]] = []
        selected_keys: set[str] = set()
        used_groups: set[str] = set()
        primary = [item for item in scored if not item[4]] or scored
        for _, row, _replay, group, _failed in primary:
            key = _stock_key(row)
            if not key or key in selected_keys:
                continue
            if group and group in used_groups:
                continue
            selected.append(row)
            selected_keys.add(key)
            if group:
                used_groups.add(group)
            if len(selected) >= 5:
                return selected
        for _, row, _replay, _group, _failed in primary:
            key = _stock_key(row)
            if not key or key in selected_keys:
                continue
            selected.append(row)
            selected_keys.add(key)
            if len(selected) >= 5:
                return selected
        return selected
    if filtered:
        filtered.sort(
            key=lambda item: (
                _float(item.get("amount_yi")) or 0.0,
                _float(item.get("change_pct")) or 0.0,
            ),
            reverse=True,
        )
        return filtered[:5]
    fallback_rows: list[dict[str, Any]] = []
    for row in fallback:
        key = _stock_key(row)
        if key and key not in seen and not _is_unsuitable_note_stock(row):
            seen.add(key)
            fallback_rows.append(row)
    return fallback_rows[:5]


def _key_stock_pool(market_replay: dict[str, Any]) -> dict[str, Any]:
    structured = market_replay.get("structured_daily_review") if isinstance(market_replay.get("structured_daily_review"), dict) else {}
    pool = structured.get("key_stock_pool") if isinstance(structured.get("key_stock_pool"), dict) else {}
    return pool


def _flow_line(market_replay: dict[str, Any]) -> str:
    flow = market_replay.get("flow_availability") if isinstance(market_replay.get("flow_availability"), dict) else {}
    participant = bool(flow.get("participant_flow_available"))
    order_size = bool(flow.get("order_size_flow_available"))
    if participant:
        return "账户级参与者资金：available，可引用对应集合。"
    if order_size:
        sources = ", ".join(flow.get("order_size_sources") or []) or "Eastmoney/THS"
        return f"账户级参与者资金：missing；可用 {sources} 大中小单订单口径，但不能据此推断账户身份。"
    return "账户级参与者资金：missing；订单大小资金流也未形成稳定证据。"


def _technical_risk_line(market_replay: dict[str, Any]) -> str:
    technical = market_replay.get("major_index_technical") if isinstance(market_replay.get("major_index_technical"), dict) else {}
    candidates = []
    for row in _as_list(technical.get("rows")):
        rsi6 = _float(row.get("rsi6"))
        bias20 = _float(row.get("bias20_pct"))
        if (rsi6 is not None and rsi6 >= 80) or (bias20 is not None and bias20 >= 10):
            severity = max(rsi6 or 0.0, (bias20 or 0.0) * 5)
            candidates.append((severity, row))
    if not candidates:
        return ""
    _severity, row = max(candidates, key=lambda item: item[0])
    return (
        f"技术面过热需要复核：{_text(row.get('name'))} RSI(6)={_fmt_num(row.get('rsi6'))}、"
        f"MA20乖离{_fmt_pct(row.get('bias20_pct'))}；若继续放量上攻但板块扩散不足，容易触发高位分歧。"
    )


def render_word_style_review(signals_context: dict[str, Any], market_replay: dict[str, Any]) -> str:
    """Render one reader-facing body for both Markdown and Word export.

    Data completeness and model checks remain upstream concerns.  This renderer
    only presents the market story, so a reader never has to decode runtime
    status, source fields, or evidence labels to understand the replay.
    """
    trade_date = _text(market_replay.get("trade_date") or signals_context.get("trade_date"), "unknown")
    date_text, weekday_text = _trade_date_text(trade_date)
    indices = _as_list(market_replay.get("major_indices"))
    breadth = market_replay.get("market_breadth") if isinstance(market_replay.get("market_breadth"), dict) else {}
    strong_boards, weak_boards = _board_rankings(market_replay)
    high_turnover = _as_list(market_replay.get("high_turnover_cores"))
    key_pool = _key_stock_pool(market_replay)
    gainers = _as_list(key_pool.get("gainers_top20"))
    pressure = _pressure_stock(high_turnover)
    strongest = strong_boards[0] if strong_boards else {}
    weakest = weak_boards[0] if weak_boards else {}
    dynamic_stocks = [row for row in _dynamic_representative_stocks(market_replay) if not _is_unsuitable_note_stock(row)]
    selected_stocks = _dedupe_stocks(dynamic_stocks, gainers[:6], high_turnover[:6], limit=6)
    coverage = market_replay.get("coverage") if isinstance(market_replay.get("coverage"), dict) else {}
    formal_ready = coverage.get("formal_ready") is True
    report_stage = _text(market_replay.get("report_stage"), "formal_postmarket")
    report_title = "A股盘后复盘" if report_stage == "formal_postmarket" and formal_ready else "A股午后观察"

    def board_label(row: dict[str, Any], fallback: str) -> str:
        value = _text(row.get("name") or row.get("board"), fallback)
        return fallback if value.lower() in {"unknown", "missing", "partial", "available"} else value

    def reader_text(value: Any, default: str = "-") -> str:
        text = _text(value, default)
        return default if text.lower() in {"unknown", "missing", "partial", "available"} else text

    def stock_reading(row: dict[str, Any]) -> str:
        change = _float(row.get("change_pct"))
        amount = _float(row.get("amount_yi"))
        if change is not None and change >= 5:
            state = "明显走强"
        elif change is not None and change <= -3:
            state = "明显走弱"
        else:
            state = "强弱分化"
        amount_text = f"，成交{_fmt_yi(amount)}亿" if amount is not None else ""
        return state + amount_text

    breadth_parts: list[str] = []
    if _float(breadth.get("up")) is not None and _float(breadth.get("down")) is not None:
        breadth_parts.append(f"上涨{breadth.get('up')}家、下跌{breadth.get('down')}家")
    if _float(breadth.get("limit_like_count")) is not None:
        breadth_parts.append(f"涨停约{breadth.get('limit_like_count')}家")
    turnover_rows = [row for row in indices if _float(row.get("amount_yi")) is not None]
    turnover_text = _turnover_sentence(indices) if turnover_rows else ""
    strongest_name = board_label(strongest, "强势方向")
    weakest_name = board_label(weakest, "弱势方向")
    conclusion_parts = [_index_brief(indices)] if indices else []
    if breadth_parts:
        conclusion_parts.append("，".join(breadth_parts))
    if strong_boards:
        conclusion_parts.append(f"资金集中在{strongest_name}")
    if weak_boards:
        conclusion_parts.append(f"{weakest_name}相对偏弱")
    conclusion = "；".join(part for part in conclusion_parts if part)
    if not conclusion:
        conclusion = "今天盘面信息有限，先以指数、宽度和成交的下一次变化为主。"

    lines: list[str] = [
        f"# {report_title} | {date_text}（{weekday_text}）",
        "",
        "## 今日一句话",
        conclusion + "。",
        "",
        "## 市场全貌",
    ]
    if indices:
        lines.extend(
            _table(
                ["指数", "收盘", "涨跌幅", "成交额(亿)"],
                [
                    [
                        _text(row.get("name")),
                        _fmt_num(row.get("close"), 2) if _float(row.get("close")) is not None else "-",
                        _fmt_pct(row.get("change_pct")) if _float(row.get("change_pct")) is not None else "-",
                        _fmt_yi(row.get("amount_yi")) if _float(row.get("amount_yi")) is not None else "-",
                    ]
                    for row in indices
                ],
            )
        )
    if turnover_text or breadth_parts:
        lines.extend(["", "；".join(part for part in (turnover_text, "，".join(breadth_parts)) if part) + "。"])

    lines.extend(["", "## 主线与资金", "### 走强方向"])
    if strong_boards:
        lines.extend(
            _table(
                ["方向", "涨跌幅", "领涨股", "盘面表现"],
                [
                    [
                        board_label(row, "-"),
                        _fmt_pct(row.get("change_pct")) if _float(row.get("change_pct")) is not None else "-",
                        reader_text(row.get("leader_name")),
                        "位居前排" if index == 0 else "保持活跃",
                    ]
                    for index, row in enumerate(strong_boards[:3])
                ],
            )
        )
    else:
        lines.append("今天没有形成明显的集中进攻方向。")

    lines.extend(["", "### 转弱方向"])
    if weak_boards:
        lines.extend(
            _table(
                ["方向", "涨跌幅", "代表表现", "盘面含义"],
                [
                    [
                        board_label(row, "-"),
                        _fmt_pct(row.get("change_pct")) if _float(row.get("change_pct")) is not None else "-",
                        reader_text(row.get("leader_name")),
                        "资金回撤" if index == 0 else "仍在承压",
                    ]
                    for index, row in enumerate(weak_boards[:3])
                ],
            )
        )
    else:
        lines.append("弱势方向没有形成集中抛压。")

    mainline_sentence = (
        f"主线判断：{strongest_name}是今天最有辨识度的方向；是否能延续，要看领涨股和板块内部能否同步走强。"
        if strong_boards
        else "主线判断：市场仍以轮动为主，暂未出现清晰的集中方向。"
    )
    lines.extend(["", mainline_sentence, "", "## 代表信号"])
    if selected_stocks:
        lines.extend(
            _table(
                ["个股", "归属方向", "涨跌幅", "成交额(亿)", "盘面表现"],
                [
                    [
                        reader_text(_stock_display(row)),
                        reader_text(row.get("_board")),
                        _fmt_pct(row.get("change_pct")) if _float(row.get("change_pct")) is not None else "-",
                        _fmt_yi(row.get("amount_yi")) if _float(row.get("amount_yi")) is not None else "-",
                        stock_reading(row),
                    ]
                    for row in selected_stocks
                ],
            )
        )
    else:
        lines.append("今天没有需要单独拎出的代表个股。")

    pressure_name = _text(pressure.get("name"), "高成交核心") if pressure else "高成交核心"
    lines.extend(
        [
            "",
            "## 明天看什么",
            *_table(
                ["观察对象", "偏强时的表现", "偏弱时的表现"],
                [
                    [strongest_name, "继续位居前排，领涨股带动板块扩散", "只剩少数个股走强，板块跟随减少"],
                    [pressure_name, "成交稳定，收盘位置改善", "放量回落，并拖累同方向个股"],
                    ["指数与宽度", "指数企稳，红盘家数同步回升", "指数走弱，或红盘家数继续收缩"],
                ],
            ),
        ]
    )
    return "\n".join(lines).strip()
