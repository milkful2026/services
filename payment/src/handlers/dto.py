"""Request/response DTOs + the `{requestId, status, data}` envelope
(services/README.md §5 — the shape the mobile app's shared ApiClient
unwraps for every service)."""

import uuid
from typing import Any

from pydantic import BaseModel, Field


class CreatePaymentRequest(BaseModel):
    purpose: str
    amountPaise: int = Field(gt=0)  # noqa: N815 — wire contract casing
    currency: str = "INR"
    method: str | None = None


class ConfirmPaymentRequest(BaseModel):
    razorpayPaymentId: str  # noqa: N815
    razorpayOrderId: str  # noqa: N815
    razorpaySignature: str  # noqa: N815


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
