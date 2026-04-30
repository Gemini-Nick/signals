# -*- coding: utf-8 -*-
"""Observation records for the Eastmoney realtime data path."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from signals.core.market_time import naive_market_now

logger = logging.getLogger("signals.sync.eastmoney_observer")

_RISK_TOKENS = (
    "403",
    "429",
    "remote end closed connection",
    "remotedisconnected",
    "ssl eof",
    "eof occurred",
    "rate limit",
    "throttl",
    "too many requests",
)


def _observe_log_path() -> Path:
    log_dir = Path(os.getenv("LONGCLAW_LOG_DIR", "/tmp/longclaw-guardian"))
    return log_dir / "signals.eastmoney.observe.log"


def _risk_signal(http_status: int | None, error: str = "") -> bool:
    if http_status in {403, 429}:
        return True
    text = str(error or "").lower()
    return any(token in text for token in _RISK_TOKENS)


def observe_eastmoney(
    db,
    *,
    endpoint: str,
    domain: str,
    request_count: int,
    returned_count: int,
    elapsed_ms: float,
    http_status: int | None = None,
    rc: Any = None,
    error: str = "",
    downgraded: bool = False,
    requested_symbols: int | None = None,
    batch_size: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one observation to both a JSONL log and Mongo."""
    now = naive_market_now("A")
    elapsed = round(float(elapsed_ms or 0.0), 2)
    record: dict[str, Any] = {
        "observed_at": now,
        "provider": "eastmoney",
        "endpoint": endpoint,
        "domain": domain,
        "request_count": int(request_count or 0),
        "requested_symbols": requested_symbols,
        "batch_size": batch_size,
        "returned_count": int(returned_count or 0),
        "elapsed_ms": elapsed,
        "http_status": http_status,
        "rc": rc,
        "error": str(error or "")[:500],
        "risk_signal": _risk_signal(http_status, error),
        "downgraded": bool(downgraded),
    }
    if extra:
        record.update(extra)

    try:
        path = _observe_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.debug("eastmoney observe log write skipped: %s", exc)

    if db is not None:
        try:
            db["provider_observations"].insert_one(record)
        except Exception as exc:
            logger.debug("provider_observations write skipped: %s", exc)
    return record
