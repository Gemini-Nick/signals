import asyncio
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
    assert rows[0]["day_change_pct"] is not None
    assert rows[0]["daily_change_pct"] is not None
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
