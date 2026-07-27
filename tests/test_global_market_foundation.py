from __future__ import annotations

from datetime import datetime

from signals.core.global_market_universe import (
    load_global_market_universe,
    market_metadata,
    market_universe,
    normalize_markets,
)
from signals.data.bar_quality import validate_ohlcv_bar
from signals.data.alpaca_source import AlpacaSource
from signals.sync.modules import global_market_foundation as foundation


def _bar(**overrides):
    bar = {
        "dt": datetime(2026, 7, 24),
        "open": 100.0,
        "high": 105.0,
        "low": 98.0,
        "close": 103.0,
        "vol": 1000,
        "amount": 103000.0,
    }
    bar.update(overrides)
    return bar


def test_ohlcv_quality_rejects_nan_and_invalid_ranges():
    assert validate_ohlcv_bar(_bar())[0] is True
    assert validate_ohlcv_bar(_bar(close=float("nan"))) == (False, "non_finite_price")
    assert validate_ohlcv_bar(_bar(low=106.0)) == (False, "low_above_high")
    assert validate_ohlcv_bar(_bar(vol=0)) == (False, "invalid_volume")


def test_global_universe_has_fixed_hk_us_anchors_and_ai_chain():
    config = load_global_market_universe()
    hk = {item["symbol"]: item for item in market_universe("HK")}
    us = {item["symbol"]: item for item in market_universe("US")}

    assert config["version"] == "2026.07.1"
    assert {"HK.800000", "HK.800100", "HK.800700", "HK.00700", "HK.09988"} <= hk.keys()
    assert {"US.SPY", "US.QQQ", "US.DIA", "US.SOXX", "US.IWM", "US.VIXY"} <= us.keys()
    assert {"US.AAPL", "US.MSFT", "US.NVDA", "US.AMZN", "US.GOOGL", "US.META", "US.TSLA"} <= us.keys()
    assert {"US.AVGO", "US.ANET", "US.COHR", "US.VRT", "US.MU", "US.TSM"} <= us.keys()
    assert us["US.SPY"]["proxy_for"] == "S&P 500"
    assert market_metadata("US")["coverage_scope"] == "core_universe"


def test_a_h_pairs_share_issuer_and_linked_listing():
    docs = {doc["symbol"]: doc for doc in foundation.security_master_documents(as_of="2026-07-27")}
    smic = docs["HK.00981"]

    assert smic["issuer_id"] == "issuer:smic"
    assert smic["linked_listing_ids"] == ["security:SH.688981"]
    assert smic["currency"] == "HKD"
    assert smic["timezone"] == "Asia/Hong_Kong"


def test_market_membership_is_versioned_and_fixed():
    docs = foundation.universe_membership_documents(effective_date="2026-07-27")
    nvda = next(doc for doc in docs if doc["symbol"] == "US.NVDA")

    assert nvda["_id"].startswith("2026.07.1:US:")
    assert nvda["fixed"] is True
    assert nvda["role"] == "anchor"


def test_markets_parameter_is_normalized_and_a_only_by_default():
    assert normalize_markets(None) == ["A"]
    assert normalize_markets(["cn", "HK", "us", "US"]) == ["A", "HK", "US"]


def test_hk_partial_latest_shard_falls_back_to_last_complete_session():
    valid = {
        **{(f"HK.{index:05d}", "2026-07-24"): {} for index in range(1200)},
        **{(f"HK.{index:05d}", "2026-07-27"): {} for index in range(21)},
    }

    assert foundation._select_session_date(valid, "HK") == "2026-07-24"
    assert foundation._select_session_date(valid, "US") == "2026-07-27"


class _NeverWriteDb:
    def __getitem__(self, _name):
        raise AssertionError("missing providers must not touch Mongo bars")


def test_missing_alpaca_and_futu_return_unavailable_without_writes(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.setenv("FUTU_HOST", "127.0.0.1")
    monkeypatch.setenv("FUTU_PORT", "1")

    result = foundation.hydrate_global_core_bars(_NeverWriteDb())

    assert result["US"]["status"] == "unavailable"
    assert result["US"]["daily_written"] == 0
    assert result["HK"]["status"] == "unavailable"
    assert result["HK"]["minute_written"] == 0


def test_alpaca_feed_is_validated_and_maps_to_request_enum():
    source = AlpacaSource("key", "secret", feed="DELAYED_SIP")

    assert source.feed == "delayed_sip"
    assert source._data_feed().value == "delayed_sip"


def test_alpaca_request_uses_the_recorded_feed(monkeypatch):
    from alpaca.data import requests as request_module
    from czsc import Freq

    captured = {}

    class _Request:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class _Client:
        def get_stock_bars(self, _request):
            return type("_Bars", (), {"data": {}})()

    monkeypatch.setattr(request_module, "StockBarsRequest", _Request)
    source = AlpacaSource("key", "secret", feed="sip")
    monkeypatch.setattr(source, "_get_client", lambda: _Client())

    assert source._fetch_bars("US.SPY", Freq.D, lookback_days=1) == []
    assert captured["feed"].value == "sip"
