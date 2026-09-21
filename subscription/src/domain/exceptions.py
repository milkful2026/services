"""Typed domain exceptions. Every exception carries a stable `error_code`
and the HTTP status handlers map it to — never a raw traceback. Mirrors
wallet's `WalletError` base-class pattern exactly."""

from typing import Any


class SubscriptionError(Exception):
    error_code: str = "SUBSCRIPTION_ERROR"
    http_status: int = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class SubscriptionNotFoundError(SubscriptionError):
    error_code = "SUBSCRIPTION_NOT_FOUND"
    http_status = 404


class InvalidScheduleError(SubscriptionError):
    error_code = "INVALID_SCHEDULE"
    http_status = 400


class InvalidRangeError(SubscriptionError):
    """A pause `from`/`until` that's in the past or where `until < from`."""

    error_code = "INVALID_RANGE"
    http_status = 400


class SubscriptionStoppedError(SubscriptionError):
    """Resume attempted on a `STOPPED` subscription — terminal, no way back."""

    error_code = "SUBSCRIPTION_STOPPED"
    http_status = 409


class DateNotDueError(SubscriptionError):
    """Skip attempted for a date `is_due` doesn't agree is actually due."""

    error_code = "DATE_NOT_DUE"
    http_status = 400


class CutoffPassedError(SubscriptionError):
    """Skip/edit attempted after the affected date's cut-off has passed."""

    error_code = "CUTOFF_PASSED"
    http_status = 409


class ProductNotEligibleError(SubscriptionError):
    """`product_id` doesn't exist, or exists but isn't subscription-eligible."""

    error_code = "PRODUCT_NOT_ELIGIBLE"
    http_status = 422


class CatalogUnavailableError(SubscriptionError):
    """Catalog Service unreachable after retries — `create` fails closed
    per MA-131 FR-1 rather than guessing product eligibility."""

    error_code = "CATALOG_UNAVAILABLE"
    http_status = 503


class ServiceUnavailableError(SubscriptionError):
    """DB unreachable — fail closed (never silently return a stale/empty
    result)."""

    error_code = "SERVICE_UNAVAILABLE"
    http_status = 503
