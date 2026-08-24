"""Shared retry-with-backoff helper for external adapters
(services/README.md §3.7's mandatory retry+backoff convention for
adapters calling out to another service). Identical copy of
user/src/adapters/retry.py — small enough that duplicating it once is
simpler than a shared package across services with no shared deploy unit.
"""

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def call_with_retry(
    fn: Callable[[], T],
    *,
    max_retries: int,
    backoff_base_seconds: float,
    retryable_exceptions: tuple[type[Exception], ...],
    on_attempt_failure: Callable[[Exception, int], None] | None = None,
) -> T:
    """Calls `fn()`, retrying up to `max_retries` times (exponential
    backoff: `backoff_base_seconds * 2**attempt`) whenever it raises one
    of `retryable_exceptions`. Any other exception propagates immediately,
    uncounted. Re-raises the final attempt's exception once retries are
    exhausted.
    """
    last_exc: Exception
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except retryable_exceptions as exc:
            last_exc = exc
            if on_attempt_failure is not None:
                on_attempt_failure(exc, attempt)
            if attempt < max_retries:
                time.sleep(backoff_base_seconds * (2**attempt))
    raise last_exc
