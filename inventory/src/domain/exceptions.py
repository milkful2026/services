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


# --- MA-118 (reserve/commit/release, batch & expiry, read API) ------------


class ProductNotFoundError(InventoryError):
    """FR-1/FR-8: "Catalog has never told Inventory this product exists"
    — distinct from "known but zero stock"."""

    error_code = "PRODUCT_NOT_FOUND"
    http_status = 404


class InsufficientStockError(InventoryError):
    """FR-2's primary overselling-prevention error — `quantity` exceeds
    `available` under the row-level lock. A specific error code, not a
    generic 500/409, per spec."""

    error_code = "INSUFFICIENT_STOCK"
    http_status = 409


class ReservationNotFoundError(InventoryError):
    """FR-3/FR-4 edge case: `commit`/`release` called on a nonexistent
    `order_ref` — distinct from the idempotent no-op case (an existing,
    already-terminal reservation)."""

    error_code = "RESERVATION_NOT_FOUND"
    http_status = 404


class ValidationError(InventoryError):
    error_code = "VALIDATION_ERROR"
    http_status = 400


# --- MA-119 (admin manual adjustment & audit trail) -----------------------


class OnHandFloorViolationError(InventoryError):
    """FR-1's first floor check: the adjustment would drive `on_hand`
    negative. Deliberately a distinct error_code from
    AvailableFloorViolationError (MA-119 §9/§10: "two distinct checks,
    two distinct error codes")."""

    error_code = "ON_HAND_FLOOR_VIOLATION"
    http_status = 400


class AvailableFloorViolationError(InventoryError):
    """FR-1's second, separate floor check: `on_hand` would stay >= 0 but
    `available` (`on_hand - reserved`) would go negative — i.e. the
    adjustment would silently invalidate stock already promised to a
    customer via an active reservation."""

    error_code = "AVAILABLE_FLOOR_VIOLATION"
    http_status = 400


class AdminAuthenticationError(InventoryError):
    """No (or malformed) caller identity reached the handler — mirrors
    user/src/handlers/admin_context.py's own AdminAuthenticationError
    error code/status exactly (same failure mode against the same
    authorizer contract, a different service)."""

    error_code = "UNAUTHENTICATED"
    http_status = 401


class AdminForbiddenError(InventoryError):
    """FR-3: an authenticated caller without the Ops role. MA-119 §5
    Security: "a non-admin authenticated request is rejected (403), not
    silently ignored"."""

    error_code = "FORBIDDEN"
    http_status = 403
