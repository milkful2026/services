"""Domain models. Plain dataclasses / enums only — no SQLAlchemy/FastAPI
types."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    CONFIRMED = "CONFIRMED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    FAILED = "FAILED"


@dataclass
class Order:
    id: str
    user_id: str
    subscription_id: str
    product_id: str
    quantity: int
    amount_paise: int
    delivery_date: date
    status: OrderStatus
    failure_reason: str | None = None
    created_at: datetime | None = None
    confirmed_at: datetime | None = None


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
