"""SQLAlchemy Core repository for `wallets` / `ledger_entries` / `outbox`.

SQLAlchemy Core only (mirrors catalog/inventory) — the same Table
definitions run against Postgres (production) and an in-memory SQLite
engine (tests). Table columns are kept column-for-column compatible with
migrations/0001_wallets_ledger.sql by hand.
"""

import base64
import json
import uuid

from shared.adapters.db_operation import SqlAlchemyOperationMixin
from shared.adapters.json_column import JSONColumn
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from domain.exceptions import (
    OrderUserMismatchError,
    RetryableConsumerError,
    ServiceUnavailableError,
    WalletProvisioningPendingError,
)
from domain.models import DebitOutcome, DebitResult, LedgerEntry, LedgerType, Wallet, WalletStatus

metadata = MetaData()

wallets_table = Table(
    "wallets",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("user_id", String(64), nullable=False, unique=True),
    Column("balance_paise", BigInteger, nullable=False, default=0),
    Column("currency", String(3), nullable=False, default="INR"),
    Column("status", String(16), nullable=False, default=WalletStatus.ACTIVE.value),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    ),
)

ledger_entries_table = Table(
    "ledger_entries",
    metadata,
    Column(
        "id",
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    ),
    Column("wallet_id", String(64), ForeignKey("wallets.id"), nullable=False),
    Column("type", String(24), nullable=False),
    Column("amount_paise", BigInteger, nullable=False),
    Column("balance_after_paise", BigInteger, nullable=False),
    Column("ref", Text, nullable=False, unique=True),
    Column("correlation_id", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

outbox_table = Table(
    "outbox",
    metadata,
    Column(
        "id",
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    ),
    Column("aggregate_id", String(64), nullable=False),
    Column("event_type", String(48), nullable=False),
    Column("payload", JSONColumn(), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


def create_schema(engine: Engine) -> None:
    """Test-only convenience — production schema ownership is the raw SQL
    migration file, not this."""
    metadata.create_all(engine)


class SqlAlchemyWalletRepository(SqlAlchemyOperationMixin):
    _unavailable_error = ServiceUnavailableError
    _log_prefix = "wallet_repository"

    def __init__(self, engine: Engine, correlation_id: str = "") -> None:
        self._engine = engine
        self._correlation_id = correlation_id

    def get_wallet_by_user(self, user_id: str) -> Wallet | None:
        with self._db_operation("get_wallet_by_user", "Failed to load wallet"):
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(wallets_table).where(wallets_table.c.user_id == user_id)
                ).fetchone()
        return None if row is None else _row_to_wallet(row)

    def insert_wallet_if_absent(self, wallet_id: str, user_id: str) -> bool:
        with self._db_operation("insert_wallet_if_absent", "Failed to create wallet"):
            with self._engine.begin() as conn:
                existing = conn.execute(
                    select(wallets_table.c.id).where(wallets_table.c.user_id == user_id)
                ).fetchone()
                if existing is not None:
                    return False
                conn.execute(
                    wallets_table.insert().values(
                        id=wallet_id,
                        user_id=user_id,
                        balance_paise=0,
                        currency="INR",
                        status=WalletStatus.ACTIVE.value,
                    )
                )
                conn.execute(
                    ledger_entries_table.insert().values(
                        wallet_id=wallet_id,
                        type=LedgerType.OPENING.value,
                        amount_paise=0,
                        balance_after_paise=0,
                        ref=f"opening:{wallet_id}",
                        correlation_id=self._correlation_id or None,
                    )
                )
                return True

    def credit_recharge(
        self,
        *,
        user_id: str,
        amount_paise: int,
        ref: str,
        correlation_id: str | None,
        outbox_payload_builder,
    ) -> Wallet | None:
        with self._db_operation("credit_recharge", "Failed to credit wallet"):
            with self._engine.begin() as conn:
                row = conn.execute(
                    select(wallets_table)
                    .where(wallets_table.c.user_id == user_id)
                    .with_for_update()
                ).fetchone()
                if row is None:
                    raise RetryableConsumerError("wallet does not exist yet")
                wallet = _row_to_wallet(row)
                if wallet.status != WalletStatus.ACTIVE:
                    raise RetryableConsumerError(f"wallet status is {wallet.status}")

                # The FOR UPDATE lock on the wallet row serializes every
                # recharge credit for this user, so a plain existence
                # check on `ref` inside the transaction is a sound
                # idempotency guard on both Postgres and the SQLite test
                # double (no need for a dialect-specific ON CONFLICT).
                # `ref` is also UNIQUE at the schema level as a backstop.
                already = conn.execute(
                    select(ledger_entries_table.c.id).where(ledger_entries_table.c.ref == ref)
                ).fetchone()
                if already is not None:
                    return None

                new_balance = wallet.balance_paise + amount_paise
                conn.execute(
                    ledger_entries_table.insert().values(
                        wallet_id=wallet.id,
                        type=LedgerType.RECHARGE.value,
                        amount_paise=amount_paise,
                        balance_after_paise=new_balance,
                        ref=ref,
                        correlation_id=correlation_id,
                    )
                )
                conn.execute(
                    wallets_table.update()
                    .where(wallets_table.c.id == wallet.id)
                    .values(balance_paise=new_balance, updated_at=func.now())
                )
                payload = outbox_payload_builder(wallet, new_balance)
                conn.execute(
                    outbox_table.insert().values(
                        aggregate_id=wallet.id,
                        event_type="WalletCredited",
                        payload=payload,
                    )
                )
                wallet.balance_paise = new_balance
                return wallet

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
        """MA-130 FR: one transaction, mirrors `credit_recharge`'s
        lock-check-write shape. `SELECT ... FOR UPDATE` the wallet row,
        then:
          - no row at all -> raise WalletProvisioningPendingError (503,
            retryable — a provisioning race, not a settled bad state).
          - `ref` already debited -> DEBITED replay, using the existing
            entry's own wallet_id/balance_after_paise (no new write); a
            `ref` that resolves to a *different* wallet than the one just
            locked raises OrderUserMismatchError (should be impossible —
            `order_id` is server-generated and globally unique). Checked
            *before* the wallet-status check so a replay of an
            already-debited order still returns DEBITED even if the
            wallet's status has since changed (e.g. FAILED) — the debit
            already happened; the status check only gates *new* debits.
          - row present but not ACTIVE -> WALLET_NOT_ACTIVE (no write).
          - insufficient balance -> INSUFFICIENT_BALANCE (no write).
          - otherwise -> insert the ORDER_DEBIT ledger row, decrement the
            balance, insert a WalletDebited outbox row, all in this one
            transaction.

        The `ref` UNIQUE constraint is the real serialization point
        across *different* wallets' locked rows (the `FOR UPDATE` above
        only serializes same-wallet callers) — two concurrent calls for
        the same order_id but different user_id can both pass the
        `existing is None` check before either commits. That race is
        caught as an IntegrityError on the insert below and resolved by
        re-reading `ref` in a fresh transaction, the same way the
        pre-insert check would have resolved it had it run second.
        """
        with self._db_operation("debit_for_order", "Failed to debit wallet"):
            try:
                with self._engine.begin() as conn:
                    row = conn.execute(
                        select(wallets_table)
                        .where(wallets_table.c.user_id == user_id)
                        .with_for_update()
                    ).fetchone()
                    if row is None:
                        raise WalletProvisioningPendingError(
                            f"no wallet row yet for user {user_id!r}"
                        )
                    wallet = _row_to_wallet(row)

                    existing = conn.execute(
                        select(ledger_entries_table).where(ledger_entries_table.c.ref == ref)
                    ).fetchone()
                    if existing is not None:
                        return _replay_outcome(wallet, existing, order_id)

                    if wallet.status != WalletStatus.ACTIVE:
                        return DebitOutcome(result=DebitResult.WALLET_NOT_ACTIVE)

                    if wallet.balance_paise < amount_paise:
                        return DebitOutcome(
                            result=DebitResult.INSUFFICIENT_BALANCE,
                            wallet_id=wallet.id,
                            balance_paise=wallet.balance_paise,
                            required_paise=amount_paise,
                        )

                    new_balance = wallet.balance_paise - amount_paise
                    conn.execute(
                        ledger_entries_table.insert().values(
                            wallet_id=wallet.id,
                            type=LedgerType.ORDER_DEBIT.value,
                            amount_paise=-amount_paise,
                            balance_after_paise=new_balance,
                            ref=ref,
                            correlation_id=correlation_id,
                        )
                    )
                    conn.execute(
                        wallets_table.update()
                        .where(wallets_table.c.id == wallet.id)
                        .values(balance_paise=new_balance, updated_at=func.now())
                    )
                    payload = outbox_payload_builder(wallet.id, new_balance)
                    conn.execute(
                        outbox_table.insert().values(
                            aggregate_id=wallet.id,
                            event_type="WalletDebited",
                            payload=payload,
                        )
                    )
                    return DebitOutcome(
                        result=DebitResult.DEBITED, wallet_id=wallet.id, balance_paise=new_balance
                    )
            except IntegrityError:
                with self._engine.connect() as conn:
                    wallet_row = conn.execute(
                        select(wallets_table).where(wallets_table.c.user_id == user_id)
                    ).fetchone()
                    existing = conn.execute(
                        select(ledger_entries_table).where(ledger_entries_table.c.ref == ref)
                    ).fetchone()
                if wallet_row is None or existing is None:
                    raise
                return _replay_outcome(_row_to_wallet(wallet_row), existing, order_id)

    def enqueue_outbox_event(self, *, aggregate_id: str, event_type: str, payload: dict) -> None:
        """Standalone one-row outbox insert for events that aren't part of
        a larger state-changing transaction (e.g. `WalletLowBalance`,
        raised as a secondary effect *after* `debit_for_order` commits)."""
        with self._db_operation("enqueue_outbox_event", "Failed to enqueue event"):
            with self._engine.begin() as conn:
                conn.execute(
                    outbox_table.insert().values(
                        aggregate_id=aggregate_id,
                        event_type=event_type,
                        payload=payload,
                    )
                )

    def find_balance_invariant_violations(self) -> list[tuple[str, int, int]]:
        """Returns `(wallet_id, balance_paise, ledger_sum_paise)` for every
        wallet whose stored balance disagrees with the sum of its own
        ledger entries. Read-only, no locking — a wallet mid-credit at the
        moment of the scan is not a real violation, just a race with this
        sweep; run periodically, not as a correctness gate."""
        with self._db_operation("find_balance_invariant_violations", "Failed to check wallets"):
            ledger_sums = select(
                ledger_entries_table.c.wallet_id.label("wallet_id"),
                func.sum(ledger_entries_table.c.amount_paise).label("ledger_sum"),
            ).group_by(ledger_entries_table.c.wallet_id).subquery()

            stmt = select(
                wallets_table.c.id,
                wallets_table.c.balance_paise,
                func.coalesce(ledger_sums.c.ledger_sum, 0).label("ledger_sum"),
            ).select_from(
                wallets_table.outerjoin(ledger_sums, wallets_table.c.id == ledger_sums.c.wallet_id)
            )
            with self._engine.connect() as conn:
                rows = conn.execute(stmt).fetchall()
        return [
            (row.id, int(row.balance_paise), int(row.ledger_sum))
            for row in rows
            if int(row.balance_paise) != int(row.ledger_sum)
        ]

    def list_ledger_entries(
        self, wallet_id: str, limit: int, before_id: int | None
    ) -> list[LedgerEntry]:
        # Keyset on `id` alone — the BIGSERIAL id is monotonic with
        # insertion, so `id DESC` is chronological and portable across
        # Postgres and the SQLite test double (which does not round-trip
        # `TIMESTAMPTZ` faithfully — same gap catalog's repo documents).
        # The (wallet_id, created_at DESC, id DESC) index still serves the
        # ordering on Postgres.
        with self._db_operation("list_ledger_entries", "Failed to load transactions"):
            stmt = select(ledger_entries_table).where(
                ledger_entries_table.c.wallet_id == wallet_id
            )
            if before_id is not None:
                stmt = stmt.where(ledger_entries_table.c.id < before_id)
            stmt = stmt.order_by(ledger_entries_table.c.id.desc()).limit(limit)
            with self._engine.connect() as conn:
                rows = conn.execute(stmt).fetchall()
        return [_row_to_entry(r) for r in rows]

    # --- outbox (used by the publisher handler) ---

    def fetch_unpublished(self, limit: int = 20) -> list[dict]:
        with self._db_operation("fetch_unpublished", "Failed to read outbox"):
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(outbox_table)
                    .where(outbox_table.c.published_at.is_(None))
                    .order_by(outbox_table.c.created_at)
                    .limit(limit)
                ).fetchall()
        return [{"id": r.id, "event_type": r.event_type, "payload": r.payload} for r in rows]

    def mark_published(self, outbox_id: int) -> None:
        with self._db_operation("mark_published", "Failed to mark outbox row"):
            with self._engine.begin() as conn:
                conn.execute(
                    outbox_table.update()
                    .where(outbox_table.c.id == outbox_id)
                    .values(published_at=func.now())
                )


def new_wallet_id() -> str:
    return f"wal_{uuid.uuid4().hex}"


def encode_cursor(entry_id: int) -> str:
    return base64.urlsafe_b64encode(
        json.dumps({"id": entry_id}).encode("utf-8")
    ).decode("ascii")


def decode_cursor(cursor: str) -> int:
    raw = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
    return int(raw["id"])


def _replay_outcome(wallet: Wallet, existing_ledger_row, order_id: str) -> DebitOutcome:
    if existing_ledger_row.wallet_id != wallet.id:
        raise OrderUserMismatchError(
            f"order {order_id!r} already debited against a different wallet"
        )
    return DebitOutcome(
        result=DebitResult.DEBITED,
        wallet_id=wallet.id,
        balance_paise=int(existing_ledger_row.balance_after_paise),
        replayed=True,
    )


def _row_to_wallet(row) -> Wallet:
    return Wallet(
        id=row.id,
        user_id=row.user_id,
        balance_paise=int(row.balance_paise),
        currency=row.currency,
        status=WalletStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_entry(row) -> LedgerEntry:
    return LedgerEntry(
        id=int(row.id),
        wallet_id=row.wallet_id,
        type=LedgerType(row.type),
        amount_paise=int(row.amount_paise),
        balance_after_paise=int(row.balance_after_paise),
        ref=row.ref,
        correlation_id=row.correlation_id,
        created_at=row.created_at,
    )
