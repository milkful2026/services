"""GET /categories — MA-116 FR-3."""

from fastapi import APIRouter, Depends

from domain.catalog_service import CatalogService
from handlers.dependencies import get_catalog_service
from handlers.dto import serialize_category, success_list_envelope

router = APIRouter(tags=["categories"])


@router.get("/categories")
def list_categories(service: CatalogService = Depends(get_catalog_service)):
    categories = service.get_categories()
    return success_list_envelope([serialize_category(c) for c in categories])
