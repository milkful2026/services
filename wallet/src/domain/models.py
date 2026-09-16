"""Domain models. Plain dataclasses / enums only — no SQLAlchemy/FastAPI types."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class WalletStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CREATING = "CREATING"
    FAILED = "FAILED"


class LedgerType(StrEnum):
    OPENING = "OPENING"
    RECHARGE = "RECHARGE"
    ORDER_DEBIT = "ORDER_DEBIT"
    REFUND = "REFUND"
    CASHBACK = "CASHBACK"
    REFERRAL_CREDIT = "REFERRAL_CREDIT"
    ADJUSTMENT = "ADJUSTMENT"


@dataclass
class Wallet:
    id: str
    user_id: str
    balance_paise: int
    currency: str
    status: WalletStatus
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class LedgerEntry:
    id: int
    wallet_id: str
    type: LedgerType
    amount_paise: int          # signed: +credit / -debit
    balance_after_paise: int
    ref: str
    correlation_id: str | None
    created_at: datetime


@dataclass
class TransactionsPage:
    items: list[LedgerEntry]
    next_cursor: str | None
