"""HTTP client for Cart Service's internal checkout routes (MA-135
FR-3/FR-4): read a user's cart, and remove the checked-out lines.

**SigV4-signed** — both routes sit behind API Gateway's AWS_IAM
authorizer, because their `userId` path parameter is only trustworthy
once API Gateway has verified the caller is this service's own execution
role (same reasoning as `user_client_adapter.py`). The signed body bytes
are exactly the bytes sent.

A 409 on remove is a definite fact (the cart moved on) — raised as
`CartVersionConflictError` so the checkout can re-read and retry; every
other failure after retries is `CartUnavailableError` (transient).
"""

import json
import logging
from dataclasses import dataclass

import requests
from requests.exceptions import RequestException
from shared.adapters.retry import call_with_retry
from shared.adapters.sigv4 import sign_request

from domain.exceptions import CartUnavailableError, CartVersionConflictError

logger = logging.getLogger(__name__)


@dataclass
class CartSnapshot:
    items: list[dict]  # Cart's own wire shape: id, productId, quantity, frequency, ...
    cart_version: int


class _RetryableCartError(Exception):
    pass


class HttpCartClient:
    def __init__(
        self,
        base_url: str,
        region_name: str,
        timeout_seconds: float = 3.0,
        max_retries: int = 2,
        backoff_base_seconds: float = 0.2,
        correlation_id: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._region_name = region_name
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._correlation_id = correlation_id

    def get_cart(self, user_id: str) -> CartSnapshot:
        url = f"{self._base_url}/cart/internal/users/{user_id}"
        return self._call("GET", url, None, "get_cart")

    def remove_items(
        self, user_id: str, item_ids: list[str], if_version: int, checkout_id: str
    ) -> CartSnapshot:
        url = f"{self._base_url}/cart/internal/users/{user_id}/remove-items"
        body = {
            "itemIds": item_ids,
            "ifVersion": if_version,
            "reason": "CHECKOUT",
            "checkoutId": checkout_id,
        }
        return self._call("POST", url, body, "remove_items")

    def _call(self, method: str, url: str, body: dict | None, op: str) -> CartSnapshot:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        base_headers = {"Content-Type": "application/json"} if payload is not None else {}

        def _attempt() -> CartSnapshot:
            try:
                auth_headers = sign_request(
                    method, url, self._region_name, body=payload, headers=base_headers
                )
            except Exception as exc:
                raise _RetryableCartError(f"failed to sign request: {exc}") from exc
            try:
                response = requests.request(
                    method,
                    url,
                    data=payload,
                    timeout=self._timeout_seconds,
                    headers={
                        **base_headers,
                        **auth_headers,
                        "x-request-id": self._correlation_id,
                    },
                )
            except RequestException as exc:
                raise _RetryableCartError(str(exc)) from exc

            if response.status_code == 200:
                try:
                    data = response.json()["data"]
                    return CartSnapshot(
                        items=list(data["items"]), cart_version=int(data["cartVersion"])
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    raise _RetryableCartError(f"malformed 200 body from Cart: {exc}") from exc
            if response.status_code == 409:
                # Not retryable — the caller decides (re-read and retry once).
                raise CartVersionConflictError("Cart version moved on")
            raise _RetryableCartError(f"Cart returned HTTP {response.status_code}")

        def _on_attempt_failure(exc: Exception, attempt: int) -> None:
            logger.error(
                f"cart_client.{op} request failed",
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
                retryable_exceptions=(_RetryableCartError,),
                on_attempt_failure=_on_attempt_failure,
            )
        except _RetryableCartError as exc:
            raise CartUnavailableError(
                f"Cart {op} failed after retries", details={"cause": str(exc)}
            ) from exc
