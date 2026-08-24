"""Abstract adapter interfaces (Protocols). Domain code depends on these
only, never on boto3/requests directly (services/README.md §3.4)."""

from typing import Protocol

from domain.models import Cart, Frequency, LineItem, Quote


class CartRepositoryPort(Protocol):
    def set_correlation_id(self, correlation_id: str) -> None: ...

    def get_cart(self, user_id: str) -> Cart:
        """Empty Cart(line_items=[], cart_version=0) if none exists yet —
        "no cart" and "empty cart" are the same state (FR-1)."""
        ...

    def add_item(
        self,
        user_id: str,
        product_id: str,
        quantity: int,
        frequency: Frequency,
        start_date: str | None,
        idempotency_key: str,
    ) -> LineItem:
        """A repeated call with the same idempotency_key within the
        bounded window returns the original result rather than creating a
        duplicate line item (FR-2)."""
        ...

    def replace_cart(
        self, user_id: str, items: list[dict], if_version: int
    ) -> Cart:
        """Raises CartVersionMismatchError if if_version doesn't match the
        stored cartVersion (FR-3)."""
        ...

    def delete_item(self, user_id: str, line_item_id: str) -> None:
        """Raises LineItemNotFoundError if line_item_id doesn't exist or
        doesn't belong to user_id (FR-4)."""
        ...


class CatalogClientPort(Protocol):
    def set_correlation_id(self, correlation_id: str) -> None: ...

    def get_available_quantity(self, product_id: str) -> int | None:
        """None means the field isn't populated yet (Catalog Service's own
        available_quantity addition, MA-120 §7, not yet implemented as of
        this service's initial build) — callers treat that as
        unknown-but-flagged, not unbounded. Raises
        StockCheckUnavailableError on a transport failure."""
        ...


class UserClientPort(Protocol):
    def set_correlation_id(self, correlation_id: str) -> None: ...

    def get_delivery_address_state(self, cognito_sub: str) -> str | None:
        """None if the caller has no default address set. Raises
        AddressLookupUnavailableError on a transport failure."""
        ...


class PricingClientPort(Protocol):
    def set_correlation_id(self, correlation_id: str) -> None: ...

    def quote(
        self,
        items: list[dict],
        delivery_state: str,
        offer_code: str | None = None,
    ) -> Quote:
        """Raises PricingUnavailableError on a transport failure."""
        ...


class WalletClientPort(Protocol):
    def set_correlation_id(self, correlation_id: str) -> None: ...

    def get_balance(self, cognito_sub: str) -> int:
        """Raises WalletCheckUnavailableError — always, today, since
        Wallet Service (MA-100) doesn't exist (see README Known Gaps)."""
        ...


class OutboxEventPublisherPort(Protocol):
    """Used only by the separate outbox_publisher_handler — never called
    from the request-handling path."""

    def set_correlation_id(self, correlation_id: str) -> None: ...

    def publish(self, event_type: str, payload: dict, correlation_id: str) -> None: ...
