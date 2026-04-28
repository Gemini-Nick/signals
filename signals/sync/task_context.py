# -*- coding: utf-8 -*-
"""Thread-local sync task configuration.

Postmarket runs task shards in parallel, so task-specific settings cannot rely
only on process-wide os.environ.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
from collections.abc import Iterator

_TASK_ENV: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar("signals_sync_task_env", default={})


def get_task_env(name: str, default: str | None = None) -> str | None:
    values = _TASK_ENV.get({})
    if name in values:
        return values[name]
    return os.getenv(name, default)


@contextlib.contextmanager
def task_env(values: dict[str, str]) -> Iterator[None]:
    current = dict(_TASK_ENV.get({}))
    current.update({key: str(value) for key, value in values.items()})
    token = _TASK_ENV.set(current)
    try:
        yield
    finally:
        _TASK_ENV.reset(token)
