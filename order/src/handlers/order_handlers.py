"""GET /orders/me, GET /orders/{id} — FR-3. Both Cognito-JWT. No public
write endpoints — every order is consumer-created from
`SubscriptionOrderDue`."""

from fastapi import APIRouter, Depends, Query
from shared.handlers.auth import current_user_id

from domain.order_service import OrderService
from handlers.dependencies import get_order_service
from handlers.dto import success_envelope

router = APIRouter(tags=["orders"])


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
