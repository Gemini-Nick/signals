"""Append-only index constituent snapshots for historical-safe seeding.

The legacy seed scripts used ``drop()`` and a single ``dt`` row.  That made a
current constituent list look like historical evidence.  This helper keeps
each payload under ``index_name + effective_date + payload_hash`` and never
deletes an earlier version.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def append_index_snapshot(
    collection: Any,
    *,
    index_name: str,
    effective_date: str,
    stocks: Iterable[Mapping[str, Any]],
    source: str,
    captured_at: str | None = None,
) -> dict[str, Any]:
    rows = [dict(row) for row in stocks]
    payload = {
        "index_name": index_name,
        "effective_date": effective_date,
        "stocks": rows,
        "source": source,
    }
    payload_hash = hashlib.sha256(_canonical(payload)).hexdigest()
    captured_at = captured_at or datetime.now(timezone.utc).isoformat()
    document = {
        "index_name": index_name,
        "effective_date": effective_date,
        # Keep dt for old diagnostic readers, but never use it as the identity.
        "dt": effective_date,
        "version": f"{effective_date}:{payload_hash[:16]}",
        "stocks": [str(row.get("code") or "") for row in rows],
        "stock_details": rows,
        "count": len(rows),
        "source": source,
        "captured_at": captured_at,
        "payload_hash": payload_hash,
        "immutable_snapshot": True,
    }
    try:
        collection.create_index(
            [("index_name", 1), ("effective_date", 1), ("payload_hash", 1)],
            unique=True,
            background=True,
            name="index_effective_payload_unique",
        )
    except Exception:
        # Index creation can be managed centrally; an insert still remains
        # append-only and the publisher will retain the payload hash.
        pass
    existing = collection.find_one({
        "index_name": index_name,
        "effective_date": effective_date,
        "payload_hash": payload_hash,
    })
    if existing:
        return {"inserted": False, "document": document}
    collection.insert_one(document)
    return {"inserted": True, "document": document}
