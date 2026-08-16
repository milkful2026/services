"""Response envelope + serialization helpers. Fixed envelope shape per
services/README.md §5 — identical to identity-auth/user/inventory's own
`{requestId, status, data}` shape, which is exactly what lets the mobile
app's shared `ApiClient` unwrap every service's responses the same way."""

import uuid
from typing import Any

from domain.models import Category, Product


def serialize_category(category: Category) -> dict[str, Any]:
    return {"id": category.id, "name": category.name, "iconName": category.icon_name}


def serialize_product(product: Product) -> dict[str, Any]:
    # MA-116 FR-4: always the B2C price, until a caller can signal B2B
    # intent (still-open Q2) — price_b2b is deliberately not serialized.
    return {
        "id": product.id,
        "categoryId": product.category_id,
        "name": product.name,
        "description": product.description,
        "unit": product.unit,
        "price": product.price_b2c,
        "imageUrl": product.image_url,
        "tag": product.tag,
        "subscriptionEligible": product.subscription_eligible,
        "isVeg": product.is_veg,
        "isOrganic": product.is_organic,
        "stockState": product.stock_state.value,
        "availableFrom": product.available_from.isoformat() if product.available_from else None,
    }


def success_envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"requestId": str(uuid.uuid4()), "status": "success", "data": data}


def success_list_envelope(data: list[Any]) -> dict[str, Any]:
    """`/categories`'s `data` is a bare array — matches the mobile client's
    `ApiClient.requestList` (see its own docstring: the same bare-array
    convention `/delivery/slots` already uses elsewhere in this
    platform), unlike every other endpoint here which wraps its list in a
    named object key (`{"products": [...]}`)."""
    return {"requestId": str(uuid.uuid4()), "status": "success", "data": data}


def error_envelope(
    error_code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "requestId": str(uuid.uuid4()),
        "status": "error",
        "data": {"errorCode": error_code, "message": message, **(details or {})},
    }
