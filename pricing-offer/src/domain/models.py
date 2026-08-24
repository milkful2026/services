"""Domain models. Plain dataclasses only — no SQLAlchemy/FastAPI/pydantic
types, matching catalog/inventory's own convention."""

from dataclasses import dataclass
from enum import StrEnum


class Frequency(StrEnum):
    ONE_TIME = "ONE_TIME"
    DAILY = "DAILY"
    ALTERNATE_DAYS = "ALTERNATE_DAYS"

    @property
    def is_subscription(self) -> bool:
        return self != Frequency.ONE_TIME

    @property
    def monthly_occurrences(self) -> int:
        """Billing occurrences per month for a subscription frequency —
        MA-122 §12 left the exact day-count convention to implementation
        time. Daily ≈ 30/month, Alternate Days ≈ 15/month. Not meaningful
        for ONE_TIME — callers must not read this for that frequency."""
        return {Frequency.DAILY: 30, Frequency.ALTERNATE_DAYS: 15}[self]


@dataclass
class QuoteLineItem:
    product_id: str
    quantity: int
    frequency: Frequency


@dataclass
class Quote:
    """Cart-level totals across every requested line item. `monthly_estimate`
    is populated only when every line item shares the same subscription
    frequency (the only shape MA-23's mobile screen — this service's only
    real caller today — ever sends: one item, one frequency); a
    mixed-frequency multi-item request leaves it `None` rather than
    guessing which frequency it should represent.
    """

    base_price: float
    tax_amount: float
    tax_rate: float
    delivery_fee: float
    net_payable: float
    monthly_estimate: float | None = None
