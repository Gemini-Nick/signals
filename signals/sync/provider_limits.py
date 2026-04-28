# -*- coding: utf-8 -*-
"""Provider-side concurrency limits and cooldown tracking for sync jobs."""
from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, TypeVar

T = TypeVar("T")

RISK_ERROR_TOKENS = (
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


class ProviderCoolingDown(RuntimeError):
    """Raised when a provider is intentionally not called during cooldown."""


@dataclass
class ProviderState:
    provider: str
    capacity: int
    semaphore: threading.BoundedSemaphore
    cooldown_until: float = 0.0
    last_error: str = ""


_LOCK = threading.Lock()
_STATES: dict[str, ProviderState] = {}


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 32) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _capacity(provider: str) -> int:
    provider_key = provider.upper().replace("-", "_")
    defaults = {"TENCENT": 4, "SINA": 2, "EASTMONEY": 1, "EM": 1}
    return _env_int(
        f"SIGNALS_PROVIDER_{provider_key}_CONCURRENCY",
        defaults.get(provider_key, 1),
        minimum=1,
        maximum=16,
    )


def _state(provider: str) -> ProviderState:
    key = provider.lower()
    with _LOCK:
        existing = _STATES.get(key)
        capacity = _capacity(key)
        if existing and existing.capacity == capacity:
            return existing
        created = ProviderState(provider=key, capacity=capacity, semaphore=threading.BoundedSemaphore(capacity))
        if existing:
            created.cooldown_until = existing.cooldown_until
            created.last_error = existing.last_error
        _STATES[key] = created
        return created


def _jitter_seconds() -> tuple[float, float]:
    raw = os.getenv("SIGNALS_PROVIDER_JITTER_SECONDS")
    if raw is None and os.getenv("PYTEST_CURRENT_TEST"):
        return 0.0, 0.0
    raw = raw or "0.3,1.2"
    try:
        low, high = raw.replace(":", ",").split(",", 1)
        low_value = max(0.0, float(low))
        high_value = max(low_value, float(high))
        return low_value, high_value
    except Exception:
        return 0.3, 1.2


def _cooldown_seconds() -> float:
    raw = os.getenv("SIGNALS_PROVIDER_COOLDOWN_SECONDS")
    if raw is None and os.getenv("PYTEST_CURRENT_TEST"):
        return 0.1
    raw = raw or "120,300"
    try:
        low, high = raw.replace(":", ",").split(",", 1)
        low_value = max(1.0, float(low))
        high_value = max(low_value, float(high))
        return random.uniform(low_value, high_value)
    except Exception:
        return random.uniform(120.0, 300.0)


def _risk_error(exc: BaseException) -> bool:
    text = f"{exc.__class__.__name__}: {exc}".lower()
    return any(token in text for token in RISK_ERROR_TOKENS)


def _write_provider_health(
    db,
    *,
    provider: str,
    endpoint: str,
    domain: str,
    status: str,
    latency_ms: float | None = None,
    error: str = "",
    cooldown_until: datetime | None = None,
) -> None:
    if db is None:
        return
    try:
        now = datetime.now()
        update = {
            "provider": provider,
            "endpoint": endpoint,
            "domain": domain,
            "status": status,
            "updated_at": now,
        }
        if latency_ms is not None:
            update["avg_latency_ms"] = round(latency_ms, 2)
        if status == "ok":
            update["last_success_at"] = now
            update["last_error_type"] = None
            update["cooldown_until"] = None
        else:
            update["last_error_at"] = now
            update["last_error_type"] = error[:240]
            if cooldown_until is not None:
                update["cooldown_until"] = cooldown_until
        db["provider_health"].update_one(
            {"provider": provider, "endpoint": endpoint, "domain": domain},
            {"$set": update},
            upsert=True,
        )
    except Exception:
        return


def provider_call(
    provider: str,
    endpoint: str,
    fn: Callable[[], T],
    *,
    db=None,
    domain: str = "market_data",
) -> T:
    """Run a provider request under per-provider concurrency and cooldown rules."""
    state = _state(provider)
    now = time.monotonic()
    if state.cooldown_until > now:
        remaining = int(state.cooldown_until - now)
        error = f"provider_cooling_down:{provider}:{remaining}s"
        _write_provider_health(db, provider=state.provider, endpoint=endpoint, domain=domain, status="cooldown", error=error)
        raise ProviderCoolingDown(error)

    low, high = _jitter_seconds()
    if high > 0:
        time.sleep(random.uniform(low, high))

    started = time.monotonic()
    with state.semaphore:
        try:
            result = fn()
        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000
            status = "cooldown" if _risk_error(exc) else "degraded"
            cooldown_dt = None
            if status == "cooldown":
                seconds = _cooldown_seconds()
                state.cooldown_until = time.monotonic() + seconds
                state.last_error = str(exc)[:240]
                cooldown_dt = datetime.now() + timedelta(seconds=seconds)
            _write_provider_health(
                db,
                provider=state.provider,
                endpoint=endpoint,
                domain=domain,
                status=status,
                latency_ms=elapsed_ms,
                error=f"{exc.__class__.__name__}: {exc}",
                cooldown_until=cooldown_dt,
            )
            raise
        elapsed_ms = (time.monotonic() - started) * 1000
        state.last_error = ""
        if state.cooldown_until <= time.monotonic():
            state.cooldown_until = 0.0
        _write_provider_health(
            db,
            provider=state.provider,
            endpoint=endpoint,
            domain=domain,
            status="ok",
            latency_ms=elapsed_ms,
        )
        return result
