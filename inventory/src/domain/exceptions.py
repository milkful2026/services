"""Typed domain exceptions. Every exception carries a stable `error_code`
and HTTP status handlers map it to — never a raw traceback."""

from typing import Any


class InventoryError(Exception):
    error_code: str = "INVENTORY_ERROR"
    http_status: int = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class InvalidPincodeError(InventoryError):
    error_code = "INVALID_PINCODE"
    http_status = 400


class ServiceUnavailableError(InventoryError):
    """DB (or other dependency) unreachable — per spec NFR, fail closed:
    the caller must treat this as not-serviceable, not silently succeed."""

    error_code = "SERVICE_UNAVAILABLE"
    http_status = 503
