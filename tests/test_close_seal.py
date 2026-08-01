# -*- coding: utf-8 -*-
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from signals.sync.close_seal import CloseSealRunner, SEAL_MODULES


def _get(row: dict[str, Any], key: str) -> Any:
    value: Any = row
    for part in key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _matches(row: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(row, item) for item in expected):
                return False
            continue
        actual = _get(row, key)
        if isinstance(expected, dict):
            if "$lt" in expected and not (actual is not None and actual < expected["$lt"]):
                return False
            if "$exists" in expected and (actual is not None) != bool(expected["$exists"]):
                return False
            if "$in" in expected and actual not in expected["$in"]:
                return False
            continue
        if actual != expected:
            return False
    return True


class FakeCollection:
    def __init__(self):
        self.rows: dict[str, dict[str, Any]] = {}

    def find_one(self, query, projection=None):
        row = next((item for item in self.rows.values() if _matches(item, query)), None)
        if row is None:
            return None
        if not projection:
            return deepcopy(row)
        return {key: deepcopy(_get(row, key)) for key, enabled in projection.items() if enabled and key != "_id"} | (
            {"_id": row["_id"]} if projection.get("_id", 1) and "_id" in row else {}
        )

    def update_one(self, query, update, upsert=False):
        row = next((item for item in self.rows.values() if _matches(item, query)), None)
        inserted = row is None
        if row is None:
            if not upsert:
                return None
            row = {key: value for key, value in query.items() if not key.startswith("$") and not isinstance(value, dict)}
            row.setdefault("_id", query.get("_id"))
            self.rows[row["_id"]] = row
        if inserted:
            row.update(deepcopy(update.get("$setOnInsert", {})))
        row.update(deepcopy(update.get("$set", {})))
        for key, value in update.get("$inc", {}).items():
            row[key] = row.get(key, 0) + value
        return None

    def find_one_and_update(self, query, update, upsert=False, return_document=None):
        del return_document
        row = next((item for item in self.rows.values() if _matches(item, query)), None)
        if row is None and not upsert:
            return None
        self.update_one(query, update, upsert=upsert)
        row_id = query.get("_id")
        return deepcopy(self.rows.get(row_id))


class FakeDB(dict):
    def __getitem__(self, name):
        if name not in self:
            self[name] = FakeCollection()
        return dict.__getitem__(self, name)


def _probe_rows(day: str, close: float = 10.0) -> dict[str, dict[str, Any]]:
    return {
        f"SH.60000{index}": {
            "trade_date": day,
            "open": close - 0.2,
            "high": close + 0.3,
            "low": close - 0.4,
            "close": close,
            "vol": 1000 + index,
            "amount": 10000 + index,
            "source_updated_at": datetime.fromisoformat(f"{day} 15:00:05"),
        }
        for index in range(8)
    }


def test_close_seal_waits_for_two_stable_probes_and_runs_modules_once():
    day = "2026-07-14"
    db = FakeDB()
    module_calls: list[str] = []

    runner = CloseSealRunner(
        db,
        lambda module, _day: module_calls.append(module)
        or {
            "module": module,
            "status": "ok",
            "result": {
                "status": "ok",
                "count": 5200 if module == "fullmarket_spot_snapshot" else 1200,
                "trade_date": day,
            },
        },
        probe_fetcher=lambda _day, _now: (8, _probe_rows(day)),
        owner="test-owner",
    )

    first = runner.tick(day, datetime.fromisoformat(f"{day} 15:00:30"))
    second = runner.tick(day, datetime.fromisoformat(f"{day} 15:02:30"))
    third = runner.tick(day, datetime.fromisoformat(f"{day} 15:05:00"))
    fourth = runner.tick(day, datetime.fromisoformat(f"{day} 15:10:00"))

    assert first["status"] == "probing"
    assert second["status"] == "probing"
    assert second["stable"] is True
    assert third["status"] == "probing"
    assert fourth["status"] == "sealed"
    assert fourth["close_finality"] == "stable_close"
    assert module_calls == list(SEAL_MODULES)


def test_hard_seal_is_partial_and_does_not_repeat_successful_modules():
    day = "2026-07-14"
    db = FakeDB()
    module_calls: list[str] = []
    probe_index = {"value": 0}

    def fetch(_day, _now):
        probe_index["value"] += 1
        return 8, _probe_rows(day, close=10.0 + probe_index["value"])

    runner = CloseSealRunner(
        db,
        lambda module, _day: module_calls.append(module) or {"module": module, "status": "ok"},
        probe_fetcher=fetch,
        owner="test-owner",
    )

    runner.tick(day, datetime.fromisoformat(f"{day} 15:00:30"))
    forced = runner.tick(day, datetime.fromisoformat(f"{day} 15:10:00"))
    retried = runner.tick(day, datetime.fromisoformat(f"{day} 15:20:00"))
    after_terminal = runner.tick(day, datetime.fromisoformat(f"{day} 15:25:00"))

    assert forced["status"] == "partial"
    assert forced["close_finality"] == "forced_provisional"
    assert retried["status"] == "partial"
    assert retried["terminal"] is True
    assert after_terminal == {"status": "partial", "skipped": True, "terminal": True}
    assert module_calls == []


def test_degraded_empty_fullmarket_never_seals():
    day = "2026-07-14"
    db = FakeDB()

    def run_module(module, _day):
        if module == "fullmarket_spot_snapshot":
            return {"module": module, "status": "degraded", "result": {"status": "degraded", "count": 0, "trade_date": day}}
        return {"module": module, "status": "ok", "result": {"status": "ok", "count": 1200, "trade_date": day}}

    runner = CloseSealRunner(
        db,
        run_module,
        probe_fetcher=lambda _day, _now: (8, _probe_rows(day)),
        owner="test-owner",
    )

    runner.tick(day, datetime.fromisoformat(f"{day} 15:00:30"))
    runner.tick(day, datetime.fromisoformat(f"{day} 15:02:30"))
    runner.tick(day, datetime.fromisoformat(f"{day} 15:05:00"))
    result = runner.tick(day, datetime.fromisoformat(f"{day} 15:10:00"))

    assert result["status"] == "partial"
    assert db["sync_runs"].find_one({"_id": f"close_seal:{day}"})["close_finality"] == "validation_failed"
    assert db["sync_tasks"].find_one({"_id": f"close_seal:{day}:fullmarket_spot_snapshot:all"})["status"] == "degraded"


def test_stable_probe_with_persistent_module_failure_stops_after_1520():
    day = "2026-07-14"
    db = FakeDB()
    calls: list[str] = []

    def run_module(module, _day):
        calls.append(module)
        if module == "fullmarket_spot_snapshot":
            return {"module": module, "status": "degraded", "result": {"status": "degraded", "count": 0, "trade_date": day}}
        return {"module": module, "status": "ok", "result": {"status": "ok", "count": 1200, "trade_date": day}}

    runner = CloseSealRunner(
        db,
        run_module,
        probe_fetcher=lambda _day, _now: (8, _probe_rows(day)),
        owner="test-owner",
    )

    for value in ("15:00:30", "15:02:30", "15:05:00", "15:10:00"):
        runner.tick(day, datetime.fromisoformat(f"{day} {value}"))
    final_attempt = runner.tick(day, datetime.fromisoformat(f"{day} 15:20:00"))
    call_count = len(calls)
    after_terminal = runner.tick(day, datetime.fromisoformat(f"{day} 15:25:00"))

    assert final_attempt["status"] == "partial"
    assert final_attempt["terminal"] is True
    assert after_terminal == {"status": "partial", "skipped": True, "terminal": True}
    assert len(calls) == call_count
