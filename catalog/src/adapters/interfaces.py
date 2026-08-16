"""Abstract adapter interfaces (Protocols). Domain code depends on these
only, never on SQLAlchemy/boto3 directly."""

from datetime import datetime
from typing import Protocol

from domain.models import Category, Product, SearchFilters, SortOrder


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
