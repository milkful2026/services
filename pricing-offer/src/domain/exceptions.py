"""Typed domain exceptions. Every exception carries a stable `error_code`
and HTTP status handlers map it to — never a raw traceback. Matches
catalog's own domain/exceptions.py convention exactly."""

from typing import Any


class PricingError(Exception):
    error_code: str = "PRICING_ERROR"
    http_status: int = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ProductPricingUnknownError(PricingError):
    """MA-122 §9's `PRODUCT_PRICING_UNKNOWN` — raised when Catalog Service
    has no such product at all (a 404 from `GET /products/{id}`). This
    build has no separate `PRODUCT_DISCONTINUED` case (that distinction
    needs an `active`/deleted concept on Catalog's own product model,
    which doesn't exist — see README's "Scope" section); every
    Catalog-side 404 maps here."""

    error_code = "PRODUCT_PRICING_UNKNOWN"
    http_status = 404


class InvalidRequestError(PricingError):
    """A malformed request — empty `items`, a non-positive `quantity`,
    an unrecognized `frequency`, or a missing `deliveryState` (still
    required per MA-122 FR-1's contract even though this build's tax
    calculation doesn't use it — see README)."""

    error_code = "INVALID_REQUEST"
    http_status = 400


class ServiceUnavailableError(PricingError):
    """Catalog Service unreachable after retries — fail closed, matching
    catalog's own ServiceUnavailableError convention (never silently
    return a zero/garbage price)."""

    error_code = "SERVICE_UNAVAILABLE"
    http_status = 503
