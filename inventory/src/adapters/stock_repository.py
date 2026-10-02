"""SQLAlchemy Core repository for `stock` / `stock_batches` /
`reservations` / `inventory_audit_log` (MA-118 §7 / MA-119 §7).

SQLAlchemy Core only (mirrors zone_repository.py/wallet_repository.py) —
the same Table definitions run against Postgres (production) and an
in-memory SQLite engine (tests). Table columns are kept column-for-column
compatible with migrations/0002_inventory_stock.sql +
0003_inventory_audit_log.sql by hand.

Concurrency: every write below takes `SELECT ... FOR UPDATE` on the
`stock` row for `product_id` first, inside one transaction — mirrors
wallet/src/adapters/wallet_repository.py's `debit_for_order` lock-check-
write shape exactly (MA-118 §6/§9, implementation-plan §2 point 1). Where
a second row also needs locking (the `reservations` row, for commit/
release), the lock order is always stock-row-first, reservation-row-
second, kept consistent across every method here specifically to avoid a
lock-ordering deadlock between two calls that need both locks.

FIFO batch consumption (FR-7/FR-2's "drawn from the oldest-expiry batch
first"): `reserve()` only ever touches the aggregate `stock.reserved`
counter — the §7 schema has no reservation-to-batch allocation table, so
there is no durable way to record *which* batch a still-pending
reservation will draw from. Physical batch consumption (decrementing
`stock_batches.quantity`) therefore happens at `commit_reservation()`
time, oldest-expiry-first, when the reservation is actually fulfilled —
this is a documented implementation decision (not explicit in FR-2's
prose, which is write-at-reserve-time ambiguous given the schema it also
defines), flagged in the impl-plan PR description, not silently assumed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import (
    Column,
    Date,
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
    AvailableFloorViolationError,
    InsufficientStockError,
    OnHandFloorViolationError,
    ProductNotFoundError,
    ReservationNotFoundError,
    ServiceUnavailableError,
)
from domain.models import (
    AuditLogEntry,
    Page,
    Reservation,
    ReservationStatus,
    Stock,
    StockBatch,
    StockState,
    StockSummary,
)
from shared.adapters.db_operation import SqlAlchemyOperationMixin

metadata = MetaData()

stock_table = Table(
    "stock",
    metadata,
    Column("product_id", String(64), primary_key=True),
    Column("on_hand", Integer, nullable=False, default=0),
    Column("reserved", Integer, nullable=False, default=0),
    Column("low_stock_threshold", Integer, nullable=False, default=10),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    ),
)

stock_batches_table = Table(
    "stock_batches",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("product_id", String(64), ForeignKey("stock.product_id"), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("expiry_date", Date, nullable=True),
    Column("available_from", Date, nullable=True),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

reservations_table = Table(
    "reservations",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("product_id", String(64), ForeignKey("stock.product_id"), nullable=False),
    Column("order_ref", Text, nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)

inventory_audit_log_table = Table(
    "inventory_audit_log",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("product_id", String(64), nullable=False),
    Column("admin_id", Text, nullable=False),
    Column("previous_quantity", Integer, nullable=False),
    Column("new_quantity", Integer, nullable=False),
    Column("adjustment", Integer, nullable=False),
    Column("reason", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


def create_schema(engine: Engine) -> None:
    """Test-only convenience — production schema ownership is the raw SQL
    migration files, not this."""
    metadata.create_all(engine)


def new_batch_id() -> str:
    return str(uuid.uuid4())


def new_reservation_id() -> str:
    return str(uuid.uuid4())


def new_audit_id() -> str:
    return str(uuid.uuid4())


class SqlAlchemyStockRepository(SqlAlchemyOperationMixin):
    _unavailable_error = ServiceUnavailableError
    _log_prefix = "stock_repository"

    def __init__(self, engine: Engine, correlation_id: str = "") -> None:
        self._engine = engine
        self._correlation_id = correlation_id

    # --- FR-8 / provisioning ----------------------------------------

    def provision_stock_if_absent(self, product_id: str) -> bool:
        with self._db_operation("provision_stock_if_absent", "Failed to provision stock row"):
            with self._engine.begin() as conn:
                existing = conn.execute(
                    select(stock_table.c.product_id).where(
                        stock_table.c.product_id == product_id
                    )
                ).fetchone()
                if existing is not None:
                    return False
                try:
                    conn.execute(
                        stock_table.insert().values(
                            product_id=product_id, on_hand=0, reserved=0
                        )
                    )
                    return True
                except IntegrityError:
                    # A concurrent provision for the same product_id won
                    # the race between our existence check and insert —
                    # same no-op-on-conflict outcome either way.
                    return False

    # --- FR-1: read ---------------------------------------------------

    def get_stock_with_next_batch(
        self, product_id: str
    ) -> tuple[Stock, StockBatch | None] | None:
        with self._db_operation("get_stock_with_next_batch", "Failed to load stock"):
            with self._engine.connect() as conn:
                stock_row = conn.execute(
                    select(stock_table).where(stock_table.c.product_id == product_id)
                ).fetchone()
                if stock_row is None:
                    return None
                stock = _row_to_stock(stock_row)
                next_batch_row = None
                if stock.available <= 0:
                    next_batch_row = conn.execute(
                        select(stock_batches_table)
                        .where(
                            stock_batches_table.c.product_id == product_id,
                            stock_batches_table.c.available_from.is_not(None),
                            stock_batches_table.c.quantity > 0,
                        )
                        .order_by(stock_batches_table.c.available_from)
                        .limit(1)
                    ).fetchone()
        next_batch = _row_to_batch(next_batch_row) if next_batch_row is not None else None
        return stock, next_batch

    # --- FR-2/FR-3/FR-4: reserve/commit/release ------------------------

    def reserve(
        self, product_id: str, order_ref: str, quantity: int, ttl_seconds: int
    ) -> tuple[Reservation, bool]:
        with self._db_operation("reserve", "Failed to reserve stock"):
            try:
                with self._engine.begin() as conn:
                    stock_row = conn.execute(
                        select(stock_table)
                        .where(stock_table.c.product_id == product_id)
                        .with_for_update()
                    ).fetchone()
                    if stock_row is None:
                        raise ProductNotFoundError(f"Unknown product {product_id!r}")
                    stock = _row_to_stock(stock_row)

                    existing = conn.execute(
                        select(reservations_table).where(
                            reservations_table.c.product_id == product_id,
                            reservations_table.c.order_ref == order_ref,
                        )
                    ).fetchone()
                    if existing is not None:
                        # FR-2 idempotency: a retried reserve for a key
                        # that already resolved (RESERVED/COMMITTED/
                        # RELEASED) returns the existing row unchanged —
                        # `available` is never re-decremented.
                        return _row_to_reservation(existing), False

                    if quantity > stock.available:
                        raise InsufficientStockError(
                            f"Requested {quantity}, only {stock.available} available",
                            {"productId": product_id, "requested": quantity,
                             "available": stock.available},
                        )

                    now = datetime.now(UTC)
                    reservation_id = new_reservation_id()
                    conn.execute(
                        reservations_table.insert().values(
                            id=reservation_id,
                            product_id=product_id,
                            order_ref=order_ref,
                            quantity=quantity,
                            status=ReservationStatus.RESERVED.value,
                            expires_at=_add_seconds(now, ttl_seconds),
                        )
                    )
                    conn.execute(
                        stock_table.update()
                        .where(stock_table.c.product_id == product_id)
                        .values(reserved=stock.reserved + quantity, updated_at=func.now())
                    )
                    created = conn.execute(
                        select(reservations_table).where(
                            reservations_table.c.id == reservation_id
                        )
                    ).one()
                    return _row_to_reservation(created), True
            except IntegrityError:
                # The reservations(product_id, order_ref) unique index
                # caught a race between two concurrent first-time reserve
                # calls for the same key (mirrors wallet_repository.py's
                # debit_for_order IntegrityError-replay handling) — the
                # loser re-reads the winner's row instead of erroring.
                with self._engine.connect() as conn:
                    winner = conn.execute(
                        select(reservations_table).where(
                            reservations_table.c.product_id == product_id,
                            reservations_table.c.order_ref == order_ref,
                        )
                    ).fetchone()
                if winner is None:
                    raise
                return _row_to_reservation(winner), False

    def commit_reservation(self, product_id: str, order_ref: str) -> Reservation:
        with self._db_operation("commit_reservation", "Failed to commit reservation"):
            with self._engine.begin() as conn:
                stock_row = conn.execute(
                    select(stock_table)
                    .where(stock_table.c.product_id == product_id)
                    .with_for_update()
                ).fetchone()
                if stock_row is None:
                    raise ProductNotFoundError(f"Unknown product {product_id!r}")
                stock = _row_to_stock(stock_row)

                reservation_row = conn.execute(
                    select(reservations_table)
                    .where(
                        reservations_table.c.product_id == product_id,
                        reservations_table.c.order_ref == order_ref,
                    )
                    .with_for_update()
                ).fetchone()
                if reservation_row is None:
                    raise ReservationNotFoundError(
                        f"No reservation for product {product_id!r}, order {order_ref!r}"
                    )
                reservation = _row_to_reservation(reservation_row)
                if reservation.status != ReservationStatus.RESERVED:
                    # FR-3: already-terminal (COMMITTED or RELEASED) is an
                    # idempotent no-op, not an error.
                    return reservation

                conn.execute(
                    stock_table.update()
                    .where(stock_table.c.product_id == product_id)
                    .values(
                        on_hand=stock.on_hand - reservation.quantity,
                        reserved=stock.reserved - reservation.quantity,
                        updated_at=func.now(),
                    )
                )
                conn.execute(
                    reservations_table.update()
                    .where(reservations_table.c.id == reservation.id)
                    .values(status=ReservationStatus.COMMITTED.value)
                )
                self._consume_batches_fifo(conn, product_id, reservation.quantity)
                reservation.status = ReservationStatus.COMMITTED
                return reservation

    def release_reservation(self, product_id: str, order_ref: str) -> Reservation:
        with self._db_operation("release_reservation", "Failed to release reservation"):
            with self._engine.begin() as conn:
                stock_row = conn.execute(
                    select(stock_table)
                    .where(stock_table.c.product_id == product_id)
                    .with_for_update()
                ).fetchone()
                if stock_row is None:
                    raise ProductNotFoundError(f"Unknown product {product_id!r}")
                stock = _row_to_stock(stock_row)

                reservation_row = conn.execute(
                    select(reservations_table)
                    .where(
                        reservations_table.c.product_id == product_id,
                        reservations_table.c.order_ref == order_ref,
                    )
                    .with_for_update()
                ).fetchone()
                if reservation_row is None:
                    raise ReservationNotFoundError(
                        f"No reservation for product {product_id!r}, order {order_ref!r}"
                    )
                reservation = _row_to_reservation(reservation_row)
                if reservation.status != ReservationStatus.RESERVED:
                    return reservation

                conn.execute(
                    stock_table.update()
                    .where(stock_table.c.product_id == product_id)
                    .values(reserved=stock.reserved - reservation.quantity, updated_at=func.now())
                )
                conn.execute(
                    reservations_table.update()
                    .where(reservations_table.c.id == reservation.id)
                    .values(status=ReservationStatus.RELEASED.value)
                )
                reservation.status = ReservationStatus.RELEASED
                return reservation

    def release_by_order_ref(self, order_ref: str) -> list[Reservation]:
        """FR-5 — releases every active (RESERVED) reservation for
        `order_ref`, across every product it touched. Idempotent: a
        redelivered OrderCancelled for an order with no RESERVED rows
        left is a no-op (empty list), matching FR-5's "already-released
        is a no-op" requirement without erroring."""
        with self._db_operation("release_by_order_ref", "Failed to release order"):
            with self._engine.connect() as conn:
                product_ids = [
                    row.product_id
                    for row in conn.execute(
                        select(reservations_table.c.product_id)
                        .where(
                            reservations_table.c.order_ref == order_ref,
                            reservations_table.c.status == ReservationStatus.RESERVED.value,
                        )
                        .distinct()
                    ).fetchall()
                ]
        released: list[Reservation] = []
        # Lock ordering: one product's stock row at a time, sorted, same
        # reasoning as sweep_expired_reservations below.
        for product_id in sorted(product_ids):
            try:
                released.append(self.release_reservation(product_id, order_ref))
            except ReservationNotFoundError:
                continue
        return [r for r in released if r.status == ReservationStatus.RELEASED]

    def sweep_expired_reservations(self, limit: int = 100) -> list[Reservation]:
        with self._db_operation("sweep_expired_reservations", "Failed to sweep reservations"):
            with self._engine.connect() as conn:
                now = datetime.now(UTC)
                candidates = conn.execute(
                    select(reservations_table.c.id, reservations_table.c.product_id)
                    .where(
                        reservations_table.c.status == ReservationStatus.RESERVED.value,
                        reservations_table.c.expires_at < now,
                    )
                    .order_by(reservations_table.c.expires_at)
                    .limit(limit)
                ).fetchall()

        released: list[Reservation] = []
        # Deterministic product_id order across a single sweep batch to
        # minimize (not eliminate) lock-ordering deadlock risk against
        # concurrent reserve/commit/release on different products; a
        # second concurrent sweep run naturally avoids re-picking a row
        # already locked by this one via Postgres's own `FOR UPDATE
        # SKIP LOCKED` inside `_sweep_one`, so no double-release is
        # possible even without this ordering.
        for row in sorted(candidates, key=lambda r: r.product_id):
            outcome = self._sweep_one(row.product_id, row.id)
            if outcome is not None:
                released.append(outcome)
        return released

    def _sweep_one(self, product_id: str, reservation_id: str) -> Reservation | None:
        with self._engine.begin() as conn:
            # SKIP LOCKED: a concurrent sweep run (or an explicit
            # release/commit already in flight for this reservation) just
            # skips this row rather than blocking — the NFR's own stated
            # "safe to run concurrently with itself" pattern. SQLite (the
            # test double) does not support SKIP LOCKED or real row
            # locking at all; `with_for_update` is a documented no-op
            # there (see zone_repository.py's own fidelity-gap note) —
            # correctness there relies on tests being single-threaded.
            reservation_row = conn.execute(
                select(reservations_table)
                .where(reservations_table.c.id == reservation_id)
                .with_for_update(skip_locked=True)
            ).fetchone()
            if reservation_row is None:
                return None
            reservation = _row_to_reservation(reservation_row)
            now = datetime.now(UTC)
            if reservation.status != ReservationStatus.RESERVED or _aware(
                reservation.expires_at
            ) >= now:
                return None

            stock_row = conn.execute(
                select(stock_table)
                .where(stock_table.c.product_id == product_id)
                .with_for_update()
            ).fetchone()
            if stock_row is None:
                return None
            stock = _row_to_stock(stock_row)

            conn.execute(
                stock_table.update()
                .where(stock_table.c.product_id == product_id)
                .values(reserved=stock.reserved - reservation.quantity, updated_at=func.now())
            )
            conn.execute(
                reservations_table.update()
                .where(reservations_table.c.id == reservation.id)
                .values(status=ReservationStatus.RELEASED.value)
            )
            reservation.status = ReservationStatus.RELEASED
            return reservation

    def _consume_batches_fifo(self, conn, product_id: str, quantity: int) -> None:
        """Physically decrements `stock_batches.quantity`, oldest-expiry-
        first, totaling `quantity` — see module docstring for why this
        happens at commit time, not reserve time. Runs inside the
        caller's own transaction/lock (the stock row is already locked by
        the caller), so no separate lock is taken here."""
        remaining = quantity
        batch_rows = conn.execute(
            select(stock_batches_table)
            .where(
                stock_batches_table.c.product_id == product_id,
                stock_batches_table.c.quantity > 0,
            )
            .order_by(stock_batches_table.c.expiry_date)
        ).fetchall()
        for batch_row in batch_rows:
            if remaining <= 0:
                break
            draw = min(remaining, batch_row.quantity)
            conn.execute(
                stock_batches_table.update()
                .where(stock_batches_table.c.id == batch_row.id)
                .values(quantity=batch_row.quantity - draw)
            )
            remaining -= draw
        # If `remaining > 0` here, on_hand was already decremented but no
        # batch had quantity to draw from — a pre-existing data drift
        # between `stock.on_hand` and the sum of its batches (e.g. an
        # admin adjustment raised on_hand without a matching batch, per
        # MA-118 §12 Q3's own documented gap). Not an error: `on_hand` is
        # the system-of-record total, batches are FIFO bookkeeping over
        # it, and FR-7 already scopes batch detail out of any API
        # correctness guarantee.

    # --- MA-119 FR-1: admin adjustment ---------------------------------

    def adjust(
        self, product_id: str, admin_id: str, adjustment: int, reason: str | None
    ) -> tuple[Stock, AuditLogEntry]:
        with self._db_operation("adjust", "Failed to adjust stock"):
            with self._engine.begin() as conn:
                stock_row = conn.execute(
                    select(stock_table)
                    .where(stock_table.c.product_id == product_id)
                    .with_for_update()
                ).fetchone()
                if stock_row is None:
                    raise ProductNotFoundError(f"Unknown product {product_id!r}")
                stock = _row_to_stock(stock_row)

                new_on_hand = stock.on_hand + adjustment
                if new_on_hand < 0:
                    raise OnHandFloorViolationError(
                        "Adjustment would drive on_hand negative",
                        {"productId": product_id, "onHand": stock.on_hand,
                         "adjustment": adjustment},
                    )
                new_available = new_on_hand - stock.reserved
                if new_available < 0:
                    raise AvailableFloorViolationError(
                        "Adjustment would drive available negative while reservations"
                        " are outstanding",
                        {"productId": product_id, "onHand": stock.on_hand,
                         "reserved": stock.reserved, "adjustment": adjustment},
                    )

                conn.execute(
                    stock_table.update()
                    .where(stock_table.c.product_id == product_id)
                    .values(on_hand=new_on_hand, updated_at=func.now())
                )
                audit_entry = self._insert_audit_row(
                    conn,
                    product_id=product_id,
                    admin_id=admin_id,
                    previous_quantity=stock.on_hand,
                    new_quantity=new_on_hand,
                    adjustment=adjustment,
                    reason=reason,
                )
                stock.on_hand = new_on_hand
                return stock, audit_entry

    # --- MA-150 FR-1: receive -------------------------------------------

    def receive_stock(
        self,
        product_id: str,
        quantity: int,
        expiry_date: date,
        admin_id: str,
        reason: str | None,
    ) -> tuple[StockBatch, Stock, AuditLogEntry]:
        with self._db_operation("receive_stock", "Failed to receive stock"):
            with self._engine.begin() as conn:
                stock_row = conn.execute(
                    select(stock_table)
                    .where(stock_table.c.product_id == product_id)
                    .with_for_update()
                ).fetchone()
                if stock_row is None:
                    raise ProductNotFoundError(f"Unknown product {product_id!r}")
                stock = _row_to_stock(stock_row)

                batch_id = new_batch_id()
                conn.execute(
                    stock_batches_table.insert().values(
                        id=batch_id,
                        product_id=product_id,
                        quantity=quantity,
                        expiry_date=expiry_date,
                        available_from=None,
                    )
                )
                new_on_hand = stock.on_hand + quantity
                conn.execute(
                    stock_table.update()
                    .where(stock_table.c.product_id == product_id)
                    .values(on_hand=new_on_hand, updated_at=func.now())
                )
                full_reason = f"goods_receipt: {reason}" if reason else "goods_receipt"
                audit_entry = self._insert_audit_row(
                    conn,
                    product_id=product_id,
                    admin_id=admin_id,
                    previous_quantity=stock.on_hand,
                    new_quantity=new_on_hand,
                    adjustment=quantity,
                    reason=full_reason,
                )
                batch_row = conn.execute(
                    select(stock_batches_table).where(stock_batches_table.c.id == batch_id)
                ).one()
                stock.on_hand = new_on_hand
                return _row_to_batch(batch_row), stock, audit_entry

    def _insert_audit_row(
        self,
        conn,
        *,
        product_id: str,
        admin_id: str,
        previous_quantity: int,
        new_quantity: int,
        adjustment: int,
        reason: str | None,
    ) -> AuditLogEntry:
        audit_id = new_audit_id()
        conn.execute(
            inventory_audit_log_table.insert().values(
                id=audit_id,
                product_id=product_id,
                admin_id=admin_id,
                previous_quantity=previous_quantity,
                new_quantity=new_quantity,
                adjustment=adjustment,
                reason=reason,
            )
        )
        row = conn.execute(
            select(inventory_audit_log_table).where(inventory_audit_log_table.c.id == audit_id)
        ).one()
        return _row_to_audit_entry(row)

    # --- MA-150 FR-2/FR-3/FR-4: admin reads -----------------------------

    def get_batches(self, product_id: str) -> list[StockBatch]:
        with self._db_operation("get_batches", "Failed to load batches"):
            with self._engine.connect() as conn:
                exists = conn.execute(
                    select(stock_table.c.product_id).where(
                        stock_table.c.product_id == product_id
                    )
                ).fetchone()
                if exists is None:
                    raise ProductNotFoundError(f"Unknown product {product_id!r}")
                rows = conn.execute(
                    select(stock_batches_table)
                    .where(stock_batches_table.c.product_id == product_id)
                    .order_by(stock_batches_table.c.expiry_date, stock_batches_table.c.received_at)
                ).fetchall()
        return [_row_to_batch(r) for r in rows]

    def list_stock(
        self, status_filter: StockState | None, page: int, page_size: int
    ) -> Page:
        with self._db_operation("list_stock", "Failed to load stock list"):
            with self._engine.connect() as conn:
                all_rows = conn.execute(
                    select(stock_table).order_by(stock_table.c.product_id)
                ).fetchall()
                stocks = [_row_to_stock(r) for r in all_rows]

                # Only products currently at available<=0 ever need a
                # batch lookup for AVAILABLE_FROM derivation (FR-1's own
                # rule, reused here per FR-3).
                zero_available_ids = [s.product_id for s in stocks if s.available <= 0]
                next_batches: dict[str, date] = {}
                if zero_available_ids:
                    batch_rows = conn.execute(
                        select(stock_batches_table)
                        .where(
                            stock_batches_table.c.product_id.in_(zero_available_ids),
                            stock_batches_table.c.available_from.is_not(None),
                            stock_batches_table.c.quantity > 0,
                        )
                        .order_by(stock_batches_table.c.product_id, stock_batches_table.c.available_from)
                    ).fetchall()
                    for row in batch_rows:
                        next_batches.setdefault(row.product_id, row.available_from)

        summaries = [
            _to_summary(s, next_batches.get(s.product_id)) for s in stocks
        ]
        if status_filter is not None:
            summaries = [s for s in summaries if s.stock_state == status_filter]

        total = len(summaries)
        start = (page - 1) * page_size
        page_items = summaries[start : start + page_size]
        return Page(items=page_items, total=total, page=page, page_size=page_size)

    def get_audit_log(self, product_id: str, page: int, page_size: int) -> Page:
        with self._db_operation("get_audit_log", "Failed to load audit log"):
            with self._engine.connect() as conn:
                exists = conn.execute(
                    select(stock_table.c.product_id).where(
                        stock_table.c.product_id == product_id
                    )
                ).fetchone()
                if exists is None:
                    raise ProductNotFoundError(f"Unknown product {product_id!r}")

                total = conn.execute(
                    select(func.count())
                    .select_from(inventory_audit_log_table)
                    .where(inventory_audit_log_table.c.product_id == product_id)
                ).scalar_one()

                rows = conn.execute(
                    select(inventory_audit_log_table)
                    .where(inventory_audit_log_table.c.product_id == product_id)
                    .order_by(inventory_audit_log_table.c.created_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                ).fetchall()
        return Page(
            items=[_row_to_audit_entry(r) for r in rows],
            total=int(total),
            page=page,
            page_size=page_size,
        )


def _add_seconds(dt: datetime, seconds: int) -> datetime:
    return dt + timedelta(seconds=seconds)


def _aware(dt: datetime) -> datetime:
    """SQLite doesn't round-trip TIMESTAMPTZ tzinfo faithfully (same
    documented gap as wallet_repository.py's list_ledger_entries) — a
    value read back from the SQLite test double can come back naive.
    Treat a naive value as UTC (every datetime this service writes is
    already UTC) rather than erroring on the comparison below."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _to_summary(stock: Stock, next_available_from: date | None) -> StockSummary:
    if stock.available > 0:
        state = StockState.IN_STOCK
        available_from = None
    elif next_available_from is not None:
        state = StockState.AVAILABLE_FROM
        available_from = next_available_from
    else:
        state = StockState.OUT_OF_STOCK
        available_from = None
    return StockSummary(
        product_id=stock.product_id,
        on_hand=stock.on_hand,
        reserved=stock.reserved,
        available=stock.available,
        low_stock_threshold=stock.low_stock_threshold,
        stock_state=state,
        available_from=available_from,
    )


def _row_to_stock(row) -> Stock:
    return Stock(
        product_id=row.product_id,
        on_hand=int(row.on_hand),
        reserved=int(row.reserved),
        low_stock_threshold=int(row.low_stock_threshold),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_batch(row) -> StockBatch:
    return StockBatch(
        id=row.id,
        product_id=row.product_id,
        quantity=int(row.quantity),
        expiry_date=row.expiry_date,
        available_from=row.available_from,
        received_at=row.received_at,
    )


def _row_to_reservation(row) -> Reservation:
    return Reservation(
        id=row.id,
        product_id=row.product_id,
        order_ref=row.order_ref,
        quantity=int(row.quantity),
        status=ReservationStatus(row.status),
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


def _row_to_audit_entry(row) -> AuditLogEntry:
    return AuditLogEntry(
        id=row.id,
        product_id=row.product_id,
        admin_id=row.admin_id,
        previous_quantity=int(row.previous_quantity),
        new_quantity=int(row.new_quantity),
        adjustment=int(row.adjustment),
        reason=row.reason,
        created_at=row.created_at,
    )
