"""The only file that talks to Razorpay — SDK/HTTP knowledge is isolated
here (services/README.md §3.4's dependency-direction rule: domain never
imports an external SDK directly).

Signature formulae (Razorpay's documented contract):
  - client callback signature = HMAC-SHA256(order_id + "|" + payment_id, key_secret)
  - webhook signature         = HMAC-SHA256(raw_request_body, webhook_secret)
Both verified here with `hmac.compare_digest` directly (not the SDK's
`utility.verify_*`, which do the same HMAC under the hood) so this
module's tests can exercise known vectors without any network access.
"""

import hashlib
import hmac
import logging

import razorpay

from adapters.retry import call_with_retry
from domain.exceptions import GatewayUnavailableError

logger = logging.getLogger(__name__)


class _RetryableGatewayError(Exception):
    pass


class RazorpayGateway:
    def __init__(
        self,
        key_id: str,
        key_secret: str,
        webhook_secret: str,
        max_retries: int = 2,
        backoff_base_seconds: float = 0.2,
    ) -> None:
        self._key_secret = key_secret
        self._webhook_secret = webhook_secret
        self._client = razorpay.Client(auth=(key_id, key_secret))
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds

    def orders_create(self, *, amount_paise: int, receipt: str, notes: dict) -> str:
        def _attempt() -> str:
            try:
                order = self._client.order.create(
                    {
                        "amount": amount_paise,
                        "currency": "INR",
                        "receipt": receipt,
                        "notes": notes,
                    }
                )
            except Exception as exc:  # noqa: BLE001 — the SDK raises its own error types
                raise _RetryableGatewayError(str(exc)) from exc
            return order["id"]

        def _on_failure(exc: Exception, attempt: int) -> None:
            logger.error(
                "razorpay_gateway.orders_create failed",
                extra={"attempt": attempt, "error": str(exc)},
            )

        try:
            return call_with_retry(
                _attempt,
                max_retries=self._max_retries,
                backoff_base_seconds=self._backoff_base_seconds,
                retryable_exceptions=(_RetryableGatewayError,),
                on_attempt_failure=_on_failure,
            )
        except _RetryableGatewayError as exc:
            raise GatewayUnavailableError(
                "Razorpay order creation failed after retries", details={"cause": str(exc)}
            ) from exc

    def verify_webhook_signature(self, raw_body: bytes, signature_header: str) -> bool:
        if not signature_header or not self._webhook_secret:
            return False
        expected = hmac.new(
            self._webhook_secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature_header)

    def verify_client_signature(
        self, razorpay_order_id: str, razorpay_payment_id: str, signature: str
    ) -> bool:
        if not signature or not self._key_secret:
            return False
        message = f"{razorpay_order_id}|{razorpay_payment_id}".encode()
        expected = hmac.new(self._key_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def fetch_order_payments(self, razorpay_order_id: str) -> list[dict]:
        def _attempt() -> list[dict]:
            try:
                result = self._client.order.payments(razorpay_order_id)
            except Exception as exc:  # noqa: BLE001
                raise _RetryableGatewayError(str(exc)) from exc
            return result.get("items", [])

        try:
            return call_with_retry(
                _attempt,
                max_retries=self._max_retries,
                backoff_base_seconds=self._backoff_base_seconds,
                retryable_exceptions=(_RetryableGatewayError,),
            )
        except _RetryableGatewayError as exc:
            raise GatewayUnavailableError(
                "Razorpay order-payments lookup failed after retries", details={"cause": str(exc)}
            ) from exc
