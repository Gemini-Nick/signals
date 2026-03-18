# -*- coding: utf-8 -*-
"""
重试装饰器 — 借鉴 Akshare-Sync 的 tenacity 策略

10 次重试，5-60 秒指数退避，适用于网络 I/O 密集的数据同步。
"""
import logging
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
    retry_if_exception_type,
)

logger = logging.getLogger("signals.sync")

# ── 可重试的异常类型 ────────────────────────────────────
_RETRYABLE = (
    ConnectionError,
    ConnectionResetError,
    ConnectionAbortedError,
    TimeoutError,
    OSError,
)

try:
    from requests.exceptions import (
        ConnectionError as ReqConnErr,
        Timeout as ReqTimeout,
        ChunkedEncodingError,
    )
    _RETRYABLE = _RETRYABLE + (ReqConnErr, ReqTimeout, ChunkedEncodingError)
except ImportError:
    pass

try:
    from urllib3.exceptions import ProtocolError, ReadTimeoutError
    _RETRYABLE = _RETRYABLE + (ProtocolError, ReadTimeoutError)
except ImportError:
    pass


def sync_retry(fn=None, *, max_attempts: int = 10,
               min_wait: int = 5, max_wait: int = 60):
    """
    同步重试装饰器。

    :param max_attempts: 最大重试次数（默认 10）
    :param min_wait: 最小等待秒数（默认 5）
    :param max_wait: 最大等待秒数（默认 60）

    用法:
        @sync_retry
        def sync_stock_daily(db, proxy_url=None):
            ...

        @sync_retry(max_attempts=5, min_wait=2)
        def sync_quick(db):
            ...
    """
    decorator = retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        retry=retry_if_exception_type(_RETRYABLE),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    if fn is not None:
        return decorator(fn)
    return decorator
