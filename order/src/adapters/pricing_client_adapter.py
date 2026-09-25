"""HTTP client for Pricing & Offer Service's `POST /pricing/quote`
(MA-101/MA-122 FR-1) — mirrors `cart/src/adapters/pricing_client_adapter.py`'s
shape, extended to distinguish Pricing's own typed
`PRODUCT_PRICING_UNKNOWN` (a definite fact: Catalog has no such product)
from every other failure (transient — Order Service's `materialize` must
fail closed on those, not on this one).

No auth on this call — `services/pricing-offer` has no Cognito JWT or
IAM authorizer, matching this codebase's existing unauthenticated-
internal-call precedent (unlike User Service's address-state endpoint —
see `user_client_adapter.py`)."""

import logging

import requests
from requests.exceptions import RequestException
from shared.adapters.retry import call_with_retry

from domain.exceptions import PricingUnavailableError, ProductPricingUnknownError
from domain.models import Quote

logger = logging.getLogger(__name__)


class _RetryablePricingError(Exception):
    pass


class HttpPricingClient:
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

    def quote(self, product_id: str, quantity: int, delivery_state: str) -> Quote:
        return self.quote_items([(product_id, quantity)], delivery_state)

    def quote_items(self, items: list[tuple[str, int]], delivery_state: str) -> Quote:
        """One aggregated ONE_TIME quote for (product_id, quantity) lines —
        MA-132's single subscription delivery, or MA-136's checkout
        one-time lines."""
        url = f"{self._base_url}/pricing/quote"
        # frequency: "ONE_TIME" — each delivery is priced and charged
        # individually, never as a monthly subscription aggregate
        # (MA-132 FR-1).
        body = {
            "items": [
                {"productId": product_id, "quantity": quantity, "frequency": "ONE_TIME"}
                for product_id, quantity in items
            ],
            "deliveryState": delivery_state,
        }

        def _attempt() -> Quote:
            try:
                response = requests.post(
                    url,
                    json=body,
                    timeout=self._timeout_seconds,
                    headers={"x-request-id": self._correlation_id},
                )
            except RequestException as exc:
                raise _RetryablePricingError(str(exc)) from exc

            if response.status_code == 200:
                try:
                    data = response.json()["data"]
                    return Quote(
                        base_price=data["basePrice"],
                        tax_amount=data["taxAmount"],
                        tax_rate=data["taxRate"],
                        delivery_fee=data["deliveryFee"],
                        net_payable=data["netPayable"],
                        monthly_estimate=data.get("monthlyEstimate"),
                        discount_amount=data.get("discountAmount"),
                        applied_offer_id=data.get("appliedOfferId"),
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    raise _RetryablePricingError(
                        f"malformed 200 body from Pricing: {exc}"
                    ) from exc
            if response.status_code == 404:
                try:
                    error_body = response.json()
                except ValueError:
                    error_body = None
                # A malformed 404 (valid JSON but not the expected
                # object shape at either level — a bare list/string/null
                # body, or a non-dict "data" — e.g. from a misbehaving
                # proxy or an API Gateway default error page) must fall
                # through to the generic retryable branch below, not
                # raise an uncaught AttributeError.
                error_data = error_body.get("data") if isinstance(error_body, dict) else None
                error_code = error_data.get("errorCode") if isinstance(error_data, dict) else None
                if error_code == "PRODUCT_PRICING_UNKNOWN":
                    unknown_product = error_data.get("productId") or (
                        items[0][0] if len(items) == 1 else None
                    )
                    # Not retryable and not a transport failure — a
                    # definite fact this attempt reports up as a typed
                    # exception, distinct from _RetryablePricingError, so
                    # materialize() can map it to a terminal PAYMENT_FAILED
                    # instead of leaving the message unacked.
                    raise ProductPricingUnknownError(
                        f"Catalog has no product {unknown_product!r}",
                        details={"productId": unknown_product},
                    )
                raise _RetryablePricingError(f"Pricing returned an unrecognized 404: {error_code}")
            raise _RetryablePricingError(f"Pricing returned HTTP {response.status_code}")

        def _on_attempt_failure(exc: Exception, attempt: int) -> None:
            logger.error(
                "pricing_client.quote request failed",
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
                retryable_exceptions=(_RetryablePricingError,),
                on_attempt_failure=_on_attempt_failure,
            )
        except _RetryablePricingError as exc:
            raise PricingUnavailableError(
                "Pricing quote request failed after retries", details={"cause": str(exc)}
            ) from exc
