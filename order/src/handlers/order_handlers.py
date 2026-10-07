"""GET /orders/me, GET /orders/{id} — FR-3; POST /orders/{id}/cancel —
MA-154. All Cognito-JWT. Orders themselves are created by the
`SubscriptionOrderDue` consumer and by checkout (checkout_handlers)."""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel
from shared.handlers.auth import current_user_id

from domain.order_service import OrderService
from handlers.dependencies import get_order_service
from handlers.dto import success_envelope

router = APIRouter(tags=["orders"])


class CancelRequest(BaseModel):
    """MA-154 FR-1. `reason` is untyped on purpose: the service validates it,
    so an unknown value is a 400 VALIDATION_ERROR rather than FastAPI's 422."""

    reason: Any = None


@router.get("/orders/me")
def list_my_orders(
    subscriptionId: str | None = Query(default=None),  # noqa: N803 — wire contract casing
    limit: int | None = Query(default=None, ge=1, le=100),
    cursor: str | None = Query(default=None),
    user_id: str = Depends(current_user_id),
    service: OrderService = Depends(get_order_service),
):
    return success_envelope(service.list_for_user(user_id, subscriptionId, limit, cursor))


@router.get("/orders/{order_id}")
def get_order(
    order_id: str,
    user_id: str = Depends(current_user_id),
    service: OrderService = Depends(get_order_service),
):
    return success_envelope(service.get(order_id, user_id))


@router.post("/orders/{order_id}/cancel")
def cancel_order(
    order_id: str,
    body: CancelRequest | None = None,
    correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    request_id: str | None = Header(default=None, alias="x-request-id"),
    user_id: str = Depends(current_user_id),
    service: OrderService = Depends(get_order_service),
):
    return success_envelope(
        service.cancel(
            order_id,
            user_id,
            body.reason if body else None,
            datetime.now(UTC),
            correlation_id or request_id or str(uuid.uuid4()),
        )
    )
