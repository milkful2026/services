"""Response envelope helpers — the fixed `{requestId, status, data}` shape
per services/README.md §5, the shape the mobile app's shared ApiClient
unwraps for every service that uses it.

Was hand-duplicated byte-for-byte in wallet/subscription/order/payment/
catalog/inventory/pricing-offer's own handlers/dto.py — moved here per
services/README.md §2. cart/user/identity-auth intentionally do NOT use
this: they return full API-Gateway Lambda response shapes
(success_response/error_response/no_content_response), a different
concern — leave those alone.
"""

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
