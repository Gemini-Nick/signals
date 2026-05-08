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


def test_shell_cache_refreshes_quote_overlay_when_quote_watermark_changes(monkeypatch):
    from signals.web.api import workbench

    class _Engine:
        def is_ready(self):
            return True

    engine = _Engine()
    monkeypatch.setattr(workbench, "_quote_overlay_for_symbol", lambda symbol: {
        "day_change_mode": "quote_intraday",
        "quote_status": "realtime",
        "day_change_pct": 2.5,
        "daily_change_pct": 2.5,
        "today_change_pct": 2.5,
        "latest_price": 10.25,
        "realtime_price": 10.25,
        "day_change_source": "quote_snapshots",
        "day_change_as_of": "2026-05-06",
    })
    try:
        cached_payload = {
            "session": {"ready": True},
            "buy_candidates": [{"kind": "stock", "symbol": "SZ.002709", "latest_price": 10.0, "day_change_pct": 1.0}],
        }
        workbench._SHELL_CACHE.update({
            "expires_at": 999999.0,
            "payload": cached_payload,
            "refreshed_at": 1.0,
            "quote_watermark": "old",
        })

        assert workbench._shell_cache_usable(cached_payload, engine, quote_watermark="new") is True
        payload = workbench._payload_from_shell_cache(cached_payload, "hit", 10.0, "new")

        assert payload["cache"]["status"] == "hit_quote_overlay"
        assert payload["buy_candidates"][0]["latest_price"] == 10.25
        assert payload["buy_candidates"][0]["day_change_pct"] == 2.5
        assert workbench._SHELL_CACHE["quote_watermark"] == "new"
    finally:
        workbench._invalidate_shell_cache()


def test_macro_indices_have_day_and_range_returns(monkeypatch):
    from signals.web.api import workbench

    df = _bars()
    monkeypatch.setattr(workbench, "_index_df", lambda symbol, freq: (df, "test_index_bars"))
    monkeypatch.setattr(workbench, "_quote_overlay_for_symbol", lambda symbol: {"quote_status": "missing", "quote_status_label": "无行情"})

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
    assert signal["type"] == "量价齐升突破"
    assert "量比" in signal["details"]
    assert signal["volume_price_relation"] == "量价同向向上"
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
    assert signal["type"] == "缩量回踩承接"
    assert "量比" in signal["details"]
    assert signal["volume_price_relation"] == "量价收敛"
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
        {
            "symbol": "SZ.002759",
            "signal_date": "2026-03-15 10:45",
            "signal_type": "5分钟右侧确认",
            "freq": "5分钟",
            "confidence": 0.72,
            "source": "sqlite.backtest.signal_records",
        },
    ])
    monkeypatch.setattr(workbench, "_load_terminal_technical_signal_rows", lambda symbol, limit=300: [])

    merged = workbench._merge_signal_pool_into_chart(chart, "SZ.002759", "30min")
    by_type = {item["type"]: item for item in merged["signals"]}

    assert by_type["日线自定义三买"]["display_scope"] == "higher_timeframe_context"
    assert by_type["30分钟缺口买"]["display_scope"] == "current_timeframe"
    assert by_type["5分钟右侧确认"]["display_scope"] == "lower_timeframe_context"
    assert by_type["5分钟右侧确认"]["dt"] == chart["ohlcv"][2]["time"]


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
    assert rows[0]["trader_action"] == "暂不参与"


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


def test_terminal_stock_pool_group_rows_uses_watch_limit_from_pool_doc(monkeypatch):
    from signals.web.api import workbench

    watch_rows = [
        {
            "symbol": f"SZ.{index:06d}",
            "name": f"观察股{index}",
            "pool_type": "watch",
            "action_status": "entry_waiting_30m_confirm",
        }
        for index in range(1, 131)
    ]

    class _Collection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "watch_limit": 114,
                "watch_stocks": watch_rows,
            }

    class _Db(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    refresh_calls = []
    monkeypatch.delenv("TERMINAL_WORKBENCH_WATCH_STOCK_LIMIT", raising=False)
    monkeypatch.setattr(workbench, "_mongo_db", lambda: _Db({"terminal_stock_pool": _Collection()}))
    monkeypatch.setattr(workbench, "_enrich_stock_row", lambda row, columns, lightweight=False: dict(row))
    monkeypatch.setattr(
        workbench,
        "_refresh_realtime_quotes_for_rows",
        lambda db, rows, *, refresh_key, limit: refresh_calls.append(limit) or {"status": "skipped"},
    )

    rows = workbench._terminal_stock_pool_group_rows([], "watch_stocks")

    assert len(rows) == 114
    assert refresh_calls == [114]


def test_terminal_stock_pool_group_rows_reads_clue_stocks_with_quote_overlay(monkeypatch):
    from signals.web.api import workbench

    class _TerminalCollection:
        def find_one(self, query=None, projection=None, sort=None):
            assert projection and projection.get("clue_stocks") == 1
            return {
                "clue_stocks": [
                    {
                        "symbol": "SZ.301363",
                        "name": "美好医疗",
                        "pool_type": "watch",
                        "action_status": "clue_pool",
                        "entry_gate_status": "clue_pool",
                        "latest_price": 88.41,
                        "day_change_pct": 5.15,
                        "inclusion_reasons": [{"reason_type": "review_sector_bullish", "signal_type": "线索池"}],
                    }
                ],
            }

    class _QuoteCollection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "symbol": "SZ.301363",
                "dt": "2026-05-08",
                "snapshot_at": datetime(2026, 5, 8, 10, 0),
                "source": "eastmoney_push2delay_ulist",
                "price": 42.92,
                "open": 42.0,
                "prev_close": 41.66,
                "change_pct": 3.02,
                "freshness": "fresh",
                "is_stale": False,
            }

    class _Db(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    monkeypatch.setattr(workbench, "_mongo_db", lambda: _Db({
        "terminal_stock_pool": _TerminalCollection(),
        "quote_snapshots": _QuoteCollection(),
    }))
    monkeypatch.setattr(workbench, "_a_day_change_mode", lambda: "quote_intraday")
    monkeypatch.setattr(workbench, "_day_change_expected_day", lambda mode=None: "2026-05-08")
    monkeypatch.setattr(workbench, "_market_now", lambda market="A": datetime(2026, 5, 8, 10, 0, 5))
    refresh_calls = []
    monkeypatch.setattr(
        workbench,
        "_refresh_realtime_quotes_for_rows",
        lambda db, rows, *, refresh_key, limit: refresh_calls.append({
            "refresh_key": refresh_key,
            "symbols": [row.get("symbol") for row in rows],
            "limit": limit,
        }) or {"status": "ok"},
    )

    rows = workbench._terminal_stock_pool_group_rows([], "clue_stocks")

    assert len(rows) == 1
    assert rows[0]["latest_price"] == 42.92
    assert rows[0]["day_change_pct"] == 3.02
    assert rows[0]["quote_prev_close_change_pct"] == 3.02
    assert rows[0]["quote_open_change_pct"] == 2.1905
    assert rows[0]["day_change_basis"] == "prev_close"
    assert rows[0]["day_change_source"] == "quote_snapshots"
    assert rows[0]["quote_status"] == "realtime"
    assert refresh_calls[0]["refresh_key"] == "clue_stocks"
    assert refresh_calls[0]["symbols"] == ["SZ.301363"]


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
    assert row["trade_stage"] == "skip_now"
    assert row["decision_stage"] == "risk_first"
    assert row["setup_mode"] == "risk_first"
    assert row["trader_action"] == "暂不参与"
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


def test_quote_overlay_falls_back_to_same_day_fullmarket_when_quote_stale(monkeypatch):
    from signals.web.api import workbench

    class _QuoteCollection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "symbol": "SZ.002780",
                "dt": "2026-05-06",
                "snapshot_at": datetime(2026, 5, 6, 15, 0),
                "source": "eastmoney_push2delay_ulist",
                "price": 15.4,
                "change_pct": -1.2,
                "freshness": "fresh",
                "is_stale": False,
            }

    class _FullmarketCollection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "symbol": "SZ.002780",
                "code": "002780",
                "trade_date": "2026-05-08",
                "date_key": "20260508",
                "snapshot_at": datetime(2026, 5, 8, 15, 5),
                "latest": 16.89,
                "price": 16.89,
                "change_pct": 3.8107,
                "open": 16.1,
                "prev_close": 16.27,
            }

    class _Db(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    monkeypatch.setattr(workbench, "_mongo_db", lambda: _Db({
        "quote_snapshots": _QuoteCollection(),
        "fullmarket_spot_snapshots": _FullmarketCollection(),
    }))
    monkeypatch.setattr(workbench, "_a_day_change_mode", lambda: "daily_close")
    monkeypatch.setattr(workbench, "_day_change_expected_day", lambda mode=None: "2026-05-08")

    row = workbench._apply_quote_overlay({"latest_price": 15.4, "day_change_pct": -1.2}, "SZ.002780")

    assert row["quote_status"] == "closed"
    assert row["quote_source"] == "fullmarket_spot_snapshots"
    assert row["latest_price"] == 16.89
    assert row["day_change_pct"] == 3.8107
    assert row["gain_pct"] == 3.8107
    assert row["day_change_source"] == "fullmarket_spot_snapshots"
    assert row["day_change_as_of"] == "2026-05-08"


def test_quote_overlay_falls_back_to_same_day_fullmarket_when_quote_missing(monkeypatch):
    from signals.web.api import workbench

    class _QuoteCollection:
        def find_one(self, query=None, projection=None, sort=None):
            return None

    class _FullmarketCollection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "symbol": "SH.600857",
                "code": "600857",
                "trade_date": "2026-05-08",
                "date_key": "20260508",
                "snapshot_at": datetime(2026, 5, 8, 10, 0),
                "latest": 12.87,
                "price": 12.87,
                "change_pct": 0.1556,
                "open": 12.82,
                "prev_close": 12.85,
            }

    class _Db(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    monkeypatch.setattr(workbench, "_mongo_db", lambda: _Db({
        "quote_snapshots": _QuoteCollection(),
        "fullmarket_spot_snapshots": _FullmarketCollection(),
    }))
    monkeypatch.setattr(workbench, "_stock_df", lambda *args, **kwargs: (_bars(), "unexpected"))
    monkeypatch.setattr(workbench, "_a_day_change_mode", lambda: "quote_intraday")
    monkeypatch.setattr(workbench, "_day_change_expected_day", lambda mode=None: "2026-05-08")
    monkeypatch.setattr(workbench, "_market_now", lambda market="A": datetime(2026, 5, 8, 10, 0, 5))

    row = workbench._enrich_stock_row({"symbol": "SH.600857", "latest_signal": "二买"}, [], lightweight=True)

    assert row["quote_status"] == "realtime"
    assert row["latest_price"] == 12.87
    assert row["day_change_pct"] == 0.1556
    assert row["gain_pct"] == 0.1556
    assert row["day_change_source"] == "fullmarket_spot_snapshots"


def test_quote_overlay_rejects_non_current_fullmarket_fallback(monkeypatch):
    from signals.web.api import workbench

    class _QuoteCollection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "symbol": "SH.600249",
                "dt": "2026-05-07",
                "snapshot_at": datetime(2026, 5, 7, 15, 0),
                "source": "eastmoney_push2delay_ulist",
                "price": 5.94,
                "change_pct": -0.5,
                "freshness": "fresh",
                "is_stale": False,
            }

    class _FullmarketCollection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "symbol": "SH.600249",
                "code": "600249",
                "trade_date": "2026-05-07",
                "date_key": "20260507",
                "snapshot_at": datetime(2026, 5, 7, 15, 5),
                "latest": 5.94,
                "price": 5.94,
                "change_pct": -0.5,
            }

    class _Db(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    monkeypatch.setattr(workbench, "_mongo_db", lambda: _Db({
        "quote_snapshots": _QuoteCollection(),
        "fullmarket_spot_snapshots": _FullmarketCollection(),
    }))
    monkeypatch.setattr(workbench, "_a_day_change_mode", lambda: "daily_close")
    monkeypatch.setattr(workbench, "_day_change_expected_day", lambda mode=None: "2026-05-08")

    row = workbench._apply_quote_overlay({"latest_price": 5.94, "day_change_pct": -0.5}, "SH.600249")

    assert row["quote_status"] == "stale"
    assert row["latest_price"] is None
    assert row["day_change_pct"] is None
    assert row["quote_stale_reason"] == "quote_day=2026-05-07, expected=2026-05-08"


def test_quote_snapshot_watermark_includes_same_day_fullmarket(monkeypatch):
    from signals.web.api import workbench

    class _QuoteCollection:
        def find_one(self, query=None, projection=None, sort=None):
            return {"snapshot_at": datetime(2026, 5, 8, 9, 31)}

    class _FullmarketCollection:
        def find_one(self, query=None, projection=None, sort=None):
            return {"snapshot_at": datetime(2026, 5, 8, 15, 5)}

    class _Db(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    monkeypatch.setattr(workbench, "_mongo_db", lambda: _Db({
        "quote_snapshots": _QuoteCollection(),
        "fullmarket_spot_snapshots": _FullmarketCollection(),
    }))
    monkeypatch.setattr(workbench, "_a_day_change_mode", lambda: "daily_close")
    monkeypatch.setattr(workbench, "_day_change_expected_day", lambda mode=None: "2026-05-08")

    watermark = workbench._quote_snapshot_watermark()

    assert "quote_snapshots:2026-05-08T09:31:00" in watermark
    assert "fullmarket_spot_snapshots:2026-05-08T15:05:00" in watermark


def test_quote_overlay_marks_old_intraday_quote_stale(monkeypatch):
    from signals.web.api import workbench

    class _QuoteCollection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "symbol": "SZ.002759",
                "dt": "2026-05-08",
                "snapshot_at": datetime(2026, 5, 8, 9, 57, 10),
                "source": "eastmoney_push2delay_ulist",
                "price": 38.95,
                "open": 38.89,
                "change_pct": 0.4125,
                "freshness": "fresh",
                "is_stale": False,
            }

    class _Db(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    monkeypatch.setattr(workbench, "_mongo_db", lambda: _Db({"quote_snapshots": _QuoteCollection()}))
    monkeypatch.setattr(workbench, "_a_day_change_mode", lambda: "quote_intraday")
    monkeypatch.setattr(workbench, "_day_change_expected_day", lambda mode=None: "2026-05-08")
    monkeypatch.setattr(workbench, "_market_now", lambda market="A": datetime(2026, 5, 8, 10, 35))

    row = workbench._apply_quote_overlay({"day_change_pct": 9.9, "latest_price": 99.0}, "SZ.002759")

    assert row["quote_status"] == "stale"
    assert row["latest_price"] is None
    assert row["day_change_pct"] is None
    assert "quote_age_seconds" in row["quote_stale_reason"]


def test_apply_quote_overlay_clears_snapshot_when_intraday_quote_missing(monkeypatch):
    from signals.web.api import workbench

    monkeypatch.setattr(workbench, "_a_day_change_mode", lambda: "quote_intraday")

    row = workbench._apply_quote_overlay(
        {
            "latest_price": 88.41,
            "realtime_price": 88.41,
            "day_change_pct": 5.15,
            "daily_change_pct": 5.15,
            "today_change_pct": 5.15,
            "gain_pct": 5.15,
            "day_change_source": "row_snapshot",
        },
        "SZ.301363",
        {"quote_status": "missing", "quote_status_label": "无行情"},
    )

    assert row["latest_price"] is None
    assert row["day_change_pct"] is None
    assert row["daily_change_pct"] is None
    assert row["today_change_pct"] is None
    assert row["gain_pct"] is None
    assert row["day_change_source"] == ""
    assert row["quote_status"] == "missing"


def test_apply_quote_overlay_closed_quote_overrides_snapshot():
    from signals.web.api import workbench

    row = workbench._apply_quote_overlay(
        {
            "latest_price": 88.41,
            "day_change_pct": 5.15,
            "daily_change_pct": 5.15,
            "today_change_pct": 5.15,
            "gain_pct": 5.15,
            "day_change_source": "row_snapshot",
        },
        "SZ.301363",
        {
            "day_change_mode": "daily_close",
            "quote_status": "closed",
            "quote_price": 30.16,
            "quote_change_pct": 4.0359,
            "quote_as_of": "2026-05-08",
        },
    )

    assert row["latest_price"] == 30.16
    assert row["day_change_pct"] == 4.0359
    assert row["gain_pct"] == 4.0359
    assert row["day_change_basis"] == "prev_close"
    assert row["day_change_source"] == "quote_snapshots"


def test_enrich_scored_stock_rows_refreshes_visible_quotes(monkeypatch):
    from signals.web.api import workbench

    refresh_calls = []
    monkeypatch.setattr(workbench, "_mongo_db", lambda: {"quote_snapshots": object()})
    monkeypatch.setattr(
        workbench,
        "_refresh_realtime_quotes_for_rows",
        lambda db, rows, *, refresh_key, limit: refresh_calls.append({
            "refresh_key": refresh_key,
            "symbols": [row.get("symbol") for row in rows],
            "limit": limit,
        }) or {"status": "ok"},
    )
    monkeypatch.setattr(workbench, "_quote_overlay_for_symbol", lambda symbol: {
        "day_change_mode": "quote_intraday",
        "quote_status": "realtime",
        "latest_price": 38.06,
        "day_change_pct": -1.882,
        "day_change_source": "quote_snapshots",
        "day_change_basis": "prev_close",
    })

    rows = workbench._enrich_scored_stock_rows([{"symbol": "SZ.002759", "name": "天际股份"}], [])

    assert refresh_calls[0]["refresh_key"] == "scored_stocks"
    assert refresh_calls[0]["symbols"] == ["SZ.002759"]
    assert rows[0]["day_change_pct"] == -1.882
    assert rows[0]["day_change_basis"] == "prev_close"


def test_slim_shell_stock_row_preserves_quote_basis_fields():
    from signals.web.api import workbench

    row = workbench._slim_shell_stock_row({
        "symbol": "SZ.002759",
        "name": "天际股份",
        "latest_price": 38.02,
        "day_change_pct": -1.985,
        "day_change_basis": "prev_close",
        "quote_open_price": 38.89,
        "quote_open_change_pct": -2.2371,
        "quote_prev_close_change_pct": -1.985,
    })

    assert row["day_change_basis"] == "prev_close"
    assert row["quote_open_price"] == 38.89
    assert row["quote_prev_close_change_pct"] == -1.985


def test_quote_overlay_prefers_realtime_quote_over_minute_change(monkeypatch):
    from signals.web.api import workbench

    monkeypatch.setattr(
        workbench,
        "_quote_overlay_for_symbol",
        lambda symbol: {
            "day_change_mode": "quote_intraday",
            "quote_status": "realtime",
            "quote_status_label": "实时",
            "latest_price": 1691.07,
            "realtime_price": 1691.07,
            "day_change_pct": 7.64,
            "daily_change_pct": 7.64,
            "today_change_pct": 7.64,
            "day_change_source": "quote_snapshots",
            "day_change_as_of": "2026-05-06",
        },
    )

    row = workbench._apply_quote_overlay(
        {
            "latest_price": 1692.61,
            "day_change_pct": 3.57,
            "daily_change_pct": 3.57,
            "today_change_pct": 3.57,
            "gain_pct": 3.57,
            "day_change_mode": "minute_intraday",
            "day_change_source": "index_bars:5min",
            "day_change_freq": "5min",
        },
        "sh000688",
    )

    assert row["latest_price"] == 1691.07
    assert row["day_change_pct"] == 7.64
    assert row["daily_change_pct"] == 7.64
    assert row["today_change_pct"] == 7.64
    assert row["gain_pct"] == 7.64
    assert row["day_change_mode"] == "quote_intraday"
    assert row["day_change_source"] == "quote_snapshots"
    assert row["day_change_freq"] == ""


def test_lightweight_stock_row_uses_quote_without_kline(monkeypatch):
    from signals.web.api import workbench

    def _unexpected_stock_df(*args, **kwargs):
        raise AssertionError("lightweight shell rows must not load kline data")

    monkeypatch.setattr(workbench, "_stock_df", _unexpected_stock_df)
    monkeypatch.setattr(workbench, "_a_day_change_mode", lambda: "quote_intraday")
    monkeypatch.setattr(
        workbench,
        "_quote_overlay_for_symbol",
        lambda symbol: {
            "day_change_mode": "quote_intraday",
            "quote_status": "realtime",
            "quote_status_label": "实时",
            "latest_price": 10.5,
            "realtime_price": 10.5,
            "day_change_pct": 3.2,
            "daily_change_pct": 3.2,
            "today_change_pct": 3.2,
            "day_change_source": "quote_snapshots",
            "day_change_as_of": "2026-05-06",
        },
    )

    row = workbench._enrich_stock_row({"symbol": "SH.600000", "latest_signal": "一买"}, [], lightweight=True)

    assert row["latest_price"] == 10.5
    assert row["day_change_pct"] == 3.2
    assert row["day_change_source"] == "quote_snapshots"
    assert row["range_returns"] == {}


def test_lightweight_stock_row_uses_closed_quote_without_kline(monkeypatch):
    from signals.web.api import workbench

    def _unexpected_stock_df(*args, **kwargs):
        raise AssertionError("lightweight shell rows must not load kline data")

    monkeypatch.setattr(workbench, "_stock_df", _unexpected_stock_df)
    monkeypatch.setattr(workbench, "_a_day_change_mode", lambda: "daily_close")
    monkeypatch.setattr(
        workbench,
        "_quote_overlay_for_symbol",
        lambda symbol: {
            "day_change_mode": "daily_close",
            "quote_status": "closed",
            "quote_status_label": "收盘",
            "quote_price": 62.09,
            "quote_change_pct": 4.67,
            "quote_source": "fullmarket_spot_snapshot",
            "quote_as_of": "2026-05-06",
        },
    )

    row = workbench._enrich_stock_row({"symbol": "SH.688400", "latest_signal": "一买"}, [], lightweight=True)

    assert row["latest_price"] == 62.09
    assert row["day_change_pct"] == 4.67
    assert row["day_change_source"] == "quote_snapshots"
    assert row["day_change_as_of"] == "2026-05-06"


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


def test_stock_row_uses_daily_close_day_change_after_close(monkeypatch):
    from signals.web.api import workbench

    daily = pd.DataFrame(
        {"open": [100.0, 110.0], "close": [100.0, 110.0]},
        index=pd.to_datetime(["2026-04-29", "2026-04-30"]),
    )
    minute_5 = pd.DataFrame(
        {"open": [100.0, 101.0], "close": [101.0, 103.0]},
        index=pd.to_datetime(["2026-04-30 09:35", "2026-04-30 09:40"]),
    )

    def fake_stock_df(symbol, freq):
        return (minute_5, "bars") if freq == "5min" else (daily, "daily_bars")

    monkeypatch.setattr(workbench, "_stock_df", fake_stock_df)
    monkeypatch.setattr(workbench, "_a_day_change_mode", lambda: "daily_close")
    monkeypatch.setattr(workbench, "_day_change_expected_day", lambda mode=None: "2026-04-30")
    monkeypatch.setattr(workbench, "_quote_overlay_for_symbol", lambda symbol: {"quote_status": "stale", "quote_status_label": "行情陈旧"})

    row = workbench._enrich_stock_row({"symbol": "SH.600000", "name": "测试股"}, [])

    assert row["day_change_pct"] == 10.0
    assert row["daily_change_pct"] == 10.0
    assert row["today_change_pct"] == 10.0
    assert row["day_change_mode"] == "daily_close"
    assert row["day_change_freq"] == ""
    assert row["latest_price"] == 110.0
    assert row["quote_status"] == "stale"


def test_index_row_uses_daily_close_day_change_after_close(monkeypatch):
    from signals.web.api import workbench

    daily = pd.DataFrame(
        {"open": [3000.0, 3300.0], "close": [3000.0, 3300.0]},
        index=pd.to_datetime(["2026-04-29", "2026-04-30"]),
    )
    minute_5 = pd.DataFrame(
        {"open": [3000.0, 3010.0], "close": [3010.0, 3060.0]},
        index=pd.to_datetime(["2026-04-30 09:35", "2026-04-30 09:40"]),
    )

    def fake_index_df(symbol, freq):
        return (minute_5, "index_bars") if freq == "5min" else (daily, "index_daily")

    monkeypatch.setattr(workbench, "_index_df", fake_index_df)
    monkeypatch.setattr(workbench, "_a_day_change_mode", lambda: "daily_close")
    monkeypatch.setattr(workbench, "_day_change_expected_day", lambda mode=None: "2026-04-30")
    monkeypatch.setattr(workbench, "_quote_overlay_for_symbol", lambda symbol: {"quote_status": "missing", "quote_status_label": "无行情"})

    row = workbench._enrich_index_row({"symbol": "sh000001", "name": "上证指数"}, [])

    assert row["day_change_pct"] == 10.0
    assert row["daily_change_pct"] == 10.0
    assert row["today_change_pct"] == 10.0
    assert row["day_change_mode"] == "daily_close"
    assert row["day_change_freq"] == ""
    assert row["latest_price"] == 3300.0


def test_index_intraday_day_change_uses_previous_daily_close(monkeypatch):
    from signals.web.api import workbench

    daily = pd.DataFrame(
        {"open": [1000.0, 1080.0], "close": [1000.0, 1100.0]},
        index=pd.to_datetime(["2026-04-29", "2026-04-30"]),
    )
    minute_5 = pd.DataFrame(
        {"open": [1200.0, 1205.0], "close": [1205.0, 1210.0]},
        index=pd.to_datetime(["2026-05-06 09:35", "2026-05-06 09:40"]),
    )

    def fake_index_df(symbol, freq):
        return (minute_5, "index_bars") if freq == "5min" else (daily, "index_daily")

    monkeypatch.setattr(workbench, "_index_df", fake_index_df)
    monkeypatch.setattr(workbench, "_a_day_change_mode", lambda: "quote_intraday")
    monkeypatch.setattr(workbench, "_quote_overlay_for_symbol", lambda symbol: {"quote_status": "missing", "quote_status_label": "无行情"})

    row = workbench._enrich_index_row({"symbol": "sh000688", "name": "科创50"}, [])

    assert row["latest_price"] == 1210.0
    assert row["day_change_pct"] == 10.0
    assert row["daily_change_pct"] == 10.0
    assert row["today_change_pct"] == 10.0
    assert row["day_change_source"] == "index_bars:5min"


def test_scored_stock_rows_refresh_even_when_stale_price_present(monkeypatch):
    from signals.web.api import workbench

    def fake_enrich(row, range_columns, **kwargs):
        row.update({
            "latest_price": 62.1,
            "day_change_pct": 2.51,
            "day_change_mode": "quote_intraday",
            "day_change_source": "quote_snapshots",
        })
        return row

    monkeypatch.setattr(workbench, "_enrich_stock_row", fake_enrich)

    rows = workbench._enrich_scored_stock_rows(
        [{"symbol": "SZ.002709", "name": "天赐材料", "latest_price": 61.78, "day_change_pct": 1.98}],
        [],
    )

    assert rows[0]["latest_price"] == 62.1
    assert rows[0]["day_change_pct"] == 2.51


def test_sector_preview_prefers_shortest_board_heat_minute(monkeypatch):
    from signals.web.api import workbench

    def fake_board_heat_chart(name, kind, freq):
        value = {"5min": 2.4, "15min": 1.8, "30min": 1.2}[freq]
        return (
            {"ohlcv": [{"time": 1777530000, "close": value}], "meta": {"freq": freq}},
            {"change_pct": value, "trade_minute": datetime(2026, 4, 30, 9, 40), "source": "test_heat"},
        )

    monkeypatch.setattr(workbench, "resolve_board_heat_name", lambda kind, label: {"query": label, "heat_name": label, "status": "exact"})
    monkeypatch.setattr(workbench, "_industry_carrier_candidates", lambda name, leader_name="": [])
    monkeypatch.setattr(workbench, "_candidate_groups", lambda candidates, heat_value=None: {})
    monkeypatch.setattr(workbench, "_latest_board_heat_day_change", lambda kind, name: (9.9, "2026-04-30"))
    monkeypatch.setattr(workbench, "_board_heat_chart", fake_board_heat_chart)

    row = workbench._sector_board_preview({"label": "半导体", "change_pct": 9.9}, "industry")

    assert row["day_change_pct"] == 2.4
    assert row["daily_change_pct"] == 2.4
    assert row["today_change_pct"] == 2.4
    assert row["gain_pct"] == 2.4
    assert row["day_change_mode"] == "minute_intraday"
    assert row["day_change_freq"] == "5min"
    assert row["day_change_source"] == "board_heat_ticks"


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
    assert payload["candidate_groups"]["upstream"]
    assert payload["candidate_stocks"]
    leader = payload["candidate_groups"]["leaders"][0]
    assert leader["leader_tier"] == "龙头"
    assert leader["chain_role"]
    assert leader["weight_score"] >= 0
    assert leader["elasticity_score"] >= 0
    assert leader["attention_score"] >= 0
    assert leader["why_watch"]
    assert isinstance(leader["risk_flags"], list)


def test_concept_minute_target_falls_back_to_chain_carrier_when_heat_missing(monkeypatch):
    from signals.web.api import workbench

    class FakeEngine:
        def get_concepts(self):
            return []

    async def fake_stock_target(symbol, raw_code, freq):
        return {
            "target": {"kind": "stock", "label": symbol, "symbol": symbol, "requested_freq": freq},
            "chart": {
                "ohlcv": [{"time": "2026-05-06 15:00", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 100}],
                "meta": {"source": "bars", "freq": freq},
            },
            "summary": {"title": "中科曙光"},
            "signals": [],
            "plan": None,
            "review": {},
            "trade": {},
        }

    monkeypatch.setattr(workbench, "_concept_theme_candidates", lambda name: [])
    monkeypatch.setattr(workbench, "_concept_rank_rows", lambda name, themes: [])
    monkeypatch.setattr(workbench, "_concept_constituent_symbols", lambda name, themes: [])
    monkeypatch.setattr(workbench, "_industry_constituent_symbols", lambda name: [])
    monkeypatch.setattr(workbench, "_board_heat_chart", lambda name, kind, freq: ({"ohlcv": [], "meta": {"source": "board_heat_ticks", "freq": freq}}, {}))
    monkeypatch.setattr(workbench, "_build_stock_target", fake_stock_target)

    payload = asyncio.run(workbench._build_concept_target(FakeEngine(), "算力租赁", "30min"))

    assert payload["target"]["kind"] == "concept"
    assert payload["target"]["carrier_kind"] == "stock"
    assert payload["target"]["fallback_reason"] == "concept_heat_not_ready"
    assert payload["chart"]["ohlcv"]
    assert payload["candidate_stocks"][0]["chain_id"] == "ai_compute"
    assert payload["candidate_stocks"][0]["node_id"] == "compute_service_operator"


def test_concept_click_uses_chain_rebuild_rollups_before_yaml(monkeypatch):
    from signals.web.api import workbench

    class _Cursor(list):
        def sort(self, *args, **kwargs):
            return self

        def limit(self, n):
            return _Cursor(self[:n])

    class _Collection:
        def __init__(self, doc=None, docs=None):
            self.doc = doc or {}
            self.docs = docs or []

        def find_one(self, query=None, projection=None, sort=None):
            return self.doc

        def find(self, query=None, projection=None):
            return _Cursor(self.docs)

    class _Db(dict):
        def __missing__(self, key):
            self[key] = _Collection()
            return self[key]

        def list_collection_names(self):
            return list(self.keys())

    db = _Db({
        "source_board_chain_mappings": _Collection(doc={
            "trade_date": "2026-04-30",
            "kind": "concept",
            "canonical_name": "锂",
            "mapping_status": "mapped",
            "chain_id": "lithium_battery",
            "chain_name": "电新/锂电池产业链",
            "node_id": "lithium_resource",
            "node_name": "锂资源",
            "layer": "upstream",
            "stage": "上游",
            "confidence": 96,
        }),
        "chain_node_security_rollups": _Collection(doc={
            "trade_date": "2026-04-30",
            "chain_id": "lithium_battery",
            "node_id": "lithium_resource",
            "top_securities": [
                {"symbol": "SZ.002466", "raw_code": "002466", "name": "天齐锂业", "is_primary_chain": True, "exposure_score": 108, "confidence": 96},
                {"symbol": "SZ.002460", "raw_code": "002460", "name": "赣锋锂业", "is_primary_chain": True, "exposure_score": 106, "confidence": 96},
            ],
        }),
    })
    monkeypatch.setattr(workbench, "_mongo_db", lambda: db)

    rows = workbench._concept_carrier_candidates("锂", [], [])

    assert rows[0]["source"] == "chain_rebuild_rollup"
    assert rows[0]["symbol"] == "SZ.002466"
    assert rows[0]["chain_id"] == "lithium_battery"
    assert rows[0]["node_id"] == "lithium_resource"


def test_chain_rebuild_candidates_ignore_stale_taxonomy_nodes(monkeypatch):
    from signals.web.api import workbench

    class _Collection:
        def __init__(self, doc=None):
            self.doc = doc or {}

        def find_one(self, query=None, projection=None, sort=None):
            return self.doc

    class _Db(dict):
        def __missing__(self, key):
            self[key] = _Collection()
            return self[key]

    db = _Db({
        "source_board_chain_mappings": _Collection(doc={
            "trade_date": "2026-04-30",
            "kind": "industry",
            "canonical_name": "贵金属",
            "mapping_status": "mapped",
            "chain_id": "nonferrous",
            "chain_name": "有色/贵金属产业链",
            "node_id": "metal_resource",
            "node_name": "金属资源",
            "confidence": 96,
        }),
        "chain_node_security_rollups": _Collection(doc={
            "top_securities": [
                {"symbol": "SZ.002460", "raw_code": "002460", "name": "赣锋锂业", "is_primary_chain": True},
            ],
        }),
    })
    monkeypatch.setattr(workbench, "_mongo_db", lambda: db)

    assert workbench._chain_rebuild_board_candidates("贵金属", "industry") == []


def test_electrolyte_relation_groups_include_upstream_and_downstream(monkeypatch):
    from signals.web.api import workbench

    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (_bars(), "unit_test"))

    candidates = workbench._taxonomy_adjacent_chain_candidates("lithium_battery", "electrolyte")
    groups = workbench._candidate_groups(candidates, heat_value=5.0)

    upstream_symbols = {item["symbol"] for item in groups["upstream"]}
    downstream_symbols = {item["symbol"] for item in groups["downstream"]}

    assert {"SZ.002407", "SZ.002759"} <= upstream_symbols
    assert {"SZ.300750", "SZ.300014"} <= downstream_symbols
    assert groups["upstream"][0]["chain_relation_type"] == "upstream"
    assert groups["downstream"][0]["chain_relation_type"] == "downstream"

    concept_candidates = workbench._concept_carrier_candidates("电解液", [], [])
    concept_groups = workbench._candidate_groups(concept_candidates, heat_value=5.0)
    assert {"SZ.002407", "SZ.002759"} <= {item["symbol"] for item in concept_groups["upstream"]}
    assert "SZ.300750" in {item["symbol"] for item in concept_groups["downstream"]}


def test_chain_heat_sector_docs_keep_chain_diversity():
    from signals.web.api import workbench

    docs = [
        {"chain_id": "semi", "node_id": "foundry", "rank": 1},
        {"chain_id": "semi", "node_id": "equipment", "rank": 2},
        {"chain_id": "semi", "node_id": "materials", "rank": 3},
        {"chain_id": "lithium", "node_id": "resource", "rank": 4},
        {"chain_id": "lithium", "node_id": "cathode", "rank": 5},
        {"chain_id": "ai_compute", "node_id": "operator", "rank": 6},
    ]

    rows = workbench._diversify_chain_heat_docs(docs, limit=5, max_nodes_per_chain=2)

    assert [row["node_id"] for row in rows[:5]] == [
        "foundry",
        "equipment",
        "resource",
        "cathode",
        "operator",
    ]
    assert sum(1 for row in rows if row["chain_id"] == "semi") == 2


def test_chain_representative_quote_rows_collects_nested_reps():
    from signals.web.api import workbench

    rows = workbench._chain_representative_quote_rows([
        {
            "leader_symbol": "SZ.300308",
            "leader_name": "中际旭创",
            "representatives": [
                {"symbol": "SZ.300308", "name": "中际旭创"},
                {"symbol": "SZ.002281", "name": "光迅科技"},
            ],
            "integrated_domains": [
                {
                    "leader_symbol": "SH.688498",
                    "leader_name": "源杰科技",
                    "representatives": [{"symbol": "SZ.300502", "name": "新易盛"}],
                }
            ],
        }
    ])

    assert [row["symbol"] for row in rows] == [
        "SZ.300308",
        "SZ.002281",
        "SH.688498",
        "SZ.300502",
    ]


def test_chain_heat_display_context_explains_driver_reference_and_representatives():
    from signals.web.api import workbench

    context = workbench._chain_heat_display_context({
        "chain_name": "基础化工产业链",
        "node_name": "化工材料",
        "integrated_domains": [
            {
                "kind": "concept",
                "name": "超导概念",
                "change_pct": 3.48,
                "up_count": 18,
                "down_count": 1,
                "mapping_confidence": 96,
            },
            {
                "kind": "industry",
                "name": "基础化工",
                "change_pct": 0.21,
                "up_count": 215,
                "down_count": 219,
                "mapping_confidence": 60,
            },
        ],
    }, {
        "leaders": [{"symbol": "SH.600309", "name": "万华化学", "day_change_pct": -5.49}],
        "elastic": [{"symbol": "SZ.002648", "name": "卫星化学", "day_change_pct": -7.94}],
    })

    assert context["change_display_kind"] == "chain_driver_change"
    assert context["primary_domain"]["name"] == "超导概念"
    assert context["reference_domain"]["name"] == "基础化工"
    assert context["representative_confirmation"]["status"] == "not_confirmed"
    assert "driver_not_same_as_chain_label" in context["mismatch_flags"]
    assert "driver_reference_divergence" in context["mismatch_flags"]
    assert "主驱动 超导概念 +3.48%" in context["change_explain"]
    assert "参考行业 基础化工 +0.21%" in context["change_explain"]


def test_slim_sector_row_preserves_chain_change_truth_fields():
    from signals.web.api import workbench

    slim = workbench._slim_shell_sector_row({
        "kind": "concept",
        "label": "基础化工产业链 · 化工材料",
        "name": "基础化工产业链 · 化工材料",
        "day_change_pct": 3.48,
        "change_display_kind": "chain_driver_change",
        "change_display_label": "驱动涨幅",
        "change_explain": "主驱动 超导概念 +3.48%；参考行业 基础化工 +0.21%；代表股未跟随",
        "primary_domain": {"kind": "concept", "name": "超导概念", "change_pct": 3.48},
        "reference_domain": {"kind": "industry", "name": "基础化工", "change_pct": 0.21},
        "representative_confirmation": {"status": "not_confirmed", "label": "代表股未跟随"},
        "mismatch_flags": ["driver_reference_divergence", "representatives_not_confirmed"],
    })

    assert slim["change_display_label"] == "驱动涨幅"
    assert slim["primary_domain"]["name"] == "超导概念"
    assert slim["reference_domain"]["change_pct"] == 0.21
    assert "representatives_not_confirmed" in slim["mismatch_flags"]


def test_lightweight_chain_representatives_apply_quote_overlay(monkeypatch):
    from signals.web.api import workbench

    monkeypatch.setattr(workbench, "_quote_overlay_for_symbol", lambda symbol: {
        "day_change_mode": "daily_close",
        "quote_status": "closed",
        "quote_price": 38.88,
        "quote_change_pct": 1.65,
        "quote_as_of": "2026-05-07",
    })
    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (pd.DataFrame(), ""))

    groups = workbench._candidate_groups_from_representatives({
        "heat_score": 58.9,
        "chain_id": "telecom_network",
        "chain_name": "通信网络/5G产业链",
        "node_id": "telecom_equipment",
        "node_name": "通信设备/网络建设",
        "representatives": [
            {
                "symbol": "SZ.000063",
                "name": "中兴通讯",
                "relation": "通信设备/5G链主",
                "representative_type": "core",
                "priority": 100,
            }
        ],
    }, lightweight=True)

    leader = groups["leaders"][0]
    assert leader["latest_price"] == 38.88
    assert leader["day_change_pct"] == 1.65
    assert leader["day_change_source"] == "quote_snapshots"


def test_chain_representatives_preserve_core_leader_rank(monkeypatch):
    from signals.web.api import workbench

    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (pd.DataFrame(), ""))

    groups = workbench._candidate_groups_from_representatives({
        "heat_score": 58.9,
        "chain_id": "copper_interconnect",
        "chain_name": "铜连接/高速连接器产业链",
        "node_id": "copper_connector_core",
        "node_name": "高速铜连接/连接器",
        "representatives": [
            {"symbol": "SZ.002130", "name": "沃尔核材", "representative_type": "core", "priority": 100},
            {"symbol": "SZ.300563", "name": "神宇股份", "representative_type": "core", "priority": 94},
            {"symbol": "SZ.300913", "name": "兆龙互连", "representative_type": "core", "priority": 90},
        ],
    }, lightweight=True)

    assert [row["leader_tier"] for row in groups["leaders"]] == ["龙头", "龙二", "龙三"]


def test_chain_heat_representatives_preserve_upstream_downstream_groups(monkeypatch):
    from signals.web.api import workbench

    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (pd.DataFrame(), ""))

    groups = workbench._candidate_groups_from_representatives({
        "heat_score": 58.9,
        "chain_id": "lithium_battery",
        "chain_name": "电新/锂电池产业链",
        "node_id": "electrolyte",
        "node_name": "电解液",
        "representatives": [
            {"symbol": "SZ.002709", "name": "天赐材料", "representative_type": "core", "priority": 100},
            {"symbol": "SZ.002407", "name": "多氟多", "representative_type": "upstream", "priority": 94},
            {"symbol": "SZ.300750", "name": "宁德时代", "representative_type": "downstream", "priority": 100},
            {"symbol": "SZ.002759", "name": "天际股份", "representative_type": "elastic", "priority": 84},
        ],
    }, lightweight=True)

    assert [row["symbol"] for row in groups["leaders"]] == ["SZ.002709"]
    assert [row["symbol"] for row in groups["upstream"]] == ["SZ.002407"]
    assert [row["symbol"] for row in groups["downstream"]] == ["SZ.300750"]
    assert [row["symbol"] for row in groups["elastic"]] == ["SZ.002759"]


def test_related_custom_signals_round_robin_symbols(monkeypatch):
    from signals.web.api import workbench

    rows_by_symbol = {
        "SZ.000001": [
            {"signal_type": f"信号A{i}", "freq": "30min", "signal_date": f"2026-05-07T10:0{i}:00"}
            for i in range(4)
        ],
        "SZ.000002": [
            {"signal_type": f"信号B{i}", "freq": "30min", "signal_date": f"2026-05-07T10:1{i}:00"}
            for i in range(3)
        ],
        "SZ.000003": [
            {"signal_type": "信号C0", "freq": "30min", "signal_date": "2026-05-07T10:20:00"}
        ],
    }
    monkeypatch.setattr(workbench, "_load_terminal_technical_signal_rows", lambda symbol, limit=80: rows_by_symbol.get(symbol, []))
    monkeypatch.setattr(workbench, "_custom_signal_rows", lambda symbol, limit=200: [])

    related = workbench._related_custom_signals_from_candidates([
        {"symbol": "SZ.000001", "name": "一号"},
        {"symbol": "SZ.000002", "name": "二号"},
        {"symbol": "SZ.000003", "name": "三号"},
    ], "30min", limit=5)

    assert [row["symbol"] for row in related] == ["SZ.000001", "SZ.000002", "SZ.000003", "SZ.000001", "SZ.000002"]


def test_chain_backed_concept_skips_generic_industry_leaders(monkeypatch):
    from signals.web.api import workbench

    monkeypatch.setattr(workbench, "_chain_rebuild_board_candidates", lambda name, kind: [])
    monkeypatch.setattr(workbench, "_concept_rank_rows", lambda name, themes: [])
    monkeypatch.setattr(workbench, "_concept_constituent_symbols", lambda name, themes: [])
    monkeypatch.setattr(workbench, "_industry_constituent_symbols", lambda name: [])

    rows = workbench._concept_carrier_candidates("锂", [], ["化学原料", "化学制品"])

    assert any(row.get("node_id") == "lithium_resource" for row in rows)
    assert "industry_leader_map" not in {row.get("source") for row in rows}
    assert "SH.600309" not in {row.get("symbol") for row in rows}
    assert "SZ.002648" not in {row.get("symbol") for row in rows}


def test_compute_leasing_skips_hardware_and_game_industry_leaders(monkeypatch):
    from signals.web.api import workbench

    monkeypatch.setattr(workbench, "_chain_rebuild_board_candidates", lambda name, kind: [])
    monkeypatch.setattr(workbench, "_concept_rank_rows", lambda name, themes: [])
    monkeypatch.setattr(workbench, "_concept_constituent_symbols", lambda name, themes: [])
    monkeypatch.setattr(workbench, "_industry_constituent_symbols", lambda name: [])

    rows = workbench._concept_carrier_candidates("算力租赁", [], ["计算机设备", "软件开发", "互联网服务"])
    symbols = {row.get("symbol") for row in rows}

    assert any(row.get("node_id") == "compute_service_operator" for row in rows)
    assert "industry_leader_map" not in {row.get("source") for row in rows}
    assert not {"SZ.000977", "SZ.002230", "SZ.002555"} & symbols


def test_stock_chain_summary_prefers_security_membership(monkeypatch):
    from signals.web.api import workbench

    class _Collection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "trade_date": "2026-04-30",
                "symbol": "SZ.002466",
                "raw_code": "002466",
                "chain_id": "lithium_battery",
                "chain_name": "电新/锂电池产业链",
                "node_id": "lithium_resource",
                "node_name": "锂资源",
                "layer": "upstream",
                "stage": "上游",
                "role": "锂资源",
                "confidence": 96,
                "exposure_score": 108,
                "is_primary_chain": True,
            }

    class _Db(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    monkeypatch.setattr(workbench, "_mongo_db", lambda: _Db({"security_chain_memberships": _Collection()}))

    summary = workbench._stock_chain_position_summary("SZ.002466")

    assert summary["source"] == "security_chain_memberships"
    assert summary["chain_id"] == "lithium_battery"
    assert summary["node_id"] == "lithium_resource"
    assert summary["chain"] == "电新/锂电池产业链"
    assert summary["node"] == "锂资源"
    assert summary["is_primary_chain"] is True


def test_stock_chain_summary_overrides_stale_taxonomy_membership(monkeypatch):
    from signals.web.api import workbench

    class _Collection:
        def find_one(self, query=None, projection=None, sort=None):
            return {
                "trade_date": "2026-05-08",
                "symbol": "SZ.002759",
                "raw_code": "002759",
                "chain_id": "lithium_battery",
                "chain_name": "电新/锂电池产业链",
                "node_id": "electrolyte",
                "node_name": "电解液",
                "layer": "midstream",
                "stage": "中游",
                "role": "六氟磷酸锂弹性标的",
                "confidence": 92,
                "exposure_score": 96,
                "is_primary_chain": True,
                "taxonomy_representative": True,
                "representative_type": "elastic",
                "representative_priority": 84,
            }

    class _Db(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    monkeypatch.setattr(workbench, "_mongo_db", lambda: _Db({"security_chain_memberships": _Collection()}))

    summary = workbench._stock_chain_position_summary("SZ.002759")

    assert summary["source"] == "industry_chains.yaml"
    assert summary["chain_id"] == "lithium_battery"
    assert summary["node_id"] == "lipf6_lithium_salt"
    assert summary["node"] == "六氟磷酸锂/锂盐"
    assert summary["stale_membership_node_id"] == "electrolyte"


def test_stock_chain_context_exposes_same_node_candidate_groups(monkeypatch):
    from signals.web.api import workbench

    class _Cursor(list):
        def sort(self, *args, **kwargs):
            return self

        def limit(self, limit):
            return _Cursor(self[:limit])

    class _Collection:
        def __init__(self, doc=None, rows=None):
            self.doc = doc or {}
            self.rows = rows or []

        def find_one(self, query=None, projection=None, sort=None):
            return self.doc

        def find(self, query=None, projection=None):
            return _Cursor(self.rows)

    class _Db(dict):
        def __missing__(self, key):
            self[key] = _Collection()
            return self[key]

    membership = {
        "trade_date": "2026-05-06",
        "symbol": "SH.688400",
        "raw_code": "688400",
        "chain_id": "robotics",
        "chain_name": "机器人/自动化产业链",
        "node_id": "automation",
        "node_name": "自动化/机器人",
        "layer": "midstream",
        "stage": "中游",
        "role": "中游",
        "confidence": 96,
        "exposure_score": 106,
        "is_primary_chain": True,
    }
    db = _Db({
        "security_chain_memberships": _Collection(doc=membership),
        "chain_node_security_rollups": _Collection(doc={
            "trade_date": "2026-05-06",
            "chain_id": "robotics",
            "node_id": "automation",
            "top_securities": [
                {"symbol": "SH.688218", "raw_code": "688218", "name": "江苏北人", "is_primary_chain": True, "exposure_score": 110, "confidence": 96},
                {"symbol": "SZ.002698", "raw_code": "002698", "name": "博实股份", "is_primary_chain": True, "exposure_score": 110, "confidence": 96},
                {"symbol": "SH.688400", "raw_code": "688400", "name": "凌云光", "is_primary_chain": True, "exposure_score": 106, "confidence": 96},
            ],
        }),
    })
    monkeypatch.setattr(workbench, "_mongo_db", lambda: db)
    monkeypatch.setattr(workbench, "_stock_df", lambda symbol, freq: (_bars(), "test_daily"))

    position = workbench._stock_chain_position_summary("SH.688400")
    context = workbench._stock_chain_context(position)

    assert context["chain_name"] == "机器人/自动化产业链"
    assert context["node_name"] == "自动化/机器人"
    assert context["mapping_chain"]["chain_id"] == "robotics"
    assert context["candidate_groups"]["leaders"]
    assert {row["symbol"] for row in context["candidate_groups"]["leaders"]} >= {"SH.688218", "SZ.002698"}
    assert context["focus_stocks_preview"]


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


def test_manual_clue_attack_focus_promotes_right_side_setup():
    from signals.web.api import workbench

    promoted = workbench._manual_clue_attack_focus_rows([
        {
            "symbol": "SZ.002050",
            "name": "三花智控",
            "missing_gates": ["trigger_30m"],
            "upper_timeframe_side": "right",
            "trade_timeframe_side": "none",
            "execution_timeframe_side": "right",
            "technical_evidence": {"signal_type": "15分钟 MACD绿柱扩大_零上"},
        },
        {
            "symbol": "SH.688802",
            "name": "沐曦股份",
            "missing_gates": ["risk_clear", "trigger_30m"],
            "upper_timeframe_side": "right",
            "trade_timeframe_side": "none",
            "execution_timeframe_side": "right",
            "technical_evidence": {"signal_type": "5分钟 趋势买"},
        },
    ], existing_focus=[])

    assert [row["symbol"] for row in promoted] == ["SZ.002050"]
    assert promoted[0]["stage_label"] == "进攻买点"
    assert promoted[0]["queue_lane"] == "entry_waiting_confirm"
    assert promoted[0]["trader_action"] == "进攻买点复核"
    assert promoted[0]["can_trade_now"] is True


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


def test_trader_task_queue_excludes_risk_rows_and_legacy_buy_without_hard_technical():
    from signals.web.api import workbench

    tasks = workbench._build_trader_task_queue(
        decision_rows=[
            {
                "title": "买点池 · 测试股",
                "symbol": "SZ.000001",
                "action_label": "复合买点",
                "queue_lane": "risk_exit_first",
                "summary": "打开图表复合买点池、关键均线方向和止损位。",
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

    assert tasks == []


def test_trade_map_uses_setup_modes_and_excludes_risk_rows():
    from signals.web.api import workbench

    trade_map = workbench._build_trade_map(
        sector_boards=[],
        focus_stocks=[
            {
                "symbol": "SZ.000001",
                "name": "左侧股",
                "setup_mode": "left_attack",
                "trader_read": "一买叠加10/20日线承接。",
            },
            {
                "symbol": "SZ.000002",
                "name": "右侧股",
                "setup_mode": "right_attack",
                "trader_read": "30m二买叠加5/10/20日线。",
            },
        ],
        watch_stocks=[
            {
                "symbol": "SZ.000003",
                "name": "观察股",
                "setup_mode": "watch",
                "trader_read": "还缺均线确认。",
            }
        ],
        risk_stocks=[
            {
                "symbol": "SZ.000004",
                "name": "风险股",
                "setup_mode": "risk_first",
                "trader_read": "非持仓不推风险动作。",
            }
        ],
        clue_stocks=[
            {
                "symbol": "SZ.000005",
                "name": "线索股",
                "setup_mode": "clue",
            }
        ],
    )

    assert [item["label"] for item in trade_map["role_filters"]] == ["全部", "低吸进攻", "右侧进攻", "盯盘观察", "线索池"]
    assert trade_map["role_counts"] == {"left_attack": 1, "right_attack": 1, "watch": 1, "clue": 1}
    assert [item["role"] for item in trade_map["items"]] == ["left_attack", "right_attack", "watch", "clue"]
    assert trade_map["risk_policy"] == "risk_stocks_excluded_from_opportunity_map"


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


def test_static_index_targets_do_not_use_recent_stock_candidate_fallback(monkeypatch):
    from signals.web.api import workbench

    monkeypatch.setattr(workbench, "_index_df", lambda symbol, freq: (_intraday_bars(), "index_bars"))
    monkeypatch.setattr(workbench, "_target_diagnostics", lambda *args, **kwargs: {"cache_probe": {"status": "hit"}})
    monkeypatch.setattr(workbench, "_ensure_engine", lambda: (_ for _ in ()).throw(RuntimeError("engine unavailable")))
    monkeypatch.setattr(workbench, "_recent_custom_signal_candidates", lambda limit=10: [
        {"symbol": "SH.601958", "name": "金钼股份", "relation": "最近自定义信号"}
    ])

    payloads = [
        asyncio.run(workbench._build_static_index_target("上证指数", "sh000001", "30min")),
        asyncio.run(workbench._build_static_index_target("创业板指", "sz399006", "30min")),
        asyncio.run(workbench._build_static_index_target("科创50", "sh000688", "30min")),
    ]

    for payload in payloads:
        assert payload["target"]["kind"] == "index"
        assert payload["candidate_stocks"] == []
        assert payload["related_custom_signals"] == []
        assert "金钼股份" not in str(payload)


def test_engine_index_target_does_not_use_global_scored_stock_candidates(monkeypatch):
    from signals.layers.index_report import IndexReport
    from signals.web.api import workbench

    class Engine:
        review_state = type("ReviewState", (), {
            "completed": False,
            "is_running": False,
            "phase": "",
            "phase_detail": "",
            "error": "",
            "start_date": "",
            "start_label": "",
            "timing": {},
            "index_reports": [],
        })()

        def get_index_reports(self):
            return [IndexReport(name="上证指数", symbol="sh000001")]

        def get_scored_symbols(self):
            raise AssertionError("index target must not read global stock candidates")

    monkeypatch.setattr(workbench, "_index_df", lambda symbol, freq: (_intraday_bars(), "index_bars"))
    monkeypatch.setattr(workbench, "_target_diagnostics", lambda *args, **kwargs: {"cache_probe": {"status": "hit"}})
    monkeypatch.setattr(workbench, "_plan_for_index", lambda *args, **kwargs: None)
    monkeypatch.setattr(workbench, "_summary_from_index", lambda report, chart: {"title": report["name"]})

    payload = asyncio.run(workbench._build_index_target(Engine(), "上证指数", "30min"))

    assert payload["target"]["kind"] == "index"
    assert payload["analysis_target"] == ""
    assert payload["candidate_stocks"] == []
    assert payload["related_custom_signals"] == []


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
