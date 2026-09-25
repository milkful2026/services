"""POST /orders/checkout — MA-136 FR-1. Cognito JWT + a required
Idempotency-Key the client reuses on every retry of the same Confirm.

Thin: validates the header and body, then hands off to CheckoutService.
Every outcome other than 200 is a typed OrderError, rendered by app.py's
exception handler as the standard error envelope (details spread into
`data`, e.g. `shortfallPaise`, `lines`)."""

import uuid

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel
from shared.handlers.auth import current_user_id

from domain.checkout_service import CheckoutService
from domain.exceptions import ValidationError
from handlers.dependencies import get_checkout_service
from handlers.dto import success_envelope

router = APIRouter(tags=["checkout"])

_MIN_KEY_LENGTH = 8
_MAX_KEY_LENGTH = 128


class CheckoutRequest(BaseModel):
    cartVersion: int  # noqa: N815 — wire contract casing
    expectedPayNowPaise: int | None = None  # noqa: N815


@router.post("/orders/checkout")
def checkout(
    body: CheckoutRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    request_id: str | None = Header(default=None, alias="x-request-id"),
    user_id: str = Depends(current_user_id),
    service: CheckoutService = Depends(get_checkout_service),
):
    key = (idempotency_key or "").strip()
    if not _MIN_KEY_LENGTH <= len(key) <= _MAX_KEY_LENGTH:
        raise ValidationError(
            f"Idempotency-Key header is required ({_MIN_KEY_LENGTH}-{_MAX_KEY_LENGTH} chars)"
        )
    result = service.checkout(
        user_id=user_id,
        idempotency_key=key,
        cart_version=body.cartVersion,
        expected_pay_now_paise=body.expectedPayNowPaise,
        correlation_id=request_id or str(uuid.uuid4()),
    )
    return success_envelope(result)
