"""Internal, service-to-service routes. Network-level auth only
(SigV4/mTLS + VPC-only in prod, not exposed via the public API Gateway).

- POST /internal/run-daily — the Daily Run (MA-131 FR-8): the EventBridge
  Scheduler rule's HTTP target in prod; a cron-like local-dev script hits
  this directly (documented Scheduler-emulation gap, MA-131 §6/§11).
- POST /internal/subscriptions — MA-136 FR-11: Order Service's cart
  checkout creates each subscription line through here. Same
  `SubscriptionService.create` as the public route, same idempotency and
  errors; only the user comes from the body instead of a JWT."""

from fastapi import APIRouter, Depends

from domain.subscription_service import SubscriptionService
from handlers.dependencies import get_subscription_service
from handlers.dto import InternalCreateSubscriptionRequest, success_envelope

router = APIRouter(tags=["internal"])


@router.post("/internal/run-daily")
def run_daily(
    service: SubscriptionService = Depends(get_subscription_service),
):
    due_subscription_ids = service.run_daily()
    return success_envelope({"dueSubscriptionIds": due_subscription_ids})


@router.post("/internal/subscriptions")
def create_subscription_internal(
    body: InternalCreateSubscriptionRequest,
    service: SubscriptionService = Depends(get_subscription_service),
):
    result = service.create(
        user_id=body.userId,
        product_id=body.productId,
        quantity=body.quantity,
        schedule=body.schedule.to_domain(),
        start_date=body.startDate,
        slot_id=body.slotId,
        idempotency_key=body.idempotencyKey,
        correlation_id=body.correlationId,
    )
    return success_envelope(result)
