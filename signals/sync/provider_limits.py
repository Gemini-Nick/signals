# -*- coding: utf-8 -*-
"""Provider-side concurrency limits and cooldown tracking for sync jobs."""
from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterable, TypeVar

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
    endpoint: str
    domain: str
    capacity: int
    semaphore: threading.BoundedSemaphore
    min_interval_seconds: float = 0.0
    last_call_at: float = 0.0
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


def _safe_env_name(value: str) -> str:
    return value.upper().replace("-", "_").replace("/", "_").replace(".", "_")


def _capacity(provider: str, endpoint: str = "") -> int:
    provider_key = provider.upper().replace("-", "_")
    endpoint_key = _safe_env_name(endpoint)
    endpoint_defaults = {
        ("TENCENT", "STOCK_DAILY"): 2,
        ("TENCENT", "STOCK_MINUTE"): 2,
        ("SINA", "STOCK_MINUTE"): 1,
        ("SINA", "STOCK_DAILY"): 1,
        ("EASTMONEY", "QUOTE_SNAPSHOT"): 2,
        ("EASTMONEY", "PUSH2DELAY_STOCK_GET"): 2,
        ("EASTMONEY", "PUSH2DELAY_CLIST_INDUSTRY"): 1,
        ("EASTMONEY", "PUSH2DELAY_CLIST_CONCEPT"): 1,
        ("EM", "PUSH2DELAY_CLIST_INDUSTRY"): 1,
        ("EM", "PUSH2DELAY_CLIST_CONCEPT"): 1,
        ("EASTMONEY", "STOCK_DAILY_HIST"): 1,
        ("EM", "STOCK_DAILY_HIST"): 1,
    }
    defaults = {"TENCENT": 2, "SINA": 1, "EASTMONEY": 1, "EM": 1}
    if endpoint_key:
        endpoint_env = os.getenv(f"SIGNALS_PROVIDER_{provider_key}_{endpoint_key}_CONCURRENCY")
        if endpoint_env is not None:
            return _env_int(
                f"SIGNALS_PROVIDER_{provider_key}_{endpoint_key}_CONCURRENCY",
                endpoint_defaults.get((provider_key, endpoint_key), defaults.get(provider_key, 1)),
                minimum=1,
                maximum=16,
            )
    return _env_int(
        f"SIGNALS_PROVIDER_{provider_key}_CONCURRENCY",
        endpoint_defaults.get((provider_key, endpoint_key), defaults.get(provider_key, 1)),
        minimum=1,
        maximum=16,
    )


def _min_interval_seconds(provider: str, endpoint: str = "") -> float:
    provider_key = _safe_env_name(provider)
    endpoint_key = _safe_env_name(endpoint)
    endpoint_defaults = {
        ("TENCENT", "STOCK_DAILY"): 0.5,
        ("TENCENT", "STOCK_MINUTE"): 0.5,
        ("SINA", "STOCK_MINUTE"): 1.0,
        ("SINA", "STOCK_DAILY"): 3.0,
        ("EASTMONEY", "QUOTE_SNAPSHOT"): 0.3,
        ("EASTMONEY", "PUSH2DELAY_STOCK_GET"): 0.3,
        ("EASTMONEY", "PUSH2DELAY_CLIST_INDUSTRY"): 3.0,
        ("EASTMONEY", "PUSH2DELAY_CLIST_CONCEPT"): 3.0,
        ("EM", "PUSH2DELAY_CLIST_INDUSTRY"): 3.0,
        ("EM", "PUSH2DELAY_CLIST_CONCEPT"): 3.0,
        ("EASTMONEY", "STOCK_DAILY_HIST"): 2.0,
        ("EM", "STOCK_DAILY_HIST"): 2.0,
    }
    default = endpoint_defaults.get((provider_key, endpoint_key), 0.0)
    names = []
    if endpoint_key:
        names.append(f"SIGNALS_PROVIDER_{provider_key}_{endpoint_key}_MIN_INTERVAL_SECONDS")
    names.append(f"SIGNALS_PROVIDER_{provider_key}_MIN_INTERVAL_SECONDS")
    for name in names:
        raw = os.getenv(name)
        if raw is None:
            continue
        try:
            return max(0.0, float(raw))
        except ValueError:
            return default
    return default


def _state(provider: str, endpoint: str, domain: str) -> ProviderState:
    key = f"{provider.lower()}:{endpoint.lower()}:{domain.lower()}"
    with _LOCK:
        existing = _STATES.get(key)
        capacity = _capacity(provider, endpoint)
        min_interval = _min_interval_seconds(provider, endpoint)
        if existing and existing.capacity == capacity and existing.min_interval_seconds == min_interval:
            return existing
        created = ProviderState(
            provider=provider.lower(),
            endpoint=endpoint.lower(),
            domain=domain.lower(),
            capacity=capacity,
            semaphore=threading.BoundedSemaphore(capacity),
            min_interval_seconds=min_interval,
        )
        if existing:
            created.cooldown_until = existing.cooldown_until
            created.last_error = existing.last_error
            created.last_call_at = existing.last_call_at
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


def _coerce_dt(value) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=None) if parsed.tzinfo is None else parsed.replace(tzinfo=None)
    return None


def _read_provider_cooldown(db, *, provider: str, endpoint: str, domain: str) -> tuple[float, str] | None:
    if db is None:
        return None
    try:
        doc = db["provider_health"].find_one(
            {"provider": provider, "endpoint": endpoint, "domain": domain},
            {"status": 1, "cooldown_until": 1, "updated_at": 1, "last_error_type": 1},
        ) or {}
    except Exception:
        return None
    if str(doc.get("status") or "").lower() != "cooldown":
        return None
    cooldown_until = _coerce_dt(doc.get("cooldown_until"))
    if cooldown_until is None:
        return None
    updated_at = _coerce_dt(doc.get("updated_at"))
    if updated_at is not None and cooldown_until <= updated_at:
        return None
    remaining = (cooldown_until - datetime.now()).total_seconds()
    if remaining <= 0:
        return None
    return remaining, str(doc.get("last_error_type") or "")


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
    preserve_last_error: bool = False,
    inc: dict[str, int] | None = None,
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
        elif status == "running":
            pass
        else:
            update["last_error_at"] = now
            if preserve_last_error:
                update["cooldown_hit_type"] = error[:240]
            else:
                update["last_error_type"] = error[:240]
            if cooldown_until is not None:
                update["cooldown_until"] = cooldown_until
        update_doc = {"$set": update}
        if inc:
            update_doc["$inc"] = inc
        db["provider_health"].update_one(
            {"provider": provider, "endpoint": endpoint, "domain": domain},
            update_doc,
            upsert=True,
        )
    except Exception:
        return


def provider_cooldown_remaining(
    db,
    provider: str,
    endpoint: str,
    *,
    domain: str = "market_data",
) -> float:
    """Return active cooldown seconds for a provider endpoint, or 0."""
    state = _state(provider, endpoint, domain)
    shared = _read_provider_cooldown(db, provider=state.provider, endpoint=endpoint, domain=domain)
    remaining = 0.0
    if shared:
        shared_remaining, shared_error = shared
        state.cooldown_until = max(state.cooldown_until, time.monotonic() + shared_remaining)
        if shared_error:
            state.last_error = shared_error[:240]
        remaining = max(remaining, shared_remaining)
    elif db is not None:
        try:
            doc = db["provider_health"].find_one(
                {"provider": state.provider, "endpoint": endpoint, "domain": domain},
                {"status": 1, "cooldown_until": 1, "updated_at": 1},
            ) or {}
        except Exception:
            doc = {}
        if str(doc.get("status") or "").lower() == "cooldown":
            cooldown_until = _coerce_dt(doc.get("cooldown_until"))
            updated_at = _coerce_dt(doc.get("updated_at"))
            if cooldown_until is not None and (
                cooldown_until <= datetime.now()
                or (updated_at is not None and cooldown_until <= updated_at)
            ):
                state.cooldown_until = 0.0
    local_remaining = state.cooldown_until - time.monotonic()
    return max(0.0, remaining, local_remaining)


def providers_all_cooling_down(
    db,
    providers: Iterable[tuple[str, str]],
    *,
    domain: str = "market_data",
) -> bool:
    """True when every provider endpoint in the set is actively cooling down."""
    rows = list(providers)
    if not rows:
        return False
    return all(provider_cooldown_remaining(db, provider, endpoint, domain=domain) > 0 for provider, endpoint in rows)


def _wait_for_token(state: ProviderState) -> None:
    interval = state.min_interval_seconds
    if interval <= 0:
        return
    while True:
        with _LOCK:
            now = time.monotonic()
            wait_seconds = state.last_call_at + interval - now
            if wait_seconds <= 0:
                state.last_call_at = now
                return
        time.sleep(min(wait_seconds, 1.0))


def provider_call(
    provider: str,
    endpoint: str,
    fn: Callable[[], T],
    *,
    db=None,
    domain: str = "market_data",
) -> T:
    """Run a provider request under per-provider concurrency and cooldown rules."""
    state = _state(provider, endpoint, domain)
    shared = _read_provider_cooldown(db, provider=state.provider, endpoint=endpoint, domain=domain)
    if shared:
        remaining, shared_error = shared
        state.cooldown_until = max(state.cooldown_until, time.monotonic() + remaining)
        if shared_error:
            state.last_error = shared_error[:240]

    now = time.monotonic()
    if state.cooldown_until > now:
        remaining = int(state.cooldown_until - now)
        error = f"provider_cooling_down:{provider}:{remaining}s"
        _write_provider_health(
            db,
            provider=state.provider,
            endpoint=endpoint,
            domain=domain,
            status="cooldown",
            error=error,
            preserve_last_error=True,
            inc={"cooldown_hit_count": 1},
        )
        raise ProviderCoolingDown(error)

    low, high = _jitter_seconds()
    if high > 0:
        time.sleep(random.uniform(low, high))

    started = time.monotonic()
    with state.semaphore:
        _wait_for_token(state)
        _write_provider_health(
            db,
            provider=state.provider,
            endpoint=endpoint,
            domain=domain,
            status="running",
            inc={"attempt_count": 1},
        )
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
                inc={"risk_error_count": 1} if status == "cooldown" else {"degraded_count": 1},
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
            inc={"success_count": 1},
        )
        return result
