from datetime import date, datetime, timezone

from signals.sync.immutable_snapshot import (
    append_backfill_snapshot_docs,
    append_snapshot_docs,
    current_run_id,
    historical_without_run_id,
)
from signals.sync.task_context import task_env


class _Result:
    upserted_count = 1


class _Collection:
    def __init__(self):
        self.operations = []

    def bulk_write(self, operations, ordered=False):
        self.operations.extend(operations)
        assert ordered is False
        return _Result()


class _DB(dict):
    def __getitem__(self, key):
        return self.setdefault(key, _Collection())


def test_append_snapshot_requires_formal_run_id():
    db = _DB()
    result = append_snapshot_docs(db, source_id="signals-fullmarket-spot", trade_date="2026-08-03", docs=[{"_id": "x", "code": "600000"}])
    assert result["written"] is False
    assert "replay_immutable_snapshots" not in db


def test_append_snapshot_is_idempotent_for_same_run_and_payload():
    db = _DB()
    docs = [{"_id": "20260803:600000", "code": "600000", "price": 10.0}]
    first = append_snapshot_docs(db, source_id="signals-fullmarket-spot", trade_date="2026-08-03", docs=docs, run_id="postmarket:2026-08-03")
    second = append_snapshot_docs(db, source_id="signals-fullmarket-spot", trade_date="2026-08-03", docs=docs, run_id="postmarket:2026-08-03")
    assert first["written"] is True
    assert second["written"] is True
    assert len(db["replay_immutable_snapshots"].operations) == 2
    assert db["replay_immutable_snapshots"].operations[0]._filter == db["replay_immutable_snapshots"].operations[1]._filter


def test_historical_without_run_id_uses_beijing_date_boundary(monkeypatch):
    monkeypatch.delenv("SIGNALS_POSTMARKET_RUN_ID", raising=False)
    monkeypatch.delenv("SIGNALS_CLOSE_SEAL_RUN_ID", raising=False)
    assert historical_without_run_id("2026-08-02", today=date(2026, 8, 3))
    assert not historical_without_run_id("2026-08-03", today=date(2026, 8, 3))
    monkeypatch.setenv("SIGNALS_POSTMARKET_RUN_ID", "postmarket:2026-08-02")
    assert not historical_without_run_id("2026-08-02", today=date(2026, 8, 3))


def test_append_backfill_snapshot_is_separate_and_marked():
    db = _DB()
    captured = datetime(2026, 8, 3, 1, 0, tzinfo=timezone.utc)
    result = append_backfill_snapshot_docs(
        db,
        source_id="signals-fullmarket-spot",
        trade_date="2026-08-02",
        docs=[{"_id": "20260802:600000", "code": "600000", "price": 10.0}],
        captured_at=captured,
    )
    assert result["written"] is True
    assert result["collection"] == "replay_backfill_snapshots"
    assert result["backfill"] is True
    operation = db["replay_backfill_snapshots"].operations[0]
    snapshot = operation._doc["$setOnInsert"]
    assert snapshot["backfill"] is True
    assert snapshot["execution_mode"] == "backfill"
    assert snapshot["run_id"] is None
    assert snapshot["trade_date"] == "2026-08-02"


def test_current_run_id_reads_task_local_postmarket_context():
    with task_env({"SIGNALS_POSTMARKET_RUN_ID": "postmarket:2026-08-03"}):
        assert current_run_id() == "postmarket:2026-08-03"
