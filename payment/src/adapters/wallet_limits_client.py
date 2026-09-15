"""HTTP client for Wallet Service's `GET /wallet/internal/limits`
(MA-127 FR-3, services/README.md §3.7 adapter pattern).

No real IAM/SigV4 signing yet — matches this codebase's existing
unauthenticated-internal-call precedent (`cart/src/adapters/
pricing_client_adapter.py`, `catalog_client_adapter.py`); Wallet Service's
own internal endpoint has no authorizer wired in its current build
either. Swapping in a real SigV4 signer later is a change inside this
adapter only — the port (`WalletLimitsPort`) and every call site are
unaffected.

Cached in-process for 5 minutes; on any failure, falls back to the
configured default bounds (never blocks a recharge attempt on this
service being briefly unavailable) and logs `limits_fallback`.
"""

import logging
import time

import requests
from requests.exceptions import RequestException

from adapters.retry import call_with_retry

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 300.0


class _RetryableLimitsError(Exception):
    pass


class HttpWalletLimitsClient:
    def __init__(
        self,
        base_url: str,
        fallback_min_paise: int,
        fallback_max_paise: int,
        timeout_seconds: float = 3.0,
        max_retries: int = 1,
        backoff_base_seconds: float = 0.2,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else ""
        self._fallback = (fallback_min_paise, fallback_max_paise)
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._cached: tuple[int, int] | None = None
        self._cached_at: float = 0.0

    def get_limits(self) -> tuple[int, int]:
        now = time.monotonic()
        if self._cached is not None and (now - self._cached_at) < _CACHE_TTL_SECONDS:
            return self._cached
        if not self._base_url:
            logger.warning("wallet_limits_client: no base URL configured, using fallback")
            return self._fallback

        def _attempt() -> tuple[int, int]:
            try:
                resp = requests.get(
                    f"{self._base_url}/wallet/internal/limits", timeout=self._timeout_seconds
                )
                resp.raise_for_status()
            except RequestException as exc:
                raise _RetryableLimitsError(str(exc)) from exc
            body = resp.json()
            data = body.get("data", body)
            return int(data["rechargeMinPaise"]), int(data["rechargeMaxPaise"])

        try:
            limits = call_with_retry(
                _attempt,
                max_retries=self._max_retries,
                backoff_base_seconds=self._backoff_base_seconds,
                retryable_exceptions=(_RetryableLimitsError,),
            )
        except _RetryableLimitsError as exc:
            logger.warning("limits_fallback", extra={"error": str(exc)})
            return self._fallback

        self._cached, self._cached_at = limits, now
        return limits
