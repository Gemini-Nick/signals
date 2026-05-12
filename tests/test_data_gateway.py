# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

import pandas as pd

from signals.data.models import DataRequest, resolve_mode


def test_resolve_historical_for_past_as_of():
    req = DataRequest(domain="board", mode="auto", as_of="2020-01-02")
    assert resolve_mode(req) == "historical"


def test_resolve_historical_for_backtest_purpose():
    req = DataRequest(domain="kline", mode="auto", purpose="backtest")
    assert resolve_mode(req) == "historical"


def test_resolve_realtime_for_intraday_purpose():
    req = DataRequest(domain="board", mode="auto", purpose="intraday")
    assert resolve_mode(req) == "realtime"


def test_historical_board_prefers_canonical(monkeypatch):
    from signals.data import gateway

    calls = {"realtime": 0}

    def fake_latest(collection, query=None):
        if collection == "board_ranking":
            return pd.DataFrame([
                {"dt": "2026-04-24", "board_name": "半导体", "change_pct": 2.3}
            ])
        return None

    def fake_realtime(domain):
        calls["realtime"] += 1
        return []

    monkeypatch.setattr(gateway, "_latest_df", fake_latest)
    monkeypatch.setattr(gateway, "_fetch_realtime_sources", fake_realtime)
    monkeypatch.setattr(gateway, "_write_data_freshness", lambda *a, **k: None)

    response = gateway.get_board_rank(DataRequest(
        domain="board",
        mode="historical",
        as_of="2026-04-24",
    ))

    assert response.mode_used == "historical"
    assert response.source == "board_ranking"
    assert calls["realtime"] == 0
    assert response.data.iloc[0]["board_name"] == "半导体"


def test_realtime_concept_reads_snapshot_without_provider(monkeypatch):
    from signals.data import gateway

    calls = {"realtime": 0}

    def fake_latest(collection, query=None):
        if collection == "concept_sina":
            return pd.DataFrame([
                {"dt": "2026-04-24", "board_name": "AI算力", "change_pct": 1.2}
            ])
        if collection == "concept_ranking":
            raise AssertionError("realtime path should not require canonical first")
        return None

    def fake_realtime(domain):
        calls["realtime"] += 1
        raise AssertionError("realtime gateway must not call providers")

    monkeypatch.setattr(gateway, "_latest_df", fake_latest)
    monkeypatch.setattr(gateway, "_read_heat_tick_snapshot", lambda domain, target: (pd.DataFrame(), "", None))
    monkeypatch.setattr(gateway, "_fetch_realtime_sources", fake_realtime)
    monkeypatch.setattr(gateway, "_write_data_freshness", lambda *a, **k: None)

    response = gateway.get_concept_rank(DataRequest(
        domain="concept",
        mode="realtime",
        as_of="2026-04-24",
    ))

    assert response.mode_used == "realtime"
    assert response.is_stale is False
    assert response.source == "concept_sina"
    assert response.data.iloc[0]["board_name"] == "AI算力"
    assert calls["realtime"] == 0


def test_realtime_board_prefers_heat_ticks(monkeypatch):
    from signals.data import gateway

    heat_df = pd.DataFrame([
        {"dt": "2026-05-08", "board_name": "通信设备", "change_pct": 3.6, "source": "eastmoney_push2delay"}
    ])

    monkeypatch.setattr(gateway, "_read_heat_tick_snapshot", lambda domain, target: (heat_df, "board_heat_ticks", "2026-05-08"))
    monkeypatch.setattr(
        gateway,
        "_read_source_snapshots",
        lambda domain: (_ for _ in ()).throw(AssertionError("source snapshots should not be read")),
    )
    monkeypatch.setattr(gateway, "_write_data_freshness", lambda *a, **k: None)

    response = gateway.get_board_rank(DataRequest(
        domain="board",
        mode="realtime",
        as_of="2026-05-08",
    ))

    assert response.mode_used == "realtime"
    assert response.source == "board_heat_ticks"
    assert response.freshness == "fresh"
    assert response.data.iloc[0]["board_name"] == "通信设备"


def test_realtime_board_default_target_switches_at_call_auction(monkeypatch):
    from signals.data import gateway

    captured = {}
    heat_df = pd.DataFrame([
        {"dt": "2026-05-12", "board_name": "通信设备", "change_pct": 3.6, "source": "eastmoney_push2delay"}
    ])

    def fake_heat_snapshot(domain, target):
        captured["target"] = target
        return heat_df, "board_heat_ticks", "2026-05-12"

    monkeypatch.setattr(gateway, "naive_market_now", lambda market: datetime(2026, 5, 12, 9, 15))
    monkeypatch.setattr(gateway, "_read_heat_tick_snapshot", fake_heat_snapshot)
    monkeypatch.setattr(gateway, "_write_data_freshness", lambda *a, **k: None)

    response = gateway.get_board_rank(DataRequest(domain="board", mode="realtime"))

    assert captured["target"] == "2026-05-12"
    assert response.as_of == "2026-05-12"
    assert response.freshness == "fresh"


def test_realtime_empty_falls_back_to_canonical_without_provider(monkeypatch):
    from signals.data import gateway

    def fake_latest(collection, query=None):
        if collection == "board_ranking":
            return pd.DataFrame([
                {"dt": "2026-04-23", "source": "canonical", "board_name": "银行", "change_pct": -0.2}
            ])
        return None

    monkeypatch.setattr(gateway, "_latest_df", fake_latest)
    monkeypatch.setattr(
        gateway,
        "_fetch_realtime_sources",
        lambda domain: (_ for _ in ()).throw(AssertionError("provider called")),
    )
    monkeypatch.setattr(gateway, "_read_heat_tick_snapshot", lambda domain, target: (pd.DataFrame(), "", None))

    response = gateway.get_board_rank(DataRequest(domain="board", mode="realtime"))

    assert response.freshness == "stale"
    assert response.source == "board_ranking"
    assert "realtime_snapshot_empty_canonical_fallback" in response.errors


def test_kline_prefers_bars_before_legacy(monkeypatch):
    from signals.data import gateway

    calls = {"legacy": 0}

    df = pd.DataFrame([
        {"dt": "2026-04-23", "open": 1, "high": 2, "low": 1, "close": 2, "vol": 100}
    ])
    df["dt"] = pd.to_datetime(df["dt"])
    df = df.set_index("dt")
    df.attrs["as_of"] = "2026-04-23"

    monkeypatch.setattr(gateway, "_load_bars_from_mongo", lambda symbol, freq: (df, "bars"))
    monkeypatch.setattr(gateway, "_load_bars_from_disk", lambda symbol, freq: (pd.DataFrame(), ""))
    monkeypatch.setattr(gateway, "_write_data_freshness", lambda *a, **k: None)

    def legacy():
        calls["legacy"] += 1
        return pd.DataFrame()

    response = gateway.get_kline(
        DataRequest(domain="kline", mode="historical", symbol="600000", freq="daily", as_of="2026-04-23"),
        legacy_fetcher=legacy,
    )

    assert response.source == "bars"
    assert response.freshness == "fresh"
    assert calls["legacy"] == 0


def test_bars_df_from_docs_preserves_cached_change_fields():
    from signals.data import gateway

    df = gateway._bars_df_from_docs([
        {
            "dt": "2026-05-06 09:35:00",
            "open": "10.0",
            "high": "10.5",
            "low": "9.9",
            "close": "10.4",
            "vol": "1000",
            "amount": "10400",
            "prev_close": "10.0",
            "change_pct": "4.0",
            "pct_chg": "4.0",
        }
    ], "bars")

    assert list(df.columns) == ["open", "high", "low", "close", "vol", "amount", "prev_close", "change_pct", "pct_chg"]
    assert df.iloc[0]["prev_close"] == 10.0
    assert df.iloc[0]["change_pct"] == 4.0


def test_bars_df_from_docs_prefers_canonical_freq_on_duplicate_dt():
    from signals.data import gateway

    df = gateway._bars_df_from_docs([
        {
            "dt": "2026-04-23",
            "open": 1,
            "high": 2,
            "low": 1,
            "close": 2,
            "vol": 100,
            "meta": {"symbol": "002759", "freq": "daily"},
        },
        {
            "dt": "2026-04-23",
            "open": 3,
            "high": 4,
            "low": 3,
            "close": 4,
            "vol": 200,
            "meta": {"symbol": "002759", "freq": "日线"},
        },
    ], "bars")

    assert len(df) == 1
    assert df.iloc[0]["close"] == 4
    assert df.iloc[0]["vol"] == 200


def test_index_bars_prefers_index_collection(monkeypatch):
    from signals.data import gateway

    calls = []
    df = pd.DataFrame([
        {"dt": "2026-04-23", "open": 1, "high": 2, "low": 1, "close": 2, "vol": 100}
    ])
    df["dt"] = pd.to_datetime(df["dt"])
    df = df.set_index("dt")
    df.attrs["as_of"] = "2026-04-23"

    def fake_load(collection, symbol, freq):
        calls.append(collection)
        if collection == "index_bars":
            return df, "index_bars"
        return pd.DataFrame(), ""

    monkeypatch.setattr(gateway, "_load_bars_from_collection", fake_load)
    monkeypatch.setattr(gateway, "_write_data_freshness", lambda *a, **k: None)

    response = gateway.get_index_bars(
        DataRequest(domain="index", mode="historical", symbol="sz399001", freq="daily", as_of="2026-04-23")
    )

    assert response.source == "index_bars"
    assert response.freshness == "fresh"
    assert calls == ["index_bars"]


def test_index_bars_do_not_fallback_to_stock_bars(monkeypatch):
    from signals.data import gateway

    calls = []

    def fake_load(collection, symbol, freq):
        calls.append(collection)
        return pd.DataFrame(), ""

    monkeypatch.setattr(gateway, "_load_bars_from_collection", fake_load)
    monkeypatch.setattr(gateway, "_write_data_freshness", lambda *a, **k: None)

    response = gateway.get_index_bars(
        DataRequest(domain="index", mode="historical", symbol="sh000001", freq="30m", as_of="2026-04-23")
    )

    assert response.source == "index_bars"
    assert response.freshness == "empty"
    assert "index_bars_cache_empty" in response.errors
    assert calls == ["index_bars"]


def test_runtime_concept_rankings_do_not_fallback_to_providers(monkeypatch):
    from signals.data.models import DataResponse
    from signals.data import gateway
    from signals.layers import industry

    monkeypatch.setattr(
        gateway,
        "get_concept_rank",
        lambda request: DataResponse(
            pd.DataFrame(),
            mode_used="realtime",
            source="none",
            freshness="empty",
            is_stale=True,
        ),
    )
    monkeypatch.setattr(
        industry,
        "_get_concepts_sina",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("provider called")),
    )

    assert industry.get_concept_rankings(mode="realtime") == []


def test_l2_industry_representatives_use_gateway_without_em_probe(monkeypatch):
    from signals.data.models import DataResponse
    from signals.data import gateway
    from signals.layers import industry

    board_df = pd.DataFrame([
        {
            "dt": "2026-04-24",
            "board_name": "半导体",
            "change_pct": 2.3,
            "leader_name": "测试股份",
            "leader_change_pct": 5.6,
        }
    ])

    monkeypatch.setattr(
        gateway,
        "get_board_rank",
        lambda request: DataResponse(
            board_df,
            mode_used="realtime",
            source="board_em",
            as_of="2026-04-24",
            freshness="fresh",
        ),
    )
    monkeypatch.setattr(industry, "get_concept_rankings", lambda **_kwargs: [])
    monkeypatch.setattr(industry, "_load_runtime_pool_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(industry, "_build_name_to_code_map", lambda: {})
    monkeypatch.setattr(industry, "_enrich_rhythm", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        industry,
        "_em_health_probe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("em probe called")),
    )

    gain, composite, merged, concepts, oversold, stats = industry.get_industry_representatives(
        top_n=1,
        mode="realtime",
    )

    assert [item.name for item in gain] == ["半导体"]
    assert concepts == []
    assert isinstance(composite, list)
    assert isinstance(merged, list)
    assert isinstance(oversold, list)
    assert "name_df" in stats
