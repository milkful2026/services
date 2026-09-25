"""HTTP client for Subscription Service's `POST /internal/subscriptions`
(MA-136 FR-11) — starts one cart subscription line on the customer's
behalf.

VPC-only, unauthenticated internal HTTP (same convention as Wallet's
internal routes). Idempotent on `idempotencyKey`, which the checkout
derives per line, so a retried or resumed checkout gets the same
subscription back rather than a second one.

A 4xx is a definite answer about *that line* (e.g. PRODUCT_NOT_ELIGIBLE,
INVALID_SCHEDULE) — `SubscriptionRejectedError(reason)`, never retried.
5xx/transport failures are retried, then `SubscriptionUnavailableError`.
"""

import logging
from datetime import date

import requests
from requests.exceptions import RequestException
from shared.adapters.retry import call_with_retry

from domain.exceptions import SubscriptionRejectedError, SubscriptionUnavailableError

logger = logging.getLogger(__name__)


class _RetryableSubscriptionError(Exception):
    pass


class HttpSubscriptionClient:
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

    def create(
        self,
        *,
        user_id: str,
        product_id: str,
        quantity: int,
        schedule_type: str,
        start_date: date,
        slot_id: str,
        idempotency_key: str,
        correlation_id: str,
    ) -> dict:
        """Returns {"subscriptionId", "nextDeliveryDate"}."""
        url = f"{self._base_url}/internal/subscriptions"
        body = {
            "userId": user_id,
            "productId": product_id,
            "quantity": quantity,
            "schedule": {"type": schedule_type},
            "startDate": start_date.isoformat(),
            "slotId": slot_id,
            "idempotencyKey": idempotency_key,
            "correlationId": correlation_id,
        }

        def _attempt() -> dict:
            try:
                response = requests.post(
                    url,
                    json=body,
                    timeout=self._timeout_seconds,
                    headers={"x-request-id": self._correlation_id},
                )
            except RequestException as exc:
                raise _RetryableSubscriptionError(str(exc)) from exc

            if response.status_code == 200:
                try:
                    data = response.json()["data"]
                    return {
                        "subscriptionId": data["subscriptionId"],
                        "nextDeliveryDate": data.get("nextDeliveryDate"),
                    }
                except (ValueError, KeyError, TypeError) as exc:
                    raise _RetryableSubscriptionError(
                        f"malformed 200 body from Subscription: {exc}"
                    ) from exc
            if 400 <= response.status_code < 500:
                reason = _error_code(response) or f"HTTP_{response.status_code}"
                raise SubscriptionRejectedError(reason)
            raise _RetryableSubscriptionError(
                f"Subscription returned HTTP {response.status_code}"
            )

        def _on_attempt_failure(exc: Exception, attempt: int) -> None:
            logger.error(
                "subscription_client.create request failed",
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
                retryable_exceptions=(_RetryableSubscriptionError,),
                on_attempt_failure=_on_attempt_failure,
            )
        except _RetryableSubscriptionError as exc:
            raise SubscriptionUnavailableError(
                "Subscription create failed after retries", details={"cause": str(exc)}
            ) from exc


def _error_code(response) -> str | None:
    try:
        data = response.json().get("data")
    except (ValueError, AttributeError):
        return None
    return data.get("errorCode") if isinstance(data, dict) else None
