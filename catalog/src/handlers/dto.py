"""Response envelope + serialization helpers. Fixed envelope shape per
services/README.md §5 — identical to identity-auth/user/inventory's own
`{requestId, status, data}` shape, which is exactly what lets the mobile
app's shared `ApiClient` unwrap every service's responses the same way."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from shared.handlers.dto import error_envelope, success_envelope  # noqa: F401

from domain.models import AdminProductSummary, CatalogAuditLogEntry, Category, Page, Product


def serialize_category(category: Category) -> dict[str, Any]:
    return {"id": category.id, "name": category.name, "iconName": category.icon_name}


def serialize_product(product: Product) -> dict[str, Any]:
    # MA-116 FR-4: `price` is always the B2C price — the mobile app's
    # existing, shipped consumption of this exact key is unchanged.
    # `priceB2b` is additive (MA-156/MA-157): this same serializer backs
    # both the public GET /products/{id} (where price_b2b stayed
    # deliberately absent until now, per MA-116's original "no caller
    # can signal B2B intent" framing) AND the admin create/edit
    # responses (POST /products, PATCH /products/{id}) — and an admin
    # IS a caller who can legitimately see/edit it. Confirmed missing by
    # live-checking the real response while building MA-157's detail/
    # edit page, which needs to show the current priceB2b value; adding
    # it here (rather than a second admin-only serializer) keeps one
    # source of truth for "the full product shape" the way isActive/
    # hsnCode/taxRate already do for the identical reason.
    # isActive IS serialized (MA-156 FR-7's edge case: a deactivated
    # product looked up by direct id must show isActive: false, not 404).
    return {
        "id": product.id,
        "categoryId": product.category_id,
        "name": product.name,
        "description": product.description,
        "unit": product.unit,
        "price": product.price_b2c,
        "priceB2b": product.price_b2b,
        "imageUrl": product.image_url,
        "tag": product.tag,
        "subscriptionEligible": product.subscription_eligible,
        "isVeg": product.is_veg,
        "isOrganic": product.is_organic,
        "stockState": product.stock_state.value,
        "availableFrom": product.available_from.isoformat() if product.available_from else None,
        "isActive": product.is_active,
        "hsnCode": product.hsn_code,
        "taxRate": product.tax_rate,
    }


# `/categories`'s `data` is a bare array — matches the mobile client's
# `ApiClient.requestList` (see its own docstring: the same bare-array
# convention `/delivery/slots` already uses elsewhere in this platform),
# unlike every other endpoint here which wraps its list in a named object
# key (`{"products": [...]}`). Named separately at call sites purely to
# document that distinction — the wrapper itself is identical either way.
success_list_envelope = success_envelope


# --- MA-156 (MA-42): admin write API --------------------------------------


class CreateProductRequest(BaseModel):
    categoryId: str  # noqa: N815 — wire contract casing
    name: str
    description: str = ""
    unit: str
    priceB2c: float  # noqa: N815
    priceB2b: float | None = None  # noqa: N815
    imageUrl: str | None = None  # noqa: N815
    tag: str | None = None
    subscriptionEligible: bool = False  # noqa: N815
    isVeg: bool = True  # noqa: N815
    isOrganic: bool = False  # noqa: N815
    hsnCode: str | None = None  # noqa: N815
    taxRate: float | None = None  # noqa: N815
    reason: str | None = None

    def to_fields(self) -> dict[str, Any]:
        return {
            "category_id": self.categoryId,
            "name": self.name,
            "description": self.description,
            "unit": self.unit,
            "price_b2c": self.priceB2c,
            "price_b2b": self.priceB2b,
            "image_url": self.imageUrl,
            "tag": self.tag,
            "subscription_eligible": self.subscriptionEligible,
            "is_veg": self.isVeg,
            "is_organic": self.isOrganic,
            "hsn_code": self.hsnCode,
            "tax_rate": self.taxRate,
        }


class UpdateProductRequest(BaseModel):
    """FR-2 — every field optional; only the ones actually present on the
    incoming JSON body are translated to changes (`to_changes()` uses
    `model_fields_set`, not a None-check, so an explicit `null` your
    caller never sends is never confused with "field omitted")."""

    categoryId: str | None = Field(default=None)  # noqa: N815
    name: str | None = None
    description: str | None = None
    unit: str | None = None
    priceB2c: float | None = None  # noqa: N815
    priceB2b: float | None = None  # noqa: N815
    imageUrl: str | None = None  # noqa: N815
    tag: str | None = None
    subscriptionEligible: bool | None = None  # noqa: N815
    isVeg: bool | None = None  # noqa: N815
    isOrganic: bool | None = None  # noqa: N815
    hsnCode: str | None = None  # noqa: N815
    taxRate: float | None = None  # noqa: N815
    isActive: bool | None = None  # noqa: N815
    reason: str | None = None

    _FIELD_TO_COLUMN = {
        "categoryId": "category_id",
        "name": "name",
        "description": "description",
        "unit": "unit",
        "priceB2c": "price_b2c",
        "priceB2b": "price_b2b",
        "imageUrl": "image_url",
        "tag": "tag",
        "subscriptionEligible": "subscription_eligible",
        "isVeg": "is_veg",
        "isOrganic": "is_organic",
        "hsnCode": "hsn_code",
        "taxRate": "tax_rate",
        "isActive": "is_active",
    }

    def to_changes(self) -> dict[str, Any]:
        set_fields = self.model_fields_set - {"reason"}
        return {self._FIELD_TO_COLUMN[f]: getattr(self, f) for f in set_fields}


class CreateCategoryRequest(BaseModel):
    name: str
    iconName: str  # noqa: N815
    sortOrder: int = 0  # noqa: N815


class UpdateCategoryRequest(BaseModel):
    name: str | None = None
    iconName: str | None = None  # noqa: N815
    sortOrder: int | None = None  # noqa: N815

    _FIELD_TO_COLUMN = {"name": "name", "iconName": "icon_name", "sortOrder": "sort_order"}

    def to_changes(self) -> dict[str, Any]:
        set_fields = self.model_fields_set
        return {self._FIELD_TO_COLUMN[f]: getattr(self, f) for f in set_fields}


def serialize_admin_product_summary(summary: AdminProductSummary) -> dict[str, Any]:
    return {
        "id": summary.id,
        "categoryId": summary.category_id,
        "name": summary.name,
        "priceB2c": summary.price_b2c,
        "priceB2b": summary.price_b2b,
        "isActive": summary.is_active,
        "stockState": summary.stock_state.value,
        "createdAt": _iso(summary.created_at),
        "updatedAt": _iso(summary.updated_at),
    }


def serialize_catalog_audit_entry(entry: CatalogAuditLogEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "productId": entry.product_id,
        "adminId": entry.admin_id,
        "action": entry.action,
        "previousValue": entry.previous_value,
        "newValue": entry.new_value,
        "reason": entry.reason,
        "createdAt": _iso(entry.created_at),
    }


def serialize_page(page: Page, item_serializer) -> dict[str, Any]:
    return {
        "items": [item_serializer(item) for item in page.items],
        "total": page.total,
        "page": page.page,
        "pageSize": page.page_size,
    }


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None
