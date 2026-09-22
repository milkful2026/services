"""SQLAlchemy Core repository for `orders` / `outbox`.

SQLAlchemy Core only (mirrors wallet/subscription/catalog/inventory) —
the same Table definitions run against Postgres (production) and an
in-memory SQLite engine (tests). Table columns are kept column-for-column
compatible with migrations/0001_orders.sql by hand.
"""

import base64
import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    TypeDecorator,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from domain.exceptions import ServiceUnavailableError
from domain.models import Order, OrdersPage, OrderStatus

logger = logging.getLogger(__name__)

metadata = MetaData()


class _JSONColumn(TypeDecorator):
    """JSONB on Postgres, JSON-in-Text on SQLite — see subscription
    service's own `_JSONColumn` for why `with_variant(Text, "sqlite")`
    alone isn't enough (SQLite can't bind a raw dict into a plain `Text`
    column, and unconditional `json.dumps` would double-encode on
    Postgres's real JSONB)."""

    impl = JSONB
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(Text())
        return dialect.type_descriptor(JSONB())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return json.dumps(value) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return json.loads(value) if isinstance(value, str) else value


orders_table = Table(
    "orders",
    metadata,
    # `seq` (not `id`) backs keyset pagination — `id` is the business key
    # (an app-generated `ord_<uuid>`, not monotonic), same split
    # wallet/subscription use their own BIGSERIAL `id` for.
    Column(
        "seq", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    ),
    Column("id", String(64), nullable=False, unique=True),
    Column("user_id", String(64), nullable=False),
    Column("subscription_id", String(64), nullable=False),
    Column("product_id", String(64), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("amount_paise", BigInteger, nullable=False),
    Column("delivery_date", Date, nullable=False),
    Column("status", String(16), nullable=False, default=OrderStatus.CREATED.value),
    Column("failure_reason", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("confirmed_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint(
        "subscription_id", "delivery_date", name="uq_orders_subscription_delivery_date"
    ),
)

outbox_table = Table(
    "outbox",
    metadata,
    Column(
        "id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    ),
    Column("aggregate_id", String(64), nullable=False),
    Column("event_type", String(48), nullable=False),
    Column("payload", _JSONColumn(), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


def create_schema(engine: Engine) -> None:
    """Test-only convenience — production schema ownership is the raw SQL
    migration file, not this."""
    metadata.create_all(engine)


class SqlAlchemyOrderRepository:
    def __init__(self, engine: Engine, correlation_id: str = "") -> None:
        self._engine = engine
        self._correlation_id = correlation_id

    @contextmanager
    def _db_operation(self, operation: str, failure_message: str) -> Iterator[None]:
        try:
            yield
        except SQLAlchemyError as exc:
            logger.error(
                f"order_repository.{operation} failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ServiceUnavailableError(failure_message) from exc

    def get_by_subscription_and_date(
        self, subscription_id: str, delivery_date: date
    ) -> Order | None:
        with self._db_operation("get_by_subscription_and_date", "Failed to load order"):
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(orders_table).where(
                        orders_table.c.subscription_id == subscription_id,
                        orders_table.c.delivery_date == delivery_date,
                    )
                ).fetchone()
        return None if row is None else _row_to_order(row)

    def insert_created(self, order: Order) -> Order:
        with self._db_operation("insert_created", "Failed to create order"):
            try:
                with self._engine.begin() as conn:
                    conn.execute(
                        orders_table.insert().values(
                            id=order.id,
                            user_id=order.user_id,
                            subscription_id=order.subscription_id,
                            product_id=order.product_id,
                            quantity=order.quantity,
                            amount_paise=order.amount_paise,
                            delivery_date=order.delivery_date,
                            status=OrderStatus.CREATED.value,
                        )
                    )
                return order
            except IntegrityError:
                # Narrow race window: another consumer instance already
                # inserted this (subscription_id, delivery_date) between
                # this call's own get_by_subscription_and_date check and
                # this insert. Return the winner rather than raising —
                # materialize's caller resumes from there, same pattern
                # subscription's insert_if_absent uses.
                existing = self.get_by_subscription_and_date(
                    order.subscription_id, order.delivery_date
                )
                if existing is None:
                    raise
                return existing

    def mark_confirmed(
        self, order_id: str, confirmed_at: datetime, outbox_event_type: str, outbox_payload: dict
    ) -> Order:
        with self._db_operation("mark_confirmed", "Failed to confirm order"):
            with self._engine.begin() as conn:
                result = conn.execute(
                    orders_table.update()
                    .where(
                        orders_table.c.id == order_id,
                        orders_table.c.status == OrderStatus.CREATED.value,
                    )
                    .values(status=OrderStatus.CONFIRMED.value, confirmed_at=confirmed_at)
                )
                if result.rowcount:
                    # Only the caller that actually transitions
                    # CREATED -> CONFIRMED publishes the event — two
                    # concurrent redeliveries both resuming the same
                    # CREATED order (materialize's own race window) must
                    # not each publish an OrderConfirmed with a fresh
                    # eventId for an order the other one already
                    # confirmed.
                    conn.execute(
                        outbox_table.insert().values(
                            aggregate_id=order_id,
                            event_type=outbox_event_type,
                            payload=outbox_payload,
                        )
                    )
        return self.get(order_id)

    def mark_payment_failed(
        self,
        order_id: str,
        failure_reason: str,
        outbox_event_type: str,
        outbox_payload: dict,
    ) -> Order:
        with self._db_operation("mark_payment_failed", "Failed to fail order"):
            with self._engine.begin() as conn:
                result = conn.execute(
                    orders_table.update()
                    .where(
                        orders_table.c.id == order_id,
                        orders_table.c.status == OrderStatus.CREATED.value,
                    )
                    .values(status=OrderStatus.PAYMENT_FAILED.value, failure_reason=failure_reason)
                )
                if result.rowcount:
                    # Same concurrent-redelivery guard as mark_confirmed.
                    conn.execute(
                        outbox_table.insert().values(
                            aggregate_id=order_id,
                            event_type=outbox_event_type,
                            payload=outbox_payload,
                        )
                    )
        return self.get(order_id)

    def insert_payment_failed(
        self, order: Order, outbox_event_type: str, outbox_payload: dict
    ) -> Order:
        """Inserts a *terminal* PAYMENT_FAILED order and its outbox row
        atomically, in one transaction — for a pre-pricing failure
        (DELIVERY_ADDRESS_UNKNOWN/PRODUCT_UNAVAILABLE), always
        amount_paise=0. Unlike insert_created (which always starts a row
        at CREATED for the priced/debit path, later transitioned by
        mark_confirmed/mark_payment_failed), this never leaves a crash
        window where the row sits CREATED with no real amount — either
        both writes land together or neither does. That matters because
        materialize()'s crash-resume path (`existing.status == CREATED`)
        always resumes at the *debit* step; a CREATED row with
        amount_paise=0 left behind by a crash between a separate insert
        and a separate fail-payment call would resume by debiting 0
        paise, which Wallet's own validation rejects."""
        with self._db_operation("insert_payment_failed", "Failed to record order failure"):
            try:
                with self._engine.begin() as conn:
                    conn.execute(
                        orders_table.insert().values(
                            id=order.id,
                            user_id=order.user_id,
                            subscription_id=order.subscription_id,
                            product_id=order.product_id,
                            quantity=order.quantity,
                            amount_paise=order.amount_paise,
                            delivery_date=order.delivery_date,
                            status=OrderStatus.PAYMENT_FAILED.value,
                            failure_reason=order.failure_reason,
                        )
                    )
                    conn.execute(
                        outbox_table.insert().values(
                            aggregate_id=order.id,
                            event_type=outbox_event_type,
                            payload=outbox_payload,
                        )
                    )
                return order
            except IntegrityError:
                # Same narrow concurrent-redelivery race insert_created
                # guards against — the winner's atomic insert+outbox
                # already landed, so there's nothing further to do.
                existing = self.get_by_subscription_and_date(
                    order.subscription_id, order.delivery_date
                )
                if existing is None:
                    raise
                return existing

    def get(self, order_id: str) -> Order | None:
        with self._db_operation("get", "Failed to load order"):
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(orders_table).where(orders_table.c.id == order_id)
                ).fetchone()
        return None if row is None else _row_to_order(row)

    def list_for_user(
        self, user_id: str, subscription_id: str | None, limit: int, before_seq: int | None
    ) -> OrdersPage:
        with self._db_operation("list_for_user", "Failed to load orders"):
            stmt = select(orders_table).where(orders_table.c.user_id == user_id)
            if subscription_id is not None:
                stmt = stmt.where(orders_table.c.subscription_id == subscription_id)
            if before_seq is not None:
                stmt = stmt.where(orders_table.c.seq < before_seq)
            stmt = stmt.order_by(orders_table.c.seq.desc()).limit(limit + 1)
            with self._engine.connect() as conn:
                rows = conn.execute(stmt).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1].seq) if (has_more and rows) else None
        return OrdersPage(items=[_row_to_order(r) for r in rows], next_cursor=next_cursor)

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


def new_order_id() -> str:
    return f"ord_{uuid.uuid4().hex}"


def encode_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"seq": seq}).encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> int:
    raw = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
    return int(raw["seq"])


def _row_to_order(row) -> Order:
    return Order(
        id=row.id,
        user_id=row.user_id,
        subscription_id=row.subscription_id,
        product_id=row.product_id,
        quantity=int(row.quantity),
        amount_paise=int(row.amount_paise),
        delivery_date=row.delivery_date,
        status=OrderStatus(row.status),
        failure_reason=row.failure_reason,
        created_at=row.created_at,
        confirmed_at=row.confirmed_at,
    )
