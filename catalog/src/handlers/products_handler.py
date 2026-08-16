"""GET /products, GET /products/{id} — MA-116 FR-1/FR-2."""

from fastapi import APIRouter, Depends, Query

from domain.catalog_service import CatalogService
from handlers.dependencies import get_catalog_service
from handlers.dto import serialize_product, success_envelope

router = APIRouter(tags=["products"])


@router.get("/products")
def list_products(
    categoryId: str = Query(...),  # noqa: N803 — matches the wire contract exactly
    service: CatalogService = Depends(get_catalog_service),
):
    products = service.get_products(categoryId)
    return success_envelope({"products": [serialize_product(p) for p in products]})


@router.get("/products/{product_id}")
def get_product(
    product_id: str,
    service: CatalogService = Depends(get_catalog_service),
):
    product = service.get_product(product_id)
    return success_envelope(serialize_product(product))
