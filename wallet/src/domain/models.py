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


class DebitResult(StrEnum):
    """MA-130 `debit_for_order` outcome. `WALLET_NOT_ACTIVE` here is a
    normal 200 response value, not an exception — see
    `WalletProvisioningPendingError`'s docstring for why the "no wallet
    row at all" case is a *separate*, retryable (503) path instead."""

    DEBITED = "DEBITED"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    WALLET_NOT_ACTIVE = "WALLET_NOT_ACTIVE"


@dataclass
class DebitOutcome:
    result: DebitResult
    wallet_id: str | None = None
    balance_paise: int | None = None  # set for DEBITED / INSUFFICIENT_BALANCE
    required_paise: int | None = None  # set only for INSUFFICIENT_BALANCE

    # Defined in domain/models.py rather than domain/wallet_service.py
    # (as the MA-25 implementation plan's prose has it) to avoid a
    # circular import: adapters/wallet_repository.py must construct
    # DebitOutcome values, and wallet_service.py already imports from
    # adapters/wallet_repository.py.
