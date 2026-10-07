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
    # MA-143: the sweep gave up (retry budget spent, or past the charge
    # deadline before it could be charged). Terminal; operators act on it.
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    # MA-144 PD-1: a checkout's order that was never charged and can no
    # longer be delivered. No event: it was never confirmed.
    CANCELLED = "CANCELLED"


# MA-143 failure reasons set by the sweep (orders.failure_reason).
FAILURE_CUTOFF_PASSED = "CUTOFF_PASSED"
FAILURE_SWEEP_EXHAUSTED = "SWEEP_EXHAUSTED"
# MA-154: the customer cancelled a CONFIRMED order before the cut-off.
FAILURE_CUSTOMER_CANCELLED = "CUSTOMER_CANCELLED"


class CancelReason(StrEnum):
    """MA-154 FR-1 — the optional reason a customer gives for cancelling."""

    ORDERED_BY_MISTAKE = "ORDERED_BY_MISTAKE"
    NOT_HOME = "NOT_HOME"
    CHANGED_MIND = "CHANGED_MIND"
    OTHER = "OTHER"


class RefundState(StrEnum):
    """MA-154 `orders.refund_state` — set only on a customer cancel. PENDING
    until Wallet confirms the refund (the sweep finishes it if the request
    couldn't); NOT_REQUIRED for a ₹0 order or when Wallet holds no debit."""

    PENDING = "PENDING"
    REFUNDED = "REFUNDED"
    NOT_REQUIRED = "NOT_REQUIRED"


class ChargeState(StrEnum):
    """MA-143 `orders.charge_state` — set only when the sweep closes an
    order; NULL on every other order. NOT_CHARGED only after a Wallet void
    (MA-142); UNKNOWN until the settle pass (FR-4b) resolves it."""

    NOT_CHARGED = "NOT_CHARGED"
    CHARGED = "CHARGED"
    UNKNOWN = "UNKNOWN"


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
    # MA-143 sweep lease / attempt bookkeeping.
    sweep_attempts: int = 0
    claimed_until: datetime | None = None
    claim_owner: str | None = None
    last_sweep_error: str | None = None
    charge_state: ChargeState | None = None
    # MA-154 customer cancel.
    cancel_reason: CancelReason | None = None
    cancelled_at: datetime | None = None
    refund_state: RefundState | None = None
    refunded_at: datetime | None = None

    @property
    def is_customer_cancelled(self) -> bool:
        return (
            self.status == OrderStatus.CANCELLED
            and self.failure_reason == FAILURE_CUSTOMER_CANCELLED
        )

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


@dataclass(frozen=True)
class DebitLookup:
    """MA-142 — proof that an order was charged: Wallet's lookup, or its
    `409 ALREADY_DEBITED` answer to a void."""

    amount_paise: int
    balance_after_paise: int
    debited_at: datetime


@dataclass(frozen=True)
class Refunded:
    """MA-153 — Wallet credited the order's refund (or replayed one that
    already landed: `replayed=True`)."""

    amount_paise: int
    balance_after_paise: int
    refunded_at: datetime
    replayed: bool = False


@dataclass(frozen=True)
class Voided:
    """MA-142 — Wallet voided the order: no debit for it can ever commit."""

    voided_at: datetime
