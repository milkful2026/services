"""SQLAlchemy Core repository for `products`/`categories`.

Per services/README.md's SQLAlchemy-Core-only convention (mirroring
inventory/src/adapters/zone_repository.py) — the same table definitions
run against both Postgres (production) and an in-memory SQLite engine
(tests).

`search()` implements MA-117's `GET /search` contract (text query +
`category`/`price`/`veg`/`organic` filters + sort) directly against this
Postgres table via SQL (ILIKE + WHERE + ORDER BY) rather than a real
OpenSearch index. MA-117's own spec flags OpenSearch as the single
biggest implementation risk in this whole spec set — no local-dev
emulation precedent exists for it anywhere in this repo. This
same-database implementation satisfies the API *contract* (same request
params, same response shape) so the mobile client (MA-115) and this
service agree on behavior; swapping the query engine underneath later
(to real OpenSearch, once that infra exists) doesn't change the contract
either side depends on.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    and_,
    func,
    or_,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from domain.exceptions import ServiceUnavailableError
from domain.models import Category, Product, SearchFilters, SortOrder, StockState

logger = logging.getLogger(__name__)

metadata = MetaData()

# Column-for-column compatible with migrations/0001_products_categories.sql
# and migrations/0002_stock_change_ordering.sql — see that file's own
# header comment for why this must be kept in sync by hand.
categories_table = Table(
    "categories",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("icon_name", String(64), nullable=False),
    Column("sort_order", Integer, nullable=False, default=0),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

products_table = Table(
    "products",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("category_id", String(64), ForeignKey("categories.id"), nullable=False),
    Column("name", String(255), nullable=False),
    Column("description", Text, nullable=False, default=""),
    Column("unit", String(64), nullable=False),
    Column("price_b2c", Numeric(10, 2), nullable=False),
    Column("price_b2b", Numeric(10, 2), nullable=True),
    Column("image_url", Text, nullable=True),
    Column("tag", String(32), nullable=True),
    Column("subscription_eligible", Boolean, nullable=False, default=False),
    Column("is_veg", Boolean, nullable=False, default=True),
    Column("is_organic", Boolean, nullable=False, default=False),
    Column("stock_state", String(16), nullable=False, default=StockState.IN_STOCK.value),
    Column("available_from", Date, nullable=True),
    Column("last_stock_event_id", String(64), nullable=True),
    # The StockChanged event's own `occurredAt` — lets apply_stock_change
    # detect an out-of-order (but distinct-event-id) redelivery, which
    # last_stock_event_id equality alone can't catch. Nullable since
    # 0001's rows predate this column.
    Column("last_stock_event_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    ),
)


def create_schema(engine: Engine) -> None:
    """Test-only convenience — production schema ownership is the raw SQL
    migration file, not this. Used to stand up the SQLite test double."""
    metadata.create_all(engine)


class SqlAlchemyProductRepository:
    def __init__(self, engine: Engine, correlation_id: str = "") -> None:
        self._engine = engine
        self._correlation_id = correlation_id

    @contextmanager
    def _db_operation(self, operation: str, failure_message: str) -> Iterator[None]:
        """Every DB-touching method wraps its query in this: same
        log-then-translate-to-ServiceUnavailableError shape everywhere,
        rather than five near-identical try/except blocks that could each
        independently drift."""
        try:
            yield
        except SQLAlchemyError as exc:
            logger.error(
                f"product_repository.{operation} failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ServiceUnavailableError(failure_message) from exc

    def get_categories(self) -> list[Category]:
        with self._db_operation("get_categories", "Failed to load categories"):
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(categories_table).order_by(categories_table.c.sort_order)
                ).fetchall()
        return [_row_to_category(row) for row in rows]

    def get_products(self, category_id: str) -> list[Product]:
        with self._db_operation("get_products", "Failed to load products"):
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(products_table)
                    .where(products_table.c.category_id == category_id)
                    .order_by(products_table.c.name)
                ).fetchall()
        return [_row_to_product(row) for row in rows]

    def get_product(self, product_id: str) -> Product | None:
        with self._db_operation("get_product", "Failed to load product"):
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(products_table).where(products_table.c.id == product_id)
                ).fetchone()
        return None if row is None else _row_to_product(row)

    def search(
        self,
        query: str | None,
        filters: SearchFilters | None,
        sort: SortOrder | None,
    ) -> list[Product]:
        stmt = select(products_table)

        conditions = []
        if query:
            like = f"%{query}%"
            conditions.append(
                or_(products_table.c.name.ilike(like), products_table.c.description.ilike(like))
            )
        if filters is not None:
            # MA-115 FR-6's own open question (Q6): repeated category
            # filters combine as OR (matches any selected category) — a
            # product has exactly one category, so AND would be an
            # impossible intersection.
            if filters.category_ids:
                conditions.append(products_table.c.category_id.in_(filters.category_ids))
            if filters.min_price is not None:
                conditions.append(products_table.c.price_b2c >= filters.min_price)
            if filters.max_price is not None:
                conditions.append(products_table.c.price_b2c <= filters.max_price)
            if filters.veg_only:
                conditions.append(products_table.c.is_veg.is_(True))
            if filters.organic_only:
                conditions.append(products_table.c.is_organic.is_(True))
        if conditions:
            stmt = stmt.where(and_(*conditions))

        if sort == SortOrder.PRICE_ASC:
            stmt = stmt.order_by(products_table.c.price_b2c.asc())
        elif sort == SortOrder.PRICE_DESC:
            stmt = stmt.order_by(products_table.c.price_b2c.desc())
        elif sort == SortOrder.NEWEST:
            stmt = stmt.order_by(products_table.c.created_at.desc())
        else:
            stmt = stmt.order_by(products_table.c.name)

        with self._db_operation("search", "Search failed"):
            with self._engine.connect() as conn:
                rows = conn.execute(stmt).fetchall()
        return [_row_to_product(row) for row in rows]

    def apply_stock_change(
        self,
        product_id: str,
        event_id: str,
        stock_state: str,
        available_from,
        occurred_at: datetime | None = None,
    ) -> bool:
        with self._db_operation("apply_stock_change", "Failed to apply stock change"):
            with self._engine.begin() as conn:
                row = conn.execute(
                    select(
                        products_table.c.id,
                        products_table.c.last_stock_event_id,
                        products_table.c.last_stock_event_at,
                    ).where(products_table.c.id == product_id)
                ).fetchone()
                if row is None:
                    logger.info(
                        "apply_stock_change: unknown productId, dropped",
                        extra={"correlationId": self._correlation_id, "productId": product_id},
                    )
                    return False
                if row.last_stock_event_id == event_id:
                    logger.info(
                        "apply_stock_change: duplicate eventId, no-op",
                        extra={"correlationId": self._correlation_id, "eventId": event_id},
                    )
                    return False
                # A *distinct* event_id can still be a stale, out-of-order
                # redelivery (SQS gives no ordering guarantee) — event_id
                # equality alone can't catch that, only occurredAt can.
                # Both timestamps missing (e.g. old rows/events predating
                # this column) falls through to applying it, matching the
                # previous equality-only behavior.
                if (
                    occurred_at is not None
                    and row.last_stock_event_at is not None
                    and _as_aware_utc(occurred_at) <= _as_aware_utc(row.last_stock_event_at)
                ):
                    logger.info(
                        "apply_stock_change: stale/out-of-order event, dropped",
                        extra={
                            "correlationId": self._correlation_id,
                            "eventId": event_id,
                            "productId": product_id,
                        },
                    )
                    return False
                conn.execute(
                    products_table.update()
                    .where(products_table.c.id == product_id)
                    .values(
                        stock_state=stock_state,
                        available_from=available_from,
                        last_stock_event_id=event_id,
                        last_stock_event_at=occurred_at,
                    )
                )
                return True


def _as_aware_utc(value: datetime) -> datetime:
    """SQLite (this repo's test double for Aurora — see this module's own
    docstring) doesn't preserve tzinfo through a round trip even for
    `DateTime(timezone=True)` columns, unlike real Postgres. Everything
    this service ever writes here is UTC, so a naive value read back is
    treated as UTC rather than left ambiguous — makes the comparison in
    apply_stock_change work the same against either database."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _row_to_category(row) -> Category:
    return Category(id=row.id, name=row.name, icon_name=row.icon_name, sort_order=row.sort_order)


def _row_to_product(row) -> Product:
    return Product(
        id=row.id,
        category_id=row.category_id,
        name=row.name,
        description=row.description,
        unit=row.unit,
        price_b2c=float(row.price_b2c),
        price_b2b=float(row.price_b2b) if row.price_b2b is not None else None,
        image_url=row.image_url,
        tag=row.tag,
        subscription_eligible=row.subscription_eligible,
        is_veg=row.is_veg,
        is_organic=row.is_organic,
        stock_state=StockState(row.stock_state),
        available_from=row.available_from,
    )
