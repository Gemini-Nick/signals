# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.sync.modules import board_cons


class _Collection:
    def __init__(self):
        self.docs = {}

    def find_one(self, query, projection=None, sort=None):
        if "_id" in query:
            return self.docs.get(query["_id"])
        return None

    def update_one(self, query, update, upsert=False):
        key = query.get("_id")
        doc = dict(self.docs.get(key, {"_id": key}))
        doc.update(update.get("$set", {}))
        self.docs[key] = doc


class _DB(dict):
    def __getitem__(self, name):
        if name not in self:
            self[name] = _Collection()
        return dict.__getitem__(self, name)


def test_eastmoney_board_code_map_paginates(monkeypatch):
    board_cons._eastmoney_board_code_map.cache_clear()
    pages = {
        "1": [{"f14": f"行业{i}", "f12": f"BK{i:04d}"} for i in range(100)],
        "2": [{"f14": "行业B", "f12": "BK0002"}],
        "3": [],
    }

    def fake_clist(params):
        return pages[str(params["pn"])]

    monkeypatch.setattr(board_cons, "_eastmoney_delay_clist", fake_clist)
    monkeypatch.setattr(board_cons, "_eastmoney_map_max_pages", lambda: 5)

    mapping = board_cons._eastmoney_board_code_map("board")

    assert mapping["行业0"] == "BK0000"
    assert mapping["行业B"] == "BK0002"
    assert len(mapping) == 101


def test_board_cons_marks_unmapped_without_retrying_fallback(monkeypatch):
    db = _DB()
    board_cons._eastmoney_board_code_map.cache_clear()
    monkeypatch.setattr(board_cons, "_get_board_list", lambda _db: ["未映射行业"])
    monkeypatch.setattr(board_cons, "_get_concept_list", lambda _db: [])
    monkeypatch.setattr(board_cons, "_eastmoney_delay_clist", lambda _params: [])
    monkeypatch.setattr(board_cons, "_batch_size", lambda: 1)
    monkeypatch.setattr(board_cons, "_max_runtime_seconds", lambda: 30)
    monkeypatch.setattr(board_cons, "_CALL_INTERVAL", 0)

    result = board_cons.sync_board_cons(db)

    assert result["status"] == "ok"
    assert result["processed"] == 1
    assert result["errors"] == 0
    assert result["unmapped"] == 1
    assert result["source_counts"]["source_unmapped"] == 1
    assert db["board_constituents"].docs["未映射行业"]["status"] == "source_unmapped"
    assert db["sync_log"].docs["board_cons:_meta"]["sample_errors"] == []
