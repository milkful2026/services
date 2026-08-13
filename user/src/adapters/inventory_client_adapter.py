"""HTTP client for Inventory's internal serviceability endpoint (spec §8).

Per services/README.md §3.7: the only place allowed to import `requests`
for this concern, with retry/backoff, configurable timeout, and typed
error mapping. Reaches Inventory by URL (env var) — whether that URL is
actually network-reachable from this service's Lambda is a separate,
flagged infrastructure gap (see README) since each service currently
provisions its own dedicated VPC.
"""

import logging

import requests
from requests.exceptions import RequestException

from adapters.retry import call_with_retry
from domain.exceptions import ExternalServiceUnavailableError, ValidationError

logger = logging.getLogger(__name__)


class _RetryableInventoryError(Exception):
    pass


class HttpInventoryClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
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

    def check_serviceability(self, pincode: str, lat: float, lng: float) -> bool:
        url = f"{self._base_url}/v1/internal/serviceability/check"
        params = {"pincode": pincode, "lat": lat, "lng": lng}

        def _attempt() -> bool:
            try:
                response = requests.get(
                    url,
                    params=params,
                    timeout=self._timeout_seconds,
                    headers={"x-correlation-id": self._correlation_id},
                )
            except RequestException as exc:
                raise _RetryableInventoryError(str(exc)) from exc

            if response.status_code == 200:
                return bool(response.json()["data"]["serviceable"])
            if response.status_code == 400:
                raise ValidationError(
                    "Inventory rejected pincode/coordinates",
                    details=response.json().get("data", {}),
                )
            raise _RetryableInventoryError(f"Inventory returned HTTP {response.status_code}")

        def _on_attempt_failure(exc: Exception, attempt: int) -> None:
            logger.error(
                "inventory_client.check_serviceability request failed",
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
                retryable_exceptions=(_RetryableInventoryError,),
                on_attempt_failure=_on_attempt_failure,
            )
        except _RetryableInventoryError as exc:
            raise ExternalServiceUnavailableError(
                "Inventory serviceability check failed after retries", details={"cause": str(exc)}
            ) from exc
