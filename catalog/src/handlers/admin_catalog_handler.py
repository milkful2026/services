"""POST /products (MA-156 FR-1), PATCH /products/{id} (FR-2), POST
/categories / PATCH /categories/{id} (FR-3), GET /admin/products (FR-4),
GET /products/{id}/audit-log (FR-5).

All six routes require the Ops role (FR-6), enforced by
`require_ops_role` — see admin_context.py for how the admin authorizer's
context reaches this handler. `adminId` for the audit row always comes
from that context, never the request body (same anti-spoofing rule as
Inventory's MA-119/MA-150).

No `/v1` path prefix — Catalog's existing routes (products_handler.py/
categories_handler.py) are bare, unlike Inventory's admin router; these
new routes follow Catalog's own convention (MA-156 §6 point 2).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from domain.catalog_service import CatalogService
from handlers.admin_context import require_ops_role
from handlers.dependencies import get_catalog_service
from handlers.dto import (
    CreateCategoryRequest,
    CreateProductRequest,
    UpdateCategoryRequest,
    UpdateProductRequest,
    serialize_admin_product_summary,
    serialize_catalog_audit_entry,
    serialize_category,
    serialize_page,
    serialize_product,
    success_envelope,
)

router = APIRouter(tags=["catalog-admin"])


@router.post("/products", status_code=201)
def create_product(
    body: CreateProductRequest,
    admin: dict = Depends(require_ops_role),
    service: CatalogService = Depends(get_catalog_service),
):
    product = service.create_product(admin["adminId"], body.reason, **body.to_fields())
    return success_envelope(serialize_product(product))


@router.patch("/products/{product_id}")
def update_product(
    product_id: str,
    body: UpdateProductRequest,
    admin: dict = Depends(require_ops_role),
    service: CatalogService = Depends(get_catalog_service),
):
    product = service.update_product(product_id, admin["adminId"], body.reason, **body.to_changes())
    return success_envelope(serialize_product(product))


@router.post("/categories", status_code=201)
def create_category(
    body: CreateCategoryRequest,
    admin: dict = Depends(require_ops_role),
    service: CatalogService = Depends(get_catalog_service),
):
    category = service.create_category(body.name, body.iconName, body.sortOrder)
    return success_envelope(serialize_category(category))


@router.patch("/categories/{category_id}")
def update_category(
    category_id: str,
    body: UpdateCategoryRequest,
    admin: dict = Depends(require_ops_role),
    service: CatalogService = Depends(get_catalog_service),
):
    category = service.update_category(category_id, **body.to_changes())
    return success_envelope(serialize_category(category))


@router.get("/admin/products")
def list_products_admin(
    categoryId: str | None = Query(default=None),  # noqa: N803 — wire contract casing
    isActive: bool | None = Query(default=None),  # noqa: N803
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=50, ge=1, le=500),  # noqa: N803
    admin: dict = Depends(require_ops_role),
    service: CatalogService = Depends(get_catalog_service),
):
    result = service.list_products_admin(categoryId, isActive, page, pageSize)
    return success_envelope(serialize_page(result, serialize_admin_product_summary))


@router.get("/products/{product_id}/audit-log")
def get_audit_log(
    product_id: str,
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=50, ge=1, le=500),  # noqa: N803
    admin: dict = Depends(require_ops_role),
    service: CatalogService = Depends(get_catalog_service),
):
    result = service.get_audit_log(product_id, page, pageSize)
    return success_envelope(serialize_page(result, serialize_catalog_audit_entry))
