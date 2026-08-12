"""GET /v1/serviceability/check — public entrypoint (FR-1)."""

from fastapi import APIRouter, Depends, Query

from domain.serviceability_service import ServiceabilityService
from handlers.dependencies import get_serviceability_service
from handlers.dto import serialize_result, success_envelope

router = APIRouter(prefix="/v1", tags=["serviceability"])


@router.get("/serviceability/check")
def check_serviceability(
    pincode: str = Query(...),
    lat: float | None = Query(None),
    lng: float | None = Query(None),
    service: ServiceabilityService = Depends(get_serviceability_service),
):
    result = service.check(pincode, lat, lng)
    return success_envelope(serialize_result(result))
