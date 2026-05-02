import asyncio
from datetime import date, datetime

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


def _intraday_bars():
    index = pd.to_datetime([
        "2026-03-15 10:00",
        "2026-03-15 10:30",
        "2026-03-15 11:00",
        "2026-03-16 10:00",
    ])
    return pd.DataFrame(
        {
            "open": [10.0, 10.4, 10.8, 11.0],
            "high": [10.5, 10.9, 11.2, 11.4],
            "low": [9.8, 10.2, 10.6, 10.9],
            "close": [10.3, 10.7, 11.0, 11.2],
            "vol": [1000] * 4,
            "amount": [10000] * 4,
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
    assert rows[0]["target_freq"] == "30min"
    assert "5min" in rows[0]["available_freqs"]
    assert rows[0]["latest_price"] is not None
    assert rows[0]["day_change_pct"] is None
    assert rows[0]["daily_change_pct"] is None
    assert rows[0]["latest_signal"]
    assert rows[0]["range_returns"]
    assert "theme_tags" in rows[0]


def test_static_index_alias_resolves_shanghai_composite():
    from signals.web.api import workbench

    assert workbench._resolve_static_index("上证综指") == ("上证指数", "sh000001")
    assert workbench._resolve_static_index("SH.000001") == ("上证指数", "sh000001")
    assert workbench._resolve_static_index("科创综指") == ("科创综指", "sh000680")
    assert workbench._resolve_static_index("sz399986") == ("中证银行", "sz399986")
    assert workbench._resolve_static_index("标普500") == ("标普500", "US.SPY")


def test_chart_merges_custom_signal_pool_rows(monkeypatch):
    from signals.web.api import workbench

    df = _bars()
    chart = workbench._chart_from_df(df, symbol="SZ.002759", freq="daily", source="test_bars")
    monkeypatch.setattr(workbench, "_load_signal_pool_rows", lambda limit=200, symbol=None: [
        {
            "symbol": "SZ.002759",
            "signal_date": "2026-03-15",
            "signal_type": "自定义三买: MACD 0轴上方确认",
            "freq": "日线",
            "confidence": 0.88,
            "price": 120.5,
            "source": "sqlite.backtest.signal_records",
        }
    ])

    merged = workbench._merge_signal_pool_into_chart(chart, "SZ.002759", "daily")

    assert merged["signals"]
    assert merged["signals"][-1]["type"] == "自定义三买: MACD 0轴上方确认"
    assert merged["signals"][-1]["source"] == "sqlite.backtest.signal_records"


def test_intraday_chart_aligns_date_only_custom_signal_to_bar(monkeypatch):
    from signals.web.api import workbench

    df = _intraday_bars()
    chart = workbench._chart_from_df(df, symbol="SZ.002759", freq="30min", source="test_bars")
    last_same_day = chart["ohlcv"][2]
    monkeypatch.setattr(workbench, "_load_signal_pool_rows", lambda limit=200, symbol=None: [
        {
            "symbol": "SZ.002759",
            "signal_date": "2026-03-15",
            "signal_type": "缺口买:突破",
            "freq": "30分钟",
            "confidence": 0.88,
            "source": "sqlite.backtest.signal_records",
        }
    ])

    merged = workbench._merge_signal_pool_into_chart(chart, "SZ.002759", "30min")

    assert merged["signals"][-1]["type"] == "缺口买:突破"
    assert merged["signals"][-1]["dt"] == last_same_day["time"]
    assert merged["signals"][-1]["chart_aligned"] is True
    assert merged["signals"][-1]["price"] == last_same_day["low"]


def test_chart_adds_volume_expansion_signal(monkeypatch):
    from signals.web.api import workbench

    index = pd.date_range("2026-03-01 10:00", periods=26, freq="30min")
    df = pd.DataFrame(
        {
            "open": [10.0] * 26,
            "high": [10.5] * 25 + [11.0],
            "low": [9.8] * 26,
            "close": [10.1] * 25 + [10.8],
            "vol": [1_000_000] * 25 + [2_600_000],
            "amount": [10_000_000] * 25 + [31_000_000],
        },
        index=index,
    )
    chart = workbench._chart_from_df(df, symbol="SZ.002709", freq="30min", source="test_bars")
    monkeypatch.setattr(workbench, "_load_signal_pool_rows", lambda limit=200, symbol=None: [])
    monkeypatch.setattr(workbench, "_load_terminal_technical_signal_rows", lambda symbol, limit=300: [])

    merged = workbench._merge_signal_pool_into_chart(chart, "SZ.002709", "30min")

    signal = merged["signals"][-1]
    assert signal["type"] == "放量突破"
    assert "量比" in signal["details"]
    assert signal["source"] == "terminal_volume_price_anomalies"
    assert signal["render_pane"] == "volume"


def test_chart_adds_extreme_volume_contraction_signal(monkeypatch):
    from signals.web.api import workbench

    index = pd.date_range("2026-03-01 10:00", periods=26, freq="30min")
    df = pd.DataFrame(
        {
            "open": [10.0] * 26,
            "high": [10.5] * 26,
            "low": [9.8] * 26,
            "close": [10.1] * 23 + [10.08, 10.05, 10.02],
            "vol": [1_000_000] * 23 + [300_000, 280_000, 260_000],
            "amount": [10_000_000] * 23 + [3_000_000, 2_800_000, 2_600_000],
        },
        index=index,
    )
    chart = workbench._chart_from_df(df, symbol="SZ.002709", freq="30min", source="test_bars")
    monkeypatch.setattr(workbench, "_load_signal_pool_rows", lambda limit=200, symbol=None: [])
    monkeypatch.setattr(workbench, "_load_terminal_technical_signal_rows", lambda symbol, limit=300: [])

    merged = workbench._merge_signal_pool_into_chart(chart, "SZ.002709", "30min")

    signal = merged["signals"][-1]
    assert signal["type"] == "缩量回踩"
    assert "量比" in signal["details"]
    assert signal["source"] == "terminal_volume_price_anomalies"
    assert signal["render_pane"] == "volume"


def test_weekly_chart_aligns_custom_signal_to_containing_week(monkeypatch):
    from signals.web.api import workbench

    df = pd.DataFrame(
        {
            "open": [10.0, 11.0],
            "high": [12.0, 13.0],
            "low": [9.0, 10.5],
            "close": [11.0, 12.5],
            "vol": [1000, 1200],
            "amount": [10000, 12000],
        },
        index=pd.to_datetime(["2026-03-13", "2026-03-20"]),
    )
    chart = workbench._chart_from_df(df, symbol="SZ.002759", freq="weekly", source="test_bars")
    target_week = chart["ohlcv"][1]
    monkeypatch.setattr(workbench, "_load_signal_pool_rows", lambda limit=200, symbol=None: [
        {
            "symbol": "SZ.002759",
            "signal_date": "2026-03-18",
            "signal_type": "自定义三买",
            "freq": "周线",
            "confidence": 0.88,
            "source": "sqlite.backtest.signal_records",
        }
    ])
    monkeypatch.setattr(workbench, "_load_terminal_technical_signal_rows", lambda symbol, limit=300: [])

    merged = workbench._merge_signal_pool_into_chart(chart, "SZ.002759", "weekly")

    assert merged["signals"][-1]["dt"] == target_week["time"]
    assert merged["signals"][-1]["chart_aligned"] is True
    assert merged["signals"][-1]["price"] == target_week["low"]


def test_intraday_chart_includes_higher_timeframe_custom_context(monkeypatch):
    from signals.web.api import workbench

    df = _intraday_bars()
    chart = workbench._chart_from_df(df, symbol="SZ.002759", freq="30min", source="test_bars")
    monkeypatch.setattr(workbench, "_load_signal_pool_rows", lambda limit=200, symbol=None: [
        {
            "symbol": "SZ.002759",
            "signal_date": "2026-03-15",
            "signal_type": "日线自定义三买",
            "freq": "日线",
            "confidence": 0.88,
            "source": "sqlite.backtest.signal_records",
        },
        {
            "symbol": "SZ.002759",
            "signal_date": "2026-03-15",
            "signal_type": "30分钟缺口买",
            "freq": "30分钟",
            "confidence": 0.76,
            "source": "sqlite.backtest.signal_records",
        },
    ])
    monkeypatch.setattr(workbench, "_load_terminal_technical_signal_rows", lambda symbol, limit=300: [])

    merged = workbench._merge_signal_pool_into_chart(chart, "SZ.002759", "30min")
    by_type = {item["type"]: item for item in merged["signals"]}

    assert by_type["日线自定义三买"]["display_scope"] == "higher_timeframe_context"
    assert by_type["30分钟缺口买"]["display_scope"] == "current_timeframe"


def test_intraday_chart_merges_terminal_technical_signals(monkeypatch):
    from signals.web.api import workbench

    df = _intraday_bars()
    chart = workbench._chart_from_df(df, symbol="SZ.002759", freq="30min", source="test_bars")
    monkeypatch.setattr(workbench, "_load_signal_pool_rows", lambda limit=200, symbol=None: [])
    monkeypatch.setattr(workbench, "_load_terminal_technical_signal_rows", lambda symbol, limit=300: [
        {
            "symbol": "SZ.002759",
            "dt": "2026-03-15 10:30",
            "signal_type": "MACD绿柱缩小_零下",
            "signal_side": "buy",
            "freq": "30分钟",
            "confidence": 0.8,
            "price": 10.7,
            "technical_evidence": {"details": "30m detector"},
        }
    ])

    merged = workbench._merge_signal_pool_into_chart(chart, "SZ.002759", "30min")

    assert merged["signals"][-1]["type"] == "MACD绿柱缩小_零下"
    assert merged["signals"][-1]["source"] == "terminal_technical_signals"
    assert merged["signals"][-1]["display_scope"] == "current_timeframe"


def test_terminal_technical_rows_use_latest_as_of(monkeypatch):
    from signals.web.api import workbench

    class _Cursor(list):
        def sort(self, *args, **kwargs):
            return self

        def limit(self, limit):
            return _Cursor(self[:limit])

    class _Collection:
        def __init__(self):
            self.find_query = {}

        def find_one(self, query=None, projection=None, sort=None):
            return {"as_of": "2026-04-30"}

        def find(self, query=None, projection=None):
            self.find_query = query or {}
            rows = [
                {"symbol": "SH.688381", "as_of": "2026-04-30", "dt": "2026-04-30 11:00"},
                {"symbol": "SH.688381", "as_of": "2026-04-29", "dt": "2026-04-29 21:45"},
            ]
            return _Cursor([row for row in rows if row["as_of"] == self.find_query.get("as_of")])

    class _Db(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    collection = _Collection()
    monkeypatch.setattr(workbench, "_mongo_db", lambda: _Db({"terminal_technical_signals": collection}))
    monkeypatch.setattr(workbench, "_probe_symbol_candidates", lambda symbol, kind="stock": ["SH.688381"])

    rows = workbench._load_terminal_technical_signal_rows("SH.688381")

    assert collection.find_query["as_of"] == "2026-04-30"
    assert [row["as_of"] for row in rows] == ["2026-04-30"]


def test_weekly_chart_uses_data_as_of_for_unfinished_current_week(monkeypatch):
    from signals.web.api import workbench

    df = pd.DataFrame(
        {
            "open": [10.0],
            "high": [12.0],
            "low": [9.0],
            "close": [11.0],
            "vol": [1000],
            "amount": [10000],
        },
        index=pd.to_datetime(["2026-05-01"]),
    )
    monkeypatch.setattr(workbench, "_market_today", lambda market="A": date(2026, 4, 29))

    chart = workbench._chart_from_df(df, symbol="sh000001", freq="weekly", source="index_bars")

    assert chart["meta"]["period_end"] == "2026-05-01"
    assert chart["meta"]["data_as_of"] == "2026-04-29"
    assert chart["meta"]["time_semantics"] == "period_data_as_of"
    assert chart["ohlcv"][-1]["volume"] == 1000
    assert chart["ohlcv"][-1]["amount"] == 10000
    assert workbench._timestamp_date(chart["ohlcv"][-1]["time"], market="A", symbol="sh000001") == "2026-04-29"


def test_board_heat_chart_declares_heat_ohlc_formula(monkeypatch):
    from signals.web.api import workbench

    df = pd.DataFrame(
        {
            "open": [1.0],
            "high": [3.0],
            "low": [0.5],
            "close": [2.0],
            "vol": [1000],
            "amount": [1000],
        },
        index=pd.to_datetime(["2026-04-29 10:30"]),
    )

    monkeypatch.setattr(
        workbench,
        "_board_heat_df",
        lambda name, kind, freq: (
            df,
            "board_heat_ticks",
            {"trade_minute": "2026-04-29 10:30", "source": "eastmoney_push2delay"},
            {"heat_name": name, "status": "exact"},
        ),
    )

    chart, _ = workbench._board_heat_chart("锂", "industry", "30min")

    assert chart["meta"]["chart_type"] == "heat_ohlc"
    assert chart["meta"]["is_price_kline"] is False
    assert chart["meta"]["ohlc_formula"]["open"] == "change_pct:first"
    assert chart["meta"]["candidate_stocks_role"] == "representatives_only_not_price_source"


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


def test_focus_stocks_do_not_create_sell_only_rows(monkeypatch):
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

    assert rows == []


def test_focus_stocks_attach_sell_warning_to_existing_buy_row(monkeypatch):
    from signals.web.api import workbench

    df = _bars()
    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (df, "test_bars"))
    monkeypatch.setattr(workbench, "_load_signal_pool_rows", lambda: [])

    rows = workbench._build_focus_stock_rows(
        buy_rows=[
            {
                "symbol": "SZ.002759",
                "name": "天际股份",
                "reason": "5m 买点",
                "score": 90,
                "metadata": {"freq": "5m"},
            }
        ],
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

    assert rows[0]["action_status"] == "risk_review"
    assert [item["badge"] for item in rows[0]["sell_timeframes"]] == ["D"]
    assert [item["badge"] for item in rows[0]["buy_timeframes"]] == ["5m"]
    assert rows[0]["latest_signal"] == "卖D/5m"
    assert rows[0]["trader_action"] == "风险复核"


def test_terminal_stock_pool_group_rows_keep_focus_risk_watch_separate(monkeypatch):
    from signals.web.api import workbench

    class _Collection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "focus_stocks": [
                    {
                        "symbol": "SZ.300575",
                        "name": "中旗新材",
                        "pool_type": "focus",
                        "action_status": "entry_ready",
                        "entry_gate_status": "entry_confirmed",
                        "inclusion_reasons": [{"reason_type": "technical_trigger", "signal_type": "三买"}],
                    }
                ],
                "risk_stocks": [
                    {
                        "symbol": "SH.688484",
                        "name": "风险股",
                        "pool_type": "risk",
                        "action_status": "risk_review",
                        "entry_gate_status": "blocked_by_risk",
                        "inclusion_reasons": [{"reason_type": "technical_trigger", "signal_side": "sell", "signal_type": "一卖"}],
                    }
                ],
                "watch_stocks": [
                    {
                        "symbol": "SZ.002812",
                        "name": "观察股",
                        "pool_type": "watch",
                        "action_status": "entry_waiting_30m_confirm",
                        "entry_gate_status": "entry_waiting_30m_confirm",
                        "inclusion_reasons": [{"reason_type": "fallback_watch", "signal_type": "线索池"}],
                    }
                ],
            }

    class _Db(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    monkeypatch.setattr(workbench, "_mongo_db", lambda: _Db({"terminal_stock_pool": _Collection()}))
    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (_bars(), "test_bars"))
    columns = workbench._watchlist_range_columns(date(2026, 4, 26))

    focus = workbench._terminal_stock_pool_group_rows(columns, "focus_stocks")
    risk = workbench._terminal_stock_pool_group_rows(columns, "risk_stocks")
    watch = workbench._terminal_stock_pool_group_rows(columns, "watch_stocks")

    assert [row["pool_type"] for row in focus] == ["focus"]
    assert [row["pool_type"] for row in risk] == ["risk"]
    assert [row["pool_type"] for row in watch] == ["watch"]
    assert focus[0]["entry_gate_status"] == "entry_confirmed"
    assert risk[0]["action_status"] == "risk_review"
    assert watch[0]["action_status"] == "entry_waiting_30m_confirm"


def test_manual_clue_rows_reuse_stock_pool_decision_fields(monkeypatch):
    from signals.web.api import workbench

    class _Cursor(list):
        def sort(self, *args, **kwargs):
            return self

        def limit(self, limit):
            return _Cursor(self[:limit])

    class _ManualClues:
        def find(self, query=None, projection=None):
            return _Cursor([
                {
                    "symbol": "SZ.002354",
                    "raw_code": "002354",
                    "name": "天娱数科",
                    "freq": "30min",
                    "active": True,
                }
            ])

    class _Db(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    monkeypatch.setattr(workbench, "_mongo_db", lambda: _Db({"terminal_manual_clues": _ManualClues()}))
    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (_bars(), "test_bars"))
    monkeypatch.setattr(workbench, "_stock_chain_position_summary", lambda symbol: {})
    monkeypatch.setattr(workbench, "_load_terminal_technical_signal_rows", lambda symbol, limit=300: [
        {
            "symbol": "SZ.002354",
            "dt": "2026-04-29 15:00",
            "as_of": "2026-04-30",
            "signal_type": "缺口买:突破",
            "signal_side": "buy",
            "freq": "日线",
            "confidence": 0.65,
            "score": 19.5,
            "price": 5.95,
            "resonance_context": {"grade": "conflict", "conflict_freqs": ["30分钟"]},
        },
        {
            "symbol": "SZ.002354",
            "dt": "2026-04-28 15:00",
            "as_of": "2026-04-30",
            "signal_type": "一买",
            "signal_side": "buy",
            "freq": "30分钟",
            "confidence": 0.7,
            "score": 28.0,
            "price": 5.38,
        },
        {
            "symbol": "SZ.002354",
            "dt": "2026-04-24 10:30",
            "as_of": "2026-04-30",
            "signal_type": "形态:头肩顶",
            "signal_side": "sell",
            "freq": "30分钟",
            "confidence": 0.75,
            "score": -11.25,
            "price": 5.57,
        },
    ])

    rows = workbench._manual_clue_rows(workbench._watchlist_range_columns(date(2026, 4, 26)))

    row = rows[0]
    assert row["manual_clue"] is True
    assert row["source_collection"] == "terminal_manual_clues"
    assert "terminal_technical_signals" in row["source_collections"]
    assert [item["badge"] for item in row["buy_timeframes"]] == ["D", "30m"]
    assert [item["badge"] for item in row["sell_timeframes"]] == ["30m"]
    assert row["timeframe_signal_sides"]["upper"]["side"] != "none"
    assert row["timeframe_signal_sides"]["trade"]["side"] != "none"
    assert row["timeframe_signal_sides"]["execution"]["side"] == "none"
    assert {"risk_clear", "period_conflict", "right_side"} <= set(row["missing_gates"])
    assert row["trade_stage"] == "clue_pool"
    assert row["can_trade_now"] is False
    assert "terminal_technical_signals" in row["evidence_summary"]


def test_stock_chart_loader_uses_requested_minute_freq(monkeypatch):
    from signals.web.api import workbench

    calls = []
    monkeypatch.setattr(workbench, "_ensure_minute_bars", lambda symbol, raw_code, freq: calls.append(freq) or True)

    assert workbench._load_stock_chart_data("SZ.002354", "002354", "30min") is True
    assert workbench._load_stock_chart_data("SZ.002354", "002354", "15min") is True

    assert calls == ["30min", "15min"]


def test_manual_clue_preheat_requests_full_execution_bundle():
    from signals.web.api import workbench

    assert workbench._manual_clue_preheat_freqs("30min") == ["30min", "daily", "15min", "5min"]
    assert workbench._manual_clue_preheat_freqs("daily") == ["daily", "30min", "15min", "5min"]


def test_manual_clue_delete_requires_confirmation():
    from fastapi import HTTPException
    from signals.web.api import workbench

    try:
        asyncio.run(workbench.delete_workbench_manual_clue("SZ.002354"))
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("manual clue delete should require explicit confirmation")


def test_manual_clue_delete_with_confirmation(monkeypatch):
    from signals.web.api import workbench

    class _Result:
        deleted_count = 1

    class _ManualClues:
        def __init__(self):
            self.query = None

        def delete_many(self, query):
            self.query = query
            return _Result()

    class _Db(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    collection = _ManualClues()
    monkeypatch.setattr(workbench, "_mongo_db", lambda: _Db({"terminal_manual_clues": collection}))
    monkeypatch.setattr(workbench, "_invalidate_shell_cache", lambda: None)

    payload = asyncio.run(workbench.delete_workbench_manual_clue("SZ.002354", confirm=True))

    assert payload == {"status": "ok", "symbol": "SZ.002354", "deleted": 1}
    assert collection.query["$or"][0]["symbol"] == "SZ.002354"


def test_quote_overlay_marks_non_current_quote_stale(monkeypatch):
    from signals.web.api import workbench

    class _QuoteCollection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "symbol": "SH.600000",
                "dt": "2026-04-29",
                "snapshot_at": datetime(2026, 4, 29, 14, 30),
                "source": "fullmarket_spot_snapshot",
                "price": 10.5,
                "change_pct": 3.2,
                "freshness": "fresh",
                "is_stale": False,
            }

    class _Db(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    monkeypatch.setattr(workbench, "_mongo_db", lambda: _Db({"quote_snapshots": _QuoteCollection()}))
    monkeypatch.setattr(workbench, "_market_today", lambda market="A": date(2026, 4, 30))
    monkeypatch.setattr(workbench, "_market_now", lambda market="A": datetime(2026, 4, 30, 10, 0))

    row = workbench._apply_quote_overlay({"today_change_pct": 9.9, "day_change_pct": 9.9}, "SH.600000")

    assert row["quote_status"] == "stale"
    assert row["quote_status_label"] == "行情陈旧"
    assert row["today_change_pct"] is None
    assert row["day_change_pct"] is None


def test_quote_overlay_marks_future_holiday_snapshot_stale(monkeypatch):
    from signals.web.api import workbench

    class _QuoteCollection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "symbol": "SH.600000",
                "dt": "2026-05-01",
                "snapshot_at": datetime(2026, 5, 1, 14, 30),
                "source": "fullmarket_spot_snapshot",
                "price": 10.5,
                "change_pct": 3.2,
                "freshness": "fresh",
                "is_stale": False,
            }

    class _Db(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    monkeypatch.setattr(workbench, "_mongo_db", lambda: _Db({"quote_snapshots": _QuoteCollection()}))
    monkeypatch.setattr(workbench, "_a_day_change_mode", lambda: "daily_close")
    monkeypatch.setattr(workbench, "_day_change_expected_day", lambda mode=None: "2026-04-30")

    overlay = workbench._quote_overlay_for_symbol("SH.600000")

    assert overlay["quote_status"] == "stale"
    assert overlay["quote_status_label"] == "行情陈旧"
    assert "quote_day=2026-05-01, expected=2026-04-30" in overlay["quote_stale_reason"]


def test_stock_summary_uses_daily_close_day_change_when_quote_stale(monkeypatch):
    from signals.web.api import workbench

    daily = pd.DataFrame(
        {"close": [25.0, 27.5]},
        index=pd.to_datetime(["2026-04-29", "2026-04-30"]),
    )
    monkeypatch.setattr(workbench, "_a_day_change_mode", lambda: "daily_close")
    monkeypatch.setattr(workbench, "_day_change_expected_day", lambda mode=None: "2026-04-30")
    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (daily, "daily_bars"))
    monkeypatch.setattr(
        workbench,
        "_quote_overlay_for_symbol",
        lambda symbol: {
            "day_change_mode": "daily_close",
            "quote_status": "stale",
            "quote_as_of": "2026-05-01",
            "quote_stale_reason": "quote_day=2026-05-01, expected=2026-04-30",
        },
    )

    summary = workbench._summary_from_stock("SH.600000", {"name": "测试股"}, {"ohlcv": [{"close": 27.5}]})

    assert summary["latest_price"] == 27.5
    assert summary["day_change_pct"] == 10.0
    assert summary["daily_change_pct"] == 10.0
    assert summary["day_change_source"] == "daily_bars_close"
    assert summary["day_change_as_of"] == "2026-04-30"
    assert summary["quote_status"] == "stale"


def test_concept_sector_preview_returns_explicit_chain_carrier(monkeypatch):
    from signals.web.api import workbench

    df = _bars()
    monkeypatch.setattr(workbench, "resolve_board_heat_name", lambda kind, label: {"query": label, "heat_name": label, "status": "exact"})
    monkeypatch.setattr(workbench, "_concept_theme_candidates", lambda name: [])
    monkeypatch.setattr(workbench, "_concept_rank_rows", lambda name, themes: [])
    monkeypatch.setattr(workbench, "_industry_constituent_symbols", lambda name: [])
    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (df, "test_carrier_bars"))

    row = workbench._sector_board_preview({"label": "电解液", "change_pct": 2.2}, "concept")

    assert row["target_kind"] == "concept"
    assert row["target_freq"] == "30min"
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
    assert "链主代表" in row["explanation"]


def test_sector_boards_expose_chain_aggregation_candidate_groups(monkeypatch):
    from signals.web.api import workbench

    df = _bars()
    monkeypatch.setattr(workbench, "resolve_board_heat_name", lambda kind, label: {"query": label, "heat_name": label, "status": "exact"})
    monkeypatch.setattr(workbench, "_concept_theme_candidates", lambda name: [])
    monkeypatch.setattr(workbench, "_concept_rank_rows", lambda name, themes: [])
    monkeypatch.setattr(workbench, "_industry_constituent_symbols", lambda name: [])
    monkeypatch.setattr(workbench, "_concept_constituent_symbols", lambda name, themes: [])
    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (df, "test_carrier_bars"))

    rows = workbench._build_sector_board_rows(
        industry_top=[{"label": "半导体设备", "change_pct": 4.1, "source": "board_em"}],
        concept_top=[
            {"label": "华为海思", "change_pct": 5.2, "source": "concept_em"},
            {"label": "HBM", "change_pct": 4.8, "source": "concept_ths"},
        ],
    )

    semiconductor_rows = [row for row in rows if row.get("chain_id") == "semiconductor"]
    assert semiconductor_rows
    assert {row["node_id"] for row in semiconductor_rows} >= {
        "semiconductor_equipment",
        "chip_design",
        "memory_chip",
    }
    for row in semiconductor_rows:
        assert row["integrated_domains"]
        assert row["candidate_groups"]["leaders"]
        assert row["focus_stocks_preview"]
        assert row["focus_stocks_preview"][0]["attention_score"] >= 0


def test_non_chain_sector_preview_is_not_forced_to_carrier(monkeypatch):
    from signals.web.api import workbench

    monkeypatch.setattr(workbench, "resolve_board_heat_name", lambda kind, label: {"query": label, "heat_name": label, "status": "exact"})
    monkeypatch.setattr(workbench, "_concept_theme_candidates", lambda name: [])
    monkeypatch.setattr(workbench, "_concept_rank_rows", lambda name, themes: [])
    monkeypatch.setattr(workbench, "_concept_constituent_symbols", lambda name, themes: [])

    row = workbench._sector_board_preview({"label": "本月解禁", "change_pct": 3.2}, "concept")

    assert row["non_chain_reason"]
    assert row["chart_target_status"] == "non_chain"
    assert row["target_freq"] == "30min"
    assert row["carrier"] == {}
    assert row["fallback_target"] == {}
    assert row["action_status"] == "非产业链观察"


def test_board_heat_name_aliases_match_cached_tick_names():
    from signals.web.api import workbench

    names = ["建筑装饰", "建筑材料", "CRO", "创新药"]

    assert workbench._choose_board_heat_name("industry", "建筑装饰和其他建筑业", names) == ("建筑装饰", "alias")
    assert workbench._choose_board_heat_name("concept", "CRO概念", names) == ("CRO", "alias")


def test_concept_target_returns_scored_candidate_groups(monkeypatch):
    from signals.web.api import workbench

    class FakeEngine:
        def get_concepts(self):
            return []

    df = _bars()

    async def fake_stock_target(symbol, raw_code, freq):
        return {
            "target": {
                "kind": "stock",
                "label": symbol,
                "symbol": symbol,
                "requested_freq": freq,
                "effective_freq": freq,
            },
            "chart": {
                "ohlcv": [{"date": "2026-01-01", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 100}],
                "meta": {"source": "test_stock_bars", "freq": freq},
            },
            "summary": {"title": "天赐材料"},
            "signals": [],
            "plan": None,
            "review": {},
            "trade": {},
        }

    monkeypatch.setattr(workbench, "_concept_theme_candidates", lambda name: [])
    monkeypatch.setattr(workbench, "_concept_rank_rows", lambda name, themes: [])
    monkeypatch.setattr(workbench, "_concept_constituent_symbols", lambda name, themes: [])
    monkeypatch.setattr(workbench, "_industry_constituent_symbols", lambda name: [])
    monkeypatch.setattr(workbench, "_ensure_daily_bars", lambda symbol, raw_code: None)
    monkeypatch.setattr(workbench, "_ensure_minute_bars", lambda symbol, raw_code, freq: None)
    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (df, "test_carrier_bars"))
    monkeypatch.setattr(workbench, "_build_stock_target", fake_stock_target)

    payload = asyncio.run(workbench._build_concept_target(FakeEngine(), "电解液", "daily"))

    assert payload["target"]["kind"] == "concept"
    assert payload["target"]["mapping_status"] == "mapped"
    assert payload["candidate_groups"]["leaders"]
    assert payload["candidate_groups"]["elastic"]
    assert payload["candidate_stocks"]
    leader = payload["candidate_groups"]["leaders"][0]
    assert leader["leader_tier"] == "龙头"
    assert leader["chain_role"]
    assert leader["weight_score"] >= 0
    assert leader["elasticity_score"] >= 0
    assert leader["attention_score"] >= 0
    assert leader["why_watch"]
    assert isinstance(leader["risk_flags"], list)


def test_trader_task_queue_is_action_oriented():
    from signals.web.api import workbench

    tasks = workbench._build_trader_task_queue(
        decision_rows=[],
        focus_stocks=[
            {
                "symbol": "SZ.002709",
                "name": "天赐材料",
                "trader_action": "可试仓",
                "queue_lane": "entry_ready",
                "latest_signal": "5m",
                "reason": "5m买点确认",
                "invalidates_when": "跌破短线防守位",
                "technical_evidence": {"signal_type": "三买"},
            }
        ],
        sector_boards=[
            {
                "label": "电解液",
                "domain": "concept",
                "target_freq": "30min",
                "explanation": "电解液 异动 · 承接 天赐材料",
                "fallback_target": {"kind": "stock", "symbol": "SZ.002709"},
            }
        ],
    )

    assert tasks
    assert tasks[0]["action_label"] == "可试仓"
    assert tasks[0]["chart_target"]["kind"] == "stock"
    assert len(tasks) == 1
    assert tasks[0]["queue_lane"] == "entry_ready"
    assert tasks[0]["invalidates_when"]


def test_trader_task_queue_excludes_observation_context():
    from signals.web.api import workbench

    tasks = workbench._build_trader_task_queue(
        decision_rows=[],
        focus_stocks=[
            {
                "symbol": "SZ.002709",
                "name": "天赐材料",
                "trader_action": "观察",
                "latest_signal": "产业链热度",
                "queue_lane": "context_only",
                "reason": "产业链升温",
            }
        ],
        sector_boards=[
            {
                "label": "电解液",
                "domain": "concept",
                "target_freq": "30min",
                "explanation": "电解液 异动",
            }
        ],
    )

    assert tasks == []


def test_trader_task_queue_excludes_legacy_buy_review_without_hard_technical():
    from signals.web.api import workbench

    tasks = workbench._build_trader_task_queue(
        decision_rows=[
            {
                "title": "确认买点 · 测试股",
                "symbol": "SZ.000001",
                "action_label": "复合买点",
                "queue_lane": "risk_exit_first",
                "summary": "打开图表确认买点、关键均线方向和止损位。",
                "reason": "背驰买",
            },
            {
                "title": "卖出复核 · 风险股",
                "symbol": "SZ.000002",
                "action_label": "复核卖点",
                "summary": "检查是否跌破5日/20日或周线信心线。",
                "reason": "背驰卖",
            },
        ],
        focus_stocks=[],
        sector_boards=[],
    )

    assert len(tasks) == 1
    assert tasks[0]["queue_lane"] == "risk_exit_first"
    assert tasks[0]["symbol"] == "SZ.000002"


def test_static_index_minute_request_does_not_fallback_to_daily(monkeypatch):
    from signals.web.api import workbench

    monkeypatch.setattr(workbench, "_index_df", lambda symbol, freq: (pd.DataFrame(), "index_bars"))
    monkeypatch.setattr(workbench, "_target_diagnostics", lambda *args, **kwargs: {"cache_probe": {"status": "miss"}})

    payload = asyncio.run(workbench._build_static_index_target("上证指数", "sh000001", "30min"))

    assert payload["target"]["requested_freq"] == "30min"
    assert payload["target"]["effective_freq"] == "30min"
    assert payload["target"]["not_ready_reason"] == "index_minute_not_ready"
    assert payload["chart"]["meta"]["fallback_reason"] == ""
    assert payload["chart"]["meta"]["not_ready_reason"] == "index_minute_not_ready"


def test_us_index_minute_request_is_explicitly_unsupported(monkeypatch):
    from signals.web.api import workbench

    monkeypatch.setattr(workbench, "_index_df", lambda symbol, freq: (pd.DataFrame(), "index_bars"))
    monkeypatch.setattr(workbench, "_ensure_engine", lambda: (_ for _ in ()).throw(RuntimeError("engine unavailable")))
    monkeypatch.setattr(workbench, "_recent_custom_signal_candidates", lambda limit=10: [])

    payload = asyncio.run(workbench._build_static_index_target("标普500", "US.SPY", "30min"))

    assert payload["target"]["not_ready_reason"] == "index_minute_unsupported"
    assert payload["target"]["cache_probe"]["status"] == "unsupported"
    assert payload["chart"]["meta"]["not_ready_reason"] == "index_minute_unsupported"


def test_stock_minute_request_does_not_fallback_to_daily(monkeypatch):
    from signals.web.api import workbench

    class FakeEngine:
        def get_status(self):
            return {"ready": True, "active_markets": ["A"]}

    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (pd.DataFrame(), "bars"))
    monkeypatch.setattr(workbench, "_trigger_stock_chart_load", lambda symbol, raw_code, freq: {
        "load_status": "triggered",
        "load_triggered": True,
        "load_eta_seconds": 10,
    })
    monkeypatch.setattr(workbench, "_merge_signal_pool_into_chart", lambda chart, symbol, freq: chart)
    monkeypatch.setattr(workbench, "analyze_stock", lambda symbol: {"symbol": symbol, "name": "测试股份"})
    monkeypatch.setattr(workbench, "_ensure_engine", lambda: FakeEngine())
    monkeypatch.setattr(workbench, "_review_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(workbench, "_target_diagnostics", lambda *args, **kwargs: {"cache_probe": {"status": "miss"}})

    payload = asyncio.run(workbench._build_stock_target("SH.600000", "600000", "30min"))

    assert payload["target"]["requested_freq"] == "30min"
    assert payload["target"]["effective_freq"] == "30min"
    assert payload["target"]["not_ready_reason"] == "stock_minute_not_ready"
    assert payload["chart"]["meta"]["fallback_reason"] == ""
    assert payload["chart"]["meta"]["load_status"] == "triggered"
    assert payload["chart"]["meta"]["load_eta_seconds"] == 10


def test_stock_30min_uses_bars_chart_not_backtest_override(monkeypatch):
    from signals.web.api import workbench

    class FakeEngine:
        def get_status(self):
            return {"ready": True, "active_markets": ["A"]}

    async def fail_backtest(*args, **kwargs):
        raise AssertionError("30min workbench chart should use bars cache")

    df = _intraday_bars()
    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (df, "bars"))
    monkeypatch.setattr(workbench, "_trigger_stock_chart_load", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fresh bars should not trigger load")))
    monkeypatch.setattr(workbench, "_call_backtest_run", fail_backtest)
    monkeypatch.setattr(workbench, "_merge_signal_pool_into_chart", lambda chart, symbol, freq: chart)
    monkeypatch.setattr(workbench, "analyze_stock", lambda symbol: {"symbol": symbol, "name": "测试股份"})
    monkeypatch.setattr(workbench, "_ensure_engine", lambda: FakeEngine())
    monkeypatch.setattr(workbench, "_review_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(workbench, "_target_diagnostics", lambda *args, **kwargs: {"cache_probe": {"status": "ready"}})

    payload = asyncio.run(workbench._build_stock_target("SH.600000", "600000", "30min"))

    assert payload["chart"]["meta"]["source"] == "bars"
    assert payload["chart"]["ohlcv"][-1]["time"] == workbench._dt_to_unix(
        df.index[-1],
        market="A",
        symbol="SH.600000",
        source="bars",
    )


def test_stock_daily_uses_bars_chart_before_backtest(monkeypatch):
    from signals.web.api import workbench

    class FakeEngine:
        def get_status(self):
            return {"ready": True, "active_markets": ["A"]}

    async def fail_backtest(*args, **kwargs):
        raise AssertionError("daily workbench chart should prefer bars cache")

    df = _bars()
    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (df, "bars"))
    monkeypatch.setattr(workbench, "_call_backtest_run", fail_backtest)
    monkeypatch.setattr(workbench, "_merge_signal_pool_into_chart", lambda chart, symbol, freq: chart)
    monkeypatch.setattr(workbench, "analyze_stock", lambda symbol: {"symbol": symbol, "name": "测试股份"})
    monkeypatch.setattr(workbench, "_ensure_engine", lambda: FakeEngine())
    monkeypatch.setattr(workbench, "_review_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(workbench, "_target_diagnostics", lambda *args, **kwargs: {"cache_probe": {"status": "ready"}})

    payload = asyncio.run(workbench._build_stock_target("SH.600000", "600000", "daily"))

    assert payload["chart"]["meta"]["source"] == "bars"
    assert payload["chart"]["ohlcv"][-1]["time"] == workbench._dt_to_unix(
        df.index[-1],
        market="A",
        symbol="SH.600000",
        source="bars",
    )


def test_stock_daily_missing_bars_triggers_loader_without_backtest(monkeypatch):
    from signals.web.api import workbench

    class FakeEngine:
        def get_status(self):
            return {"ready": True, "active_markets": ["A"]}

    async def fail_backtest(*args, **kwargs):
        raise AssertionError("missing bars should show not-ready and trigger cache load")

    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (pd.DataFrame(), "bars"))
    monkeypatch.setattr(workbench, "_call_backtest_run", fail_backtest)
    monkeypatch.setattr(workbench, "_trigger_stock_chart_load", lambda symbol, raw_code, freq: {
        "load_status": "triggered",
        "load_triggered": True,
        "load_target_freq": freq,
        "load_eta_seconds": 15,
        "load_retry_after_seconds": 17,
    })
    monkeypatch.setattr(workbench, "_merge_signal_pool_into_chart", lambda chart, symbol, freq: chart)
    monkeypatch.setattr(workbench, "analyze_stock", lambda symbol: {"symbol": symbol, "name": "测试股份"})
    monkeypatch.setattr(workbench, "_ensure_engine", lambda: FakeEngine())
    monkeypatch.setattr(workbench, "_review_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(workbench, "_target_diagnostics", lambda *args, **kwargs: {"cache_probe": {"status": "miss"}})

    payload = asyncio.run(workbench._build_stock_target("SH.600000", "600000", "daily"))

    assert payload["chart"]["ohlcv"] == []
    assert payload["chart"]["meta"]["cache_status"] == "not_ready"
    assert payload["chart"]["meta"]["not_ready_reason"] == "daily_cache_missing"
    assert payload["chart"]["meta"]["load_status"] == "triggered"
    assert payload["chart"]["meta"]["load_eta_seconds"] == 15


def test_stock_30min_resamples_from_fresh_5min_when_direct_cache_lags(monkeypatch):
    from signals.data.models import DataResponse
    from signals.web.api import workbench

    stale_30m = pd.DataFrame(
        {
            "open": [17.0],
            "high": [17.4],
            "low": [16.8],
            "close": [17.2],
            "vol": [1000],
            "amount": [10000],
        },
        index=pd.to_datetime(["2026-04-29 15:00"]),
    )
    fresh_5m = pd.DataFrame(
        {
            "open": [17.10, 17.15, 17.18, 17.20, 17.22, 17.24, 17.26, 17.28, 17.30],
            "high": [17.20, 17.25, 17.30, 17.35, 17.36, 17.38, 17.40, 17.42, 17.44],
            "low": [17.00, 17.05, 17.10, 17.12, 17.14, 17.16, 17.18, 17.20, 17.22],
            "close": [17.15, 17.18, 17.20, 17.22, 17.24, 17.26, 17.28, 17.30, 17.33],
            "vol": [100] * 9,
            "amount": [1000] * 9,
        },
        index=pd.to_datetime([
            "2026-04-30 13:05",
            "2026-04-30 13:10",
            "2026-04-30 13:15",
            "2026-04-30 13:20",
            "2026-04-30 13:25",
            "2026-04-30 13:30",
            "2026-04-30 13:35",
            "2026-04-30 13:40",
            "2026-04-30 13:45",
        ]),
    )

    def fake_get_kline(request):
        if request.freq == "30m":
            return DataResponse(
                stale_30m,
                mode_used="historical",
                source="bars",
                as_of="2026-04-29",
                freshness="stale",
                is_stale=True,
            )
        if request.freq == "5m":
            return DataResponse(
                fresh_5m,
                mode_used="historical",
                source="bars",
                as_of="2026-04-30",
                freshness="fresh",
                is_stale=False,
            )
        raise AssertionError(f"unexpected freq {request.freq}")

    monkeypatch.setattr(workbench, "get_kline", fake_get_kline)

    df, source = workbench._stock_df("SH.600438", "30min")

    assert source == "bars;resampled_from=5min;resampled_to=30min"
    assert df.index[-1] == pd.Timestamp("2026-04-30 14:00")
    assert df.iloc[-1]["open"] == 17.26
    assert df.iloc[-1]["close"] == 17.33
    assert df.iloc[-1]["vol"] == 300
    assert df.attrs["as_of"] == "2026-04-30"
    assert df.attrs["resampled_from_freq"] == "5min"
    assert df.attrs["direct_latest_bar_time"] == "2026-04-29T15:00:00"


def test_quote_future_holiday_date_is_stale_against_expected_daily_close():
    from signals.web.api import workbench

    assert workbench._quote_day_is_stale("2026-05-01", "2026-04-30", "daily_close") is True
