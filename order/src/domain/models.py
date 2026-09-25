"""Domain models. Plain dataclasses / enums only — no SQLAlchemy/FastAPI
types."""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    CONFIRMED = "CONFIRMED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    FAILED = "FAILED"


class OrderSource(StrEnum):
    """MA-136 — SUBSCRIPTION: materialized from SubscriptionOrderDue
    (one product, `subscription_id` set). CHECKOUT: a cart checkout's
    one-time lines (one or more `items`, no subscription)."""

    SUBSCRIPTION = "SUBSCRIPTION"
    CHECKOUT = "CHECKOUT"


@dataclass
class OrderItem:
    product_id: str
    quantity: int


@dataclass
class Order:
    id: str
    user_id: str
    # None for a CHECKOUT order — its lines live in `items`.
    subscription_id: str | None
    product_id: str | None
    quantity: int | None
    amount_paise: int
    delivery_date: date
    status: OrderStatus
    failure_reason: str | None = None
    created_at: datetime | None = None
    confirmed_at: datetime | None = None
    source: OrderSource = OrderSource.SUBSCRIPTION
    checkout_id: str | None = None
    items: list[OrderItem] = field(default_factory=list)

    def item_list(self) -> list[OrderItem]:
        """Every order as a list of lines — a SUBSCRIPTION order's single
        product/quantity, or a CHECKOUT order's own items."""
        if self.items:
            return self.items
        if self.product_id is not None and self.quantity is not None:
            return [OrderItem(product_id=self.product_id, quantity=self.quantity)]
        return []


@dataclass
class Quote:
    """Pricing Service's `POST /pricing/quote` response — mirrors
    `cart/src/domain/models.py`'s own `Quote` shape."""

    base_price: float
    tax_amount: float
    tax_rate: float
    delivery_fee: float
    net_payable: float
    monthly_estimate: float | None = None
    discount_amount: float | None = None
    applied_offer_id: str | None = None


@dataclass
class OrdersPage:
    items: list[Order]
    next_cursor: str | None


@dataclass
class DebitResult:
    """Wallet Service's `POST /wallet/internal/debit` response, per
    MA-130 FR-1's three-way 200-status contract."""

    status: str  # "DEBITED" | "INSUFFICIENT_BALANCE" | "WALLET_NOT_ACTIVE"
    balance_after_paise: int | None = None
