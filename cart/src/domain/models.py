"""Domain models. Plain dataclasses only — no boto3/pydantic types
(services/README.md §3.4)."""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class Frequency(StrEnum):
    """Wire values match the mobile app's own `Frequency` enum and
    MA-101/MA-122's shared `POST /pricing/quote` contract exactly."""

    ONE_TIME = "ONE_TIME"
    DAILY = "DAILY"
    ALTERNATE_DAYS = "ALTERNATE_DAYS"

    @property
    def is_subscription(self) -> bool:
        return self != Frequency.ONE_TIME


@dataclass
class LineItem:
    id: str
    product_id: str
    quantity: int
    frequency: Frequency
    start_date: date | None
    added_at: datetime


@dataclass
class Cart:
    line_items: list[LineItem] = field(default_factory=list)
    cart_version: int = 0


@dataclass
class Quote:
    """Mirrors MA-101/MA-122's `POST /pricing/quote` response shape, as
    merged (that spec's own PR #9 fix: `monthly_estimate` is
    tax/delivery/discount-inclusive, never unit price alone)."""

    base_price: float
    tax_amount: float
    tax_rate: float
    delivery_fee: float
    net_payable: float
    monthly_estimate: float | None = None
    discount_amount: float | None = None
    applied_offer_id: str | None = None


@dataclass
class CartView:
    """`GET /cart`'s full response shape (FR-1): the stored cart plus its
    live pricing breakdown. `quote` is `None` only for a genuinely empty
    cart (see `cart_service.CartService.get_cart`'s own docstring for why
    that's a deliberate, real-implementation-driven deviation from MA-121
    §5's "GET /cart makes two of these unconditionally" — Pricing &
    Offer's actual `POST /pricing/quote` rejects an empty `items` list
    outright, so calling it for zero line items can only ever fail)."""

    cart: Cart
    quote: Quote | None
