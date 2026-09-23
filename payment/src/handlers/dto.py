"""Request/response DTOs + the `{requestId, status, data}` envelope
(services/README.md §5 — the shape the mobile app's shared ApiClient
unwraps for every service)."""

from pydantic import BaseModel, Field
from shared.handlers.dto import error_envelope, success_envelope  # noqa: F401


class CreatePaymentRequest(BaseModel):
    purpose: str
    amountPaise: int = Field(gt=0)  # noqa: N815 — wire contract casing
    currency: str = "INR"
    method: str | None = None


class ConfirmPaymentRequest(BaseModel):
    razorpayPaymentId: str  # noqa: N815
    razorpayOrderId: str  # noqa: N815
    razorpaySignature: str  # noqa: N815
