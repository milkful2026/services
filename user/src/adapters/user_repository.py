"""SQLAlchemy Core repository for users/addresses/user_consents/
outbox_events/zone_slots. Per services/README.md §3.7: the only place
allowed to import SQLAlchemy for this concern.

Same portable-types approach as MA-95's zone_repository.py — the same
table definitions run against Postgres (production) and an in-memory
SQLite engine (tests), a documented fidelity gap.
"""

import logging
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    MetaData,
    String,
    Table,
    and_,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from adapters.retry import call_with_retry
from domain.exceptions import (
    CustomerNotFoundError,
    ExternalServiceUnavailableError,
)
from domain.models import (
    Address,
    Consent,
    CustomerAccount,
    CustomerPage,
    DeliverySlot,
    RegistrationResult,
    UserProfile,
    UserStatusHistoryEntry,
)

logger = logging.getLogger(__name__)

metadata = MetaData()

users_table = Table(
    "users",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("cognito_sub", String(128), nullable=False, unique=True),
    Column("name", String(100), nullable=False),
    Column("mobile", String(20), nullable=False),
    Column("email", String(255), nullable=True),
    Column("preferred_slot_id", String(64), nullable=True),
    # Added by migrations/0002_add_account_type.sql — spec MA-107 FR-1.
    # No write path sets "B2B" yet; register() always inserts "B2C".
    Column("account_type", String(16), nullable=False, default="B2C"),
    CheckConstraint("account_type IN ('B2C', 'B2B')", name="ck_users_account_type"),
    # Added by migrations/0004_customer_status.sql — MA-139 §7. Additive,
    # defaulted 'Active' for every pre-existing row.
    Column("status", String(16), nullable=False, default="Active"),
    CheckConstraint(
        "status IN ('Active', 'Suspended', 'Deactivated')", name="ck_users_status"
    ),
    Column("status_reason", String(500), nullable=True),
    Column("status_effective_from", Date, nullable=True),
    Column("suspended_until", Date, nullable=True),
)

# MA-139 §7 — durable audit trail for every status transition. Never
# updated/deleted, only inserted — this service's own fallback source of
# truth until MA-50's audit pipeline exists (spec §3).
user_status_history_table = Table(
    "user_status_history",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(36), ForeignKey("users.id"), nullable=False),
    Column("previous_status", String(16), nullable=True),
    Column("new_status", String(16), nullable=False),
    Column("reason", String(500), nullable=True),
    Column("effective_from", Date, nullable=True),
    Column("actor_admin_id", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

addresses_table = Table(
    "addresses",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(36), ForeignKey("users.id"), nullable=False),
    Column("lines", JSON, nullable=False),
    Column("city", String(100), nullable=False),
    Column("state", String(100), nullable=False),
    Column("pincode", String(6), nullable=False),
    Column("lat", Float, nullable=False),
    Column("lng", Float, nullable=False),
    Column("landmark", String(255), nullable=True),
    Column("is_default", Boolean, nullable=False, default=False),
    # Added by migrations/0003_add_zone_id.sql — MA-25 Step 6 companion
    # change. Nullable: existing rows, and any registration predating
    # the mobile client sending zoneId, have none.
    Column("zone_id", String(64), nullable=True),
)

user_consents_table = Table(
    "user_consents",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(36), ForeignKey("users.id"), nullable=False),
    Column("type", String(32), nullable=False),
    Column("version", String(32), nullable=True),
    Column("accepted", Boolean, nullable=False, default=True),
    Column("accepted_at", String(64), nullable=False),
)

outbox_events_table = Table(
    "outbox_events",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("aggregate_id", String(36), nullable=False),
    Column("type", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("published_at", String(64), nullable=True),
    # Matches migrations/0001's created_at + idx_outbox_events_unpublished
    # partial index — needed so get_unpublished_outbox_events can actually
    # guarantee oldest-first delivery instead of relying on unspecified
    # scan order.
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

zone_slots_table = Table(
    "zone_slots",
    metadata,
    Column("zone_id", String(64), primary_key=True),
    Column("slot_id", String(64), primary_key=True),
    Column("label", String(100), nullable=False),
    Column("active", Boolean, nullable=False, default=True),
)


def create_schema(engine: Engine) -> None:
    """Test-only convenience — production schema ownership is the raw SQL
    migration file, not this."""
    metadata.create_all(engine)


class SqlAlchemyUserRepository:
    def __init__(self, engine: Engine, correlation_id: str = "") -> None:
        self._engine = engine
        self._correlation_id = correlation_id

    def set_correlation_id(self, correlation_id: str) -> None:
        self._correlation_id = correlation_id

    @staticmethod
    def _fetch_user_with_default_address(conn, cognito_sub: str):
        """Shared by get_by_cognito_sub and get_profile_by_sub — both need
        the same 'user row, then its default address row' read, just
        mapped to different result types. Returns (user_row, default_row)
        with user_row None if no such user exists."""
        user_row = conn.execute(
            select(users_table).where(users_table.c.cognito_sub == cognito_sub)
        ).fetchone()
        if user_row is None:
            return None, None
        default_row = conn.execute(
            select(addresses_table).where(
                addresses_table.c.user_id == user_row.id,
                addresses_table.c.is_default.is_(True),
            )
        ).fetchone()
        return user_row, default_row

    def get_by_cognito_sub(self, cognito_sub: str) -> RegistrationResult | None:
        """Cheap indexed read, no transaction — lets callers short-circuit
        a duplicate registration (skipping the Inventory call and Cognito
        sync entirely) before ever attempting `register()`."""
        try:
            with self._engine.connect() as conn:
                user_row, default_row = self._fetch_user_with_default_address(conn, cognito_sub)
                if user_row is None:
                    return None
        except SQLAlchemyError as exc:
            logger.error(
                "user_repository.get_by_cognito_sub failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError(
                "Failed to look up existing registration"
            ) from exc

        return RegistrationResult(
            user_id=user_row.id,
            default_address_id=default_row.id if default_row else "",
            is_new_user=False,
        )

    def get_profile_by_sub(self, cognito_sub: str) -> UserProfile | None:
        """Spec MA-107 FR-2 — resolved by the JWT `sub` claim only, never
        a client-supplied ID (services/README.md §5b)."""
        try:
            with self._engine.connect() as conn:
                user_row, default_row = self._fetch_user_with_default_address(conn, cognito_sub)
                if user_row is None:
                    return None
        except SQLAlchemyError as exc:
            logger.error(
                "user_repository.get_profile_by_sub failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to load profile") from exc

        return UserProfile(
            user_id=user_row.id,
            name=user_row.name,
            mobile=user_row.mobile,
            account_type=user_row.account_type,
            default_address_id=default_row.id if default_row else "",
            default_address_state=default_row.state if default_row else None,
            default_address_zone_id=default_row.zone_id if default_row else None,
            default_address=_row_to_address(default_row) if default_row else None,
        )

    def register(
        self,
        cognito_sub: str,
        mobile: str,
        name: str,
        email: str | None,
        addresses: list[Address],
        preferred_slot_id: str | None,
        consents: list[Consent],
        outbox_event_type: str,
        outbox_payload: dict,
    ) -> RegistrationResult:
        try:
            with self._engine.begin() as conn:
                existing = conn.execute(
                    select(users_table).where(users_table.c.cognito_sub == cognito_sub)
                ).fetchone()
                if existing is not None:
                    default_row = conn.execute(
                        select(addresses_table).where(
                            addresses_table.c.user_id == existing.id,
                            addresses_table.c.is_default.is_(True),
                        )
                    ).fetchone()
                    return RegistrationResult(
                        user_id=existing.id,
                        default_address_id=default_row.id if default_row else "",
                        is_new_user=False,
                    )

                user_id = str(uuid.uuid4())
                conn.execute(
                    users_table.insert().values(
                        id=user_id,
                        cognito_sub=cognito_sub,
                        name=name,
                        mobile=mobile,
                        email=email,
                        preferred_slot_id=preferred_slot_id,
                    )
                )

                default_address_id = ""
                for address in addresses:
                    address_id = str(uuid.uuid4())
                    if address.is_default:
                        default_address_id = address_id
                    conn.execute(
                        addresses_table.insert().values(
                            id=address_id,
                            user_id=user_id,
                            lines=address.lines,
                            city=address.city,
                            state=address.state,
                            pincode=address.pincode,
                            lat=address.lat,
                            lng=address.lng,
                            landmark=address.landmark,
                            is_default=address.is_default,
                            zone_id=address.zone_id,
                        )
                    )

                for consent in consents:
                    conn.execute(
                        user_consents_table.insert().values(
                            id=str(uuid.uuid4()),
                            user_id=user_id,
                            type=consent.type,
                            version=consent.version,
                            accepted=consent.accepted,
                            accepted_at=consent.accepted_at,
                        )
                    )

                # Same transaction as everything above — the whole point
                # of the outbox pattern (spec §9): a publish failure can
                # never mean the user wasn't created, or vice versa.
                conn.execute(
                    outbox_events_table.insert().values(
                        id=str(uuid.uuid4()),
                        aggregate_id=user_id,
                        type=outbox_event_type,
                        payload=outbox_payload,
                        published_at=None,
                    )
                )
        except IntegrityError as exc:
            # A concurrent registration for the same cognito_sub committed
            # first (the pre-check above and this INSERT aren't atomic
            # with each other). Per the idempotency contract this Port
            # documents, that must resolve to the existing row, not a 503.
            logger.info(
                "user_repository.register lost a concurrent-registration race,"
                " returning the existing row",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            existing_result = self.get_by_cognito_sub(cognito_sub)
            if existing_result is not None:
                return existing_result
            raise ExternalServiceUnavailableError("Failed to persist registration") from exc
        except SQLAlchemyError as exc:
            logger.error(
                "user_repository.register failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to persist registration") from exc

        return RegistrationResult(
            user_id=user_id, default_address_id=default_address_id, is_new_user=True
        )

    def get_delivery_slots(self, zone_id: str) -> list[DeliverySlot]:
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(zone_slots_table).where(
                        zone_slots_table.c.zone_id == zone_id,
                        zone_slots_table.c.active.is_(True),
                    )
                ).fetchall()
        except SQLAlchemyError as exc:
            logger.error(
                "user_repository.get_delivery_slots failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to load delivery slots") from exc

        return [DeliverySlot(id=row.slot_id, label=row.label) for row in rows]

    def get_unpublished_outbox_events(self, limit: int) -> list[dict]:
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(outbox_events_table)
                    .where(outbox_events_table.c.published_at.is_(None))
                    .order_by(outbox_events_table.c.created_at)
                    .limit(limit)
                ).fetchall()
        except SQLAlchemyError as exc:
            logger.error(
                "user_repository.get_unpublished_outbox_events failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to read outbox") from exc

        return [
            {"id": row.id, "aggregateId": row.aggregate_id, "type": row.type, "payload": row.payload}
            for row in rows
        ]

    def mark_outbox_published(self, event_id: str) -> None:
        def _attempt() -> None:
            with self._engine.begin() as conn:
                conn.execute(
                    update(outbox_events_table)
                    .where(outbox_events_table.c.id == event_id)
                    .values(published_at=datetime.now(UTC).isoformat())
                )

        def _on_attempt_failure(exc: Exception, attempt: int) -> None:
            logger.error(
                "user_repository.mark_outbox_published failed, retrying",
                extra={
                    "correlationId": self._correlation_id,
                    "attempt": attempt,
                    "error": str(exc),
                },
            )

        try:
            # Retried here specifically because the caller already
            # published this event to EventBridge — a transient failure
            # marking it published (as opposed to failing to publish in
            # the first place) risks a duplicate re-delivery on the next
            # scheduled run, so it's worth a few quick attempts before
            # giving up.
            call_with_retry(
                _attempt,
                max_retries=2,
                backoff_base_seconds=0.1,
                retryable_exceptions=(SQLAlchemyError,),
                on_attempt_failure=_on_attempt_failure,
            )
        except SQLAlchemyError as exc:
            raise ExternalServiceUnavailableError("Failed to mark outbox event published") from exc

    # --- MA-139: Customer Account Status ---

    def list_customers(
        self, status: str | None, search: str | None, page: int, page_size: int
    ) -> CustomerPage:
        """Spec section 4 FR-1. `lastStatusChangeAt` is computed via an
        outer join to a per-user MAX(created_at) subquery over
        user_status_history -- NULL for an account that's never had a
        status change, exactly as the spec requires (never falls back to
        status_effective_from, which is a DATE, not a timestamptz).
        Wrapped in call_with_retry (adapters/retry.py) -- same shape as
        identity-auth's structurally identical admin_user_repository.py
        list() -- so this DB read gets the same retry resilience every
        other adapter call in this service has, instead of failing on
        the first transient error."""
        last_change_subq = (
            select(
                user_status_history_table.c.user_id,
                func.max(user_status_history_table.c.created_at).label(
                    "last_status_change_at"
                ),
            )
            .group_by(user_status_history_table.c.user_id)
            .subquery()
        )

        conditions = []
        if status is not None:
            conditions.append(users_table.c.status == status)
        if search:
            like = f"%{search.strip().lower()}%"
            conditions.append(
                or_(
                    func.lower(users_table.c.name).like(like),
                    func.lower(users_table.c.mobile).like(like),
                    func.lower(func.coalesce(users_table.c.email, "")).like(like),
                )
            )

        def _attempt():
            with self._engine.connect() as conn:
                count_query = select(func.count()).select_from(users_table)
                if conditions:
                    count_query = count_query.where(and_(*conditions))
                total = conn.execute(count_query).scalar_one()

                query = select(users_table, last_change_subq.c.last_status_change_at).select_from(
                    users_table.outerjoin(
                        last_change_subq, users_table.c.id == last_change_subq.c.user_id
                    )
                )
                if conditions:
                    query = query.where(and_(*conditions))
                rows = conn.execute(
                    query.order_by(users_table.c.name)
                    .limit(page_size)
                    .offset((page - 1) * page_size)
                ).fetchall()
                return total, rows

        try:
            total, rows = call_with_retry(
                _attempt,
                max_retries=2,
                backoff_base_seconds=0.1,
                retryable_exceptions=(SQLAlchemyError,),
            )
        except SQLAlchemyError as exc:
            logger.error(
                "user_repository.list_customers failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to list customers") from exc

        items = [
            _row_to_customer_account(row, last_status_change_at=row.last_status_change_at)
            for row in rows
        ]
        return CustomerPage(items=items, total=total, page=page, page_size=page_size)

    def get_customer_by_id(self, customer_id: str) -> CustomerAccount | None:
        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(users_table).where(users_table.c.id == customer_id)
                ).fetchone()
                if row is None:
                    return None
                last_change_row = conn.execute(
                    select(func.max(user_status_history_table.c.created_at)).where(
                        user_status_history_table.c.user_id == customer_id
                    )
                ).fetchone()
        except SQLAlchemyError as exc:
            logger.error(
                "user_repository.get_customer_by_id failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to load customer") from exc

        last_status_change_at = last_change_row[0] if last_change_row else None
        return _row_to_customer_account(row, last_status_change_at=last_status_change_at)

    def get_status_history(self, customer_id: str) -> list[UserStatusHistoryEntry]:
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(user_status_history_table)
                    .where(user_status_history_table.c.user_id == customer_id)
                    .order_by(user_status_history_table.c.created_at.desc())
                ).fetchall()
        except SQLAlchemyError as exc:
            logger.error(
                "user_repository.get_status_history failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to load status history") from exc

        return [
            UserStatusHistoryEntry(
                id=row.id,
                user_id=row.user_id,
                previous_status=row.previous_status,
                new_status=row.new_status,
                reason=row.reason,
                effective_from=row.effective_from,
                actor_admin_id=row.actor_admin_id,
                created_at=row.created_at,
            )
            for row in rows
        ]

    def update_customer_status(
        self,
        customer_id: str,
        *,
        new_status: str,
        status_reason: str | None,
        status_effective_from: date | None,
        history_effective_from: date | None,
        suspended_until: date | None,
        actor_admin_id: str,
        outbox_event_type: str,
        outbox_payload: dict,
    ) -> CustomerAccount:
        """One transaction (spec §6/§9): UPDATE users + INSERT
        user_status_history + INSERT outbox_events — same shape as
        register()'s own transactional-outbox write."""
        try:
            with self._engine.begin() as conn:
                existing = conn.execute(
                    select(users_table).where(users_table.c.id == customer_id)
                ).fetchone()
                if existing is None:
                    raise CustomerNotFoundError(f"No customer {customer_id!r}")

                conn.execute(
                    users_table.update()
                    .where(users_table.c.id == customer_id)
                    .values(
                        status=new_status,
                        status_reason=status_reason,
                        status_effective_from=status_effective_from,
                        suspended_until=suspended_until,
                    )
                )
                conn.execute(
                    user_status_history_table.insert().values(
                        id=str(uuid.uuid4()),
                        user_id=customer_id,
                        previous_status=existing.status,
                        new_status=new_status,
                        reason=status_reason,
                        effective_from=history_effective_from,
                        actor_admin_id=actor_admin_id,
                    )
                )
                conn.execute(
                    outbox_events_table.insert().values(
                        id=str(uuid.uuid4()),
                        aggregate_id=customer_id,
                        type=outbox_event_type,
                        payload=outbox_payload,
                        published_at=None,
                    )
                )
        except CustomerNotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.error(
                "user_repository.update_customer_status failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to update customer status") from exc

        updated = self.get_customer_by_id(customer_id)
        if updated is None:  # pragma: no cover — can't happen, just committed the row
            raise CustomerNotFoundError(f"No customer {customer_id!r}")
        return updated

    def list_expired_suspensions(self, as_of: date) -> list[CustomerAccount]:
        """Spec §4 FR-7 sweep candidates. `last_status_change_at` is left
        unset here — the sweep only needs `id`/`cognito_sub`, and paying
        for the history join on every run adds nothing it uses."""
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(users_table).where(
                        users_table.c.status == "Suspended",
                        users_table.c.suspended_until <= as_of,
                    )
                ).fetchall()
        except SQLAlchemyError as exc:
            logger.error(
                "user_repository.list_expired_suspensions failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to load expired suspensions") from exc

        return [_row_to_customer_account(row, last_status_change_at=None) for row in rows]


def _row_to_customer_account(row, last_status_change_at) -> CustomerAccount:
    return CustomerAccount(
        id=row.id,
        name=row.name,
        mobile=row.mobile,
        email=row.email,
        account_type=row.account_type,
        status=row.status,
        status_reason=row.status_reason,
        last_status_change_at=last_status_change_at,
        cognito_sub=row.cognito_sub,
        suspended_until=row.suspended_until,
    )


def _row_to_address(row) -> Address:
    """MA-135 FR-6 — the stored default address row, verbatim (no
    re-geocoding): exactly what the onboarding Google Maps / Places screen
    saved."""
    return Address(
        id=row.id,
        lines=list(row.lines or []),
        city=row.city,
        state=row.state,
        pincode=row.pincode,
        lat=float(row.lat),
        lng=float(row.lng),
        landmark=row.landmark,
        is_default=bool(row.is_default),
        zone_id=row.zone_id,
    )
