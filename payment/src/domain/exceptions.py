"""Typed domain exceptions. Every exception carries a stable `error_code`
and the HTTP status handlers map it to — never a raw traceback."""

from typing import Any


class PaymentError(Exception):
    error_code: str = "PAYMENT_ERROR"
    http_status: int = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class PaymentNotFoundError(PaymentError):
    error_code = "PAYMENT_NOT_FOUND"
    http_status = 404


class UnsupportedPurposeError(PaymentError):
    error_code = "UNSUPPORTED_PURPOSE"
    http_status = 400


class AmountOutOfRangeError(PaymentError):
    error_code = "AMOUNT_OUT_OF_RANGE"
    http_status = 422


class IdempotencyKeyReusedError(PaymentError):
    """Same (user_id, idempotency_key) but a different amount/purpose/
    currency than the stored row — the caller must not get an order for
    an amount they didn't just ask for (PR #16 round-2 finding #8)."""

    error_code = "IDEMPOTENCY_KEY_REUSED"
    http_status = 409


class SignatureInvalidError(PaymentError):
    error_code = "SIGNATURE_INVALID"
    http_status = 400


class OrderMismatchError(PaymentError):
    error_code = "ORDER_MISMATCH"
    http_status = 409


class GatewayUnavailableError(PaymentError):
    error_code = "GATEWAY_UNAVAILABLE"
    http_status = 503


class ServiceUnavailableError(PaymentError):
    """DB unreachable — fail closed."""

    error_code = "SERVICE_UNAVAILABLE"
    http_status = 503
