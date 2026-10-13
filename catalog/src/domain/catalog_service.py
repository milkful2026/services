"""Catalog read/search orchestration (MA-116 FR-1-3, MA-117 FR-1),
StockChanged application (MA-116 FR-5), and admin product/category
writes (MA-156/MA-42). Thin — most of the actual query logic lives in
the repository adapter; this layer's job is validating inputs and
translating "not found"/invalid input into the right domain exception.
"""

import uuid
from datetime import datetime

from adapters.interfaces import CatalogEventPublisherPort, ProductRepositoryPort
from domain.exceptions import CategoryNotFoundError, ProductNotFoundError, ValidationError
from domain.models import Category, Page, Product, SearchFilters, SortOrder

# Columns that are NOT NULL in products_table (migrations/0001 + 0003) —
# used by update_product() to reject an explicit JSON `null` for one of
# these with a 400, rather than letting it reach the DB as `SET col =
# NULL` and surface as a 503 from the resulting constraint violation.
# Maps to the field's wire (camelCase) name, so the resulting
# ValidationError message matches every other validation message here
# (`priceB2c must be...`, not `price_b2c must be...`). `price_b2b`/
# `image_url`/`tag`/`hsn_code`/`tax_rate` are deliberately absent — those
# columns are nullable, so `null` for them is legitimate.
_NOT_NULLABLE_PRODUCT_FIELDS = {
    "category_id": "categoryId",
    "name": "name",
    "description": "description",
    "unit": "unit",
    "price_b2c": "priceB2c",
    "subscription_eligible": "subscriptionEligible",
    "is_veg": "isVeg",
    "is_organic": "isOrganic",
    "is_active": "isActive",
}
# Same reasoning for categories_table (migrations/0001) — all three
# columns this API can edit are NOT NULL.
_NOT_NULLABLE_CATEGORY_FIELDS = {"name": "name", "icon_name": "iconName", "sort_order": "sortOrder"}


class CatalogService:
    def __init__(
        self,
        repository: ProductRepositoryPort,
        event_publisher: CatalogEventPublisherPort | None = None,
    ) -> None:
        self._repository = repository
        self._event_publisher = event_publisher

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

    # --- MA-156 (MA-42): admin write API ----------------------------------

    def create_product(self, admin_id: str, reason: str | None, **fields) -> Product:
        """FR-1. `fields` keys are the snake_case subset of
        `_WRITABLE_PRODUCT_FIELDS` — translation from the wire's
        camelCase happens at the handler/DTO layer, not here."""
        self._validate_category(fields.get("category_id"))
        self._validate_price(fields.get("price_b2c"))
        self._validate_non_empty("name", fields.get("name"))
        self._validate_non_empty("unit", fields.get("unit"))

        product = self._repository.create_product(str(uuid.uuid4()), admin_id, reason, **fields)
        if self._event_publisher is not None:
            # §11 Risk 1 / §4 FR-1: a publish failure must not undo the
            # already-committed product row — the adapter itself logs and
            # swallows it (see catalog_event_publisher.py), never raises.
            self._event_publisher.publish_catalog_updated(product.id)
        return product

    def update_product(self, product_id: str, admin_id: str, reason: str | None, **changes) -> Product:
        """FR-2. No `CatalogUpdated` publish on edit (Inventory's consumer
        only cares that the product exists, once, on create)."""
        self._reject_null_for_not_nullable(changes, _NOT_NULLABLE_PRODUCT_FIELDS)
        if "category_id" in changes:
            self._validate_category(changes["category_id"])
        if "price_b2c" in changes:
            self._validate_price(changes["price_b2c"])
        if "name" in changes:
            self._validate_non_empty("name", changes["name"])
        if "unit" in changes:
            self._validate_non_empty("unit", changes["unit"])

        return self._repository.update_product(product_id, admin_id, reason, **changes)

    def create_category(self, name: str, icon_name: str, sort_order: int = 0) -> Category:
        return self._repository.create_category(
            str(uuid.uuid4()), name=name, icon_name=icon_name, sort_order=sort_order
        )

    def update_category(self, category_id: str, **changes) -> Category:
        self._reject_null_for_not_nullable(changes, _NOT_NULLABLE_CATEGORY_FIELDS)
        return self._repository.update_category(category_id, **changes)

    def list_products_admin(
        self, category_id: str | None, is_active: bool | None, page: int, page_size: int
    ) -> Page:
        return self._repository.list_products_admin(category_id, is_active, page, page_size)

    def get_audit_log(self, product_id: str, page: int, page_size: int) -> Page:
        return self._repository.get_audit_log(product_id, page, page_size)

    # --- internal ----------------------------------------------------------

    @staticmethod
    def _reject_null_for_not_nullable(changes: dict, not_nullable_fields: dict) -> None:
        """A PATCH field present in `changes` with value `None` means the
        caller sent an explicit JSON `null` (every field on
        UpdateProductRequest/UpdateCategoryRequest is `Optional`, so
        `to_changes()`'s `model_fields_set` check already told us this
        key was actually present on the wire, not merely omitted). For a
        NOT NULL column that must be rejected as a 400 here — otherwise
        it reaches the DB as `SET col = NULL` and surfaces as a 503 from
        the resulting constraint violation instead."""
        for field, wire_name in not_nullable_fields.items():
            if field in changes and changes[field] is None:
                raise ValidationError(f"{wire_name} must not be null")

    def _validate_category(self, category_id: str | None) -> None:
        if category_id is not None and not self._repository.category_exists(category_id):
            raise CategoryNotFoundError(f"Unknown category {category_id!r}")

    @staticmethod
    def _validate_price(price_b2c) -> None:
        if price_b2c is not None and price_b2c <= 0:
            raise ValidationError("priceB2c must be a positive number")

    @staticmethod
    def _validate_non_empty(field_name: str, value: str | None) -> None:
        if value is not None and not value.strip():
            raise ValidationError(f"{field_name} must not be empty")
