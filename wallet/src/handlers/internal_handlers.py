"""Service-to-service endpoints (SigV4/mTLS in prod, VPC-only; not
exposed via the public API Gateway):
- GET /wallet/internal/limits — Payment Service (MA-126) reads the
  recharge bounds from here.
- POST /wallet/internal/debit, GET /wallet/internal/balance — Order
  Service (MA-132/MA-25) debits a subscription order and reads a
  balance from here.
- POST /wallet/internal/debits/{orderId}/void — Order Service (MA-142)
  fences an order before closing it without charge: afterwards no debit
  for it can commit, or, if it was already debited, the debit comes back.
- GET /wallet/internal/debits/{orderId} — read-only debit/void lookup
  (MA-142), for diagnostics; never the basis for closing an order.
- POST /wallet/internal/refunds — Order Service (MA-153/MA-32) credits a
  cancelled order's debit back to the same wallet. Idempotent."""

import logging
import re

from fastapi import APIRouter, Depends, Request

from domain.exceptions import InvalidOrderIdError, WalletError
from domain.wallet_service import WalletService
from handlers.dependencies import get_wallet_service
from handlers.dto import (
    DebitRequest,
    RefundRequest,
    VoidRequest,
    serialize_debit_outcome,
    serialize_refund,
    success_envelope,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["internal"])

# Order ids are server-generated (`ord_<uuid>`); validated explicitly
# because this app has no RequestValidationError -> 400 mapping.
_ORDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# MA-153 FR-1 — caller-chosen; MA-32 always sends `cancel`.
_REFUND_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


@router.get("/wallet/internal/limits")
def get_internal_limits(
    service: WalletService = Depends(get_wallet_service),
):
    return success_envelope(service.get_internal_limits())


def _validate_order_id(order_id: str | None) -> None:
    if not order_id or not _ORDER_ID.match(order_id):
        raise InvalidOrderIdError("orderId must be 1-64 characters of [A-Za-z0-9_-]")


@router.post("/wallet/internal/refunds")
def refund_for_order(
    body: RefundRequest,
    service: WalletService = Depends(get_wallet_service),
):
    if not body.userId:
        raise InvalidOrderIdError("userId is required")
    _validate_order_id(body.orderId)
    if not body.refundId or not _REFUND_ID.match(body.refundId):
        raise InvalidOrderIdError("refundId must be 1-32 characters of [A-Za-z0-9_-]")
    if body.amountPaise is None:
        raise InvalidOrderIdError("amountPaise is required")
    outcome = service.refund_for_order(
        user_id=body.userId,
        order_id=body.orderId,
        refund_id=body.refundId,
        amount_paise=body.amountPaise,
        correlation_id=body.correlationId,
    )
    return success_envelope(serialize_refund(outcome))


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


@router.post("/wallet/internal/debits/{orderId}/void")
def void_debit_for_order(
    orderId: str,
    body: VoidRequest,
    request: Request,
    service: WalletService = Depends(get_wallet_service),
):
    _validate_order_id(orderId)
    correlation_id = request.headers.get("X-Correlation-Id", "")
    try:
        voided = service.void_debit_for_order(body.userId, orderId)
    except WalletError as exc:
        _log_debit_call("debit_void", orderId, exc.error_code, correlation_id)
        raise
    _log_debit_call("debit_void", orderId, "VOIDED", correlation_id)
    return success_envelope(voided)


@router.get("/wallet/internal/debits/{orderId}")
def get_debit_for_order(
    orderId: str,
    request: Request,
    service: WalletService = Depends(get_wallet_service),
):
    _validate_order_id(orderId)
    correlation_id = request.headers.get("X-Correlation-Id", "")
    try:
        debit = service.get_debit_for_order(orderId)
    except WalletError as exc:
        _log_debit_call("debit_lookup", orderId, exc.error_code, correlation_id)
        raise
    _log_debit_call("debit_lookup", orderId, debit["status"], correlation_id)
    return success_envelope(debit)


def _log_debit_call(event: str, order_id: str, outcome: str, correlation_id: str) -> None:
    logger.info(
        event,
        extra={"orderId": order_id, "outcome": outcome, "correlationId": correlation_id},
    )
