"""SQLAlchemy Core repository for `products`/`categories`/`catalog_audit_log`.

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
import uuid
from datetime import UTC, datetime

from shared.adapters.db_operation import SqlAlchemyOperationMixin
from sqlalchemy import (
    JSON,
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

from domain.exceptions import CategoryNotFoundError, ProductNotFoundError, ServiceUnavailableError
from domain.models import (
    AdminProductSummary,
    CatalogAuditLogEntry,
    Category,
    Page,
    Product,
    SearchFilters,
    SortOrder,
    StockState,
)

logger = logging.getLogger(__name__)

metadata = MetaData()

# Column-for-column compatible with migrations/0001_products_categories.sql,
# migrations/0002_stock_change_ordering.sql, and migrations/0003_catalog_admin.sql
# — see that file's own header comment for why this must be kept in sync by hand.
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
    # MA-156 (MA-42) — admin-write fields, additive per 0003_catalog_admin.sql.
    Column("is_active", Boolean, nullable=False, default=True),
    Column("hsn_code", String(16), nullable=True),
    Column("tax_rate", Numeric(5, 2), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    ),
)

# MA-156 (MA-42) — generic action/previous_value/new_value JSONB diff, not
# a copy of Inventory's typed-integer inventory_audit_log (§7/§11 Risk 5).
# JSON (not JSONB) here, same reasoning as zone_repository.py's
# pincode_prefixes/polygon columns — keeps this Core table portable to
# the SQLite test double; the raw migration still declares JSONB for
# real Postgres.
catalog_audit_log_table = Table(
    "catalog_audit_log",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("product_id", String(64), ForeignKey("products.id"), nullable=False),
    Column("admin_id", String(64), nullable=False),
    Column("action", String(16), nullable=False),
    Column("previous_value", JSON, nullable=True),
    Column("new_value", JSON, nullable=True),
    Column("reason", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


def create_schema(engine: Engine) -> None:
    """Test-only convenience — production schema ownership is the raw SQL
    migration file, not this. Used to stand up the SQLite test double."""
    metadata.create_all(engine)


def new_audit_id() -> str:
    return str(uuid.uuid4())


# catalog_audit_log.previous_value/new_value are stored camelCase, matching
# every other field this API serializes — portal-ui (MA-157) renders this
# diff directly, so it must match the wire contract, not the DB's own
# snake_case column names.
_COLUMN_TO_WIRE = {
    "category_id": "categoryId",
    "name": "name",
    "description": "description",
    "unit": "unit",
    "price_b2c": "priceB2c",
    "price_b2b": "priceB2b",
    "image_url": "imageUrl",
    "tag": "tag",
    "subscription_eligible": "subscriptionEligible",
    "is_veg": "isVeg",
    "is_organic": "isOrganic",
    "is_active": "isActive",
    "hsn_code": "hsnCode",
    "tax_rate": "taxRate",
}


class SqlAlchemyProductRepository(SqlAlchemyOperationMixin):
    _unavailable_error = ServiceUnavailableError
    _log_prefix = "product_repository"

    def __init__(self, engine: Engine, correlation_id: str = "") -> None:
        self._engine = engine
        self._correlation_id = correlation_id

    def get_categories(self) -> list[Category]:
        with self._db_operation("get_categories", "Failed to load categories"):
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(categories_table).order_by(categories_table.c.sort_order)
                ).fetchall()
        return [_row_to_category(row) for row in rows]

    def get_products(self, category_id: str) -> list[Product]:
        # FR-7: list context filters out inactive products.
        with self._db_operation("get_products", "Failed to load products"):
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(products_table)
                    .where(
                        products_table.c.category_id == category_id,
                        products_table.c.is_active.is_(True),
                    )
                    .order_by(products_table.c.name)
                ).fetchall()
        return [_row_to_product(row) for row in rows]

    def get_product(self, product_id: str) -> Product | None:
        # FR-7: a direct-id lookup never filters on is_active — an old
        # cart/order/subscription reference must keep resolving.
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
        # FR-7: search is a discovery path, like the public list — filters
        # out inactive products too.
        stmt = select(products_table)

        conditions = [products_table.c.is_active.is_(True)]
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

    # --- MA-156 (MA-42): admin write API ----------------------------------

    def category_exists(self, category_id: str) -> bool:
        with self._db_operation("category_exists", "Failed to check category"):
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(categories_table.c.id).where(categories_table.c.id == category_id)
                ).fetchone()
        return row is not None

    def create_product(
        self, product_id: str, admin_id: str, reason: str | None, **fields
    ) -> Product:
        values = {
            "id": product_id,
            "is_active": True,
            "stock_state": StockState.IN_STOCK.value,
            **fields,
        }
        with self._db_operation("create_product", "Failed to create product"):
            with self._engine.begin() as conn:
                conn.execute(products_table.insert().values(**values))
                row = conn.execute(
                    select(products_table).where(products_table.c.id == product_id)
                ).one()
                product = _row_to_product(row)
                self._insert_audit_row(
                    conn,
                    product_id=product_id,
                    admin_id=admin_id,
                    action="CREATE",
                    previous_value=None,
                    new_value=_to_wire_keys(_product_row_to_json(row)),
                    reason=reason,
                )
        return product

    def update_product(
        self, product_id: str, admin_id: str, reason: str | None, **changes
    ) -> Product:
        with self._db_operation("update_product", "Failed to update product"):
            with self._engine.begin() as conn:
                # Locked so a concurrent PATCH on the same product can't
                # read this same "before" snapshot — without this, two
                # overlapping updates can each write an audit row whose
                # previous_value doesn't reflect the other's already-
                # committed change, corrupting the one thing
                # catalog_audit_log exists to get right. SQLite (the test
                # double) doesn't enforce real row locking — same
                # documented fidelity gap as zone_repository.py/
                # stock_repository.py's own with_for_update() calls.
                before_row = conn.execute(
                    select(products_table)
                    .where(products_table.c.id == product_id)
                    .with_for_update()
                ).fetchone()
                if before_row is None:
                    raise ProductNotFoundError(f"Unknown product {product_id!r}")

                if not changes:
                    return _row_to_product(before_row)

                conn.execute(
                    products_table.update()
                    .where(products_table.c.id == product_id)
                    .values(**changes, updated_at=func.now())
                )
                after_row = conn.execute(
                    select(products_table).where(products_table.c.id == product_id)
                ).one()

                # Only the fields that actually changed go into the audit
                # row's previous_value/new_value — not the whole row
                # (FR-2) — so an entry reads as "what actually changed."
                before_json = _product_row_to_json(before_row)
                after_json = _product_row_to_json(after_row)
                changed_keys = [k for k in changes if before_json.get(k) != after_json.get(k)]
                self._insert_audit_row(
                    conn,
                    product_id=product_id,
                    admin_id=admin_id,
                    action="UPDATE",
                    previous_value=_to_wire_keys({k: before_json[k] for k in changed_keys}),
                    new_value=_to_wire_keys({k: after_json[k] for k in changed_keys}),
                    reason=reason,
                )
                return _row_to_product(after_row)

    def create_category(self, category_id: str, **fields) -> Category:
        with self._db_operation("create_category", "Failed to create category"):
            with self._engine.begin() as conn:
                conn.execute(categories_table.insert().values(id=category_id, **fields))
                row = conn.execute(
                    select(categories_table).where(categories_table.c.id == category_id)
                ).one()
        return _row_to_category(row)

    def update_category(self, category_id: str, **changes) -> Category:
        with self._db_operation("update_category", "Failed to update category"):
            with self._engine.begin() as conn:
                existing = conn.execute(
                    select(categories_table.c.id).where(categories_table.c.id == category_id)
                ).fetchone()
                if existing is None:
                    raise CategoryNotFoundError(f"Unknown category {category_id!r}")
                if changes:
                    conn.execute(
                        categories_table.update()
                        .where(categories_table.c.id == category_id)
                        .values(**changes)
                    )
                row = conn.execute(
                    select(categories_table).where(categories_table.c.id == category_id)
                ).one()
        return _row_to_category(row)

    def list_products_admin(
        self, category_id: str | None, is_active: bool | None, page: int, page_size: int
    ) -> Page:
        conditions = []
        if category_id is not None:
            conditions.append(products_table.c.category_id == category_id)
        if is_active is not None:
            conditions.append(products_table.c.is_active.is_(is_active))

        with self._db_operation("list_products_admin", "Failed to load admin product list"):
            with self._engine.connect() as conn:
                count_stmt = select(func.count()).select_from(products_table)
                list_stmt = select(products_table).order_by(products_table.c.name)
                if conditions:
                    count_stmt = count_stmt.where(and_(*conditions))
                    list_stmt = list_stmt.where(and_(*conditions))
                total = conn.execute(count_stmt).scalar_one()
                rows = conn.execute(
                    list_stmt.offset((page - 1) * page_size).limit(page_size)
                ).fetchall()
        return Page(
            items=[_row_to_admin_summary(r) for r in rows],
            total=int(total),
            page=page,
            page_size=page_size,
        )

    def get_audit_log(self, product_id: str, page: int, page_size: int) -> Page:
        with self._db_operation("get_audit_log", "Failed to load audit log"):
            with self._engine.connect() as conn:
                exists = conn.execute(
                    select(products_table.c.id).where(products_table.c.id == product_id)
                ).fetchone()
                if exists is None:
                    raise ProductNotFoundError(f"Unknown product {product_id!r}")

                total = conn.execute(
                    select(func.count())
                    .select_from(catalog_audit_log_table)
                    .where(catalog_audit_log_table.c.product_id == product_id)
                ).scalar_one()

                rows = conn.execute(
                    select(catalog_audit_log_table)
                    .where(catalog_audit_log_table.c.product_id == product_id)
                    .order_by(catalog_audit_log_table.c.created_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                ).fetchall()
        return Page(
            items=[_row_to_audit_entry(r) for r in rows],
            total=int(total),
            page=page,
            page_size=page_size,
        )

    def _insert_audit_row(
        self,
        conn,
        *,
        product_id: str,
        admin_id: str,
        action: str,
        previous_value: dict | None,
        new_value: dict | None,
        reason: str | None,
    ) -> None:
        conn.execute(
            catalog_audit_log_table.insert().values(
                id=new_audit_id(),
                product_id=product_id,
                admin_id=admin_id,
                action=action,
                previous_value=previous_value,
                new_value=new_value,
                reason=reason,
            )
        )


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
        is_active=row.is_active,
        hsn_code=row.hsn_code,
        tax_rate=float(row.tax_rate) if row.tax_rate is not None else None,
    )


def _row_to_admin_summary(row) -> AdminProductSummary:
    return AdminProductSummary(
        id=row.id,
        category_id=row.category_id,
        name=row.name,
        price_b2c=float(row.price_b2c),
        price_b2b=float(row.price_b2b) if row.price_b2b is not None else None,
        is_active=row.is_active,
        stock_state=StockState(row.stock_state),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_audit_entry(row) -> CatalogAuditLogEntry:
    return CatalogAuditLogEntry(
        id=row.id,
        product_id=row.product_id,
        admin_id=row.admin_id,
        action=row.action,
        previous_value=row.previous_value,
        new_value=row.new_value,
        reason=row.reason,
        created_at=row.created_at,
    )


def _to_wire_keys(snake_case_dict: dict) -> dict:
    # `.get(k, k)`, not `[k]` — `_COLUMN_TO_WIRE` is a hand-maintained
    # list that has to stay in sync with `_WRITABLE_PRODUCT_FIELDS`
    # (catalog_service.py) and `products_table` across multiple files
    # with no shared source of truth. A future writable column added to
    # the other two but missed here must not raise an uncaught KeyError
    # from inside an open transaction (which `_db_operation` — only
    # catches SQLAlchemyError — would let propagate as a raw 500); it
    # instead falls back to the column's own snake_case name, a visible,
    # debuggable degradation rather than a crash.
    return {_COLUMN_TO_WIRE.get(k, k): v for k, v in snake_case_dict.items()}


def _product_row_to_json(row) -> dict:
    """Full-row snapshot used both as FR-1's `new_value` (the whole row)
    and as the before/after source FR-2's diff picks changed keys from.
    Keyed by the same snake_case column names `update_product`'s
    `**changes` kwargs use, so `changed_keys` lookups line up directly."""
    return {
        "category_id": row.category_id,
        "name": row.name,
        "description": row.description,
        "unit": row.unit,
        "price_b2c": float(row.price_b2c),
        "price_b2b": float(row.price_b2b) if row.price_b2b is not None else None,
        "image_url": row.image_url,
        "tag": row.tag,
        "subscription_eligible": row.subscription_eligible,
        "is_veg": row.is_veg,
        "is_organic": row.is_organic,
        "is_active": row.is_active,
        "hsn_code": row.hsn_code,
        "tax_rate": float(row.tax_rate) if row.tax_rate is not None else None,
    }

