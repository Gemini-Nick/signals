# -*- coding: utf-8 -*-
"""Atomic publication helpers for the single authoritative terminal stock pool."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

POOL_KEY = {"pool": "terminal_stock_pool", "market": "A"}
POLICY_VERSION = "single_fused_postmarket_v1"
DEFAULT_LEASE_SECONDS = 180
DEFAULT_HEARTBEAT_SECONDS = 45
MAX_LEASE_SECONDS = 10 * 60
BUILD_INPUT_TTL_MINUTES = 30

_VOLATILE_KEYS = {
    "_id",
    "revision",
    "generation_id",
    "published_at",
    "updated_at",
    "build_control",
    "last_successful_publish",
    "last_failed_attempt",
    "latest_price",
    "price",
    "change",
    "change_pct",
    "latest_change_pct",
    "quote_status",
    "quote_snapshot_at",
    "confirmation_30m",
    "display_watermarks",
}
_REASON_KEYS = (
    "reason_type",
    "signal_family",
    "freq",
    "signal_side",
    "signal_type",
    "source_collection",
    "source_doc_id",
    "as_of",
    "event_date",
    "score",
    "weight",
)
_ROW_KEYS = (
    "symbol",
    "raw_code",
    "name",
    "pool_type",
    "rank",
    "rank_score",
    "score",
    "entry_gate_status",
    "action_status",
    "trade_stage",
    "setup_mode",
    "signal_origin",
    "signal_family",
    "latest_signal",
    "source_tags",
    "source_collections",
    "reason",
    "rank_reason",
    "invalidates_when",
    "ma_alignment",
    "knowledge_confirmation",
    "chain_context",
    "chain_position",
)
_POOL_LIST_KEYS = ("focus_stocks", "risk_stocks", "watch_stocks", "clue_stocks")
_PAYLOAD_KEYS = (
    "pool",
    "market",
    "dt",
    "trade_date",
    "base_trade_date",
    "policy_version",
    "stock_limit",
    "risk_limit",
    "watch_limit",
    "clue_limit",
    "candidate_count",
    "raw_candidate_count",
    "strict_candidate_count",
    "fallback_count",
    "fallback_enabled",
    "pool_counts",
    "sector_transition_context_counts",
    "broad_market_context",
    "reason_counts",
    "candidate_counts_by_source",
    "candidate_counts_by_side",
    "candidate_counts_by_freq",
    "coverage_by_freq",
    "required_freqs",
    "optional_freqs",
    "is_full_market_complete",
    "coverage_status",
    "ranking_version",
    "source",
    "source_policy",
    "selection_policy",
)


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if key not in _VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, float):
        return round(value, 6)
    return _iso(value)


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_reason(reason: dict[str, Any]) -> dict[str, Any]:
    return {
        key: reason.get(key)
        for key in _REASON_KEYS
        if reason.get(key) not in (None, "", [], {})
    }


def _canonical_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {
        key: row.get(key)
        for key in _ROW_KEYS
        if row.get(key) not in (None, "", [], {})
    }
    reasons = [
        _canonical_reason(reason)
        for reason in row.get("inclusion_reasons") or []
        if isinstance(reason, dict)
    ]
    reasons.sort(
        key=lambda item: tuple(str(item.get(key) or "") for key in _REASON_KEYS[:7])
    )
    out["inclusion_reasons"] = reasons
    return out


def pool_hashes(pool_doc: dict[str, Any]) -> tuple[str, str]:
    membership: list[dict[str, Any]] = []
    payload: dict[str, Any] = {
        key: pool_doc.get(key)
        for key in _PAYLOAD_KEYS
        if pool_doc.get(key) not in (None, "", [], {})
    }
    for group in _POOL_LIST_KEYS:
        rows = [
            _canonical_row(row)
            for row in pool_doc.get(group) or []
            if isinstance(row, dict)
        ]
        rows.sort(key=lambda row: (int(row.get("rank") or 0), str(row.get("symbol") or "")))
        payload[group] = rows
        for row in rows:
            membership.append({
                "symbol": row.get("symbol"),
                "final_pool": group.removesuffix("_stocks"),
                "reasons": [
                    {
                        key: reason.get(key)
                        for key in _REASON_KEYS[:7]
                        if reason.get(key) not in (None, "")
                    }
                    for reason in row.get("inclusion_reasons") or []
                ],
            })
    membership.sort(key=lambda row: (str(row.get("final_pool") or ""), str(row.get("symbol") or "")))
    return canonical_hash(membership), canonical_hash(payload)


def watermark_hash(watermarks: dict[str, Any]) -> str:
    return canonical_hash(watermarks)


def _owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"


def acquire_build_lease(
    db: Any,
    *,
    requested_trade_date: str,
    trigger: str,
    now: datetime,
) -> dict[str, Any]:
    current = db["terminal_stock_pool"].find_one(POOL_KEY) or {}
    current_base_date = str(current.get("base_trade_date") or current.get("trade_date") or "")[:10]
    if current_base_date and requested_trade_date < current_base_date:
        return {
            "status": "stale_trigger",
            "reason": "requested_trade_date_older_than_published_pool",
            "requested_trade_date": requested_trade_date,
            "current_base_trade_date": current_base_date,
        }

    owner = _owner_id()
    attempt_id = uuid.uuid4().hex
    lease_until = now + timedelta(seconds=DEFAULT_LEASE_SECONDS)
    query = {
        **POOL_KEY,
        "$or": [
            {"build_control.lease_until": {"$exists": False}},
            {"build_control.lease_until": {"$lte": now}},
        ],
    }
    update = {
        "$setOnInsert": {
            **POOL_KEY,
            "revision": 0,
            "publish_status": "unavailable",
        },
        "$inc": {"build_control.fence_token": 1},
        "$set": {
            "build_control.lease_owner": owner,
            "build_control.lease_until": lease_until,
            "build_control.active_attempt": {
                "attempt_id": attempt_id,
                "trigger": trigger,
                "requested_trade_date": requested_trade_date,
                "status": "running",
                "started_at": now,
                "owner": owner,
            },
        },
    }
    try:
        leased = db["terminal_stock_pool"].find_one_and_update(
            query,
            update,
            upsert=not bool(current),
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        leased = None
    if not leased:
        return {
            "status": "busy",
            "reason": "terminal_pool_build_lease_held",
            "requested_trade_date": requested_trade_date,
        }
    token = int(((leased.get("build_control") or {}).get("fence_token")) or 0)
    return {
        "status": "acquired",
        "owner": owner,
        "fence_token": token,
        "attempt_id": attempt_id,
        "expected_revision": int(leased.get("revision") or 0),
        "current": leased,
        "started_at": now,
        "lease_until": lease_until,
    }


class LeaseHeartbeat:
    def __init__(self, db: Any, lease: dict[str, Any], *, started_at: datetime):
        self.db = db
        self.lease = lease
        self.started_at = started_at
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.lease.get("status") != "acquired":
            return
        self.thread = threading.Thread(
            target=self._run,
            name="terminal-pool-lease-heartbeat",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1)

    def _run(self) -> None:
        while not self.stop_event.wait(DEFAULT_HEARTBEAT_SECONDS):
            now = datetime.now(tz=self.started_at.tzinfo)
            if (now - self.started_at).total_seconds() >= MAX_LEASE_SECONDS:
                return
            lease_until = min(
                now + timedelta(seconds=DEFAULT_LEASE_SECONDS),
                self.started_at + timedelta(seconds=MAX_LEASE_SECONDS),
            )
            result = self.db["terminal_stock_pool"].update_one(
                {
                    **POOL_KEY,
                    "build_control.lease_owner": self.lease["owner"],
                    "build_control.fence_token": self.lease["fence_token"],
                },
                {"$set": {"build_control.lease_until": lease_until}},
            )
            if not int(getattr(result, "matched_count", 0) or 0):
                return


def stage_candidate(
    db: Any,
    *,
    generation_id: str,
    pool_doc: dict[str, Any],
    watermarks_before_hash: str,
    watermarks_after_hash: str,
    now: datetime,
) -> None:
    db["terminal_pool_build_inputs"].update_one(
        {"_id": generation_id},
        {"$set": {
            "generation_id": generation_id,
            "base_trade_date": pool_doc.get("base_trade_date"),
            "policy_version": pool_doc.get("policy_version"),
            "membership_hash": pool_doc.get("membership_hash"),
            "payload_hash": pool_doc.get("payload_hash"),
            "eligibility_manifest_hash_before": watermarks_before_hash,
            "eligibility_manifest_hash_after": watermarks_after_hash,
            "candidate": pool_doc,
            "created_at": now,
            "expires_at": now + timedelta(minutes=BUILD_INPUT_TTL_MINUTES),
        }},
        upsert=True,
    )


def cleanup_staged_candidate(db: Any, generation_id: str) -> None:
    try:
        db["terminal_pool_build_inputs"].delete_one({"_id": generation_id})
    except PyMongoError:
        pass


def finish_attempt(
    db: Any,
    lease: dict[str, Any],
    *,
    status: str,
    reason: str,
    now: datetime,
    generation_id: str = "",
    requested_trade_date: str = "",
) -> bool:
    attempt = {
        "attempt_id": lease.get("attempt_id"),
        "generation_id": generation_id,
        "trigger": "postmarket",
        "requested_trade_date": requested_trade_date,
        "status": status,
        "reason": reason,
        "started_at": lease.get("started_at"),
        "finished_at": now,
        "owner": lease.get("owner"),
        "fence_token": lease.get("fence_token"),
    }
    result = db["terminal_stock_pool"].update_one(
        {
            **POOL_KEY,
            "build_control.lease_owner": lease.get("owner"),
            "build_control.fence_token": lease.get("fence_token"),
        },
        {
            "$set": {
                "build_control.last_completed_attempt": attempt,
                **({"last_failed_attempt": attempt} if status == "failed" else {}),
            },
            "$unset": {
                "build_control.active_attempt": "",
                "build_control.lease_owner": "",
                "build_control.lease_until": "",
            },
        },
    )
    return bool(int(getattr(result, "matched_count", 0) or 0))


def publish_candidate(
    db: Any,
    lease: dict[str, Any],
    pool_doc: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    current = db["terminal_stock_pool"].find_one(POOL_KEY) or {}
    current_watermarks = current.get("eligibility_watermarks") if isinstance(current.get("eligibility_watermarks"), dict) else {}
    candidate_watermarks = pool_doc.get("eligibility_watermarks") if isinstance(pool_doc.get("eligibility_watermarks"), dict) else {}
    for family, current_watermark in current_watermarks.items():
        if not isinstance(current_watermark, dict):
            continue
        candidate_watermark = candidate_watermarks.get(family) if isinstance(candidate_watermarks.get(family), dict) else {}
        current_event_date = str(current_watermark.get("event_date") or "")
        candidate_event_date = str(candidate_watermark.get("event_date") or "")
        current_revision = int(current_watermark.get("manifest_revision") or 0)
        candidate_revision = int(candidate_watermark.get("manifest_revision") or 0)
        if (
            current_event_date
            and candidate_event_date
            and candidate_event_date < current_event_date
        ) or candidate_revision < current_revision:
            finish_attempt(
                db,
                lease,
                status="failed",
                reason=f"stale_source_watermark:{family}",
                now=now,
                generation_id=str(pool_doc.get("generation_id") or ""),
                requested_trade_date=str(pool_doc.get("base_trade_date") or ""),
            )
            return {
                "status": "rejected",
                "reason": "stale_source_watermark",
                "source_family": family,
                "revision": int(current.get("revision") or 0),
            }
    if (
        current.get("publish_status") == "published"
        and current.get("payload_hash")
        and current.get("payload_hash") == pool_doc.get("payload_hash")
    ):
        healthy = finish_attempt(
            db,
            lease,
            status="healthy",
            reason="noop_identical_payload",
            now=now,
            generation_id=str(pool_doc.get("generation_id") or ""),
            requested_trade_date=str(pool_doc.get("base_trade_date") or ""),
        )
        return {
            "status": "noop" if healthy else "superseded",
            "reason": "identical_payload" if healthy else "superseded_fence_token",
            "revision": int(current.get("revision") or 0),
            "generation_id": current.get("generation_id"),
        }

    expected_revision = int(lease.get("expected_revision") or 0)
    next_revision = expected_revision + 1
    published = {
        **pool_doc,
        "revision": next_revision,
        "published_at": now,
        "updated_at": now,
        "publish_status": "published",
        "publish_trigger": "postmarket",
        "publisher_fence_token": lease.get("fence_token"),
        "last_successful_publish": {
            "revision": next_revision,
            "generation_id": pool_doc.get("generation_id"),
            "base_trade_date": pool_doc.get("base_trade_date"),
            "published_at": now,
            "membership_hash": pool_doc.get("membership_hash"),
            "payload_hash": pool_doc.get("payload_hash"),
        },
        "build_control.last_completed_attempt": {
            "attempt_id": lease.get("attempt_id"),
            "generation_id": pool_doc.get("generation_id"),
            "trigger": "postmarket",
            "requested_trade_date": pool_doc.get("base_trade_date"),
            "base_trade_date": pool_doc.get("base_trade_date"),
            "status": "healthy",
            "reason": "published",
            "started_at": lease.get("started_at"),
            "finished_at": now,
            "owner": lease.get("owner"),
            "fence_token": lease.get("fence_token"),
        },
    }
    update_set = {
        key: value
        for key, value in published.items()
        if key != "build_control.last_completed_attempt"
    }
    update_set["build_control.last_completed_attempt"] = published["build_control.last_completed_attempt"]
    result = db["terminal_stock_pool"].update_one(
        {
            **POOL_KEY,
            "revision": expected_revision,
            "build_control.lease_owner": lease.get("owner"),
            "build_control.fence_token": lease.get("fence_token"),
            "$or": [
                {"base_trade_date": {"$exists": False}},
                {"base_trade_date": {"$lte": pool_doc.get("base_trade_date")}},
            ],
        },
        {
            "$set": update_set,
            "$unset": {
                "build_control.active_attempt": "",
                "build_control.lease_owner": "",
                "build_control.lease_until": "",
            },
        },
    )
    if not int(getattr(result, "matched_count", 0) or 0):
        return {
            "status": "superseded",
            "reason": "revision_or_fence_token_changed",
            "revision": expected_revision,
        }
    return {
        "status": "published",
        "reason": "published",
        "revision": next_revision,
        "generation_id": pool_doc.get("generation_id"),
    }
