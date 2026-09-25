"""MA-136 — the resumable checkout record. Plain dataclasses / enums only.

A checkout moves STARTED -> PAID -> SUBSCRIPTIONS_DONE and finishes
COMPLETED (or PAYMENT_FAILED at the charge step). Every step is safe to
run again, so a retried request with the same Idempotency-Key resumes at
`step` instead of starting over.
"""

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class CheckoutStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    PAYMENT_FAILED = "PAYMENT_FAILED"


class CheckoutStep(StrEnum):
    STARTED = "STARTED"
    PAID = "PAID"
    SUBSCRIPTIONS_DONE = "SUBSCRIPTIONS_DONE"


@dataclass
class CheckoutLine:
    """Snapshot of one cart line at checkout start — the cart itself is
    never re-read on resume."""

    line_id: str
    product_id: str
    quantity: int
    frequency: str  # ONE_TIME | DAILY | ALTERNATE_DAYS
    start_date: date | None = None
    slot_id: str | None = None

    @property
    def is_subscription(self) -> bool:
        return self.frequency != "ONE_TIME"

    def to_dict(self) -> dict:
        return {
            "lineId": self.line_id,
            "productId": self.product_id,
            "quantity": self.quantity,
            "frequency": self.frequency,
            "startDate": self.start_date.isoformat() if self.start_date else None,
            "slotId": self.slot_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CheckoutLine":
        start = data.get("startDate")
        return cls(
            line_id=data["lineId"],
            product_id=data["productId"],
            quantity=int(data["quantity"]),
            frequency=data["frequency"],
            start_date=date.fromisoformat(start) if start else None,
            slot_id=data.get("slotId"),
        )


@dataclass
class SubscriptionLineResult:
    line_id: str
    product_id: str
    status: str  # CREATED | FAILED
    subscription_id: str | None = None
    next_delivery_date: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict:
        data = {
            "lineId": self.line_id,
            "productId": self.product_id,
            "status": self.status,
        }
        if self.status == "CREATED":
            data["subscriptionId"] = self.subscription_id
            data["nextDeliveryDate"] = self.next_delivery_date
        else:
            data["reason"] = self.reason
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "SubscriptionLineResult":
        return cls(
            line_id=data["lineId"],
            product_id=data["productId"],
            status=data["status"],
            subscription_id=data.get("subscriptionId"),
            next_delivery_date=data.get("nextDeliveryDate"),
            reason=data.get("reason"),
        )


@dataclass
class Checkout:
    id: str
    user_id: str
    idempotency_key: str
    cart_version: int
    status: CheckoutStatus
    step: CheckoutStep
    lines: list[CheckoutLine]
    pay_now_paise: int
    delivery_date: date
    order_id: str | None = None
    subscription_results: list[SubscriptionLineResult] = field(default_factory=list)
    # The stored response for replay: the FR-9 success body, or for
    # PAYMENT_FAILED {"error": {errorCode, message, httpStatus, details}}.
    result: dict | None = None

    @property
    def one_time_lines(self) -> list[CheckoutLine]:
        return [line for line in self.lines if not line.is_subscription]

    @property
    def subscription_lines(self) -> list[CheckoutLine]:
        return [line for line in self.lines if line.is_subscription]
