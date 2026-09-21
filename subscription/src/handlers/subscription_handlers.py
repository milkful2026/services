"""POST /subscriptions, .../pause, .../resume, .../stop, .../skip,
PATCH /subscriptions/{id}, GET /subscriptions/me, GET /subscriptions/{id}
— FR-1 through FR-7, FR-9. All Cognito-JWT."""

from fastapi import APIRouter, Depends
from shared.handlers.auth import current_user_id

from domain.subscription_service import SubscriptionService
from handlers.dependencies import correlation_id, get_subscription_service
from handlers.dto import (
    CreateSubscriptionRequest,
    EditRequest,
    PauseRequest,
    SkipRequest,
    success_envelope,
)

router = APIRouter(tags=["subscriptions"])


@router.post("/subscriptions")
def create_subscription(
    body: CreateSubscriptionRequest,
    user_id: str = Depends(current_user_id),
    corr_id: str = Depends(correlation_id),
    service: SubscriptionService = Depends(get_subscription_service),
):
    result = service.create(
        user_id=user_id,
        product_id=body.productId,
        quantity=body.quantity,
        schedule=body.schedule.to_domain(),
        start_date=body.startDate,
        slot_id=body.slotId,
        idempotency_key=body.idempotencyKey,
        correlation_id=corr_id,
    )
    return success_envelope(result)


@router.post("/subscriptions/{subscription_id}/pause")
def pause_subscription(
    subscription_id: str,
    body: PauseRequest,
    user_id: str = Depends(current_user_id),
    service: SubscriptionService = Depends(get_subscription_service),
):
    result = service.pause(subscription_id, user_id, from_=body.from_, until=body.until)
    return success_envelope(result)


@router.post("/subscriptions/{subscription_id}/resume")
def resume_subscription(
    subscription_id: str,
    user_id: str = Depends(current_user_id),
    service: SubscriptionService = Depends(get_subscription_service),
):
    result = service.resume(subscription_id, user_id)
    return success_envelope(result)


@router.post("/subscriptions/{subscription_id}/stop")
def stop_subscription(
    subscription_id: str,
    user_id: str = Depends(current_user_id),
    service: SubscriptionService = Depends(get_subscription_service),
):
    result = service.stop(subscription_id, user_id)
    return success_envelope(result)


@router.post("/subscriptions/{subscription_id}/skip")
def skip_delivery(
    subscription_id: str,
    body: SkipRequest,
    user_id: str = Depends(current_user_id),
    service: SubscriptionService = Depends(get_subscription_service),
):
    service.skip(subscription_id, user_id, body.date)
    return success_envelope({"status": "skipped"})


@router.patch("/subscriptions/{subscription_id}")
def edit_subscription(
    subscription_id: str,
    body: EditRequest,
    user_id: str = Depends(current_user_id),
    service: SubscriptionService = Depends(get_subscription_service),
):
    result = service.edit(
        subscription_id,
        user_id,
        quantity=body.quantity,
        schedule=body.schedule.to_domain() if body.schedule else None,
    )
    return success_envelope(result)


@router.get("/subscriptions/me")
def list_my_subscriptions(
    user_id: str = Depends(current_user_id),
    service: SubscriptionService = Depends(get_subscription_service),
):
    return success_envelope({"subscriptions": service.list_for_user(user_id)})


@router.get("/subscriptions/{subscription_id}")
def get_subscription(
    subscription_id: str,
    user_id: str = Depends(current_user_id),
    service: SubscriptionService = Depends(get_subscription_service),
):
    return success_envelope(service.get(subscription_id, user_id))
