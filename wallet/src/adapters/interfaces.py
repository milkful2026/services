"""Ports the domain depends on. Adapters implement these; the domain
never imports SQLAlchemy or boto3 directly."""

from typing import Protocol

from domain.models import LedgerEntry, LedgerType, Wallet


class WalletRepositoryPort(Protocol):
    def get_wallet_by_user(self, user_id: str) -> Wallet | None: ...

    def insert_wallet_if_absent(self, wallet_id: str, user_id: str) -> bool:
        """Returns True if a new wallet + opening ledger entry were
        created, False if one already existed (idempotent on user_id)."""
        ...

    def credit_recharge(
        self,
        *,
        user_id: str,
        amount_paise: int,
        ref: str,
        correlation_id: str | None,
        outbox_payload_builder,
    ) -> Wallet | None:
        """One transaction: SELECT ... FOR UPDATE the wallet; INSERT the
        RECHARGE ledger entry ON CONFLICT (ref) DO NOTHING; if inserted,
        bump balance and enqueue a WalletCredited outbox row built by
        `outbox_payload_builder(wallet, balance_after_paise)`. Returns the
        updated Wallet on a fresh credit, None on a duplicate (ref
        conflict). Raises RetryableConsumerError if the wallet is absent
        or not ACTIVE."""
        ...

    def list_ledger_entries(
        self, wallet_id: str, limit: int, before: tuple[str, int] | None
    ) -> list[LedgerEntry]: ...


class OutboxPort(Protocol):
    def enqueue(self, aggregate_id: str, event_type: str, payload: dict) -> None: ...


class LedgerTypeAware(Protocol):
    type: LedgerType
