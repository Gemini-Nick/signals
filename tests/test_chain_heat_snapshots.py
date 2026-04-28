# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.sync.modules.chain_heat import _aggregate


def test_chain_heat_aggregate_builds_realtime_node_fields():
    latest = datetime(2026, 4, 28, 10, 30)
    rows = [{
        "kind": "industry",
        "name": "半导体",
        "source": "eastmoney_push2delay",
        "rank": 1,
        "change_pct": 2.4,
        "up_count": 44,
        "down_count": 6,
        "leader_name": "测试龙头",
        "leader_change_pct": 8.8,
        "trade_minute": latest,
        "heat_score": 61.5,
        "momentum_5m": 0.4,
        "momentum_15m": 0.8,
        "momentum_30m": 1.2,
        "chain_id": "semiconductor",
        "chain_name": "半导体产业链",
        "node_id": "wafer_foundry",
        "node_name": "晶圆制造",
        "layer": "midstream",
        "stage": "",
        "mapping_confidence": 92,
        "representatives": [
            {"symbol": "SH.688981", "name": "中芯国际", "representative_type": "core", "priority": 100},
            {"symbol": "SZ.002371", "name": "北方华创", "representative_type": "elastic", "priority": 90},
        ],
    }]

    snapshots = _aggregate(rows, latest)

    assert len(snapshots) == 1
    node = snapshots[0]
    assert node["chain_id"] == "semiconductor"
    assert node["node_id"] == "wafer_foundry"
    assert node["phase"] == "accelerating"
    assert node["trading_signal"] == "chain_acceleration"
    assert node["heat_source"] == "eastmoney_push2delay"
    assert node["taxonomy_source"] == "industry_chains.yaml"
    assert node["momentum_5m"] == 0.4
    assert node["representatives"][0]["symbol"] == "SH.688981"
