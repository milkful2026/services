"""Service-to-service endpoints (SigV4/mTLS in prod, VPC-only; not
exposed via the public API Gateway):
- GET /wallet/internal/limits — Payment Service (MA-126) reads the
  recharge bounds from here.
- POST /wallet/internal/debit, GET /wallet/internal/balance — Order
  Service (MA-132/MA-25) debits a subscription order and reads a
  balance from here.
- GET /wallet/internal/debits/{orderId} — Order Service's sweep (MA-142)
  asks whether an order was charged before cancelling it."""

import logging
import re

from fastapi import APIRouter, Depends, Request

from domain.exceptions import InvalidOrderIdError
from domain.wallet_service import WalletService
from handlers.dependencies import get_wallet_service
from handlers.dto import DebitRequest, serialize_debit_outcome, success_envelope

logger = logging.getLogger(__name__)

router = APIRouter(tags=["internal"])

# Order ids are server-generated (`ord_<uuid>`); validated explicitly
# because this app has no RequestValidationError -> 400 mapping.
_ORDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


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


@router.get("/wallet/internal/debits/{orderId}")
def get_debit_for_order(
    orderId: str,
    request: Request,
    service: WalletService = Depends(get_wallet_service),
):
    if not _ORDER_ID.match(orderId):
        raise InvalidOrderIdError("orderId must be 1-64 characters of [A-Za-z0-9_-]")
    correlation_id = request.headers.get("X-Correlation-Id", "")
    try:
        debit = service.get_debit_for_order(orderId)
    except Exception:
        logger.info(
            "debit_lookup",
            extra={"orderId": orderId, "found": False, "correlationId": correlation_id},
        )
        raise
    logger.info(
        "debit_lookup",
        extra={"orderId": orderId, "found": True, "correlationId": correlation_id},
    )
    return success_envelope(debit)
