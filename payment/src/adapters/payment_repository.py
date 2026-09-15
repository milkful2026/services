"""SQLAlchemy Core repository for `payments` / `payment_events` / `outbox`.

SQLAlchemy Core only (mirrors catalog/wallet) — the same Table
definitions run against Postgres (production) and an in-memory SQLite
engine (tests). Columns are kept column-for-column compatible with
migrations/0001_payments.sql by hand.
"""

import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from domain.exceptions import ServiceUnavailableError
from domain.models import Payment, PaymentMethod, PaymentStatus, Purpose

logger = logging.getLogger(__name__)

metadata = MetaData()

payments_table = Table(
    "payments",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("user_id", String(64), nullable=False),
    Column("purpose", String(16), nullable=False),
    Column("amount_paise", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False, default="INR"),
    Column("status", String(16), nullable=False, default=PaymentStatus.CREATED.value),
    Column("method", String(16), nullable=True),
    Column("razorpay_order_id", Text, nullable=True, unique=True),
    Column("razorpay_payment_id", Text, nullable=True),
    Column("razorpay_signature", Text, nullable=True),
    Column("idempotency_key", Text, nullable=False),
    Column("failure_code", Text, nullable=True),
    Column("failure_reason", Text, nullable=True),
    Column("correlation_id", Text, nullable=False),
    Column("captured_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    ),
)

payment_events_table = Table(
    "payment_events",
    metadata,
    Column(
        "id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    ),
    Column("payment_id", String(64), nullable=False),
    Column("source", Text, nullable=False),
    Column("raw_payload", JSONB().with_variant(Text, "sqlite"), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
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


def new_payment_id() -> str:
    return f"pay_{uuid.uuid4().hex}"


def _dump(payload: dict) -> object:
    return json.dumps(payload)


class SqlAlchemyPaymentRepository:
    def __init__(self, engine: Engine, correlation_id: str = "") -> None:
        self._engine = engine
        self._correlation_id = correlation_id

    @contextmanager
    def _db_operation(self, operation: str, failure_message: str) -> Iterator[None]:
        try:
            yield
        except SQLAlchemyError as exc:
            logger.error(
                f"payment_repository.{operation} failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ServiceUnavailableError(failure_message) from exc

    def get_by_user_idem(self, user_id: str, idempotency_key: str) -> Payment | None:
        with self._db_operation("get_by_user_idem", "Failed to load payment"):
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(payments_table).where(
                        (payments_table.c.user_id == user_id)
                        & (payments_table.c.idempotency_key == idempotency_key)
                    )
                ).fetchone()
        return None if row is None else _row_to_payment(row)

    def insert_created(
        self,
        *,
        payment_id: str,
        user_id: str,
        purpose: str,
        amount_paise: int,
        currency: str,
        method: str | None,
        idempotency_key: str,
        correlation_id: str,
    ) -> Payment:
        with self._db_operation("insert_created", "Failed to create payment"):
            with self._engine.begin() as conn:
                conn.execute(
                    payments_table.insert().values(
                        id=payment_id,
                        user_id=user_id,
                        purpose=purpose,
                        amount_paise=amount_paise,
                        currency=currency,
                        status=PaymentStatus.CREATED.value,
                        method=method,
                        idempotency_key=idempotency_key,
                        correlation_id=correlation_id,
                    )
                )
                row = conn.execute(
                    select(payments_table).where(payments_table.c.id == payment_id)
                ).fetchone()
        return _row_to_payment(row)

    def set_order_id(self, payment_id: str, razorpay_order_id: str) -> None:
        with self._db_operation("set_order_id", "Failed to persist gateway order"):
            with self._engine.begin() as conn:
                conn.execute(
                    payments_table.update()
                    .where(payments_table.c.id == payment_id)
                    .values(razorpay_order_id=razorpay_order_id, updated_at=func.now())
                )

    def lock_by_id(self, payment_id: str) -> Payment | None:
        with self._db_operation("lock_by_id", "Failed to load payment"):
            with self._engine.begin() as conn:
                row = conn.execute(
                    select(payments_table)
                    .where(payments_table.c.id == payment_id)
                    .with_for_update()
                ).fetchone()
        return None if row is None else _row_to_payment(row)

    def lock_by_order_id(self, razorpay_order_id: str) -> Payment | None:
        with self._db_operation("lock_by_order_id", "Failed to load payment"):
            with self._engine.begin() as conn:
                row = conn.execute(
                    select(payments_table)
                    .where(payments_table.c.razorpay_order_id == razorpay_order_id)
                    .with_for_update()
                ).fetchone()
        return None if row is None else _row_to_payment(row)

    def set_status(
        self,
        payment_id: str,
        *,
        status: str,
        method: str | None = None,
        razorpay_payment_id: str | None = None,
        razorpay_signature: str | None = None,
        failure_code: str | None = None,
        failure_reason: str | None = None,
        captured_at_now: bool = False,
    ) -> None:
        values: dict[str, object] = {"status": status, "updated_at": func.now()}
        if method is not None:
            values["method"] = method
        if razorpay_payment_id is not None:
            values["razorpay_payment_id"] = razorpay_payment_id
        if razorpay_signature is not None:
            values["razorpay_signature"] = razorpay_signature
        if failure_code is not None:
            values["failure_code"] = failure_code
        if failure_reason is not None:
            values["failure_reason"] = failure_reason
        if captured_at_now:
            values["captured_at"] = func.now()
        with self._db_operation("set_status", "Failed to update payment"):
            with self._engine.begin() as conn:
                conn.execute(
                    payments_table.update()
                    .where(payments_table.c.id == payment_id)
                    .values(**values)
                )

    def append_event(self, payment_id: str, source: str, raw_payload: dict) -> None:
        with self._db_operation("append_event", "Failed to record payment event"):
            with self._engine.begin() as conn:
                conn.execute(
                    payment_events_table.insert().values(
                        payment_id=payment_id, source=source, raw_payload=_dump(raw_payload)
                    )
                )

    def enqueue_outbox(self, payment_id: str, event_type: str, payload: dict) -> None:
        with self._db_operation("enqueue_outbox", "Failed to enqueue event"):
            with self._engine.begin() as conn:
                conn.execute(
                    outbox_table.insert().values(
                        aggregate_id=payment_id, event_type=event_type, payload=_dump(payload)
                    )
                )

    def list_stale(self, status_in: tuple[str, ...], older_than_seconds: float) -> list[Payment]:
        cutoff = datetime.now(UTC).timestamp() - older_than_seconds
        with self._db_operation("list_stale", "Failed to list stale payments"):
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(payments_table).where(payments_table.c.status.in_(status_in))
                ).fetchall()
        out = []
        for row in rows:
            updated_at = row.updated_at
            aware = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=UTC)
            if aware.timestamp() <= cutoff:
                out.append(_row_to_payment(row))
        return out

    def get(self, payment_id: str) -> Payment | None:
        with self._db_operation("get", "Failed to load payment"):
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(payments_table).where(payments_table.c.id == payment_id)
                ).fetchone()
        return None if row is None else _row_to_payment(row)

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
        out = []
        for r in rows:
            payload = r.payload
            if isinstance(payload, str):
                payload = json.loads(payload)
            out.append(
                {
                    "id": r.id,
                    "event_type": r.event_type,
                    "payload": payload,
                    "created_at": r.created_at,
                }
            )
        return out

    def mark_published(self, outbox_id: int) -> None:
        with self._db_operation("mark_published", "Failed to mark outbox row"):
            with self._engine.begin() as conn:
                conn.execute(
                    outbox_table.update()
                    .where(outbox_table.c.id == outbox_id)
                    .values(published_at=func.now())
                )


def _row_to_payment(row) -> Payment:
    return Payment(
        id=row.id,
        user_id=row.user_id,
        purpose=Purpose(row.purpose),
        amount_paise=int(row.amount_paise),
        currency=row.currency,
        status=PaymentStatus(row.status),
        method=PaymentMethod(row.method) if row.method else None,
        razorpay_order_id=row.razorpay_order_id,
        razorpay_payment_id=row.razorpay_payment_id,
        razorpay_signature=row.razorpay_signature,
        idempotency_key=row.idempotency_key,
        failure_code=row.failure_code,
        failure_reason=row.failure_reason,
        correlation_id=row.correlation_id,
        captured_at=row.captured_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
