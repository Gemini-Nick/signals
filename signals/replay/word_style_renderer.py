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
            ["指数", "低点时间", "低点", "高点时间", "收盘", "低点后修复"],
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
    bucket_labels = [
        ("market_core", "主线容量/动态核心"),
        ("market_elastic_confirmed", "封板弹性"),
        ("market_elastic", "强势弹性"),
        ("failed_emotion", "失败对照"),
        ("pressure_core", "压力核心"),
    ]
    for board in _as_list(market_replay.get("dynamic_market_representatives")):
        board_name = _text(board.get("board") or board.get("driver_name"))
        for bucket, label in bucket_labels:
            for row in _as_list(board.get(bucket))[:1]:
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
        return "账户级主力/散户资金：available，可引用对应集合。"
    if order_size:
        sources = ", ".join(flow.get("order_size_sources") or []) or "Eastmoney/THS"
        return f"账户级主力/散户资金：missing；可用 {sources} 大中小单订单口径，不等同主力/散户账户拆分。"
    return "账户级主力/散户资金：missing；订单大小资金流也未形成稳定证据。"


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
    """Return a long Word-style report body without notification gate."""
    trade_date = _text(market_replay.get("trade_date") or signals_context.get("trade_date"), "unknown")
    date_text, weekday_text = _trade_date_text(trade_date)
    short_date = _short_date(trade_date)
    statuses = _status_map(market_replay)
    indices = _as_list(market_replay.get("major_indices"))
    breadth = market_replay.get("market_breadth") if isinstance(market_replay.get("market_breadth"), dict) else {}
    strong_boards, weak_boards = _board_rankings(market_replay)
    trend_map = _trend_board_map(market_replay)
    high_turnover = _as_list(market_replay.get("high_turnover_cores"))
    key_pool = _key_stock_pool(market_replay)
    gainers = _as_list(key_pool.get("gainers_top20"))
    dynamic_stocks = [row for row in _dynamic_representative_stocks(market_replay) if not _is_unsuitable_note_stock(row)]
    dynamic_failed = [
        row
        for row in dynamic_stocks
        if _text(row.get("_word_type")) in {"失败对照", "压力核心"}
        and (_float(row.get("amount_yi")) or 0.0) >= 1.0
    ]
    events = _event_chain_map(market_replay)
    daily_replays = _stock_daily_replay_map(market_replay)
    pressure = _pressure_stock(high_turnover)
    strongest = strong_boards[0] if strong_boards else {}
    weakest = weak_boards[0] if weak_boards else {}
    technical_risk = _technical_risk_line(market_replay)

    pressure_text = (
        f"{_text(pressure.get('name'))}成交{_fmt_yi(pressure.get('amount_yi'))}亿、涨跌{_fmt_pct(pressure.get('change_pct'))}"
        if pressure
        else "高成交压力锚 unknown"
    )
    breadth_text = (
        f"上涨{breadth.get('up')}家、下跌{breadth.get('down')}家、近似涨停{breadth.get('limit_like_count')}家"
        if breadth.get("status") == "available"
        else "涨跌家数 unknown"
    )
    board_minute_status = _status_text(statuses, "板块分钟线")
    turnover_text = _turnover_sentence(indices)
    market_state_text = _market_state_sentence(indices, breadth)

    lines: list[str] = [
        f"{short_date}A股盘后复盘报告（Signals组装版）",
        f"A股盘后复盘报告 | {date_text}（{weekday_text}）",
        "",
        "昨日观察点校准",
        *_calibration_lines(market_replay),
        "",
        "核心结论",
        (
            f"{short_date}复盘结论：{_index_brief(indices)}；日度板块最强为{_board_name(strongest)}"
            f"{_fmt_pct(strongest.get('change_pct'))}，弱项为{_board_name(weakest)}{_fmt_pct(weakest.get('change_pct'))}。"
            f"{turnover_text}，全市场宽度为{breadth_text}，高成交压力中心看{pressure_text}。"
            f"数据边界上，{board_minute_status}，因此本报告保留 Word 式层级和表格，但不硬写分钟级卡位时间。"
        ),
        "",
        "一、市场整体状态",
        "1. 指数表现",
    ]
    lines.extend(
        _table(
            ["指数", "收盘", "涨跌幅", "成交额(亿)", "较前日成交", "日内振幅"],
            [
                [
                    _text(row.get("name")),
                    _fmt_num(row.get("close")),
                    _fmt_pct(row.get("change_pct")),
                    _fmt_yi(row.get("amount_yi")),
                    _fmt_pct(row.get("amount_change_pct")),
                    _fmt_pct(row.get("amplitude_pct")),
                ]
                for row in indices
            ],
        )
    )
    lines.extend(
        [
            "",
            f"{turnover_text}。{market_state_text}",
            "",
            "2. 分时结构",
            f"- 指数分钟线：{_status_text(statuses, '指数分钟线')}。",
            f"- 板块分钟线：{board_minute_status}。",
            *_index_intraday_lines(market_replay),
            "- 可确认部分：本地指数日线、指数5分钟线和个股日线可确认开高低收、涨跌幅、成交额；板块资金切换节点仍以板块分钟线状态为准。",
            "",
            "3. 技术面",
        ]
    )
    cycle = market_replay.get("index_cycle") if isinstance(market_replay.get("index_cycle"), dict) else {}
    lines.extend(_technical_lines(market_replay, cycle))

    limit_counts = key_pool.get("limit_pool_counts") if isinstance(key_pool.get("limit_pool_counts"), dict) else {}
    limit_up_count = key_pool.get("limit_up_count")
    failed_limit_count = key_pool.get("failed_limit_count")
    limit_down_count = key_pool.get("limit_down_count")
    linked_limit_count = key_pool.get("linked_limit_count")
    seal_success_rate = key_pool.get("seal_success_rate_pct")
    exact_pool_level = "confirmed" if limit_counts else "unknown"
    lines.extend(
        [
            "",
            "二、市场情绪",
        ]
    )
    lines.extend(
        _table(
            ["指标", "数值", "口径", "证据等级"],
            [
                ["上涨家数", _text(breadth.get("up"), "unknown"), "fullmarket_spot_snapshots", _text(breadth.get("evidence_level"), "unknown")],
                ["下跌家数", _text(breadth.get("down"), "unknown"), "fullmarket_spot_snapshots", _text(breadth.get("evidence_level"), "unknown")],
                ["涨停(精确)", _text(limit_up_count, "unknown"), "market_limit_pools", exact_pool_level],
                ["炸板(精确)", _text(failed_limit_count, "unknown"), "market_limit_pools", exact_pool_level],
                ["跌停(精确)", _text(limit_down_count, "unknown"), "market_limit_pools", exact_pool_level],
                ["连板>=2", _text(linked_limit_count, "unknown"), "market_limit_pools.consecutive_limit_count", exact_pool_level],
                ["封板率", f"{_fmt_num(seal_success_rate)}%" if seal_success_rate is not None else "unknown", "limit_up/(limit_up+failed_limit)", exact_pool_level],
                ["近似涨停", _text(breadth.get("limit_like_count"), "unknown"), "日线涨跌幅阈值近似", "inferred"],
                ["近似跌停", _text(breadth.get("down_limit_like_count"), "unknown"), "日线涨跌幅阈值近似", "inferred"],
                ["账户级资金流", "missing" if not market_replay.get("flow_availability", {}).get("participant_flow_available") else "available", "flow_availability", "unknown"],
            ],
        )
    )
    pool_note = (
        "market_limit_pools 已提供精确涨停/炸板/跌停口径。"
        if exact_pool_level == "confirmed"
        else "market_limit_pools 缺失或样本不足时，封板率、连板高度和炸板率不能 confirmed；不从 Word 样本回填。"
    )
    lines.extend(
        [
            "",
            f"情绪拆解：{_flow_line(market_replay)} {pool_note}",
            "",
            "三、板块深度拆解",
            "1. 今日最强板块 TOP10",
        ]
    )
    lines.extend(
        _table(
            ["排名", "方向", "类型", "涨跌幅", "成交额/换手", "5日", "20日", "领涨", "证据"],
            [
                [
                    str(idx),
                    _text(row.get("name")),
                    _text(row.get("kind"), "unknown"),
                    _fmt_pct(row.get("change_pct")),
                    f"{_fmt_yi(row.get('amount_yi'))}亿 / {_fmt_pct(row.get('turnover_pct'))}",
                    _fmt_pct(_trend_for_board(row, trend_map).get("change_5d_pct")),
                    _fmt_pct(_trend_for_board(row, trend_map).get("change_20d_pct")),
                    f"{_text(row.get('leader_name'), 'unknown')} {_fmt_pct(row.get('leader_change_pct'))}",
                    _text(row.get("source"), "unknown"),
                ]
                for idx, row in enumerate(strong_boards[:10], start=1)
            ],
        )
    )
    lines.extend(["", "2. 今日弱项/撤退方向"])
    lines.extend(
        _table(
            ["排名", "方向", "类型", "涨跌幅", "领涨/抗跌", "证据"],
            [
                [
                    str(idx),
                    _text(row.get("name")),
                    _text(row.get("kind"), "unknown"),
                    _fmt_pct(row.get("change_pct")),
                    _text(row.get("leader_name"), "unknown"),
                    _text(row.get("source"), "unknown"),
                ]
                for idx, row in enumerate(weak_boards[:8], start=1)
            ],
        )
    )
    second = strong_boards[1] if len(strong_boards) > 1 else {}
    third = strong_boards[2] if len(strong_boards) > 2 else {}
    strongest_trend = _trend_for_board(strongest, trend_map)
    trend_note = (
        f"5日{_fmt_pct(strongest_trend.get('change_5d_pct'))}、20日{_fmt_pct(strongest_trend.get('change_20d_pct'))}"
        if strongest_trend
        else "5日/20日趋势 unknown"
    )
    lines.extend(
        [
            "",
            f"主线观察：{_board_name(strongest)}是日度最强方向，{trend_note}，证据来自{_text(strongest.get('source'), 'unknown')}；分钟级启动和卡位时点仍为 unknown。",
            f"候选主线：{_board_name(second)}排名靠前，但需要次日继续看成交额、上涨家数和领涨股扩散。",
            f"补涨/轮动：{_board_name(third)}只按日度强度列观察，不能替代板块分钟线。",
            f"撤退线：{_board_name(weakest)}在日度排行靠后，若次日仍弱于全市场宽度，则继续作为风险方向处理。",
            *_direction_candidate_lines(strong_boards, weak_boards, trend_map),
            "",
            "四、个股精选",
        ]
    )
    comparable_gainers = [row for row in gainers if not _is_unsuitable_note_stock(row)]
    selected_stocks = _dedupe_stocks(dynamic_stocks, comparable_gainers[:12], high_turnover[:8], limit=20)
    lines.extend(
        _table(
            ["类型", "个股", "涨跌幅", "成交额(亿)", "日内结构", "明日观察"],
            [
                [
                    _text(row.get("_word_type")) or ("高成交核心" if row in high_turnover[:8] else "强势/异动"),
                    _stock_display(row),
                    _fmt_pct(row.get("change_pct")),
                    _fmt_yi(row.get("amount_yi")),
                    _stock_structure(row, events),
                    "看高成交是否继续承接；若放量回落则降级为压力锚。",
                ]
                for row in selected_stocks
            ],
        )
    )
    lines.extend(
        [
            "",
            "五、强趋势股启动回溯",
        ]
    )
    trend_rows = _trend_candidates(dynamic_stocks + gainers, high_turnover[:3], daily_replays)
    if not trend_rows:
        lines.append("强趋势样本：unknown，缺少可用涨幅/成交额样本。")
    trend_keys = {_stock_key(item) for item in trend_rows if _stock_key(item)}
    for row in trend_rows:
        name = _stock_display(row)
        replay = _stock_daily_replay_for(row, daily_replays)
        failed_samples = _failed_samples_for_trend(row, replay, daily_replays, dynamic_failed, exclude_keys=trend_keys)
        acceleration_text = (
            f"{_text(replay.get('acceleration_date'))}，{_text(replay.get('acceleration_event'))}"
            if replay
            else "unknown"
        )
        replay_summary = (
            f"{_text(replay.get('start_date'))}至{_text(replay.get('end_date'))}累计{_fmt_pct(replay.get('total_change_pct'))}；"
            f"成交额口径={_text(replay.get('amount_status'), 'unknown')}"
            if replay
            else "日线回放 unknown"
        )
        lines.extend(
            [
                f"### {name}",
                *_table(
                    ["环节", "复盘内容"],
                    [
                        ["启动识别", f"当日涨跌幅{_fmt_pct(row.get('change_pct'))}、成交{_fmt_yi(row.get('amount_yi'))}亿；日线加速点：{acceleration_text}。"],
                        ["板块联动", _chain_linkage_text(row, replay)],
                        ["区间表现", replay_summary],
                        ["当日回放", _stock_structure(row, events)],
                        ["交易复核", "不输出买入/卖出/目标/止损；只转换为次日验证条件。"],
                        ["成功/失败对照", _failed_sample_text(failed_samples)],
                    ],
                ),
                *_daily_replay_lines(replay),
                "",
            ]
        )

    chain_pressure_pool = _chain_pressure_pool(trend_rows, daily_replays)
    if chain_pressure_pool:
        lines.extend(
            [
                "同链高成交弱化样本池",
                *_table(
                    ["样本", "产业链/节点", "涨跌幅", "成交额(亿)", "弱化证据", "复盘用途"],
                    [
                        [
                            _stock_display(row),
                            "/".join(item for item in (_text(row.get("chain_name")), _text(row.get("node_name"))) if item) or "unknown",
                            _fmt_pct(row.get("change_pct")),
                            _fmt_yi(row.get("amount_yi")),
                            "、".join(_text(item) for item in row.get("_weakness_reasons", []) if _text(item)),
                            "只作为同链承接/分化观察，不替代强趋势样本。",
                        ]
                        for row in chain_pressure_pool
                    ],
                ),
                "",
            ]
        )

    lines.extend(
        [
            "六、风险提示",
            "1. 板块分钟线缺失时，不能还原 Word 中那类精确资金切换时间，只能保留为 unknown。",
            "2. market_limit_pools 缺失或样本不足时，封板率、连板高度、炸板率不能 confirmed。",
            f"3. 高成交压力需要重点复核：{pressure_text}；若继续放量回落，会压制同方向扩散。",
            f"4. {technical_risk}" if technical_risk else "4. 技术面风险：major_index_technical 未显示明显过热指数，继续观察指数与个股宽度是否背离。",
            "5. 账户级主力/散户资金缺失时，Eastmoney/THS 大中小单只能作为订单大小口径，不能写成主力/散户精确买卖。",
            "6. 日度板块排行只能证明收盘强弱，不能单独证明盘中主线胜出。",
            "",
            "七、明日观察清单",
        ]
    )
    lines.extend(
        _table(
            ["观察对象", "关键指标", "走强条件", "走弱/否定条件"],
            [
                [
                    _board_name(strongest),
                    "排行、上涨家数、领涨股",
                    "继续位居前排且领涨股不回落",
                    "掉出前排或领涨股高开低走",
                ],
                [
                    _text(pressure.get("name")) if pressure else "高成交核心",
                    "成交额、收盘位置、高点回撤",
                    "缩量稳住或放量收高",
                    "继续放量回落并带弱同方向",
                ],
                [
                    "四大指数",
                    "上证/创业板/科创相对强弱",
                    "指数不拖累且强方向扩散",
                    "指数回落同时强方向缩容",
                ],
                [
                    "数据补齐",
                    "board_heat_ticks / market_limit_pools",
                    "分钟和封板池恢复后再提升结论等级",
                    "仍缺失则继续以日线证据降级输出",
                ],
            ],
        )
    )
    lines.extend(
        [
            "",
            f"数据来源：Signals Mongo / MCP evidence package | {date_text}收盘 | Word 参考仅用于结构与评估，不作为正文数据源",
        ]
    )
    return "\n".join(lines).strip()
