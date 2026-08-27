"""HTTP client for Catalog Service's existing public `GET /products/{id}`
(services/README.md §3.7 adapter pattern) — calls Catalog, not Inventory,
for stock quantity (see README's Architecture Decisions #1: Inventory has
no such concept at all; this is Catalog's data, per MA-120 §7).

No new Catalog-side endpoint needed: `GET /products/{id}` already exists
(`services/catalog/src/handlers/products_handler.py`) — this adapter just
reads its `availableQuantity` field, which is `null` until Catalog's own
`available_quantity` addition (MA-120 §7) lands. `None` here is a valid,
expected response today, not a transport failure.
"""

import logging

import requests
from requests.exceptions import RequestException

from adapters.retry import call_with_retry
from domain.exceptions import StockCheckUnavailableError

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
        correlation_id: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._correlation_id = correlation_id

    def set_correlation_id(self, correlation_id: str) -> None:
        self._correlation_id = correlation_id

    def get_available_quantity(self, product_id: str) -> int | None:
        url = f"{self._base_url}/products/{product_id}"

        def _attempt() -> int | None:
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
                    quantity = response.json()["data"].get("availableQuantity")
                except (ValueError, KeyError, AttributeError, TypeError) as exc:
                    # A 200 whose body isn't the expected {"data": {...}}
                    # envelope (non-JSON, an error envelope, an HTML error
                    # page from a proxy, `data` as a list) is treated like
                    # any other transport failure — retried, then surfaced
                    # as StockCheckUnavailableError — never propagated raw,
                    # which interfaces.py documents as this adapter's only
                    # failure mode.
                    raise _RetryableCatalogError(
                        f"malformed 200 body from Catalog: {exc}"
                    ) from exc
                return quantity
            if response.status_code == 404:
                # Deliberately not raised as a distinct "product vanished"
                # case here — the caller (cart_service) already knows the
                # product_id it's validating; a 404 from Catalog for a
                # product this cart references is surfaced the same as an
                # unknown quantity (None), not a hard failure of this call.
                return None
            raise _RetryableCatalogError(f"Catalog returned HTTP {response.status_code}")

        def _on_attempt_failure(exc: Exception, attempt: int) -> None:
            logger.error(
                "catalog_client.get_available_quantity request failed",
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
            raise StockCheckUnavailableError(
                "Catalog stock check failed after retries", details={"cause": str(exc)}
            ) from exc
