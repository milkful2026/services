"""Domain models. Plain dataclasses / enums only — no SQLAlchemy/FastAPI/
Razorpay SDK types."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Purpose(StrEnum):
    WALLET_RECHARGE = "WALLET_RECHARGE"
    ORDER = "ORDER"          # reserved — not built in this slice (MA-126 SS3)


class PaymentStatus(StrEnum):
    CREATED = "CREATED"
    CONFIRMING = "CONFIRMING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"


class PaymentMethod(StrEnum):
    UPI = "UPI"
    CARD = "CARD"
    NETBANKING = "NETBANKING"
    WALLET = "WALLET"
    OTHER = "OTHER"


@dataclass
class Payment:
    id: str
    user_id: str
    purpose: Purpose
    amount_paise: int
    currency: str
    status: PaymentStatus
    idempotency_key: str
    correlation_id: str
    method: PaymentMethod | None = None
    razorpay_order_id: str | None = None
    razorpay_payment_id: str | None = None
    razorpay_signature: str | None = None
    failure_code: str | None = None
    failure_reason: str | None = None
    captured_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
