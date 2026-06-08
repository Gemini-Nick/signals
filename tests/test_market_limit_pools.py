# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.sync.engine import COLLECTION_DOMAINS, LIVE_PLAN_BY_MODULE, MODULE_TARGETS, REALTIME_MODULES
from signals.sync.modules import ALL_MODULES
from signals.sync.modules.market_limit_pools import _date_key, _normalize_row


def test_normalize_limit_up_pool_row_keeps_board_quality_fields():
    doc = _normalize_row(
        {
            "代码": "688017",
            "名称": "绿的谐波",
            "涨跌幅": 20.0,
            "最新价": 393.0,
            "成交额": 7928887040,
            "换手率": 12.21,
            "封板资金": 120000000,
            "首次封板时间": "130501",
            "最后封板时间": "130501",
            "炸板次数": 0,
            "涨停统计": "1/1",
            "连板数": 1,
            "所属行业": "自动化设备",
        },
        pool="limit_up",
        trade_date="2026-06-05",
        source="eastmoney_zt_pool",
        snapshot_at=datetime(2026, 6, 5, 13, 5),
    )

    assert doc is not None
    assert doc["snapshot_minute"] == "13:05"
    assert doc["symbol"] == "SH.688017"
    assert doc["first_limit_up_time"] == "130501"
    assert doc["last_limit_up_time"] == "130501"
    assert doc["open_count"] == 0
    assert doc["seal_amount"] == 120000000
    assert doc["consecutive_limit_count"] == 1
    assert doc["industry"] == "自动化设备"


def test_date_key_accepts_compact_and_dashed_dates():
    assert _date_key("20260605") == ("20260605", "2026-06-05")
    assert _date_key("2026-06-05") == ("20260605", "2026-06-05")


def test_market_limit_pool_is_registered_for_sync_engine():
    module_names = {name for name, _, _ in ALL_MODULES}

    assert "market_limit_pools" in module_names
    assert MODULE_TARGETS["market_limit_pools"] == ("market_limit_pools",)
    assert COLLECTION_DOMAINS["market_limit_pools"] == "market_limit_pool"
    assert "market_limit_pools" in REALTIME_MODULES
    assert LIVE_PLAN_BY_MODULE["market_limit_pools"].lane == "workbench_lane"
