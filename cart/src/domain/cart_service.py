"""MA-121 (as merged) FR-1–FR-6's business rules. Orchestrates only
through the Port interfaces (`adapters/interfaces.py`) — never
boto3/requests directly (services/README.md §3.4).
"""

from domain.exceptions import (
    DeliveryAddressRequiredError,
    OutOfStockError,
    ValidationError,
    WalletBalanceTooLowError,
)
from domain.models import Cart, CartView, Frequency, LineItem


class CartService:
    def __init__(
        self,
        repository,
        catalog_client,
        user_client,
        pricing_client,
        wallet_client,
        wallet_minimum_balance: int,
    ) -> None:
        self._repository = repository
        self._catalog_client = catalog_client
        self._user_client = user_client
        self._pricing_client = pricing_client
        self._wallet_client = wallet_client
        self._wallet_minimum_balance = wallet_minimum_balance

    def set_correlation_id(self, correlation_id: str) -> None:
        for client in (
            self._repository,
            self._catalog_client,
            self._user_client,
            self._pricing_client,
            self._wallet_client,
        ):
            client.set_correlation_id(correlation_id)

    def get_cart(self, user_id: str) -> CartView:
        cart = self._repository.get_cart(user_id)

        if not cart.line_items:
            # Deliberate deviation from MA-121 §5's "GET /cart makes two
            # of these unconditionally (address lookup + pricing quote)
            # on every call": Pricing & Offer's actual, real
            # `POST /pricing/quote` (pricing_service.py) raises
            # InvalidRequestError on an empty `items` list — calling it
            # for a cart with nothing in it can only ever fail, for a
            # question ("what does this empty cart cost") that has no
            # meaningful answer anyway. FR-1's own "no cart yet -> 200
            # with an empty list and cartVersion: 0" already treats an
            # empty cart as a fast, simple case; skipping both downstream
            # calls here is consistent with that, not a shortcut around
            # it.
            return CartView(cart=cart, quote=None)

        delivery_state = self._user_client.get_delivery_address_state(user_id)
        if delivery_state is None:
            raise DeliveryAddressRequiredError(
                "No default delivery address set for this account"
            )

        items_payload = [
            {
                "product_id": li.product_id,
                "quantity": li.quantity,
                "frequency": str(li.frequency),
            }
            for li in cart.line_items
        ]
        quote = self._pricing_client.quote(items_payload, delivery_state)
        return CartView(cart=cart, quote=quote)

    def add_item(
        self,
        user_id: str,
        product_id: str,
        quantity: int,
        frequency: Frequency,
        start_date: str | None,
        idempotency_key: str | None,
    ) -> LineItem:
        self._validate_item(quantity, frequency, start_date)
        self._check_stock(product_id, quantity)
        if frequency.is_subscription:
            self._check_wallet_gate(user_id)

        return self._repository.add_item(
            user_id, product_id, quantity, frequency, start_date, idempotency_key
        )

    def replace_cart(self, user_id: str, items: list[dict], if_version: int) -> Cart:
        # FR-3's full-replace semantics: everything in `items` is
        # validated and stock-checked, but the wallet gate (FR-6) only
        # applies to line items that are new or actually changing
        # (quantity/frequency/start_date) from what's already stored —
        # MA-121's own PR #8 fix, so an unrelated edit elsewhere in the
        # cart can't be blocked by a subscription item that was already
        # approved and isn't being touched by this request.
        current = self._repository.get_cart(user_id)
        current_by_id = {li.id: li for li in current.line_items}

        for item in items:
            quantity = item["quantity"]
            frequency = item["frequency"]
            start_date = item.get("start_date")
            self._validate_item(quantity, frequency, start_date)
            self._check_stock(item["product_id"], quantity)

            existing = current_by_id.get(item.get("id"))
            is_new_or_changed = (
                existing is None
                or existing.quantity != quantity
                or existing.frequency != frequency
                or existing.start_date != start_date
            )
            if frequency.is_subscription and is_new_or_changed:
                self._check_wallet_gate(user_id)

        return self._repository.replace_cart(user_id, items, if_version)

    def delete_item(self, user_id: str, line_item_id: str) -> None:
        self._repository.delete_item(user_id, line_item_id)

    def _validate_item(
        self, quantity: int, frequency: Frequency, start_date: str | None
    ) -> None:
        if quantity < 1:
            raise ValidationError(f"quantity must be at least 1 (got {quantity})")
        if frequency == Frequency.ONE_TIME and start_date is not None:
            raise ValidationError("startDate must not be set for a ONE_TIME item")
        if frequency != Frequency.ONE_TIME and start_date is None:
            raise ValidationError("startDate is required for a subscription item")

    def _check_stock(self, product_id: str, quantity: int) -> None:
        # None (Catalog's own available_quantity addition, MA-120 §7,
        # not yet implemented) means unknown-but-flagged, not unbounded —
        # never raises OutOfStockError against a None, matching
        # CatalogClientPort's own documented contract.
        available = self._catalog_client.get_available_quantity(product_id)
        if available is not None and quantity > available:
            raise OutOfStockError(
                f"Requested quantity {quantity} exceeds available stock",
                details={"productId": product_id, "availableQuantity": available},
            )

    def _check_wallet_gate(self, user_id: str) -> None:
        balance = self._wallet_client.get_balance(user_id)
        if balance < self._wallet_minimum_balance:
            raise WalletBalanceTooLowError(
                "Wallet balance is below the minimum required for a subscription item",
                details={"minimumRequired": self._wallet_minimum_balance},
            )
