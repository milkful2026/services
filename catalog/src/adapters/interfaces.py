"""Abstract adapter interfaces (Protocols). Domain code depends on these
only, never on SQLAlchemy/boto3 directly."""

from datetime import datetime
from typing import Protocol

from domain.models import Category, Page, Product, SearchFilters, SortOrder


class ProductRepositoryPort(Protocol):
    def get_categories(self) -> list[Category]: ...

    def get_products(self, category_id: str) -> list[Product]: ...

    def get_product(self, product_id: str) -> Product | None: ...

    def search(
        self, query: str | None, filters: SearchFilters | None, sort: SortOrder | None
    ) -> list[Product]:
        """MA-117 FR-1's contract, implemented directly against this
        service's own Aurora table rather than a real OpenSearch index —
        see product_repository.py's own docstring for why."""
        ...

    def apply_stock_change(
        self,
        product_id: str,
        event_id: str,
        stock_state: str,
        available_from,
        occurred_at: datetime | None = None,
    ) -> bool:
        """Returns False (no-op) if `event_id` was already applied to this
        product, or if `occurred_at` is not after the last-applied event's
        own timestamp (a stale, out-of-order redelivery) — MA-116 FR-5's
        idempotency guarantee against SQS redelivery. Returns True if the
        update was actually applied."""
        ...

    # --- MA-156 (MA-42): admin write API ----------------------------------

    def category_exists(self, category_id: str) -> bool: ...

    def create_product(
        self, product_id: str, admin_id: str, reason: str | None, **fields
    ) -> Product:
        """FR-1 — one transaction: INSERT products (is_active=true) ->
        INSERT catalog_audit_log (action='CREATE', previous_value=NULL,
        new_value=<full new row>, reason)."""
        ...

    def update_product(
        self, product_id: str, admin_id: str, reason: str | None, **changes
    ) -> Product:
        """FR-2 — one transaction: UPDATE products SET <changed fields> ->
        INSERT catalog_audit_log (action='UPDATE', previous_value/
        new_value restricted to only the fields that actually changed).
        Raises ProductNotFoundError for an unknown id."""
        ...

    def create_category(self, category_id: str, **fields) -> Category: ...

    def update_category(self, category_id: str, **changes) -> Category:
        """Raises CategoryNotFoundError for an unknown id."""
        ...

    def list_products_admin(
        self, category_id: str | None, is_active: bool | None, page: int, page_size: int
    ) -> Page:
        """FR-4 — includes inactive products, unlike the public reads."""
        ...

    def get_audit_log(self, product_id: str, page: int, page_size: int) -> Page:
        """FR-5 — newest-first. Raises ProductNotFoundError for an unknown
        product id."""
        ...


class CatalogEventPublisherPort(Protocol):
    def publish_catalog_updated(self, product_id: str) -> None:
        """FR-1 — published after the create transaction commits. A
        publish failure is logged by the adapter and swallowed, never
        raised (§11 Risk 1) — the product row is already committed."""
        ...
