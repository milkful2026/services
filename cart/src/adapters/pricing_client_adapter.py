"""HTTP client for Pricing & Offer Service's `POST /pricing/quote`
(MA-101/MA-122 FR-1, services/README.md §3.7 adapter pattern).

No auth on this call — `services/pricing-offer` has no Cognito JWT or
IAM authorizer of any kind in its current (scoped-down, no-infra-yet)
build, unlike User Service's internal endpoint (see
`user_client_adapter.py`). Matches this codebase's existing
unauthenticated-internal-call precedent (`catalog_client_adapter.py`).
"""

import logging

import requests
from requests.exceptions import RequestException

from adapters.retry import call_with_retry
from domain.exceptions import PricingUnavailableError
from domain.models import Quote

logger = logging.getLogger(__name__)


class _RetryablePricingError(Exception):
    pass


class HttpPricingClient:
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

    def quote(
        self,
        items: list[dict],
        delivery_state: str,
        offer_code: str | None = None,
    ) -> Quote:
        url = f"{self._base_url}/pricing/quote"
        # Field names match MA-101/MA-122's merged contract exactly
        # (quote_handler.py's QuoteRequestDto) — camelCase over the wire,
        # same as every other cross-service JSON body in this codebase.
        body = {
            "items": [
                {
                    "productId": item["product_id"],
                    "quantity": item["quantity"],
                    "frequency": item["frequency"],
                }
                for item in items
            ],
            "deliveryState": delivery_state,
            "offerCode": offer_code,
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
                    # Same reasoning as catalog_client_adapter.py: a 200
                    # whose body doesn't match the documented envelope is
                    # a transport-layer failure from this adapter's own
                    # contract's point of view, not a value to propagate
                    # raw — retried, then surfaced as
                    # PricingUnavailableError.
                    raise _RetryablePricingError(
                        f"malformed 200 body from Pricing: {exc}"
                    ) from exc
            if response.status_code == 400:
                # Pricing's own InvalidRequestError (e.g. empty items,
                # missing deliveryState, non-positive quantity) — a bug in
                # how this adapter built the request, not a transient
                # failure. Not retried; surfaces the same as any other
                # Pricing failure since callers only distinguish
                # "available" from "unavailable" here (MA-121 §9 has no
                # separate "bad request to Pricing" error code).
                try:
                    details = response.json().get("data", {})
                except ValueError:
                    # A non-JSON 400 body (API Gateway request validation,
                    # a proxy, a WAF) still means the same thing to callers
                    # — Pricing is unusable for this request — but must not
                    # raise an unmapped JSONDecodeError past call_with_retry.
                    details = {}
                raise PricingUnavailableError(
                    "Pricing rejected the quote request",
                    details=details,
                )
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
