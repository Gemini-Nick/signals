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
        return "本板块无有效失败对比样本。"
    row = failed_samples[0]
    return (
        f"失败/弱化样本：{_stock_display(row)}，涨跌{_fmt_pct(row.get('change_pct'))}、"
        f"成交{_fmt_yi(row.get('amount_yi'))}亿；弱点在收盘承接或封板质量不如成功样本。"
    )


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


def _trend_candidates(rows: list[dict[str, Any]], fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        filtered.append(row)
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


def render_word_style_review(signals_context: dict[str, Any], market_replay: dict[str, Any]) -> str:
    """Return a long Word-style report body without notification gate."""
    trade_date = _text(market_replay.get("trade_date") or signals_context.get("trade_date"), "unknown")
    date_text, weekday_text = _trade_date_text(trade_date)
    short_date = _short_date(trade_date)
    statuses = _status_map(market_replay)
    indices = _as_list(market_replay.get("major_indices"))
    breadth = market_replay.get("market_breadth") if isinstance(market_replay.get("market_breadth"), dict) else {}
    strong_boards, weak_boards = _board_rankings(market_replay)
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
    pressure = _pressure_stock(high_turnover)
    strongest = strong_boards[0] if strong_boards else {}
    weakest = weak_boards[0] if weak_boards else {}

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
            "- 可确认部分：本地指数日线和个股日线可确认开高低收、涨跌幅、成交额；精确到 09:30-15:00 的资金切换节点为 unknown。",
            "",
            "3. 技术面",
        ]
    )
    cycle = market_replay.get("index_cycle") if isinstance(market_replay.get("index_cycle"), dict) else {}
    lines.extend(
        _table(
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
    )

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
            ["排名", "方向", "类型", "涨跌幅", "成交额/换手", "领涨", "证据"],
            [
                [
                    str(idx),
                    _text(row.get("name")),
                    _text(row.get("kind"), "unknown"),
                    _fmt_pct(row.get("change_pct")),
                    f"{_fmt_yi(row.get('amount_yi'))}亿 / {_fmt_pct(row.get('turnover_pct'))}",
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
    lines.extend(
        [
            "",
            f"主线观察：{_board_name(strongest)}是日度最强方向，证据来自{_text(strongest.get('source'), 'unknown')}；分钟级启动和卡位时点仍为 unknown。",
            f"候选主线：{_board_name(second)}排名靠前，但需要次日继续看成交额、上涨家数和领涨股扩散。",
            f"补涨/轮动：{_board_name(third)}只按日度强度列观察，不能替代板块分钟线。",
            f"撤退线：{_board_name(weakest)}在日度排行靠后，若次日仍弱于全市场宽度，则继续作为风险方向处理。",
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
    trend_rows = _trend_candidates(dynamic_stocks + gainers, high_turnover[:3])
    if not trend_rows:
        lines.append("强趋势样本：unknown，缺少可用涨幅/成交额样本。")
    for row in trend_rows:
        name = _stock_display(row)
        lines.extend(
            [
                f"### {name}",
                *_table(
                    ["环节", "复盘内容"],
                    [
                        ["启动识别", f"当日涨跌幅{_fmt_pct(row.get('change_pct'))}、成交{_fmt_yi(row.get('amount_yi'))}亿；精确启动时间 unknown。"],
                        ["板块联动", "需要 board_heat_ticks 或 constituents 映射确认；当前只列为日度强势样本。"],
                        ["当日回放", _stock_structure(row, events)],
                        ["交易复核", "不输出买入/卖出/目标/止损；只转换为次日验证条件。"],
                        ["成功/失败对照", _failed_sample_text(dynamic_failed)],
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
            "4. 账户级主力/散户资金缺失时，Eastmoney/THS 大中小单只能作为订单大小口径，不能写成主力/散户精确买卖。",
            "5. 日度板块排行只能证明收盘强弱，不能单独证明盘中主线胜出。",
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
