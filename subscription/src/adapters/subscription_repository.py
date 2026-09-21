"""SQLAlchemy Core repository for `subscriptions` / `subscription_skips`
/ `subscription_run_log` / `outbox`.

SQLAlchemy Core only (mirrors wallet/catalog/inventory) — the same Table
definitions run against Postgres (production) and an in-memory SQLite
engine (tests). Table columns are kept column-for-column compatible with
migrations/0001_subscriptions.sql by hand.
"""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from domain.exceptions import ServiceUnavailableError
from domain.models import PendingEdit, Schedule, Subscription, SubscriptionStatus

logger = logging.getLogger(__name__)

metadata = MetaData()

subscriptions_table = Table(
    "subscriptions",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("user_id", String(64), nullable=False),
    Column("product_id", String(64), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("schedule", JSONB().with_variant(Text, "sqlite"), nullable=False),
    Column("slot_id", String(64), nullable=False),
    Column("status", String(16), nullable=False, default=SubscriptionStatus.ACTIVE.value),
    Column("start_date", Date, nullable=False),
    Column("pause_from", Date, nullable=True),
    Column("pause_until", Date, nullable=True),
    Column("pending_edit", JSONB().with_variant(Text, "sqlite"), nullable=True),
    Column("idempotency_key", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    ),
    UniqueConstraint("user_id", "idempotency_key", name="uq_subscriptions_user_idempotency"),
)

subscription_skips_table = Table(
    "subscription_skips",
    metadata,
    Column(
        "id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    ),
    Column("subscription_id", String(64), ForeignKey("subscriptions.id"), nullable=False),
    Column("skipped_date", Date, nullable=False),
)

subscription_run_log_table = Table(
    "subscription_run_log",
    metadata,
    Column(
        "id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    ),
    Column("subscription_id", String(64), ForeignKey("subscriptions.id"), nullable=False),
    Column("delivery_date", Date, nullable=False),
    UniqueConstraint("subscription_id", "delivery_date", name="uq_run_log_subscription_date"),
)

outbox_table = Table(
    "outbox",
    metadata,
    Column(
        "id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    ),
    Column("aggregate_id", String(64), nullable=False),
    Column("event_type", String(48), nullable=False),
    Column("payload", JSONB().with_variant(Text, "sqlite"), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


def create_schema(engine: Engine) -> None:
    """Test-only convenience — production schema ownership is the raw SQL
    migration file, not this."""
    metadata.create_all(engine)


def _dump(payload: dict) -> str:
    return json.dumps(payload)


def _load(value) -> dict | None:
    if value is None:
        return None
    return json.loads(value) if isinstance(value, str) else value


class SqlAlchemySubscriptionRepository:
    def __init__(self, engine: Engine, correlation_id: str = "") -> None:
        self._engine = engine
        self._correlation_id = correlation_id

    @contextmanager
    def _db_operation(self, operation: str, failure_message: str) -> Iterator[None]:
        try:
            yield
        except SQLAlchemyError as exc:
            logger.error(
                f"subscription_repository.{operation} failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ServiceUnavailableError(failure_message) from exc

    # --- create / idempotency ---

    def get_by_idempotency_key(self, user_id: str, idempotency_key: str) -> Subscription | None:
        with self._db_operation("get_by_idempotency_key", "Failed to load subscription"):
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(subscriptions_table).where(
                        subscriptions_table.c.user_id == user_id,
                        subscriptions_table.c.idempotency_key == idempotency_key,
                    )
                ).fetchone()
        return None if row is None else _row_to_subscription(row)

    def insert_if_absent(
        self,
        *,
        subscription: Subscription,
        idempotency_key: str,
        same_day_delivery_date: date | None,
        outbox_event_type: str | None,
        outbox_payload: dict | None,
    ) -> tuple[Subscription, bool]:
        with self._db_operation("insert_if_absent", "Failed to create subscription"):
            try:
                with self._engine.begin() as conn:
                    conn.execute(
                        subscriptions_table.insert().values(
                            id=subscription.id,
                            user_id=subscription.user_id,
                            product_id=subscription.product_id,
                            quantity=subscription.quantity,
                            schedule=_dump(subscription.schedule.to_dict()),
                            slot_id=subscription.slot_id,
                            status=subscription.status.value,
                            start_date=subscription.start_date,
                            idempotency_key=idempotency_key,
                        )
                    )
                    if same_day_delivery_date is not None:
                        conn.execute(
                            subscription_run_log_table.insert().values(
                                subscription_id=subscription.id,
                                delivery_date=same_day_delivery_date,
                            )
                        )
                        conn.execute(
                            outbox_table.insert().values(
                                aggregate_id=subscription.id,
                                event_type=outbox_event_type,
                                payload=_dump(outbox_payload),
                            )
                        )
                return subscription, True
            except IntegrityError:
                # The caller already checked get_by_idempotency_key before
                # calling — this is only the narrow concurrent-request
                # race window, not the common replay path.
                existing = self.get_by_idempotency_key(subscription.user_id, idempotency_key)
                if existing is None:
                    raise  # a different UNIQUE violation (e.g. PK collision) — re-raise
                return existing, False

    # --- reads ---

    def get_by_id(self, subscription_id: str) -> Subscription | None:
        with self._db_operation("get_by_id", "Failed to load subscription"):
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(subscriptions_table).where(subscriptions_table.c.id == subscription_id)
                ).fetchone()
        return None if row is None else _row_to_subscription(row)

    def list_by_user(self, user_id: str) -> list[Subscription]:
        with self._db_operation("list_by_user", "Failed to load subscriptions"):
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(subscriptions_table)
                    .where(subscriptions_table.c.user_id == user_id)
                    .order_by(subscriptions_table.c.created_at)
                ).fetchall()
        return [_row_to_subscription(r) for r in rows]

    def list_active(self) -> list[Subscription]:
        with self._db_operation("list_active", "Failed to load active subscriptions"):
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(subscriptions_table).where(
                        subscriptions_table.c.status == SubscriptionStatus.ACTIVE.value
                    )
                ).fetchall()
        return [_row_to_subscription(r) for r in rows]

    # --- lifecycle writes ---

    def update_status(self, subscription_id: str, status: SubscriptionStatus) -> Subscription:
        with self._db_operation("update_status", "Failed to update subscription"):
            with self._engine.begin() as conn:
                conn.execute(
                    subscriptions_table.update()
                    .where(subscriptions_table.c.id == subscription_id)
                    .values(status=status.value, updated_at=func.now())
                )
        return self.get_by_id(subscription_id)

    def update_pause(
        self,
        subscription_id: str,
        *,
        pause_from: date | None,
        pause_until: date | None,
        status: SubscriptionStatus,
    ) -> Subscription:
        with self._db_operation("update_pause", "Failed to update subscription"):
            with self._engine.begin() as conn:
                conn.execute(
                    subscriptions_table.update()
                    .where(subscriptions_table.c.id == subscription_id)
                    .values(
                        pause_from=pause_from,
                        pause_until=pause_until,
                        status=status.value,
                        updated_at=func.now(),
                    )
                )
        return self.get_by_id(subscription_id)

    def apply_edit_now(
        self, subscription_id: str, quantity: int, schedule: Schedule
    ) -> Subscription:
        with self._db_operation("apply_edit_now", "Failed to update subscription"):
            with self._engine.begin() as conn:
                conn.execute(
                    subscriptions_table.update()
                    .where(subscriptions_table.c.id == subscription_id)
                    .values(
                        quantity=quantity,
                        schedule=_dump(schedule.to_dict()),
                        pending_edit=None,
                        updated_at=func.now(),
                    )
                )
        return self.get_by_id(subscription_id)

    def set_pending_edit(self, subscription_id: str, pending_edit: PendingEdit) -> Subscription:
        with self._db_operation("set_pending_edit", "Failed to update subscription"):
            with self._engine.begin() as conn:
                conn.execute(
                    subscriptions_table.update()
                    .where(subscriptions_table.c.id == subscription_id)
                    .values(pending_edit=_dump(pending_edit.to_dict()), updated_at=func.now())
                )
        return self.get_by_id(subscription_id)

    def apply_pending_edit(
        self, subscription_id: str, quantity: int, schedule: Schedule
    ) -> Subscription:
        return self.apply_edit_now(subscription_id, quantity, schedule)

    # --- skips ---

    def insert_skip(self, subscription_id: str, skip_date: date) -> None:
        with self._db_operation("insert_skip", "Failed to record skip"):
            with self._engine.begin() as conn:
                conn.execute(
                    subscription_skips_table.insert().values(
                        subscription_id=subscription_id, skipped_date=skip_date
                    )
                )

    def list_skip_dates(self, subscription_id: str) -> set[date]:
        with self._db_operation("list_skip_dates", "Failed to load skips"):
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(subscription_skips_table.c.skipped_date).where(
                        subscription_skips_table.c.subscription_id == subscription_id
                    )
                ).fetchall()
        return {r.skipped_date for r in rows}

    def list_logged_dates(self, subscription_id: str) -> set[date]:
        with self._db_operation("list_logged_dates", "Failed to load run log"):
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(subscription_run_log_table.c.delivery_date).where(
                        subscription_run_log_table.c.subscription_id == subscription_id
                    )
                ).fetchall()
        return {r.delivery_date for r in rows}

    # --- Daily Run ---

    def list_logged_subscription_ids(self, delivery_date: date) -> set[str]:
        with self._db_operation("list_logged_subscription_ids", "Failed to load run log"):
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(subscription_run_log_table.c.subscription_id).where(
                        subscription_run_log_table.c.delivery_date == delivery_date
                    )
                ).fetchall()
        return {r.subscription_id for r in rows}

    def insert_run_log_and_enqueue(
        self,
        *,
        subscription_id: str,
        delivery_date: date,
        outbox_event_type: str,
        outbox_payload: dict,
    ) -> bool:
        with self._db_operation("insert_run_log_and_enqueue", "Failed to record due date"):
            try:
                with self._engine.begin() as conn:
                    conn.execute(
                        subscription_run_log_table.insert().values(
                            subscription_id=subscription_id, delivery_date=delivery_date
                        )
                    )
                    conn.execute(
                        outbox_table.insert().values(
                            aggregate_id=subscription_id,
                            event_type=outbox_event_type,
                            payload=_dump(outbox_payload),
                        )
                    )
                return True
            except IntegrityError:
                # subscription_run_log's UNIQUE(subscription_id, delivery_date)
                # already has this pair — a duplicate Scheduler invocation
                # (FR-8 idempotency). No second outbox row.
                return False

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
        return [{"id": r.id, "event_type": r.event_type, "payload": _load(r.payload)} for r in rows]

    def mark_published(self, outbox_id: int) -> None:
        with self._db_operation("mark_published", "Failed to mark outbox row"):
            with self._engine.begin() as conn:
                conn.execute(
                    outbox_table.update()
                    .where(outbox_table.c.id == outbox_id)
                    .values(published_at=func.now())
                )


def _row_to_subscription(row) -> Subscription:
    return Subscription(
        id=row.id,
        user_id=row.user_id,
        product_id=row.product_id,
        quantity=int(row.quantity),
        schedule=Schedule.from_dict(_load(row.schedule)),
        slot_id=row.slot_id,
        status=SubscriptionStatus(row.status),
        start_date=row.start_date,
        pause_from=row.pause_from,
        pause_until=row.pause_until,
        pending_edit=PendingEdit.from_dict(_load(row.pending_edit)) if row.pending_edit else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
