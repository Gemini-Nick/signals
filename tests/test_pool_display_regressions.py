from __future__ import annotations

from datetime import datetime

from signals.sync.engine import SyncEngine
from signals.sync.modules import quote_snapshots, terminal_pool
from signals.web.api import workbench


class _Cursor(list):
    def sort(self, *args, **kwargs):
        return self

    def limit(self, count):
        return _Cursor(self[:count])


class _Collection:
    def __init__(self, *, doc=None, rows=None, count=0):
        self.doc = doc
        self.rows = rows or []
        self.count = count

    def find_one(self, *args, **kwargs):
        return self.doc

    def find(self, *args, **kwargs):
        return _Cursor(self.rows)

    def aggregate(self, *args, **kwargs):
        return _Cursor([])

    def estimated_document_count(self, *args, **kwargs):
        return self.count


class _Db(dict):
    def __missing__(self, key):
        value = _Collection()
        self[key] = value
        return value


def test_hot_rank_clues_are_part_of_live_quote_universe(monkeypatch):
    db = _Db({
        "hot_rank_clues": _Collection(rows=[
            {"symbol": "SZ.002415", "active": True, "score": 86.09},
        ]),
        "terminal_stock_pool": _Collection(doc={}),
        "market_pools": _Collection(doc={"symbols": []}),
    })
    monkeypatch.setattr(quote_snapshots, "_iter_strategy_snapshot_symbols", lambda db=None: [])
    monkeypatch.setattr(quote_snapshots, "macro_watchlist", lambda: [])
    monkeypatch.setattr(quote_snapshots, "_latest_chain_heat_representative_symbols", lambda db: [])

    assert "SZ.002415" in quote_snapshots._hot_quote_symbols(db)


def test_merged_hot_rank_clue_never_falls_back_to_hard_signal_copy():
    summary = workbench._shell_stock_trade_summary(
        {
            "latest_signal": "东财+同花顺热榜 + 沿5周线攀爬",
            "missing_condition": "等新的技术确认",
            "inclusion_reasons": [{
                "reason_type": "hot_rank_clue",
                "source_collection": "hot_rank_clues",
            }],
        },
        entry_factor={},
        display_badges=[],
    )

    assert summary == "东财+同花顺热榜 + 沿5周线攀爬；等新的技术确认"


def test_current_climb_and_custom_signals_are_displayable():
    climb = workbench._shell_display_badge_from_reason({
        "reason_type": "technical_trigger",
        "signal_family": "ma_climb",
        "signal_side": "buy",
        "signal_type": "日线攀爬",
        "freq": "日线",
        "evidence": {"ma_climb": {"running": True, "climb_score": 82}},
    })
    custom = workbench._shell_display_badge_from_reason({
        "reason_type": "technical_trigger",
        "signal_family": "entry_factor",
        "signal_side": "buy",
        "signal_type": "拒绝回调相对强度",
        "freq": "日线",
        "score": 90,
    })

    assert (climb["kind"], climb["label"]) == ("ma_climb", "日线攀爬")
    assert (custom["kind"], custom["label"]) == ("buy_signal", "日拒绝回调相对强度")


def test_climb_reserves_display_capacity_when_regular_signals_fill_limit(monkeypatch):
    regular = [
        {
            "signal_family": "hard_technical",
            "signal_side": "buy",
            "signal_type": f"自定义信号{index}",
            "freq": "5分钟",
            "as_of": "2026-08-03",
            "dt": f"2026-08-03T13:{index:02d}:00",
        }
        for index in range(12)
    ]
    climb = {
        "signal_family": "ma_climb",
        "signal_side": "buy",
        "signal_type": "日线攀爬",
        "freq": "日线",
        "as_of": "2026-07-31",
        "dt": "2026-07-31T00:00:00",
        "technical_evidence": {"ma_climb": {"running": True, "climb_score": 75}},
    }
    monkeypatch.setattr(
        workbench,
        "_load_terminal_technical_signal_rows",
        lambda symbol, limit=80: [*regular, climb],
    )

    reasons = workbench._terminal_technical_signal_reasons("SH.600489", limit=12)

    assert len(reasons) == 12
    assert reasons[0]["signal_family"] == "ma_climb"


def test_terminal_pool_persists_custom_signal_badges():
    badge = terminal_pool._display_badge_for_reason({
        "reason_type": "technical_trigger",
        "signal_family": "entry_factor",
        "signal_side": "buy",
        "signal_type": "拒绝回调相对强度",
        "freq": "日线",
        "score": 90,
    })

    assert badge["kind"] == "buy_signal"
    assert badge["label"] == "日拒绝回调相对强度"


def test_rejected_terminal_publication_is_not_classified_ok():
    engine = object.__new__(SyncEngine)
    engine.db = _Db({"terminal_stock_pool": _Collection(count=1)})

    status, reason = engine._classify_result(
        "terminal_realtime_pool",
        {"status": "rejected", "reason": "ineligible_sources", "published": False},
    )

    assert status == "degraded"
    assert reason == "ineligible_sources"
