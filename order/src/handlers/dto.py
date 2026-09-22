"""Response envelope helpers. Fixed `{requestId, status, data}` shape per
services/README.md §5 — the shape the mobile app's shared ApiClient
unwraps for every service. No request DTOs — this service has no public
write endpoints (FR-3 is read-only; every order is consumer-created)."""

import uuid
from typing import Any


def success_envelope(data: dict[str, Any] | list[Any]) -> dict[str, Any]:
    return {"requestId": str(uuid.uuid4()), "status": "success", "data": data}


def error_envelope(
    error_code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "requestId": str(uuid.uuid4()),
        "status": "error",
        "data": {"errorCode": error_code, "message": message, **(details or {})},
    }
