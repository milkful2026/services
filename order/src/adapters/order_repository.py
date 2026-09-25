"""SQLAlchemy Core repository for `orders` / `order_items` / `checkouts` /
`outbox`.

SQLAlchemy Core only (mirrors wallet/subscription/catalog/inventory) —
the same Table definitions run against Postgres (production) and an
in-memory SQLite engine (tests). Table columns are kept column-for-column
compatible with migrations/0001_orders.sql + 0002_checkout.sql by hand.
"""

import base64
import json
import uuid
from datetime import date, datetime

from shared.adapters.db_operation import SqlAlchemyOperationMixin
from shared.adapters.json_column import JSONColumn
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from domain.checkout_models import (
    Checkout,
    CheckoutLine,
    CheckoutStatus,
    CheckoutStep,
    SubscriptionLineResult,
)
from domain.exceptions import CheckoutInProgressError, ServiceUnavailableError
from domain.models import Order, OrderItem, OrderSource, OrdersPage, OrderStatus

metadata = MetaData()


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
    # Nullable since MA-136: a CHECKOUT order has no subscription and keeps
    # its lines in order_items (see the CHECK constraint below).
    Column("subscription_id", String(64), nullable=True),
    Column("product_id", String(64), nullable=True),
    Column("quantity", Integer, nullable=True),
    Column("amount_paise", BigInteger, nullable=False),
    Column("delivery_date", Date, nullable=False),
    Column("status", String(16), nullable=False, default=OrderStatus.CREATED.value),
    Column("failure_reason", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("confirmed_at", DateTime(timezone=True), nullable=True),
    Column(
        "source", String(16), nullable=False, default=OrderSource.SUBSCRIPTION.value,
        server_default=OrderSource.SUBSCRIPTION.value,
    ),
    Column("checkout_id", String(64), nullable=True, unique=True),
    UniqueConstraint(
        "subscription_id", "delivery_date", name="uq_orders_subscription_delivery_date"
    ),
    CheckConstraint(
        "(source = 'SUBSCRIPTION' AND subscription_id IS NOT NULL"
        " AND product_id IS NOT NULL AND quantity IS NOT NULL)"
        " OR (source = 'CHECKOUT' AND checkout_id IS NOT NULL AND subscription_id IS NULL)",
        name="orders_source_shape",
    ),
)

order_items_table = Table(
    "order_items",
    metadata,
    Column("order_id", String(64), ForeignKey("orders.id"), primary_key=True),
    Column("line_no", Integer, primary_key=True),
    Column("product_id", String(64), nullable=False),
    Column("quantity", Integer, nullable=False),
)

checkouts_table = Table(
    "checkouts",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("user_id", String(64), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("cart_version", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("step", String(24), nullable=False),
    Column("lines", JSONColumn(), nullable=False),
    Column("pay_now_paise", BigInteger, nullable=False),
    Column("delivery_date", Date, nullable=False),
    Column("order_id", String(64), nullable=True),
    Column("subscription_results", JSONColumn(), nullable=False),
    Column("result", JSONColumn(), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("user_id", "idempotency_key", name="uq_checkouts_user_key"),
    # At most one live checkout per user — both dialects support partial
    # unique indexes, so SQLite tests exercise the same guard as Postgres.
    Index(
        "checkouts_one_live_per_user",
        "user_id",
        unique=True,
        sqlite_where=text("status = 'IN_PROGRESS'"),
        postgresql_where=text("status = 'IN_PROGRESS'"),
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
    Column("payload", JSONColumn(), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


def create_schema(engine: Engine) -> None:
    """Test-only convenience — production schema ownership is the raw SQL
    migration file, not this."""
    metadata.create_all(engine)


class SqlAlchemyOrderRepository(SqlAlchemyOperationMixin):
    _unavailable_error = ServiceUnavailableError
    _log_prefix = "order_repository"

    def __init__(self, engine: Engine, correlation_id: str = "") -> None:
        self._engine = engine
        self._correlation_id = correlation_id

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
                if row is None:
                    return None
                items = self._load_items(conn, [row.id]).get(row.id, [])
        return _row_to_order(row, items)

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
                checkout_ids = [
                    r.id for r in rows if r.source == OrderSource.CHECKOUT.value
                ]
                items = self._load_items(conn, checkout_ids) if checkout_ids else {}
        next_cursor = encode_cursor(rows[-1].seq) if (has_more and rows) else None
        return OrdersPage(
            items=[_row_to_order(r, items.get(r.id, [])) for r in rows],
            next_cursor=next_cursor,
        )

    @staticmethod
    def _load_items(conn, order_ids: list[str]) -> dict[str, list[OrderItem]]:
        rows = conn.execute(
            select(order_items_table)
            .where(order_items_table.c.order_id.in_(order_ids))
            .order_by(order_items_table.c.order_id, order_items_table.c.line_no)
        ).fetchall()
        items: dict[str, list[OrderItem]] = {}
        for row in rows:
            items.setdefault(row.order_id, []).append(
                OrderItem(product_id=row.product_id, quantity=int(row.quantity))
            )
        return items

    # --- MA-136: checkouts ---

    def get_checkout(self, user_id: str, idempotency_key: str) -> Checkout | None:
        with self._db_operation("get_checkout", "Failed to load checkout"):
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(checkouts_table).where(
                        checkouts_table.c.user_id == user_id,
                        checkouts_table.c.idempotency_key == idempotency_key,
                    )
                ).fetchone()
        return None if row is None else _row_to_checkout(row)

    def get_live_checkout(self, user_id: str) -> Checkout | None:
        with self._db_operation("get_live_checkout", "Failed to load checkout"):
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(checkouts_table).where(
                        checkouts_table.c.user_id == user_id,
                        checkouts_table.c.status == CheckoutStatus.IN_PROGRESS.value,
                    )
                ).fetchone()
        return None if row is None else _row_to_checkout(row)

    def start_checkout(self, checkout: Checkout, order: Order | None) -> Checkout:
        """One transaction: the IN_PROGRESS checkout row, plus — when there
        are one-time lines — its CREATED order and order_items. A race on
        the same (user, key) returns the winner's row (the caller resumes
        it); a race against a *different* live checkout for this user
        raises CheckoutInProgressError."""
        with self._db_operation("start_checkout", "Failed to start checkout"):
            try:
                with self._engine.begin() as conn:
                    conn.execute(
                        checkouts_table.insert().values(
                            id=checkout.id,
                            user_id=checkout.user_id,
                            idempotency_key=checkout.idempotency_key,
                            cart_version=checkout.cart_version,
                            status=checkout.status.value,
                            step=checkout.step.value,
                            lines=[line.to_dict() for line in checkout.lines],
                            pay_now_paise=checkout.pay_now_paise,
                            delivery_date=checkout.delivery_date,
                            order_id=checkout.order_id,
                            subscription_results=[],
                            result=None,
                        )
                    )
                    if order is not None:
                        conn.execute(
                            orders_table.insert().values(
                                id=order.id,
                                user_id=order.user_id,
                                subscription_id=None,
                                product_id=None,
                                quantity=None,
                                amount_paise=order.amount_paise,
                                delivery_date=order.delivery_date,
                                status=OrderStatus.CREATED.value,
                                source=OrderSource.CHECKOUT.value,
                                checkout_id=checkout.id,
                            )
                        )
                        conn.execute(
                            order_items_table.insert(),
                            [
                                {
                                    "order_id": order.id,
                                    "line_no": i,
                                    "product_id": item.product_id,
                                    "quantity": item.quantity,
                                }
                                for i, item in enumerate(order.items)
                            ],
                        )
                return checkout
            except IntegrityError:
                same_key = self.get_checkout(checkout.user_id, checkout.idempotency_key)
                if same_key is not None:
                    return same_key
                raise CheckoutInProgressError(
                    "Another checkout is already in progress for this account"
                ) from None

    def update_checkout(
        self,
        checkout_id: str,
        *,
        step: CheckoutStep | None = None,
        status: CheckoutStatus | None = None,
        subscription_results: list[SubscriptionLineResult] | None = None,
        result: dict | None = None,
    ) -> None:
        values: dict = {"updated_at": func.now()}
        if step is not None:
            values["step"] = step.value
        if status is not None:
            values["status"] = status.value
        if subscription_results is not None:
            values["subscription_results"] = [r.to_dict() for r in subscription_results]
        if result is not None:
            values["result"] = result
        with self._db_operation("update_checkout", "Failed to update checkout"):
            with self._engine.begin() as conn:
                conn.execute(
                    checkouts_table.update()
                    .where(checkouts_table.c.id == checkout_id)
                    .values(**values)
                )

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


def _row_to_order(row, items: list[OrderItem] | None = None) -> Order:
    return Order(
        id=row.id,
        user_id=row.user_id,
        subscription_id=row.subscription_id,
        product_id=row.product_id,
        quantity=int(row.quantity) if row.quantity is not None else None,
        amount_paise=int(row.amount_paise),
        delivery_date=row.delivery_date,
        status=OrderStatus(row.status),
        failure_reason=row.failure_reason,
        created_at=row.created_at,
        confirmed_at=row.confirmed_at,
        source=OrderSource(row.source),
        checkout_id=row.checkout_id,
        items=items or [],
    )


def _row_to_checkout(row) -> Checkout:
    return Checkout(
        id=row.id,
        user_id=row.user_id,
        idempotency_key=row.idempotency_key,
        cart_version=int(row.cart_version),
        status=CheckoutStatus(row.status),
        step=CheckoutStep(row.step),
        lines=[CheckoutLine.from_dict(d) for d in row.lines],
        pay_now_paise=int(row.pay_now_paise),
        delivery_date=row.delivery_date,
        order_id=row.order_id,
        subscription_results=[
            SubscriptionLineResult.from_dict(d) for d in (row.subscription_results or [])
        ],
        result=row.result,
    )
