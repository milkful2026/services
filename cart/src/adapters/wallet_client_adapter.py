"""Client for Wallet Service's service-to-service balance read,
`GET /wallet/internal/balance?userId=` (MA-130 FR-3) — the FR-6 wallet
gate for subscription line items.

Replaces the original always-unavailable stub written before Wallet
Service existed (MA-135): with subscriptions going through the cart
(MA-34), the gate has to answer for real. Unauthenticated internal HTTP
inside the VPC, the same convention Order Service's own wallet adapter
uses for this service's sibling endpoints.

Returns **paise** (Wallet's own unit). An empty base URL still fails
closed with `WalletCheckUnavailableError`, exactly like the stub did, so
an environment that hasn't configured Wallet can't silently skip the
gate.
"""

import logging

import requests
from requests.exceptions import RequestException

from adapters.retry import call_with_retry
from domain.exceptions import WalletCheckUnavailableError

logger = logging.getLogger(__name__)


class _RetryableWalletError(Exception):
    pass


class HttpWalletClient:
    def __init__(
        self,
        base_url: str = "",
        timeout_seconds: float = 3.0,
        max_retries: int = 2,
        backoff_base_seconds: float = 0.2,
        correlation_id: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._correlation_id = correlation_id

    def set_correlation_id(self, correlation_id: str) -> None:
        self._correlation_id = correlation_id

    def get_balance(self, cognito_sub: str) -> int:
        if not self._base_url:
            raise WalletCheckUnavailableError(
                "Wallet Service base URL is not configured",
                details={"cognitoSub": cognito_sub},
            )
        url = f"{self._base_url}/wallet/internal/balance"

        def _attempt() -> int:
            try:
                response = requests.get(
                    url,
                    params={"userId": cognito_sub},
                    timeout=self._timeout_seconds,
                    headers={"x-request-id": self._correlation_id},
                )
            except RequestException as exc:
                raise _RetryableWalletError(str(exc)) from exc
            if response.status_code != 200:
                raise _RetryableWalletError(f"Wallet returned HTTP {response.status_code}")
            try:
                return int(response.json()["data"]["balancePaise"])
            except (ValueError, KeyError, TypeError) as exc:
                raise _RetryableWalletError(f"malformed 200 body from Wallet: {exc}") from exc

        def _on_attempt_failure(exc: Exception, attempt: int) -> None:
            logger.error(
                "wallet_client.get_balance request failed",
                extra={
                    "correlationId": self._correlation_id,
                    "attempt": attempt,
                    "error": str(exc),
                },
            )

        try:
            return call_with_retry(
                _attempt,
                max_retries=self._max_retries,
                backoff_base_seconds=self._backoff_base_seconds,
                retryable_exceptions=(_RetryableWalletError,),
                on_attempt_failure=_on_attempt_failure,
            )
        except _RetryableWalletError as exc:
            raise WalletCheckUnavailableError(
                "Wallet balance check failed after retries", details={"cause": str(exc)}
            ) from exc
