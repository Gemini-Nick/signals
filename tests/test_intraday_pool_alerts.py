# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from signals.notify.intraday_pool_alerts import process_terminal_stock_pool_alerts


class _Collection:
    def __init__(self):
        self.docs = {}

    def find_one(self, query=None, projection=None, sort=None):
        query = query or {}
        doc = self.docs.get(query.get("_id"))
        return dict(doc) if doc else None

    def update_one(self, query=None, update=None, upsert=False):
        query = query or {}
        update = update or {}
        doc_id = query.get("_id")
        if not doc_id:
            return None
        inserted = doc_id not in self.docs
        if inserted and not upsert:
            return None
        doc = self.docs.setdefault(doc_id, {"_id": doc_id})
        if inserted:
            doc.update(update.get("$setOnInsert", {}))
        doc.update(update.get("$set", {}))
        return None


class _Db(dict):
    def __getitem__(self, key):
        if key not in self:
            self[key] = _Collection()
        return super().__getitem__(key)


def _pool_doc(change_pct=10.0, status="attack_entry"):
    return {
        "trade_date": "2026-05-08",
        "updated_at": datetime(2026, 5, 8, 13, 43),
        "focus_stocks": [
            {
                "symbol": "SH.600391",
                "raw_code": "600391",
                "name": "航发科技",
                "action_status": status,
                "trade_stage": "attack_entry",
                "stage_label": "进攻买点",
                "trader_action": "进攻买点复核",
                "can_trade_now": True,
                "change_pct": change_pct,
                "latest_price": 42.57,
                "latest_signal": "缩量回踩承接",
                "event_latest_dt": "2026-05-08 13:40",
                "primary_chain": "军工装备产业链",
                "entry_logic_summary": "日/周背景和5m/15m右侧确认，30m未补齐",
                "invalidates_when": "5m/15m转弱、30m迟迟不补或产业链高潮",
            }
        ],
    }


def test_intraday_pool_alert_sends_limit_move_once(monkeypatch):
    monkeypatch.setenv("SIGNALS_CURRENT_SYNC_LANE", "workbench_lane")
    db = _Db()
    sent = []

    result = process_terminal_stock_pool_alerts(
        db,
        _pool_doc(),
        notify_func=sent.append,
        require_channel=False,
    )
    second = process_terminal_stock_pool_alerts(
        db,
        _pool_doc(),
        notify_func=sent.append,
        require_channel=False,
    )

    assert result["sent"] == 1
    assert second["sent"] == 0
    assert len(sent) == 1
    assert "Signals 买点池提醒：强势/涨停" in sent[0]
    assert "航发科技 SH.600391" in sent[0]
    assert "涨幅：+10.00%" in sent[0]
    assert "进攻买点" in sent[0]


def test_intraday_pool_alert_can_send_later_strong_move_after_entry(monkeypatch):
    monkeypatch.setenv("SIGNALS_CURRENT_SYNC_LANE", "workbench_lane")
    db = _Db()
    sent = []

    first = process_terminal_stock_pool_alerts(
        db,
        _pool_doc(change_pct=3.2),
        notify_func=sent.append,
        require_channel=False,
    )
    second = process_terminal_stock_pool_alerts(
        db,
        _pool_doc(change_pct=10.0),
        notify_func=sent.append,
        require_channel=False,
    )

    assert first["sent"] == 1
    assert second["sent"] == 1
    assert "Signals 买点池提醒\n" in sent[0]
    assert "Signals 买点池提醒：强势/涨停" in sent[1]


def test_intraday_pool_alert_skips_non_live_lane(monkeypatch):
    monkeypatch.delenv("SIGNALS_CURRENT_SYNC_LANE", raising=False)
    db = _Db()
    sent = []

    result = process_terminal_stock_pool_alerts(
        db,
        _pool_doc(),
        notify_func=sent.append,
        require_channel=False,
    )

    assert result["status"] == "disabled"
    assert "not_live_workbench_lane" in result["reason"]
    assert sent == []
