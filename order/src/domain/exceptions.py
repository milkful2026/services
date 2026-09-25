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


# --- MA-136: cart checkout ---------------------------------------------------
# Each maps 1:1 to an outcome the app handles (MA-137 FR-7). Pre-start
# rejections (400/402/409/422) persist nothing, so retrying with the same
# Idempotency-Key simply re-evaluates.


class ValidationError(OrderError):
    error_code = "VALIDATION_ERROR"
    http_status = 400


class CartEmptyError(OrderError):
    error_code = "CART_EMPTY"
    http_status = 409


class CartChangedError(OrderError):
    """The cart's version moved since the customer reviewed it."""

    error_code = "CART_CHANGED"
    http_status = 409


class CheckoutInProgressError(OrderError):
    """Another checkout for this user is still IN_PROGRESS (a different
    Idempotency-Key) — at most one live checkout per user."""

    error_code = "CHECKOUT_IN_PROGRESS"
    http_status = 409


class PriceChangedError(OrderError):
    error_code = "PRICE_CHANGED"
    http_status = 409


class LineInvalidError(OrderError):
    """`details.lines`: [{lineId, reason}] with reason SLOT_MISSING |
    START_DATE_PAST | PRODUCT_UNAVAILABLE."""

    error_code = "LINE_INVALID"
    http_status = 422


class DeliveryAddressUnknownError(OrderError):
    error_code = "DELIVERY_ADDRESS_UNKNOWN"
    http_status = 422


class InsufficientBalanceError(OrderError):
    """`details`: balancePaise, requiredPaise, shortfallPaise."""

    error_code = "INSUFFICIENT_BALANCE"
    http_status = 402


class WalletNotActiveError(OrderError):
    error_code = "WALLET_NOT_ACTIVE"
    http_status = 403


class DependencyUnavailableError(OrderError):
    """A dependency was unreachable *before* anything was persisted — safe
    to retry with the same key."""

    error_code = "DEPENDENCY_UNAVAILABLE"
    http_status = 503


class CheckoutIncompleteError(OrderError):
    """A dependency failed *after* the checkout started (possibly after the
    charge). The checkout stays IN_PROGRESS; retrying with the same key
    resumes it — never a second charge."""

    error_code = "CHECKOUT_INCOMPLETE"
    http_status = 503


class StoredCheckoutFailureError(OrderError):
    """Replay of a checkout that already ended PAYMENT_FAILED: re-raises
    the exact stored outcome, code and status included."""

    def __init__(
        self, error_code: str, http_status: int, message: str, details: dict | None = None
    ) -> None:
        super().__init__(message, details)
        self.error_code = error_code
        self.http_status = http_status


# Adapter-level failures the checkout maps onto the outcomes above.


class CartUnavailableError(OrderError):
    error_code = "CART_UNAVAILABLE"
    http_status = 503


class CartVersionConflictError(OrderError):
    error_code = "CART_VERSION_CONFLICT"
    http_status = 409


class SubscriptionUnavailableError(OrderError):
    error_code = "SUBSCRIPTION_UNAVAILABLE"
    http_status = 503


class SubscriptionRejectedError(OrderError):
    """Subscription Service gave a definite 4xx for one line (e.g.
    PRODUCT_NOT_ELIGIBLE, INVALID_SCHEDULE) — `reason` is its errorCode."""

    error_code = "SUBSCRIPTION_REJECTED"
    http_status = 422

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


class WalletBalanceUnavailableError(OrderError):
    error_code = "WALLET_UNAVAILABLE"
    http_status = 503
