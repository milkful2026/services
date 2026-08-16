"""Catalog read/search orchestration (MA-116 FR-1-3, MA-117 FR-1) and
StockChanged application (MA-116 FR-5). Thin — most of the actual query
logic lives in the repository adapter; this layer's job is validating
inputs and translating "not found" into the right domain exception."""

from datetime import datetime

from adapters.interfaces import ProductRepositoryPort
from domain.exceptions import ProductNotFoundError
from domain.models import Category, Product, SearchFilters, SortOrder


class CatalogService:
    def __init__(self, repository: ProductRepositoryPort) -> None:
        self._repository = repository

    def get_categories(self) -> list[Category]:
        return self._repository.get_categories()

    def get_products(self, category_id: str) -> list[Product]:
        return self._repository.get_products(category_id)

    def get_product(self, product_id: str) -> Product:
        product = self._repository.get_product(product_id)
        if product is None:
            raise ProductNotFoundError(f"No product with id {product_id!r}")
        return product

    def search(
        self,
        query: str | None,
        filters: SearchFilters | None,
        sort: SortOrder | None,
    ) -> list[Product]:
        return self._repository.search(query, filters, sort)

    def apply_stock_change(
        self,
        product_id: str,
        event_id: str,
        stock_state: str,
        available_from,
        occurred_at: datetime | None = None,
    ) -> bool:
        """MA-116 FR-5. Unknown productId is deliberately a silent no-op,
        not an error — per this service's own spec's Edge Cases table:
        "could be a race with product creation, or a productId typo
        upstream", not worth failing the whole consumer over. Returns
        whether the update was actually applied (False for a redelivered
        duplicate `event_id`, a stale out-of-order `occurred_at`, or an
        unknown product), purely so the SQS consumer can log a clearer
        message — not otherwise acted on."""
        return self._repository.apply_stock_change(
            product_id, event_id, stock_state, available_from, occurred_at
        )
