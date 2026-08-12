"""SQLAlchemy Core repository for `serviceability_zones`.

Per services/README.md §3.7: the only place allowed to import SQLAlchemy
for this concern. The same `serviceability_zones_table` definition runs
against both Postgres (production) and an in-memory SQLite engine (tests)
— see migrations/0001_serviceability_zones.sql's header comment for why
the column types (JSON, not native arrays/PostGIS) were chosen to make
that possible. Point-in-polygon/prefix matching happens in the domain
layer, not SQL — this repository only ever does `WHERE active = true`.
"""

import logging

from sqlalchemy import Boolean, Column, JSON, MetaData, String, Table, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from domain.exceptions import ServiceUnavailableError
from domain.models import Slot, Zone

logger = logging.getLogger(__name__)

metadata = MetaData()

serviceability_zones_table = Table(
    "serviceability_zones",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("pincode_prefixes", JSON, nullable=False, default=list),
    Column("polygon", JSON, nullable=True),
    Column("slot_config", JSON, nullable=False, default=list),
)


def create_schema(engine: Engine) -> None:
    """Test-only convenience — production schema ownership is the raw SQL
    migration file, not this. Used to stand up the SQLite test double."""
    metadata.create_all(engine)


class SqlAlchemyZoneRepository:
    def __init__(self, engine: Engine, correlation_id: str = "") -> None:
        self._engine = engine
        self._correlation_id = correlation_id

    def get_active_zones(self) -> list[Zone]:
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(serviceability_zones_table).where(
                        serviceability_zones_table.c.active.is_(True)
                    )
                ).fetchall()
        except SQLAlchemyError as exc:
            logger.error(
                "zone_repository.get_active_zones failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ServiceUnavailableError("Failed to load serviceability zones") from exc

        return [_row_to_zone(row) for row in rows]


def _row_to_zone(row) -> Zone:
    polygon_raw = row.polygon
    polygon = [tuple(p) for p in polygon_raw] if polygon_raw else None
    return Zone(
        id=row.id,
        name=row.name,
        active=row.active,
        pincode_prefixes=list(row.pincode_prefixes or []),
        polygon=polygon,
        slots=[Slot(id=s["id"], label=s["label"]) for s in (row.slot_config or [])],
    )
