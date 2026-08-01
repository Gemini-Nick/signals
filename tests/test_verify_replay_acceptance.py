import importlib.util
from pathlib import Path

from signals.replay import market_replay


_SPEC = importlib.util.spec_from_file_location(
    "verify_replay_acceptance",
    Path(__file__).parents[1] / "scripts" / "verify_replay_acceptance.py",
)
verify_replay_acceptance = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(verify_replay_acceptance)


class _Collection:
    def __init__(self, docs):
        self.docs = docs

    def count_documents(self, query):
        return sum(all(doc.get(k) == v for k, v in query.items()) for doc in self.docs)

    def distinct(self, field, query):
        return [doc.get(field) for doc in self.docs if all(doc.get(k) == v for k, v in query.items())]

    def find(self, query=None):
        query = query or {}
        return [doc for doc in self.docs if all(doc.get(k) == v for k, v in query.items())]


class _Db(dict):
    def list_collection_names(self):
        return list(self)


def _readiness(*_args, **_kwargs):
    return {
        "formal_ready": True,
        "close_seal": {
            "status": "sealed",
            "formal_ready": True,
            "close_finality": "stable_close",
            "modules": {"fullmarket_spot_snapshot": "ok", "etf_spot_snapshot": "ok"},
        },
        "stock_daily": {"status": "available", "official_coverage_pct": 99.1, "expected_shards": 2, "shard_count": 2},
    }


def test_audit_requires_close_seal_coverage_and_immutable(monkeypatch):
    monkeypatch.setattr(market_replay, "build_market_replay_readiness", _readiness)
    db = _Db({
        "replay_immutable_snapshots": _Collection([{"trade_date": "2026-08-03", "run_id": "close_seal:2026-08-03"}]),
        "replay_backfill_snapshots": _Collection([]),
    })
    result = verify_replay_acceptance.audit(db, "2026-08-03")
    assert result["status"] == "PASS"
    assert result["read_only"] is True


def test_audit_rejects_missing_immutable_snapshot(monkeypatch):
    monkeypatch.setattr(market_replay, "build_market_replay_readiness", _readiness)
    db = _Db({"replay_immutable_snapshots": _Collection([])})
    result = verify_replay_acceptance.audit(db, "2026-08-03")
    assert result["status"] == "NOT_READY"
    assert result["checks"]["immutable_snapshot_present"] is False


def test_audit_rejects_unmarked_backfill(monkeypatch):
    monkeypatch.setattr(market_replay, "build_market_replay_readiness", _readiness)
    db = _Db({
        "replay_immutable_snapshots": _Collection([{"trade_date": "2026-08-03", "run_id": "close_seal:2026-08-03"}]),
        "replay_backfill_snapshots": _Collection([{"trade_date": "2026-08-03", "backfill": False}]),
    })
    result = verify_replay_acceptance.audit(db, "2026-08-03")
    assert result["status"] == "NOT_READY"
    assert result["checks"]["backfill_is_explicit"] is False
