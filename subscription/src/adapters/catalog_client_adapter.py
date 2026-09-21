"""HTTP client for Catalog Service's `GET /products/{id}` — MA-131 FR-1's
product-existence + `subscriptionEligible` check at create time.

Fails closed, no fallback (mirrors `cart/src/adapters/catalog_client_adapter.py`'s
shape, not `wallet_limits_client.py`'s cache-and-fall-back one) — MA-131
FR-1 requires "Catalog unavailable -> create fails closed", never a
guessed eligibility."""

import logging

import requests
from requests.exceptions import RequestException
from shared.adapters.retry import call_with_retry

from domain.exceptions import CatalogUnavailableError

logger = logging.getLogger(__name__)


class _RetryableCatalogError(Exception):
    pass


class HttpCatalogClient:
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

    def get_product(self, product_id: str) -> dict | None:
        url = f"{self._base_url}/products/{product_id}"

        def _attempt() -> dict | None:
            try:
                response = requests.get(
                    url,
                    timeout=self._timeout_seconds,
                    headers={"x-correlation-id": self._correlation_id},
                )
            except RequestException as exc:
                raise _RetryableCatalogError(str(exc)) from exc

            if response.status_code == 200:
                try:
                    return response.json()["data"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise _RetryableCatalogError(
                        f"malformed 200 body from Catalog: {exc}"
                    ) from exc
            if response.status_code == 404:
                return None
            raise _RetryableCatalogError(f"Catalog returned HTTP {response.status_code}")

        def _on_attempt_failure(exc: Exception, attempt: int) -> None:
            logger.error(
                "catalog_client.get_product request failed",
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
                retryable_exceptions=(_RetryableCatalogError,),
                on_attempt_failure=_on_attempt_failure,
            )
        except _RetryableCatalogError as exc:
            raise CatalogUnavailableError(
                "Catalog product check failed after retries", details={"cause": str(exc)}
            ) from exc
