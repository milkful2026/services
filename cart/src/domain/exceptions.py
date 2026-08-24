"""Typed domain exceptions. Every exception carries a stable `error_code`
and HTTP status handlers map it to — never a raw traceback. One per
distinct failure mode MA-121 §9 enumerates (services/README.md §5c/§9)."""

from typing import Any


class CartServiceError(Exception):
    error_code: str = "CART_SERVICE_ERROR"
    http_status: int = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(CartServiceError):
    error_code = "VALIDATION_ERROR"
    http_status = 400


class LineItemNotFoundError(CartServiceError):
    """FR-4 — a missing or foreign line-item id. Never distinguishes the
    two in the response (same-shaped 404 either way, per FR-4's own
    "doesn't leak whether an ID belongs to a different caller" rule)."""

    error_code = "NOT_FOUND"
    http_status = 404


class CartVersionMismatchError(CartServiceError):
    """FR-3 — `PUT /cart` sent with a stale `ifVersion` (another device
    wrote first)."""

    error_code = "CART_VERSION_MISMATCH"
    http_status = 409


class OutOfStockError(CartServiceError):
    """FR-2/FR-3 — requested quantity exceeds Catalog's reported
    available_quantity."""

    error_code = "OUT_OF_STOCK"
    http_status = 422


class StockCheckUnavailableError(CartServiceError):
    """Catalog Service unreachable during stock re-validation."""

    error_code = "STOCK_CHECK_UNAVAILABLE"
    http_status = 503


class DeliveryAddressRequiredError(CartServiceError):
    """No default address set for this caller — Pricing can't compute
    CGST/SGST-vs-IGST without a delivery state."""

    error_code = "DELIVERY_ADDRESS_REQUIRED"
    http_status = 422


class AddressLookupUnavailableError(CartServiceError):
    """User Service's internal address-state endpoint unreachable."""

    error_code = "ADDRESS_LOOKUP_UNAVAILABLE"
    http_status = 503


class PricingUnavailableError(CartServiceError):
    """Pricing & Offer Service unreachable — `GET /cart` fails the whole
    request rather than returning unpriced data."""

    error_code = "PRICING_UNAVAILABLE"
    http_status = 503


class WalletBalanceTooLowError(CartServiceError):
    """FR-6 — caller's wallet balance is below the minimum for a
    subscription-frequency line item."""

    error_code = "WALLET_BALANCE_TOO_LOW"
    http_status = 422


class WalletCheckUnavailableError(CartServiceError):
    """Wallet Service unreachable (or, today, doesn't exist at all — see
    README Known Gaps) — fails closed, distinct from
    WalletBalanceTooLowError so the mobile client can distinguish "you
    don't have enough" from "we couldn't check."""

    error_code = "WALLET_CHECK_UNAVAILABLE"
    http_status = 503
