# -*- coding: utf-8 -*-
"""Versioned backtest-history snapshots for Agent OS replay."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "backtest-history.v2"
DEFAULT_HISTORY_LIMIT = 50
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:,-]{0,220}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_history_root(repo_root: Path | None = None) -> Path:
    root = repo_root or Path(__file__).resolve().parents[2]
    return root / ".longclaw" / "domain-pack" / "backtest-history"


def _safe_history_id(value: Any) -> str:
    history_id = str(value or "").strip()
    if not history_id:
        history_id = f"backtest-{uuid.uuid4().hex}"
    if not _ID_RE.match(history_id) or history_id in {".", ".."}:
        raise ValueError("invalid backtest history id")
    return history_id


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class BacktestHistoryStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_history_root()

    def list(self, *, limit: int = DEFAULT_HISTORY_LIMIT, include_deleted: bool = False) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if row.get("deletedAt") and not include_deleted:
                continue
            rows.append(row)
        rows.sort(key=lambda item: str(item.get("createdAt") or ""), reverse=True)
        return rows[: max(1, min(int(limit or DEFAULT_HISTORY_LIMIT), 200))]

    def get(self, history_id: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
        path = self._path(history_id)
        if not path.exists():
            return None
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("deletedAt") and not include_deleted:
            return None
        return row

    def save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        record = dict(_json_safe(dict(payload or {})))
        history_id = _safe_history_id(record.get("id"))
        existing = self.get(history_id, include_deleted=True) or {}
        now = _utc_now()
        record["id"] = history_id
        record["schema_version"] = SCHEMA_VERSION
        record["createdAt"] = str(record.get("createdAt") or existing.get("createdAt") or now)
        if not record.get("deletedAt"):
            record["deletedAt"] = None
        self._write(history_id, record)
        return record

    def delete(self, history_id: str) -> dict[str, Any]:
        safe_id = _safe_history_id(history_id)
        now = _utc_now()
        record = self.get(safe_id, include_deleted=True) or {
            "id": safe_id,
            "schema_version": SCHEMA_VERSION,
            "mode": "deleted",
            "title": "",
            "meta": "",
            "codes": [],
            "freq": "",
            "createdAt": now,
        }
        record["schema_version"] = SCHEMA_VERSION
        record["deletedAt"] = str(record.get("deletedAt") or now)
        self._write(safe_id, record)
        return record

    def _path(self, history_id: str) -> Path:
        return self.root / f"{_safe_history_id(history_id)}.json"

    def _write(self, history_id: str, record: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(history_id)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(_json_safe(record), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)
