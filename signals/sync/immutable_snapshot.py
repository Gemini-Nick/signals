"""Append-only replay snapshot storage for formal point-in-time evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from signals.sync.task_context import get_task_env


COLLECTION = "replay_immutable_snapshots"
BACKFILL_COLLECTION = "replay_backfill_snapshots"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def current_run_id() -> str | None:
    # Postmarket shards carry run identity in task-local context; fall back to
    # process environment for standalone collectors and CLI diagnostics.
    value = get_task_env("SIGNALS_POSTMARKET_RUN_ID") or get_task_env("SIGNALS_CLOSE_SEAL_RUN_ID")
    return str(value).strip() if value and str(value).strip() else None


def historical_without_run_id(
    trade_date: str,
    *,
    run_id: str | None = None,
    today: date | None = None,
) -> bool:
    """Return whether a historical refresh must be isolated from formal data.

    A refresh for an earlier trading date is a backfill unless it carries the
    explicit postmarket or close-seal run id.  The date comparison is made in
    Beijing time so a late-night UTC process cannot accidentally treat a
    previous market day as today's formal snapshot.
    """
    if run_id or current_run_id():
        return False
    try:
        requested = date.fromisoformat(str(trade_date)[:10])
    except (TypeError, ValueError):
        return False
    beijing_today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    return requested < beijing_today


def append_snapshot_docs(
    db: Any,
    *,
    source_id: str,
    trade_date: str,
    docs: list[dict[str, Any]],
    run_id: str | None = None,
) -> dict[str, int | bool]:
    """Append immutable copies keyed by run/source/payload, never overwrite.

    The normal working collection may still be upserted for compatibility.  A
    formal run must additionally call this function; without a run id it
    returns ``written=false`` so an ad-hoc repair cannot masquerade as a
    point-in-time observation.
    """
    effective_run_id = run_id or current_run_id()
    if not effective_run_id or not docs:
        return {"written": False, "inserted": 0, "skipped": len(docs)}
    operations = []
    for doc in docs:
        body = dict(doc)
        body.pop("_id", None)
        payload_hash = hashlib.sha256(_canonical(body)).hexdigest()
        item_key = str(doc.get("code") or doc.get("symbol") or doc.get("_id") or "")
        immutable_id = hashlib.sha256(
            _canonical({
                "run_id": effective_run_id,
                "source_id": source_id,
                "trade_date": trade_date,
                "item_key": item_key,
                "payload_hash": payload_hash,
            })
        ).hexdigest()
        snapshot = {
            "_id": immutable_id,
            "run_id": effective_run_id,
            "source_id": source_id,
            "trade_date": trade_date,
            "item_key": item_key,
            "payload_hash": payload_hash,
            "captured_at": datetime.now(timezone.utc),
            "payload": body,
        }
        # Import lazily so lightweight unit tests and CLI diagnostics do not
        # need pymongo's UpdateOne class just to inspect the helper.
        from pymongo import UpdateOne

        operations.append(UpdateOne({"_id": immutable_id}, {"$setOnInsert": snapshot}, upsert=True))
    result = db[COLLECTION].bulk_write(operations, ordered=False)
    return {"written": True, "inserted": int(result.upserted_count), "skipped": len(docs) - int(result.upserted_count)}


def append_backfill_snapshot_docs(
    db: Any,
    *,
    source_id: str,
    trade_date: str,
    docs: list[dict[str, Any]],
    reason: str = "historical_without_run_id",
    captured_at: datetime | None = None,
) -> dict[str, int | bool | str]:
    """Write historical refreshes to an append-only, non-formal collection.

    This path intentionally has no dependency on a formal run id.  Every
    record is marked ``backfill=true`` and is keyed by its payload hash plus
    capture time, so it cannot overwrite the date/code keyed working tables.
    """
    if not docs:
        return {
            "written": False,
            "inserted": 0,
            "skipped": 0,
            "collection": BACKFILL_COLLECTION,
            "backfill": True,
            "reason": reason,
        }
    captured = captured_at or datetime.now(timezone.utc)
    operations = []
    for doc in docs:
        body = dict(doc)
        body.pop("_id", None)
        payload_hash = hashlib.sha256(_canonical(body)).hexdigest()
        item_key = str(doc.get("code") or doc.get("symbol") or doc.get("_id") or "")
        identity = {
            "source_id": source_id,
            "trade_date": trade_date,
            "item_key": item_key,
            "payload_hash": payload_hash,
            "captured_at": captured,
        }
        backfill_id = hashlib.sha256(_canonical(identity)).hexdigest()
        snapshot = {
            "_id": backfill_id,
            "run_id": None,
            "source_id": source_id,
            "trade_date": trade_date,
            "item_key": item_key,
            "payload_hash": payload_hash,
            "captured_at": captured,
            "backfill_at": captured,
            "backfill": True,
            "execution_mode": "backfill",
            "backfill_reason": reason,
            "payload": body,
        }
        from pymongo import UpdateOne

        operations.append(UpdateOne({"_id": backfill_id}, {"$setOnInsert": snapshot}, upsert=True))
    result = db[BACKFILL_COLLECTION].bulk_write(operations, ordered=False)
    inserted = int(result.upserted_count)
    return {
        "written": True,
        "inserted": inserted,
        "skipped": len(docs) - inserted,
        "collection": BACKFILL_COLLECTION,
        "backfill": True,
        "reason": reason,
    }
