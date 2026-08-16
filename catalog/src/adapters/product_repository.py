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

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    and_,
    or_,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from domain.exceptions import ServiceUnavailableError
from domain.models import Category, Product, SearchFilters, SortOrder, StockState

logger = logging.getLogger(__name__)

metadata = MetaData()

categories_table = Table(
    "categories",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("icon_name", String(64), nullable=False),
    Column("sort_order", Integer, nullable=False, default=0),
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
)


def create_schema(engine: Engine) -> None:
    """Test-only convenience — production schema ownership is the raw SQL
    migration file, not this. Used to stand up the SQLite test double."""
    metadata.create_all(engine)


class SqlAlchemyProductRepository:
    def __init__(self, engine: Engine, correlation_id: str = "") -> None:
        self._engine = engine
        self._correlation_id = correlation_id

    def get_categories(self) -> list[Category]:
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(categories_table).order_by(categories_table.c.sort_order)
                ).fetchall()
        except SQLAlchemyError as exc:
            self._log_failure("get_categories", exc)
            raise ServiceUnavailableError("Failed to load categories") from exc
        return [_row_to_category(row) for row in rows]

    def get_products(self, category_id: str) -> list[Product]:
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(products_table)
                    .where(products_table.c.category_id == category_id)
                    .order_by(products_table.c.name)
                ).fetchall()
        except SQLAlchemyError as exc:
            self._log_failure("get_products", exc)
            raise ServiceUnavailableError("Failed to load products") from exc
        return [_row_to_product(row) for row in rows]

    def get_product(self, product_id: str) -> Product | None:
        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(products_table).where(products_table.c.id == product_id)
                ).fetchone()
        except SQLAlchemyError as exc:
            self._log_failure("get_product", exc)
            raise ServiceUnavailableError("Failed to load product") from exc
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
            # No `created_at` in this table's Core definition (unused by
            # any other query) — id is a uuid, not sortable by recency, so
            # newest-first isn't actually derivable from this schema yet.
            # Falls back to name order rather than a wrong-looking recency
            # sort; flagged as a real gap, not silently ignored.
            logger.warning("search: 'newest' sort requested but no recency column exists yet")
            stmt = stmt.order_by(products_table.c.name)
        else:
            stmt = stmt.order_by(products_table.c.name)

        try:
            with self._engine.connect() as conn:
                rows = conn.execute(stmt).fetchall()
        except SQLAlchemyError as exc:
            self._log_failure("search", exc)
            raise ServiceUnavailableError("Search failed") from exc
        return [_row_to_product(row) for row in rows]

    def apply_stock_change(
        self,
        product_id: str,
        event_id: str,
        stock_state: str,
        available_from,
    ) -> bool:
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    select(
                        products_table.c.id, products_table.c.last_stock_event_id
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
                conn.execute(
                    products_table.update()
                    .where(products_table.c.id == product_id)
                    .values(
                        stock_state=stock_state,
                        available_from=available_from,
                        last_stock_event_id=event_id,
                    )
                )
                return True
        except SQLAlchemyError as exc:
            self._log_failure("apply_stock_change", exc)
            raise ServiceUnavailableError("Failed to apply stock change") from exc

    def _log_failure(self, operation: str, exc: Exception) -> None:
        logger.error(
            f"product_repository.{operation} failed",
            extra={"correlationId": self._correlation_id, "error": str(exc)},
        )


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
