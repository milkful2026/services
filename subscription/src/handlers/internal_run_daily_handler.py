"""POST /internal/run-daily — the Daily Run (MA-131 FR-8). Network-level
auth only (SigV4/mTLS + VPC-only in prod, not exposed via the public API
Gateway) — the EventBridge Scheduler rule's HTTP target in prod; a
cron-like local-dev script hits this directly (documented Scheduler-
emulation gap, MA-131 §6/§11)."""

from fastapi import APIRouter, Depends

from domain.subscription_service import SubscriptionService
from handlers.dependencies import get_subscription_service
from handlers.dto import success_envelope

router = APIRouter(tags=["internal"])


@router.post("/internal/run-daily")
def run_daily(
    service: SubscriptionService = Depends(get_subscription_service),
):
    due_subscription_ids = service.run_daily()
    return success_envelope({"dueSubscriptionIds": due_subscription_ids})
