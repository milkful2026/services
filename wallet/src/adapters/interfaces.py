"""Ports the domain depends on. Adapters implement these; the domain
never imports SQLAlchemy or boto3 directly."""

from typing import Protocol

from domain.models import (
    DebitOutcome,
    DebitVoid,
    LedgerEntry,
    LedgerType,
    RefundOutcome,
    Wallet,
)


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
        self,
        wallet_id: str,
        limit: int,
        before_id: int | None,
        types: frozenset[LedgerType] | None = None,
    ) -> list[LedgerEntry]:
        """Newest first, entries with `id < before_id` when given; `types`
        (MA-148) restricts to those ledger types."""
        ...

    def get_ledger_entry_by_ref(self, ref: str) -> LedgerEntry | None:
        """MA-142 — plain read by the unique `ref`; no lock, no write."""
        ...

    def get_void(self, ref: str) -> DebitVoid | None:
        """MA-142 — plain read of the void for `ref`; no lock, no write."""
        ...

    def void_debit_for_order(self, user_id: str, ref: str) -> LedgerEntry | DebitVoid:
        """MA-142 FR-2, one transaction under the user's wallet row lock:
        the existing ledger entry for `ref` (nothing written), or the void —
        inserted now, or the one a previous call stored."""
        ...

    def find_balance_invariant_violations(self) -> list[tuple[str, int, int]]:
        """Returns (wallet_id, balance_paise, ledger_sum_paise) for every
        wallet where they disagree."""
        ...

    def debit_for_order(
        self,
        *,
        user_id: str,
        order_id: str,
        amount_paise: int,
        ref: str,
        correlation_id: str | None,
        outbox_payload_builder,
    ) -> DebitOutcome:
        """One transaction: SELECT ... FOR UPDATE the wallet; no row ->
        raises WalletProvisioningPendingError; not ACTIVE ->
        WALLET_NOT_ACTIVE; `ref` already debited -> DEBITED replay (or
        OrderUserMismatchError if it belongs to a different wallet); `ref`
        voided -> raises DebitVoidedError; insufficient balance ->
        INSUFFICIENT_BALANCE; otherwise inserts the ORDER_DEBIT ledger
        row, decrements the balance, and enqueues
        a WalletDebited outbox row built by
        `outbox_payload_builder(wallet_id, balance_after_paise)`."""
        ...

    def refund_for_order(
        self,
        *,
        user_id: str,
        order_id: str,
        refund_id: str,
        amount_paise: int,
        ref: str,
        correlation_id: str | None,
        outbox_payload_builder,
    ) -> RefundOutcome:
        """MA-153 FR-2, one transaction: SELECT ... FOR UPDATE the wallet (no
        row -> WalletNotFoundError); `ref` already refunded -> replay (or
        RefundOrderUserMismatchError on another wallet); no ORDER_DEBIT for
        the order -> RefundDebitNotFoundError; over the debited amount ->
        RefundExceedsDebitError; otherwise inserts the REFUND ledger row,
        increments the balance (whatever the wallet's status), and enqueues
        a WalletRefunded outbox row built by
        `outbox_payload_builder(wallet_id, balance_after_paise)`."""
        ...

    def enqueue_outbox_event(self, *, aggregate_id: str, event_type: str, payload: dict) -> None:
        """A standalone one-row outbox insert, for events raised as a
        secondary effect after another write already committed (e.g.
        WalletLowBalance after debit_for_order)."""
        ...


class OutboxPort(Protocol):
    def enqueue(self, aggregate_id: str, event_type: str, payload: dict) -> None: ...


class LedgerTypeAware(Protocol):
    type: LedgerType
