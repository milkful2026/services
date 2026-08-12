"""GET /v1/internal/serviceability/check — internal entrypoint (FR-2),
for User Service only.

Auth is enforced at the network/infra layer (security group restricting
which service can reach this path via the internal ALB), not in this
handler — see the CDK stack and README's flagged decision: this is a
simplification of the spec's literal "IAM/mTLS" wording. The handler
itself is intentionally identical to the public one otherwise.
"""

from fastapi import APIRouter, Depends, Query

from domain.serviceability_service import ServiceabilityService
from handlers.dependencies import get_serviceability_service
from handlers.dto import serialize_result, success_envelope

router = APIRouter(prefix="/v1/internal", tags=["serviceability-internal"])


@router.get("/serviceability/check")
def check_serviceability_internal(
    pincode: str = Query(...),
    lat: float | None = Query(None),
    lng: float | None = Query(None),
    service: ServiceabilityService = Depends(get_serviceability_service),
):
    result = service.check(pincode, lat, lng)
    return success_envelope(serialize_result(result))
