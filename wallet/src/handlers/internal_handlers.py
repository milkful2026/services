"""Service-to-service endpoints (SigV4/mTLS in prod, VPC-only; not
exposed via the public API Gateway):
- GET /wallet/internal/limits — Payment Service (MA-126) reads the
  recharge bounds from here.
- POST /wallet/internal/debit, GET /wallet/internal/balance — Order
  Service (MA-132/MA-25) debits a subscription order and reads a
  balance from here."""

from fastapi import APIRouter, Depends

from domain.wallet_service import WalletService
from handlers.dependencies import get_wallet_service
from handlers.dto import DebitRequest, serialize_debit_outcome, success_envelope

router = APIRouter(tags=["internal"])


@router.get("/wallet/internal/limits")
def get_internal_limits(
    service: WalletService = Depends(get_wallet_service),
):
    return success_envelope(service.get_internal_limits())


@router.post("/wallet/internal/debit")
def debit_for_order(
    body: DebitRequest,
    service: WalletService = Depends(get_wallet_service),
):
    outcome = service.debit_for_order(
        user_id=body.userId,
        order_id=body.orderId,
        amount_paise=body.amountPaise,
        correlation_id=body.correlationId,
    )
    return success_envelope(serialize_debit_outcome(outcome))


@router.get("/wallet/internal/balance")
def get_internal_balance(
    userId: str,
    service: WalletService = Depends(get_wallet_service),
):
    return success_envelope(service.get_internal_balance(userId))
