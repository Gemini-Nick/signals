# -*- coding: utf-8 -*-
from __future__ import annotations

import pandas as pd
import pytest

from signals.sync import provider_limits
from signals.sync.modules import stock_daily


class _Cursor(list):
    def sort(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self


class _Collection:
    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, query=None, projection=None):
        return _Cursor(self.docs)

    def find_one(self, query, projection=None, sort=None):
        for doc in self.docs:
            if all(doc.get(key) == value for key, value in query.items()):
                return doc
        return None

    def aggregate(self, pipeline):
        return list(self.docs)


class _DB(dict):
    def __getitem__(self, name):
        if name not in self:
            self[name] = _Collection()
        return dict.__getitem__(self, name)


@pytest.fixture(autouse=True)
def _clear_provider_state():
    provider_limits._STATES.clear()
    yield
    provider_limits._STATES.clear()


def test_stock_daily_uses_tencent_primary_without_akshare(monkeypatch):
    calls = []

    def fake_tencent_daily(code, *, start_date="", end_date="", count=800, timeout=8.0):
        calls.append((code, start_date, end_date, timeout))
        return pd.DataFrame([{
            "日期": "2026-04-27",
            "开盘": 10.0,
            "收盘": 10.5,
            "最高": 10.8,
            "最低": 9.9,
            "成交量": 12345,
            "成交额": 0,
        }])

    monkeypatch.setenv("STOCK_DAILY_PRIMARY_SOURCE", "tencent")
    monkeypatch.setattr(stock_daily, "fetch_tencent_daily", fake_tencent_daily)
    monkeypatch.setattr(
        stock_daily.ak,
        "stock_zh_a_hist",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("akshare eastmoney should not be called")),
    )

    docs = stock_daily._sync_one_stock("600019", "20260427", "20260427")

    assert calls == [("600019", "20260427", "20260427", 8.0)]
    assert len(docs) == 1
    assert docs[0]["meta"]["source"] == "tencent"
    assert docs[0]["close"] == 10.5


def test_stock_daily_tencent_empty_returns_without_akshare(monkeypatch):
    monkeypatch.setenv("STOCK_DAILY_PRIMARY_SOURCE", "tencent")
    monkeypatch.setattr(stock_daily, "fetch_tencent_daily", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        stock_daily.ak,
        "stock_zh_a_hist",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("akshare eastmoney should not be called")),
    )

    assert stock_daily._sync_one_stock("600423", "20260427", "20260427") == []


def test_stock_daily_sina_etf_fallback_qfq_adjusts_stable_factor(monkeypatch):
    dates = pd.bdate_range("2026-03-02", periods=25)
    raw_rows = []
    qfq_rows = []
    for idx, dt in enumerate(dates):
        close = 3.0 + idx * 0.03
        raw_rows.append({
            "date": dt,
            "open": close - 0.03,
            "high": close + 0.06,
            "low": close - 0.09,
            "close": close,
            "volume": 100000 + idx,
            "amount": 200000 + idx,
        })
        qfq_rows.append({
            "日期": dt,
            "开盘": round((close - 0.03) / 3, 3),
            "最高": round((close + 0.06) / 3, 3),
            "最低": round((close - 0.09) / 3, 3),
            "收盘": close / 3,
            "成交量": 1000 + idx,
            "成交额": 0,
        })

    monkeypatch.setenv("STOCK_DAILY_PRIMARY_SOURCE", "tencent")
    monkeypatch.setattr(
        stock_daily.ak,
        "stock_zh_a_hist",
        lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("eastmoney unavailable")),
    )
    monkeypatch.setattr(
        stock_daily.ak,
        "fund_etf_hist_sina",
        lambda symbol: pd.DataFrame(raw_rows) if symbol == "sh515880" else pd.DataFrame(),
    )
    monkeypatch.setattr(stock_daily, "fetch_tencent_daily", lambda *args, **kwargs: pd.DataFrame(qfq_rows))

    docs = stock_daily._sync_one_stock("515880", "20200101", "20260430")

    assert len(docs) == len(raw_rows)
    assert docs[0]["source"] == "sina_etf_qfq_factor"
    assert docs[0]["meta"]["source"] == "sina_etf_qfq_factor"
    assert docs[0]["close"] == pytest.approx(1.0)
    assert docs[-1]["close"] == pytest.approx(round(raw_rows[-1]["close"] / 3, 3))
    assert docs[0]["vol"] == raw_rows[0]["volume"]
    assert docs[0]["meta"]["source_volume_unit"] == "shares"


def test_stock_daily_provider_prefix_maps_bj_920_codes():
    assert stock_daily._daily_provider_prefix("920118") == "bj"
    assert stock_daily._daily_provider_prefix("900901") == "sh"


def test_stock_daily_active_repair_lookback_defaults_to_incremental(monkeypatch):
    monkeypatch.delenv("STOCK_DAILY_REPAIR_LOOKBACK_DAYS", raising=False)
    monkeypatch.delenv("STOCK_DAILY_ACTIVE_REPAIR_LOOKBACK_DAYS", raising=False)

    assert stock_daily._repair_lookback_days_for_scope("active") == 0
    assert stock_daily._repair_lookback_days_for_scope("manual_only_codes") == 0
    assert stock_daily._repair_lookback_days_for_scope("all") == 0

    monkeypatch.setenv("STOCK_DAILY_REPAIR_LOOKBACK_DAYS", "30")
    assert stock_daily._repair_lookback_days_for_scope("all") == 30


def test_active_stock_codes_include_all_terminal_stock_pool_groups():
    db = _DB({
        "terminal_stock_pool": _Collection([{
            "pool": "terminal_stock_pool",
            "market": "A",
            "stocks": [{"raw_code": "600001"}],
            "risk_stocks": [{"symbol": "SH.600002"}],
            "watch_stocks": [{"raw_code": "300003"}],
            "clue_stocks": [{"symbol": "SZ.300004"}],
        }]),
    })

    codes = stock_daily._get_active_stock_codes(db)

    assert "600001" in codes
    assert "600002" in codes
    assert "300003" in codes
    assert "300004" in codes


def test_active_stock_codes_include_macro_industry_etfs(monkeypatch):
    monkeypatch.delenv("STOCK_DAILY_ONLY_CODES", raising=False)
    monkeypatch.setenv("STOCK_DAILY_MAX_CODES", "0")
    monkeypatch.setattr(stock_daily, "_macro_etf_pure_codes", lambda: ["511090", "513130"])

    db = _DB({
        "terminal_stock_pool": _Collection([{
            "pool": "terminal_stock_pool",
            "market": "A",
            "stocks": [{"raw_code": "600001"}],
        }]),
    })

    codes = stock_daily._get_active_stock_codes(db)

    assert "511090" in codes
    assert "513130" in codes
    assert codes.index("511090") < codes.index("600001")


def test_get_all_stock_codes_falls_back_to_cached_universe(monkeypatch):
    db = _DB({
        "sync_log": _Collection([
            {"module": "stock_daily", "symbol": "600001"},
            {"module": "stock_daily", "symbol": "SZ.300001"},
        ]),
        "market_pools": _Collection([
            {
                "pool": "active",
                "symbols": ["SH.600001", "BJ.430001"],
                "items": [{"symbol": "SZ.000001"}],
            }
        ]),
        "bars": _Collection([
            {"_id": "SH.600002"},
        ]),
    })

    monkeypatch.setattr(
        stock_daily.ak,
        "stock_info_a_code_name",
        lambda: (_ for _ in ()).throw(ConnectionError("sse eof")),
    )
    monkeypatch.setattr(
        stock_daily.ak,
        "stock_zh_a_spot_em",
        lambda: (_ for _ in ()).throw(ConnectionError("push2 proxy")),
    )

    assert stock_daily._get_all_stock_codes(db) == [
        "600001",
        "300001",
        "430001",
        "000001",
        "600002",
    ]


def test_get_stock_codes_all_scope_uses_cached_universe_without_raising(monkeypatch):
    db = _DB({
        "sync_log": _Collection([
            {"module": "stock_daily", "symbol": "600001"},
        ]),
    })

    monkeypatch.setenv("STOCK_DAILY_SCOPE", "all")
    monkeypatch.setenv("SIGNALS_SYNC_FULL_STOCK_DAILY", "false")
    monkeypatch.setattr(
        stock_daily.ak,
        "stock_info_a_code_name",
        lambda: (_ for _ in ()).throw(ConnectionError("bse ssl eof")),
    )
    monkeypatch.setattr(
        stock_daily.ak,
        "stock_zh_a_spot_em",
        lambda: (_ for _ in ()).throw(ConnectionError("remote disconnected")),
    )

    assert stock_daily._get_stock_codes(db) == (["600001"], "all")


def test_stock_daily_all_scope_merges_cached_etf_spot_codes():
    db = _DB({
        "fullmarket_spot_snapshots": _Collection([
            {
                "date_key": "20260702",
                "code": "562590",
                "asset_class": "etf",
                "source": "eastmoney_etf_spot",
                "price": 3.82,
                "open": 4.01,
                "high": 4.02,
                "low": 3.82,
            },
        ]),
    })

    codes = stock_daily._merge_cached_spot_codes(["600001"], db)

    assert codes == ["600001", "562590"]


def test_stock_daily_all_scope_merges_etf_spot_codes_without_valid_quote():
    db = _DB({
        "etf_spot_snapshots": _Collection([
            {
                "date_key": "20260703",
                "code": "512480",
                "price": None,
                "open": None,
                "high": None,
                "low": None,
            },
        ]),
    })

    codes = stock_daily._merge_cached_spot_codes(["600001"], db)

    assert codes == ["600001", "512480"]


def test_get_all_stock_codes_includes_macro_industry_etfs(monkeypatch):
    monkeypatch.setattr(stock_daily.ak, "stock_info_a_code_name", lambda: pd.DataFrame({"code": ["600001"]}))
    monkeypatch.setattr(stock_daily, "_macro_etf_pure_codes", lambda: ["511090"])

    assert stock_daily._get_all_stock_codes(_DB()) == ["600001", "511090"]
