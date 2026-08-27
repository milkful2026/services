"""HTTP client for Catalog Service's `GET /products/{id}` (MA-116 FR-2,
already live). Per services/README.md §3.7: the only place allowed to
import `requests` for this concern, with retry/backoff and typed error
mapping — mirrors user/src/adapters/inventory_client_adapter.py's own
shape exactly.

Synchronous, not event-driven — see pricing_service.py's module docstring
for why this build calls Catalog directly instead of consuming a
`CatalogUpdated` event into a local read model (that pipeline, and the
Catalog Service schema changes it needs, don't exist in this build).
"""

import logging

import requests
from requests.exceptions import RequestException

from adapters.retry import call_with_retry
from domain.exceptions import (
    CatalogIntegrationError,
    ProductPricingUnknownError,
    ServiceUnavailableError,
)

logger = logging.getLogger(__name__)


class _RetryableCatalogError(Exception):
    pass


class HttpCatalogClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        max_retries: int = 2,
        backoff_base_seconds: float = 0.2,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds

    def get_price(self, product_id: str, correlation_id: str = "") -> float:
        url = f"{self._base_url}/products/{product_id}"
        headers = {"x-correlation-id": correlation_id}

        def _attempt() -> float:
            try:
                response = requests.get(url, timeout=self._timeout_seconds, headers=headers)
            except RequestException as exc:
                raise _RetryableCatalogError(str(exc)) from exc

            if response.status_code == 200:
                try:
                    return float(response.json()["data"]["price"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise CatalogIntegrationError(
                        "Catalog Service returned a 200 with no usable price",
                        details={"productId": product_id},
                    ) from exc
            if response.status_code == 404:
                raise ProductPricingUnknownError(
                    f"No such product: {product_id}", details={"productId": product_id}
                )
            if response.status_code < 500:
                # A non-404 4xx means Catalog rejected this exact request —
                # retrying it unchanged won't produce a different answer.
                raise CatalogIntegrationError(
                    f"Catalog Service rejected the request: HTTP {response.status_code}",
                    details={"productId": product_id, "status": response.status_code},
                )
            raise _RetryableCatalogError(f"Catalog Service returned HTTP {response.status_code}")

        def _on_attempt_failure(exc: Exception, attempt: int) -> None:
            logger.error(
                "catalog_client.get_price request failed",
                extra={
                    "correlationId": correlation_id,
                    "productId": product_id,
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
            raise ServiceUnavailableError(
                "Catalog Service unreachable", details={"cause": str(exc)}
            ) from exc
