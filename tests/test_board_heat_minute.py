# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

import pandas as pd

from signals.sync.modules.board_heat_minute import _tick_docs


def test_board_heat_tick_docs_capture_minute_heat_fields():
    df = pd.DataFrame([{
        "板块名称": "半导体",
        "板块代码": "BK1036",
        "涨跌幅": 3.2,
        "换手率": 4.5,
        "上涨家数": 48,
        "下跌家数": 3,
        "领涨股票": "测试股份",
        "领涨股票-涨跌幅": 9.8,
    }])

    docs = _tick_docs(df, kind="industry", now=datetime(2026, 4, 27, 10, 30, 12))

    assert docs == [{
        "kind": "industry",
        "name": "半导体",
        "board_name": "半导体",
        "code": "BK1036",
        "source": "eastmoney_push2delay",
        "dt": datetime(2026, 4, 27),
        "trade_minute": datetime(2026, 4, 27, 10, 30),
        "snapshot_at": datetime(2026, 4, 27, 10, 30, 12),
        "rank_idx": 0,
        "price": None,
        "change_pct": 3.2,
        "change_amount": None,
        "market_value": None,
        "turnover_pct": 4.5,
        "up_count": 48,
        "down_count": 3,
        "leader_name": "测试股份",
        "leader_change_pct": 9.8,
    }]
