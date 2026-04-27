from datetime import date

import pandas as pd


def _bars():
    index = pd.date_range("2026-01-01", periods=80, freq="D")
    return pd.DataFrame(
        {
            "open": range(80, 160),
            "high": range(81, 161),
            "low": range(79, 159),
            "close": range(80, 160),
            "vol": [1000] * 80,
            "amount": [10000] * 80,
        },
        index=index,
    )


def test_watchlist_range_columns_include_all_key_presets():
    from signals.web.api import workbench

    columns = workbench._watchlist_range_columns(date(2026, 4, 26))
    keys = [item["key"] for item in columns]

    assert {"ytd", "1w", "1m", "3m", "924", "deepseek", "tariff"} <= set(keys)
    assert len(columns) > 4


def test_macro_indices_have_day_and_range_returns(monkeypatch):
    from signals.web.api import workbench

    df = _bars()
    monkeypatch.setattr(workbench, "_index_df", lambda symbol, freq: (df, "test_index_bars"))

    columns = workbench._watchlist_range_columns(date(2026, 4, 26))
    rows = workbench._build_macro_index_rows(reports=[], range_columns=columns)

    assert rows
    assert rows[0]["kind"] == "index"
    assert rows[0]["target_kind"] == "index"
    assert rows[0]["day_change_pct"] is not None
    assert rows[0]["daily_change_pct"] is not None
    assert rows[0]["latest_signal"]
    assert rows[0]["range_returns"]
    assert "theme_tags" in rows[0]


def test_focus_stocks_aggregate_buy_points_by_timeframe(monkeypatch):
    from signals.web.api import workbench

    df = _bars()
    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (df, "test_bars"))
    monkeypatch.setattr(workbench, "_load_signal_pool_rows", lambda: [
        {"symbol": "SZ.002759", "signal_type": "30分钟买点", "freq": "30分钟", "score": 75},
        {"symbol": "SZ.002759", "signal_type": "15分钟买点", "freq": "15min", "score": 72},
        {"symbol": "SZ.002759", "signal_type": "5分钟买点", "freq": "5min", "score": 69},
    ])

    rows = workbench._build_focus_stock_rows(
        buy_rows=[
            {
                "symbol": "SZ.002759",
                "name": "天际股份",
                "reason": "日线候选: 买点",
                "score": 80,
                "metadata": {"freq": "日线"},
            }
        ],
        sell_rows=[],
        decision_rows=[],
        range_columns=workbench._watchlist_range_columns(date(2026, 4, 26)),
    )

    badges = [item["badge"] for item in rows[0]["buy_timeframes"]]
    assert badges == ["D", "30m", "15m", "5m"]


def test_focus_stocks_keep_sell_warning_timeframes(monkeypatch):
    from signals.web.api import workbench

    df = _bars()
    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (df, "test_bars"))
    monkeypatch.setattr(workbench, "_load_signal_pool_rows", lambda: [])

    rows = workbench._build_focus_stock_rows(
        buy_rows=[],
        sell_rows=[
            {
                "symbol": "SZ.002759",
                "name": "天际股份",
                "reason": "日线预警: 跌破二十日均线",
                "score": 88,
                "metadata": {"freq": "日线"},
            }
        ],
        decision_rows=[],
        range_columns=workbench._watchlist_range_columns(date(2026, 4, 26)),
    )

    assert rows[0]["action_status"] == "exit_review"
    assert [item["badge"] for item in rows[0]["sell_timeframes"]] == ["D"]
    assert rows[0]["latest_signal"] == "卖D"


def test_concept_sector_preview_returns_explicit_chain_carrier(monkeypatch):
    from signals.web.api import workbench

    df = _bars()
    monkeypatch.setattr(workbench, "_concept_theme_candidates", lambda name: [])
    monkeypatch.setattr(workbench, "_concept_rank_rows", lambda name, themes: [])
    monkeypatch.setattr(workbench, "_industry_constituent_symbols", lambda name: [])
    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (df, "test_carrier_bars"))

    row = workbench._sector_board_preview({"label": "电解液", "change_pct": 2.2}, "concept")

    assert row["target_kind"] == "concept"
    assert row["fallback_target"]["symbol"] == "SZ.002709"
    assert row["mapping_chain"]["node_id"] == "electrolyte"
    assert row["latest_price"] is None
    assert row["range_returns"] == {}
    assert row["range_return_status"] == "board_kline_missing"
    assert row["carrier_latest_price"] is not None
    assert row["carrier_range_returns"]
    assert row["carrier_range_return_source"] == "carrier_stock"
    assert row["lane"] == "board_lane"
    assert row["second_screen_role"] == "hot_sector_explanation"
    assert "承接" in row["explanation"]


def test_trader_task_queue_is_action_oriented():
    from signals.web.api import workbench

    tasks = workbench._build_trader_task_queue(
        decision_rows=[],
        focus_stocks=[
            {
                "symbol": "SZ.002709",
                "name": "天赐材料",
                "trader_action": "可试仓",
                "latest_signal": "5m",
                "reason": "5m买点确认",
                "invalidates_when": "跌破短线防守位",
            }
        ],
        sector_boards=[
            {
                "label": "电解液",
                "domain": "concept",
                "explanation": "电解液 异动 · 承接 天赐材料",
                "fallback_target": {"kind": "stock", "symbol": "SZ.002709"},
            }
        ],
    )

    assert tasks
    assert tasks[0]["action_label"] == "可试仓"
    assert tasks[0]["chart_target"]["kind"] == "stock"
    assert tasks[0]["invalidates_when"]
