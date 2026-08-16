"""GET /search — MA-117 FR-1. Parses the repeated `filters=` query params
(`category:{id}`, `price:{min}-{max}`, `veg:true`, `organic:true`) — the
exact contract MA-115's mobile client builds via
`CatalogFilters.toQueryParams()`."""

from fastapi import APIRouter, Depends, Query

from domain.catalog_service import CatalogService
from domain.exceptions import InvalidRequestError
from domain.models import SearchFilters, SortOrder
from handlers.dependencies import get_catalog_service
from handlers.dto import serialize_product, success_envelope

router = APIRouter(tags=["search"])


def _parse_filters(raw: list[str]) -> SearchFilters:
    category_ids: list[str] = []
    min_price: float | None = None
    max_price: float | None = None
    veg_only = False
    organic_only = False

    for entry in raw:
        key, _, value = entry.partition(":")
        if key == "category":
            category_ids.append(value)
        elif key == "price":
            low, _, high = value.partition("-")
            if low:
                min_price = float(low)
            if high:
                max_price = float(high)
        elif key == "veg" and value == "true":
            veg_only = True
        elif key == "organic" and value == "true":
            organic_only = True
        # An unrecognized facet key is ignored, not a 400 — MA-117's own
        # Edge Cases table: forward-compatible if the mobile client and
        # this API drift slightly during parallel development.

    return SearchFilters(
        category_ids=category_ids,
        min_price=min_price,
        max_price=max_price,
        veg_only=veg_only,
        organic_only=organic_only,
    )


@router.get("/search")
def search(
    q: str | None = Query(None),
    filters: list[str] = Query(default=[]),
    sort: str | None = Query(None),
    service: CatalogService = Depends(get_catalog_service),
):
    # Both raise a bare ValueError on a malformed value (bad `sort`, a
    # non-numeric `price` bound) — translated into InvalidRequestError so
    # app.py's CatalogError handler returns a clean 400 in the documented
    # envelope instead of falling through to FastAPI's default bare 500.
    try:
        parsed_filters = _parse_filters(filters) if filters else None
        parsed_sort = SortOrder(sort) if sort else None
    except ValueError as exc:
        raise InvalidRequestError(f"Invalid search query parameters: {exc}") from exc
    products = service.search(q, parsed_filters, parsed_sort)
    return success_envelope({"products": [serialize_product(p) for p in products]})
