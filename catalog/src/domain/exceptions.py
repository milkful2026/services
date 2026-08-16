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


class ServiceUnavailableError(CatalogError):
    """DB unreachable — fail closed, matching Inventory's own NFR
    convention (never silently return an empty/stale list)."""

    error_code = "SERVICE_UNAVAILABLE"
    http_status = 503
