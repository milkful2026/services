"""Typed domain exceptions. Every exception carries a stable `error_code`
and HTTP status handlers map it to — never a raw traceback."""

from typing import Any


class CatalogError(Exception):
    error_code: str = "CATALOG_ERROR"
    http_status: int = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ProductNotFoundError(CatalogError):
    error_code = "PRODUCT_NOT_FOUND"
    http_status = 404


class InvalidRequestError(CatalogError):
    """A malformed query param (unrecognized `sort`, non-numeric `price`
    filter bound) — a 400, not a 500; kept distinct from ProductNotFoundError
    since it means the request itself is wrong, not that it correctly
    referred to something absent."""

    error_code = "INVALID_REQUEST"
    http_status = 400


class ServiceUnavailableError(CatalogError):
    """DB unreachable — fail closed, matching Inventory's own NFR
    convention (never silently return an empty/stale list)."""

    error_code = "SERVICE_UNAVAILABLE"
    http_status = 503


# --- MA-156 (MA-42): admin write API --------------------------------------


class CategoryNotFoundError(CatalogError):
    """FR-1: `POST /products` with an unknown `categoryId` — a distinct
    error_code from `PRODUCT_NOT_FOUND` per spec, since the two are
    different missing entities."""

    error_code = "CATEGORY_NOT_FOUND"
    http_status = 404


class ValidationError(CatalogError):
    """FR-1/FR-2: non-positive `priceB2c`, empty `name`/`unit` — a 400,
    not a 500."""

    error_code = "VALIDATION_ERROR"
    http_status = 400


class AdminAuthenticationError(CatalogError):
    """No (or malformed) caller identity reached the handler — mirrors
    inventory/src/domain/exceptions.py's own AdminAuthenticationError
    exactly (same failure mode against the same authorizer contract, a
    different service)."""

    error_code = "UNAUTHENTICATED"
    http_status = 401


class AdminForbiddenError(CatalogError):
    """FR-6: an authenticated caller without the Ops/SuperAdmin role."""

    error_code = "FORBIDDEN"
    http_status = 403
