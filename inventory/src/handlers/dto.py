"""Response envelope + serialization helpers. Fixed envelope shape per
services/README.md §5."""

import uuid
from typing import Any

from domain.models import ServiceabilityResult


def serialize_result(result: ServiceabilityResult) -> dict[str, Any]:
    return {
        "serviceable": result.serviceable,
        "zoneId": result.zone_id,
        "zoneName": result.zone_name,
        "slots": [{"id": s.id, "label": s.label} for s in result.slots],
        "message": result.message,
        "waitlistAvailable": result.waitlist_available,
    }


def success_envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"requestId": str(uuid.uuid4()), "status": "success", "data": data}


def error_envelope(
    error_code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "requestId": str(uuid.uuid4()),
        "status": "error",
        "data": {"errorCode": error_code, "message": message, **(details or {})},
    }
