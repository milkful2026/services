"""Typed domain exceptions. Every exception carries a stable `error_code`
and the HTTP status handlers map it to — never a raw traceback. Mirrors
wallet's `WalletError` base-class pattern exactly.

Two different exception classes intentionally distinguish "the upstream
service is unreachable" (transient — `materialize` must fail closed,
leave the SQS message unacked, and let redelivery retry once the
dependency recovers) from a *definite* answer like "no address on file"
or "product no longer exists" (not transient — retrying changes
nothing, so `materialize` records a terminal `PAYMENT_FAILED` order and
acks the message). Conflating the two would either retry forever on a
fact that will never change, or permanently fail an order because of a
momentary outage."""

from typing import Any


class OrderError(Exception):
    error_code: str = "ORDER_ERROR"
    http_status: int = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class OrderNotFoundError(OrderError):
    error_code = "ORDER_NOT_FOUND"
    http_status = 404


class InvalidCursorError(OrderError):
    error_code = "INVALID_CURSOR"
    http_status = 400


class ServiceUnavailableError(OrderError):
    """DB unreachable — fail closed (never silently return a stale/empty
    result)."""

    error_code = "SERVICE_UNAVAILABLE"
    http_status = 503


class AddressLookupUnavailableError(OrderError):
    """User Service's address-state endpoint unreachable after retries —
    transient. `materialize` must NOT create an order for this attempt;
    the SQS message stays unacked so redelivery retries once User
    Service recovers."""

    error_code = "ADDRESS_LOOKUP_UNAVAILABLE"
    http_status = 503


class PricingUnavailableError(OrderError):
    """Pricing Service unreachable after retries, or answered with
    something this adapter can't use — transient, same posture as
    `AddressLookupUnavailableError`."""

    error_code = "PRICING_UNAVAILABLE"
    http_status = 503


class ProductPricingUnknownError(OrderError):
    """Pricing Service's own `PRODUCT_PRICING_UNKNOWN` (Catalog has no
    such product) — a definite fact, not transient. `materialize` records
    a terminal `PAYMENT_FAILED` order (`reason: PRODUCT_UNAVAILABLE`) and
    acks the message; the subscription itself is not auto-stopped."""

    error_code = "PRODUCT_PRICING_UNKNOWN"
    http_status = 404


class WalletUnavailableError(OrderError):
    """Wallet Service's debit endpoint unreachable after retries (or
    still returning 503 WALLET_PROVISIONING_PENDING after retries) —
    transient. The order stays `CREATED`; the SQS message is left
    unacked so redelivery resumes at the debit step (Wallet's own
    `orderId` idempotency makes a repeated debit call safe)."""

    error_code = "WALLET_UNAVAILABLE"
    http_status = 503
