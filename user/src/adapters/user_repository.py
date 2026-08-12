"""SQLAlchemy Core repository for users/addresses/user_consents/
outbox_events/zone_slots. Per services/README.md §3.7: the only place
allowed to import SQLAlchemy for this concern.

Same portable-types approach as MA-95's zone_repository.py — the same
table definitions run against Postgres (production) and an in-memory
SQLite engine (tests), a documented fidelity gap.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, Column, Float, ForeignKey, MetaData, String, Table, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from domain.exceptions import ExternalServiceUnavailableError
from domain.models import Address, Consent, DeliverySlot, RegistrationResult

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
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    update(outbox_events_table)
                    .where(outbox_events_table.c.id == event_id)
                    .values(published_at=datetime.now(UTC).isoformat())
                )
        except SQLAlchemyError as exc:
            logger.error(
                "user_repository.mark_outbox_published failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to mark outbox event published") from exc
