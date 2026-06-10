import json
import math
from types import SimpleNamespace

from signals.web.services.serializers import serialize_index_report


def _index_report(**overrides):
    defaults = {
        "name": "上证指数",
        "symbol": "sh000001",
        "data_available": True,
        "latest_price": 3983.3,
        "daily_last_dt": None,
        "snapshot_price": 3983.3,
        "snapshot_dt": None,
        "snapshot_freq": "daily",
        "intraday_change": 0.0,
        "summary": "",
        "daily_trend": "",
        "daily_last_direction": "",
        "daily_latest_signal": "",
        "daily_bi_count": 0,
        "daily_zs": None,
        "f30_trend": "",
        "f30_last_direction": "",
        "f30_latest_signal": "",
        "f30_bi_count": 0,
        "f30_zs": None,
        "f15_trend": "",
        "f15_last_direction": "",
        "f15_latest_signal": "",
        "f15_bi_count": 0,
        "f15_zs": None,
        "has_buy_signal": False,
        "has_sell_signal": False,
        "is_bullish": False,
        "three_level_aligned": False,
        "ma_context": None,
        "scenario_branches": None,
        "recent_5d_return": 0.0,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_serialize_index_report_replaces_nonfinite_numbers():
    report = _index_report(
        latest_price=math.nan,
        snapshot_price=math.inf,
        intraday_change=-math.inf,
        daily_zs=SimpleNamespace(zd=math.nan, zg=math.inf, bi_count=3),
        recent_5d_return=math.nan,
    )

    payload = serialize_index_report(report)

    assert payload["latest_price"] is None
    assert payload["snapshot_price"] is None
    assert payload["intraday_change"] is None
    assert payload["daily_zs"]["zd"] is None
    assert payload["daily_zs"]["zg"] is None
    assert payload["recent_5d_return"] is None
    json.dumps(payload, allow_nan=False)
