"""HTTP client for Wallet Service's `POST /wallet/internal/debit`
(MA-130 FR-1) — the order-creation critical path's payment step.

No auth on this call (plain unauthenticated internal HTTP), matching
this service's general pattern (unlike User Service's SigV4-signed
address-state endpoint — see `user_client_adapter.py`).

`DEBITED` / `INSUFFICIENT_BALANCE` / `WALLET_NOT_ACTIVE` are all 200
responses per MA-130's own contract-shape fix — this adapter must never
treat any of the three as an HTTP error. A `503 WALLET_PROVISIONING_PENDING`
(MA-130's own review-added split for "no wallet row yet") is retried like
any other 5xx, and if still failing after retries surfaces as
`WalletUnavailableError` — the same transient/fail-closed posture as
`AddressLookupUnavailableError`/`PricingUnavailableError`, so a
subscription's first order right after registration retries via SQS
redelivery instead of permanently failing."""

import logging

import requests
from requests.exceptions import RequestException
from shared.adapters.retry import call_with_retry

from domain.exceptions import WalletUnavailableError
from domain.models import DebitResult

logger = logging.getLogger(__name__)


class _RetryableWalletError(Exception):
    pass


class HttpWalletClient:
    def __init__(
        self,
        base_url: str,
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

    def debit(
        self, user_id: str, order_id: str, amount_paise: int, correlation_id: str
    ) -> DebitResult:
        url = f"{self._base_url}/wallet/internal/debit"
        body = {
            "userId": user_id,
            "orderId": order_id,
            "amountPaise": amount_paise,
            "correlationId": correlation_id,
        }

        def _attempt() -> DebitResult:
            try:
                response = requests.post(
                    url,
                    json=body,
                    timeout=self._timeout_seconds,
                    headers={"x-request-id": self._correlation_id},
                )
            except RequestException as exc:
                raise _RetryableWalletError(str(exc)) from exc

            if response.status_code == 200:
                try:
                    data = response.json()["data"]
                    return DebitResult(
                        status=data["status"], balance_after_paise=data.get("balanceAfterPaise")
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    raise _RetryableWalletError(
                        f"malformed 200 body from Wallet: {exc}"
                    ) from exc
            # 503 WALLET_PROVISIONING_PENDING included — retried like any
            # other 5xx, never treated as one of the three typed outcomes.
            raise _RetryableWalletError(f"Wallet returned HTTP {response.status_code}")

        def _on_attempt_failure(exc: Exception, attempt: int) -> None:
            logger.error(
                "wallet_client.debit request failed",
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
            raise WalletUnavailableError(
                "Wallet debit request failed after retries", details={"cause": str(exc)}
            ) from exc
