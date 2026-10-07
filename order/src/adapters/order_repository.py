"""SQLAlchemy Core repository for `orders` / `order_items` / `checkouts` /
`carried_subscription_keys` / `outbox`.

SQLAlchemy Core only (mirrors wallet/subscription/catalog/inventory) —
the same Table definitions run against Postgres (production) and an
in-memory SQLite engine (tests). Table columns are kept column-for-column
compatible with migrations/0001_orders.sql + 0002_checkout.sql +
0003_sweep.sql + 0004_customer_cancel.sql by hand.
"""

import base64
import json
import uuid
from datetime import UTC, date, datetime, timedelta

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
    case,
    func,
    select,
    text,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from domain.checkout_models import (
    TERMINAL_CHECKOUT_STATUSES,
    Checkout,
    CheckoutLine,
    CheckoutStatus,
    CheckoutStep,
    SubscriptionLineResult,
)
from domain.exceptions import CheckoutInProgressError, LeaseLostError, ServiceUnavailableError
from domain.models import (
    FAILURE_CUSTOMER_CANCELLED,
    FAILURE_CUTOFF_PASSED,
    FAILURE_SWEEP_EXHAUSTED,
    CancelReason,
    ChargeState,
    Order,
    OrderItem,
    OrderSource,
    OrdersPage,
    OrderStatus,
    RefundState,
)

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
    # MA-143 sweep lease / attempt bookkeeping (0003_sweep.sql).
    Column("sweep_attempts", Integer, nullable=False, default=0, server_default="0"),
    Column("claimed_until", DateTime(timezone=True), nullable=True),
    Column("claim_owner", String(64), nullable=True),
    Column("last_sweep_error", Text, nullable=True),
    Column("charge_state", String(16), nullable=True),
    # MA-154 customer cancel (0004_customer_cancel.sql).
    Column("cancel_reason", Text, nullable=True),
    Column("cancelled_at", DateTime(timezone=True), nullable=True),
    Column("refund_state", Text, nullable=True),
    Column("refunded_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint(
        "subscription_id", "delivery_date", name="uq_orders_subscription_delivery_date"
    ),
    CheckConstraint(
        "cancel_reason IN ('ORDERED_BY_MISTAKE', 'NOT_HOME', 'CHANGED_MIND', 'OTHER')",
        name="orders_cancel_reason_check",
    ),
    CheckConstraint(
        "refund_state IN ('PENDING', 'REFUNDED', 'NOT_REQUIRED')",
        name="orders_refund_state_check",
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
    # MA-143 sweep lease / attempt bookkeeping (0003_sweep.sql).
    Column("sweep_attempts", Integer, nullable=False, default=0, server_default="0"),
    Column("claimed_until", DateTime(timezone=True), nullable=True),
    Column("claim_owner", String(64), nullable=True),
    Column("last_sweep_error", Text, nullable=True),
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

# MA-144 FR-4a (PD-2): a subscription create whose outcome is unknown,
# carried to the next checkout of the same cart line (0003_sweep.sql).
carried_subscription_keys_table = Table(
    "carried_subscription_keys",
    metadata,
    Column("user_id", String(64), primary_key=True),
    Column("cart_line_id", String(64), primary_key=True),
    Column("idempotency_key", String(128), nullable=False),
    Column("checkout_id", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
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
                    .values(
                        status=OrderStatus.CONFIRMED.value,
                        confirmed_at=confirmed_at,
                        **_LEASE_CLEARED,
                    )
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
                    .values(
                        status=OrderStatus.PAYMENT_FAILED.value,
                        failure_reason=failure_reason,
                        **_LEASE_CLEARED,
                    )
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

    def get_checkout_by_id(self, checkout_id: str) -> Checkout | None:
        with self._db_operation("get_checkout_by_id", "Failed to load checkout"):
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(checkouts_table).where(checkouts_table.c.id == checkout_id)
                ).fetchone()
        return None if row is None else _row_to_checkout(row)

    def start_checkout(
        self,
        checkout: Checkout,
        order: Order | None,
        *,
        claim_owner: str | None = None,
        lease_seconds: float = 0,
    ) -> Checkout:
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
                            # MA-144: the starting request holds the lease.
                            claim_owner=claim_owner,
                            claimed_until=(
                                self._db_now_plus(lease_seconds) if claim_owner else None
                            ),
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
                live = self.get_live_checkout(checkout.user_id)
                raise CheckoutInProgressError(
                    "Another checkout is already in progress for this account",
                    {"checkoutId": live.id} if live is not None else None,
                ) from None

    def update_checkout(
        self,
        checkout_id: str,
        *,
        step: CheckoutStep | None = None,
        status: CheckoutStatus | None = None,
        subscription_results: list[SubscriptionLineResult] | None = None,
        result: dict | None = None,
        user_id: str | None = None,
        carry_keys: dict[str, str] | None = None,
        forget_line_ids: list[str] | None = None,
    ) -> None:
        """`carry_keys` ({lineId: idempotency key}) and `forget_line_ids`
        (MA-144 FR-4a) change `user_id`'s carried subscription keys in the
        same transaction as the checkout update: carried with the PD-2
        FAILED results, forgotten with a line's definitive result."""
        values: dict = {"updated_at": func.now()}
        if step is not None:
            values["step"] = step.value
        if status is not None:
            values["status"] = status.value
            if status in TERMINAL_CHECKOUT_STATUSES:
                values.update(_LEASE_CLEARED)  # MA-144: a finished checkout holds nothing
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
                for line_id, key in (carry_keys or {}).items():
                    # ON CONFLICT DO NOTHING: a line that fails again keeps
                    # the key its first attempt used.
                    conn.execute(
                        self._insert(carried_subscription_keys_table)
                        .values(
                            user_id=user_id,
                            cart_line_id=line_id,
                            idempotency_key=key,
                            checkout_id=checkout_id,
                        )
                        .on_conflict_do_nothing(index_elements=["user_id", "cart_line_id"])
                    )
                if forget_line_ids:
                    conn.execute(
                        carried_subscription_keys_table.delete().where(
                            carried_subscription_keys_table.c.user_id == user_id,
                            carried_subscription_keys_table.c.cart_line_id.in_(forget_line_ids),
                        )
                    )

    def get_carried_keys(self, user_id: str) -> dict[str, tuple[str, str]]:
        """MA-144 FR-4a — {cart line id: (idempotency key, checkout id that
        carried it)} for `user_id`."""
        with self._db_operation("get_carried_keys", "Failed to load carried keys"):
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(carried_subscription_keys_table).where(
                        carried_subscription_keys_table.c.user_id == user_id
                    )
                ).fetchall()
        return {r.cart_line_id: (r.idempotency_key, r.checkout_id) for r in rows}

    def _insert(self, table):
        dialect = postgresql if self._engine.dialect.name == "postgresql" else sqlite
        return dialect.insert(table)

    # --- MA-143: sweep lease + selection ---
    #
    # Every lease timestamp comes from the database clock (never the app's),
    # so tasks with skewed clocks agree on whether a lease has expired.
    # None of these touch `updated_at`: a claim alone must not make a stuck
    # checkout look fresh to the next sweep run.

    def _db_now_plus(self, seconds: float):
        if self._engine.dialect.name == "sqlite":
            # SQLite has no interval type; datetime('now', '+N seconds') is UTC,
            # in the same 'YYYY-MM-DD HH:MM:SS' shape SQLAlchemy stores.
            return func.datetime("now", f"{seconds:+.0f} seconds")
        return func.now() + timedelta(seconds=seconds)

    def _lease_free(self, table):
        return table.c.claimed_until.is_(None) | (table.c.claimed_until < self._db_now_plus(0))

    def _claim(self, table, record_id: str, owner: str, lease_seconds: float, *where) -> bool:
        with self._db_operation("claim", "Failed to claim record"):
            with self._engine.begin() as conn:
                result = conn.execute(
                    table.update()
                    .where(table.c.id == record_id, self._lease_free(table), *where)
                    .values(claimed_until=self._db_now_plus(lease_seconds), claim_owner=owner)
                )
        return result.rowcount == 1

    def _renew(self, table, record_id: str, owner: str, lease_seconds: float) -> bool:
        with self._db_operation("renew", "Failed to renew lease"):
            with self._engine.begin() as conn:
                result = conn.execute(
                    table.update()
                    .where(table.c.id == record_id, table.c.claim_owner == owner)
                    .values(claimed_until=self._db_now_plus(lease_seconds))
                )
        return result.rowcount == 1

    def _release(self, table, record_id: str, owner: str) -> None:
        with self._db_operation("release", "Failed to release lease"):
            with self._engine.begin() as conn:
                conn.execute(
                    table.update()
                    .where(table.c.id == record_id, table.c.claim_owner == owner)
                    .values(**_LEASE_CLEARED)
                )

    def claim_order(self, order_id: str, owner: str, lease_seconds: float) -> bool:
        """Leases a CREATED order to `owner`; False if anyone else holds it."""
        return self._claim(
            orders_table,
            order_id,
            owner,
            lease_seconds,
            orders_table.c.status == OrderStatus.CREATED.value,
        )

    def renew_order(self, order_id: str, owner: str, lease_seconds: float) -> bool:
        return self._renew(orders_table, order_id, owner, lease_seconds)

    def release_order(self, order_id: str, owner: str) -> None:
        self._release(orders_table, order_id, owner)

    def list_stale_subscription_orders(self, older_than_seconds: float, limit: int) -> list[str]:
        with self._db_operation("list_stale_subscription_orders", "Failed to list orders"):
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(orders_table.c.id)
                    .where(
                        orders_table.c.source == OrderSource.SUBSCRIPTION.value,
                        orders_table.c.status == OrderStatus.CREATED.value,
                        orders_table.c.created_at < self._db_now_plus(-older_than_seconds),
                        self._lease_free(orders_table),
                    )
                    .order_by(orders_table.c.created_at)
                    .limit(limit)
                ).fetchall()
        return [r.id for r in rows]

    def record_order_sweep_failure(
        self, order_id: str, owner: str, error_code: str, max_attempts: int
    ) -> bool:
        """One failed sweep attempt: +1 attempt, remember why, release the
        lease, and escalate to NEEDS_ATTENTION(SWEEP_EXHAUSTED) in the same
        update if that reaches `max_attempts` — with charge_state UNKNOWN,
        since no void succeeded (the settle pass resolves it). Returns
        whether it escalated."""
        exhausted = orders_table.c.sweep_attempts + 1 >= max_attempts
        with self._db_operation("record_order_sweep_failure", "Failed to record attempt"):
            with self._engine.begin() as conn:
                conn.execute(
                    orders_table.update()
                    .where(
                        orders_table.c.id == order_id,
                        orders_table.c.claim_owner == owner,
                        orders_table.c.status == OrderStatus.CREATED.value,
                    )
                    .values(
                        sweep_attempts=orders_table.c.sweep_attempts + 1,
                        last_sweep_error=error_code,
                        status=case(
                            (exhausted, OrderStatus.NEEDS_ATTENTION.value),
                            else_=orders_table.c.status,
                        ),
                        failure_reason=case(
                            (exhausted, FAILURE_SWEEP_EXHAUSTED),
                            else_=orders_table.c.failure_reason,
                        ),
                        charge_state=case(
                            (exhausted, ChargeState.UNKNOWN.value),
                            else_=orders_table.c.charge_state,
                        ),
                        **_LEASE_CLEARED,
                    )
                )
                status = conn.execute(
                    select(orders_table.c.status).where(orders_table.c.id == order_id)
                ).scalar_one()
        return status == OrderStatus.NEEDS_ATTENTION.value

    def close_order(self, order_id: str, owner: str, reason: str) -> bool:
        """MA-143 FR-4a — CREATED -> NEEDS_ATTENTION(reason), NOT_CHARGED;
        releases the lease. Only after Wallet voided the order."""
        with self._db_operation("close_order", "Failed to close order"):
            with self._engine.begin() as conn:
                result = conn.execute(
                    orders_table.update()
                    .where(
                        orders_table.c.id == order_id,
                        orders_table.c.claim_owner == owner,
                        orders_table.c.status == OrderStatus.CREATED.value,
                    )
                    .values(
                        status=OrderStatus.NEEDS_ATTENTION.value,
                        failure_reason=reason,
                        charge_state=ChargeState.NOT_CHARGED.value,
                        **_LEASE_CLEARED,
                    )
                )
        return result.rowcount == 1

    # --- MA-143 FR-4b: charge settle pass ---

    def list_unknown_charge_orders(self, limit: int) -> list[str]:
        with self._db_operation("list_unknown_charge_orders", "Failed to list orders"):
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(orders_table.c.id)
                    .where(
                        orders_table.c.status == OrderStatus.NEEDS_ATTENTION.value,
                        orders_table.c.charge_state == ChargeState.UNKNOWN.value,
                        self._lease_free(orders_table),
                    )
                    .order_by(orders_table.c.created_at)
                    .limit(limit)
                ).fetchall()
        return [r.id for r in rows]

    def claim_unknown_charge_order(self, order_id: str, owner: str, lease_seconds: float) -> bool:
        return self._claim(
            orders_table,
            order_id,
            owner,
            lease_seconds,
            orders_table.c.status == OrderStatus.NEEDS_ATTENTION.value,
            orders_table.c.charge_state == ChargeState.UNKNOWN.value,
        )

    def settle_charge_state(self, order_id: str, owner: str, charge_state: ChargeState) -> bool:
        """UNKNOWN -> what Wallet said; releases the lease. `status` never
        changes here."""
        with self._db_operation("settle_charge_state", "Failed to settle charge"):
            with self._engine.begin() as conn:
                result = conn.execute(
                    orders_table.update()
                    .where(
                        orders_table.c.id == order_id,
                        orders_table.c.claim_owner == owner,
                        orders_table.c.charge_state == ChargeState.UNKNOWN.value,
                    )
                    .values(charge_state=charge_state.value, **_LEASE_CLEARED)
                )
        return result.rowcount == 1

    # --- MA-154: customer cancel + refund ---

    def cancel_by_customer(
        self,
        order_id: str,
        *,
        reason: CancelReason | None,
        now: datetime,
        refund_state: RefundState,
        outbox_payload: dict,
    ) -> bool:
        """FR-3 Step A — CONFIRMED -> CANCELLED(CUSTOMER_CANCELLED) plus the
        OrderCancelled outbox row, in one transaction. False (nothing
        written) when the order wasn't CONFIRMED any more: a concurrent cancel
        or another transition won, and the caller re-reads."""
        with self._db_operation("cancel_by_customer", "Failed to cancel order"):
            with self._engine.begin() as conn:
                result = conn.execute(
                    orders_table.update()
                    .where(
                        orders_table.c.id == order_id,
                        orders_table.c.status == OrderStatus.CONFIRMED.value,
                    )
                    .values(
                        status=OrderStatus.CANCELLED.value,
                        failure_reason=FAILURE_CUSTOMER_CANCELLED,
                        cancel_reason=reason.value if reason else None,
                        cancelled_at=now.astimezone(UTC),
                        refund_state=refund_state.value,
                    )
                )
                if result.rowcount != 1:
                    return False
                conn.execute(
                    outbox_table.insert().values(
                        aggregate_id=order_id,
                        event_type="OrderCancelled",
                        payload=outbox_payload,
                    )
                )
        return True

    def mark_refund_state(
        self,
        order_id: str,
        state: RefundState,
        *,
        refunded_at: datetime | None = None,
        owner: str | None = None,
    ) -> bool:
        """PENDING -> `state`, and releases any lease. Conditional on PENDING,
        so the request and the sweep can both try it safely; with `owner`,
        only that lease holder's update counts."""
        where = [
            orders_table.c.id == order_id,
            orders_table.c.refund_state == RefundState.PENDING.value,
        ]
        if owner is not None:
            where.append(orders_table.c.claim_owner == owner)
        with self._db_operation("mark_refund_state", "Failed to record refund"):
            with self._engine.begin() as conn:
                result = conn.execute(
                    orders_table.update()
                    .where(*where)
                    .values(
                        refund_state=state.value,
                        refunded_at=refunded_at.astimezone(UTC) if refunded_at else None,
                        **_LEASE_CLEARED,
                    )
                )
        return result.rowcount == 1

    def list_pending_refunds(self, older_than_seconds: float, limit: int) -> list[str]:
        """FR-5 — PENDING refunds cancelled more than `older_than_seconds`
        ago (by the database clock), with a free lease, oldest first."""
        with self._db_operation("list_pending_refunds", "Failed to list refunds"):
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(orders_table.c.id)
                    .where(
                        orders_table.c.refund_state == RefundState.PENDING.value,
                        orders_table.c.cancelled_at < self._db_now_plus(-older_than_seconds),
                        self._lease_free(orders_table),
                    )
                    .order_by(orders_table.c.cancelled_at)
                    .limit(limit)
                ).fetchall()
        return [r.id for r in rows]

    def claim_pending_refund(self, order_id: str, owner: str, lease_seconds: float) -> bool:
        return self._claim(
            orders_table,
            order_id,
            owner,
            lease_seconds,
            orders_table.c.refund_state == RefundState.PENDING.value,
        )

    def oldest_pending_refund_at(self) -> datetime | None:
        """For the `order.refund.pending_age_seconds` metric (UTC)."""
        with self._db_operation("oldest_pending_refund_at", "Failed to read refunds"):
            with self._engine.connect() as conn:
                oldest = conn.execute(
                    select(func.min(orders_table.c.cancelled_at)).where(
                        orders_table.c.refund_state == RefundState.PENDING.value
                    )
                ).scalar_one()
        if oldest is None:
            return None
        if isinstance(oldest, str):  # SQLite's func.min returns the stored text
            oldest = datetime.fromisoformat(oldest)
        return oldest if oldest.tzinfo else oldest.replace(tzinfo=UTC)

    # --- MA-144: checkout lease + recovery ---

    def claim_checkout(self, checkout_id: str, owner: str, lease_seconds: float) -> bool:
        """Leases an IN_PROGRESS checkout to `owner`; False if anyone else holds it."""
        return self._claim(
            checkouts_table,
            checkout_id,
            owner,
            lease_seconds,
            checkouts_table.c.status == CheckoutStatus.IN_PROGRESS.value,
        )

    def renew_checkout(self, checkout_id: str, owner: str, lease_seconds: float) -> bool:
        return self._renew(checkouts_table, checkout_id, owner, lease_seconds)

    def release_checkout(self, checkout_id: str, owner: str) -> None:
        self._release(checkouts_table, checkout_id, owner)

    def lease_remaining_seconds(self, checkout_id: str) -> float | None:
        """For `retryAfterSeconds` only — a hint, so the app clock is fine here."""
        with self._db_operation("lease_remaining_seconds", "Failed to load checkout"):
            with self._engine.connect() as conn:
                claimed_until = conn.execute(
                    select(checkouts_table.c.claimed_until).where(
                        checkouts_table.c.id == checkout_id
                    )
                ).scalar_one_or_none()
        if claimed_until is None:
            return None
        if claimed_until.tzinfo is None:
            claimed_until = claimed_until.replace(tzinfo=UTC)  # SQLite drops the offset
        return (claimed_until - datetime.now(UTC)).total_seconds()

    def list_stale_checkouts(self, older_than_seconds: float, limit: int) -> list[str]:
        with self._db_operation("list_stale_checkouts", "Failed to list checkouts"):
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(checkouts_table.c.id)
                    .where(
                        checkouts_table.c.status == CheckoutStatus.IN_PROGRESS.value,
                        checkouts_table.c.updated_at < self._db_now_plus(-older_than_seconds),
                        self._lease_free(checkouts_table),
                    )
                    .order_by(checkouts_table.c.updated_at)
                    .limit(limit)
                ).fetchall()
        return [r.id for r in rows]

    def record_checkout_sweep_failure(self, checkout_id: str, owner: str, error_code: str) -> int:
        """+1 sweep attempt and the reason; returns the new count. Keeps the
        lease — the sweep decides next (release, escalate or finish), which
        depends on how far the checkout got."""
        with self._db_operation("record_checkout_sweep_failure", "Failed to record attempt"):
            with self._engine.begin() as conn:
                conn.execute(
                    checkouts_table.update()
                    .where(
                        checkouts_table.c.id == checkout_id,
                        checkouts_table.c.claim_owner == owner,
                    )
                    .values(
                        sweep_attempts=checkouts_table.c.sweep_attempts + 1,
                        last_sweep_error=error_code,
                    )
                )
                return int(
                    conn.execute(
                        select(checkouts_table.c.sweep_attempts).where(
                            checkouts_table.c.id == checkout_id
                        )
                    ).scalar_one()
                )

    def cancel_checkout_and_order(
        self, checkout_id: str, order_id: str, owner: str, result: dict
    ) -> None:
        """PD-1, one transaction: the never-charged order (voided in Wallet,
        or ₹0) -> CANCELLED, NOT_CHARGED, and the checkout -> CANCELLED
        (lease and one-live lock released). Either both
        change or neither: a guard that matches nothing raises LeaseLostError
        and rolls back."""
        with self._db_operation("cancel_checkout_and_order", "Failed to cancel checkout"):
            with self._engine.begin() as conn:
                order_rows = conn.execute(
                    orders_table.update()
                    .where(
                        orders_table.c.id == order_id,
                        orders_table.c.status == OrderStatus.CREATED.value,
                    )
                    .values(
                        status=OrderStatus.CANCELLED.value,
                        failure_reason=FAILURE_CUTOFF_PASSED,
                        charge_state=ChargeState.NOT_CHARGED.value,
                    )
                ).rowcount
                checkout_rows = conn.execute(
                    checkouts_table.update()
                    .where(
                        checkouts_table.c.id == checkout_id,
                        checkouts_table.c.claim_owner == owner,
                        checkouts_table.c.status == CheckoutStatus.IN_PROGRESS.value,
                    )
                    .values(
                        status=CheckoutStatus.CANCELLED.value,
                        result=result,
                        updated_at=func.now(),
                        **_LEASE_CLEARED,
                    )
                ).rowcount
                if order_rows != 1 or checkout_rows != 1:
                    raise LeaseLostError(
                        "Checkout or order changed underneath the cancel",
                        {"checkoutId": checkout_id},
                    )

    def escalate_checkout(
        self, checkout_id: str, owner: str, error_code: str, order_id: str | None
    ) -> None:
        """Checkout -> NEEDS_ATTENTION (lease and one-live lock released) and,
        if given and still unpaid, its order -> NEEDS_ATTENTION(SWEEP_EXHAUSTED)
        with charge_state UNKNOWN for the settle pass."""
        with self._db_operation("escalate_checkout", "Failed to escalate checkout"):
            with self._engine.begin() as conn:
                conn.execute(
                    checkouts_table.update()
                    .where(
                        checkouts_table.c.id == checkout_id,
                        checkouts_table.c.claim_owner == owner,
                        checkouts_table.c.status == CheckoutStatus.IN_PROGRESS.value,
                    )
                    .values(
                        status=CheckoutStatus.NEEDS_ATTENTION.value,
                        last_sweep_error=error_code,
                        updated_at=func.now(),
                        **_LEASE_CLEARED,
                    )
                )
                if order_id is not None:
                    conn.execute(
                        orders_table.update()
                        .where(
                            orders_table.c.id == order_id,
                            orders_table.c.status == OrderStatus.CREATED.value,
                        )
                        .values(
                            status=OrderStatus.NEEDS_ATTENTION.value,
                            failure_reason=FAILURE_SWEEP_EXHAUSTED,
                            charge_state=ChargeState.UNKNOWN.value,
                        )
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


_LEASE_CLEARED = {"claimed_until": None, "claim_owner": None}


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
        sweep_attempts=int(row.sweep_attempts or 0),
        claimed_until=row.claimed_until,
        claim_owner=row.claim_owner,
        last_sweep_error=row.last_sweep_error,
        charge_state=ChargeState(row.charge_state) if row.charge_state else None,
        cancel_reason=CancelReason(row.cancel_reason) if row.cancel_reason else None,
        cancelled_at=row.cancelled_at,
        refund_state=RefundState(row.refund_state) if row.refund_state else None,
        refunded_at=row.refunded_at,
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
        updated_at=row.updated_at,
        sweep_attempts=int(row.sweep_attempts or 0),
        claimed_until=row.claimed_until,
        claim_owner=row.claim_owner,
        last_sweep_error=row.last_sweep_error,
    )
