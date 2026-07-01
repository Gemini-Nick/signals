# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

import pandas as pd

from signals.sync.engine import COLLECTION_DOMAINS, MODULE_TARGETS
from signals.sync.modules import ALL_MODULES
from signals.sync.modules import hot_rank_clues as hot_rank_module
from signals.sync.modules.hot_rank_clues import (
    _analyze_candidate,
    _fetch_eastmoney_hot_rank,
    _load_wind_export_rows,
    _merge_hot_rows,
    _weekly_from_daily,
)


def _rising_daily_frame(periods: int = 60) -> pd.DataFrame:
    dates = pd.date_range("2026-03-01", periods=periods, freq="B")
    closes = [10.0 + idx * 0.05 for idx in range(periods)]
    return pd.DataFrame(
        {
            "open": [value * 0.995 for value in closes],
            "high": [value * 1.015 for value in closes],
            "low": [value * 0.985 for value in closes],
            "close": closes,
            "vol": [1000000 + idx * 12000 for idx in range(periods)],
            "amount": [10000000 + idx * 100000 for idx in range(periods)],
        },
        index=dates,
    )


def test_merge_hot_rows_keeps_source_overlap_and_best_rank():
    rows = [
        {"code": "600000", "symbol": "SH.600000", "name": "浦发银行", "source": "eastmoney", "rank": 8},
        {"code": "600000", "symbol": "SH.600000", "name": "浦发银行", "source": "ths", "rank": 5},
        {"code": "600000", "symbol": "SH.600000", "name": "浦发银行", "source": "eastmoney", "rank": 12},
        {"code": "000001", "symbol": "SZ.000001", "name": "平安银行", "source": "wind", "rank": 3},
    ]

    merged = _merge_hot_rows(rows)

    first = merged[0]
    assert first["code"] == "600000"
    assert first["sources"] == ["eastmoney", "ths"]
    assert first["ranks"] == {"eastmoney": 8, "ths": 5}


def test_analyze_candidate_selects_hot_rank_ma_climb_shape():
    daily = _rising_daily_frame()
    item = {
        "code": "600000",
        "symbol": "SH.600000",
        "name": "浦发银行",
        "sources": ["eastmoney", "ths"],
        "ranks": {"eastmoney": 6, "ths": 9},
    }

    doc = _analyze_candidate(
        item,
        daily=daily,
        weekly=_weekly_from_daily(daily),
        min_score=62,
        rank_limit=100,
        now=datetime(2026, 4, 28, 21, 15),
    )

    assert doc["active"] is True
    assert doc["selected"] is True
    assert "daily_ma5_climb" in doc["strategy_tags"]
    assert doc["score"] >= 62
    assert "热榜" in doc["reason_summary"]


def test_load_wind_export_rows_accepts_common_chinese_columns(tmp_path):
    path = tmp_path / "wind_hot_rank.csv"
    path.write_text("证券代码,证券简称,排名,热度\n600000.SH,浦发银行,7,88\n", encoding="utf-8-sig")

    rows, source_path = _load_wind_export_rows(str(tmp_path), 100)

    assert source_path == str(path)
    assert rows == [
        {
            "code": "600000",
            "symbol": "SH.600000",
            "name": "浦发银行",
            "source": "wind",
            "rank": 7,
            "hot_score": 88.0,
            "pct_chg": 0.0,
            "topic": "",
        }
    ]


def test_fetch_eastmoney_hot_rank_uses_native_push2delay_without_bad_v_param(monkeypatch):
    calls = []

    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _Session:
        def __init__(self):
            self.trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, **kwargs):
            calls.append(("post", url, kwargs))
            return _Response({
                "data": [
                    {"sc": "SZ000725", "rk": 1, "rc": 0},
                    {"sc": "SH600000", "rk": 2, "rc": 1},
                ]
            })

        def get(self, url, **kwargs):
            calls.append(("get", url, kwargs, self.trust_env))
            return _Response({
                "data": {
                    "diff": [
                        {"f12": "000725", "f14": "京东方A", "f2": 8.58, "f3": -1.15},
                        {"f12": "600000", "f14": "浦发银行", "f2": 10.12, "f3": 0.59},
                    ]
                }
            })

    monkeypatch.setattr(hot_rank_module.requests, "Session", _Session)

    rows = _fetch_eastmoney_hot_rank(2)

    get_call = next(item for item in calls if item[0] == "get")
    assert "push2delay.eastmoney.com" in get_call[1]
    assert "?v=" not in get_call[2]["params"]["secids"]
    assert get_call[3] is False
    assert rows == [
        {
            "code": "000725",
            "symbol": "SZ.000725",
            "name": "京东方A",
            "source": "eastmoney",
            "rank": 1,
            "hot_score": 0.0,
            "pct_chg": -1.15,
            "topic": "",
        },
        {
            "code": "600000",
            "symbol": "SH.600000",
            "name": "浦发银行",
            "source": "eastmoney",
            "rank": 2,
            "hot_score": 1.0,
            "pct_chg": 0.59,
            "topic": "",
        },
    ]


def test_hot_rank_clues_registered_for_sync_engine():
    module_names = {name for name, _, _ in ALL_MODULES}

    assert "hot_rank_clues" in module_names
    assert MODULE_TARGETS["hot_rank_clues"] == ("hot_rank_clues",)
    assert COLLECTION_DOMAINS["hot_rank_clues"] == "hot_rank_clue"
