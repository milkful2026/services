"""GET /wallet/me, GET /wallet/me/status (MA-1 legacy), GET
/wallet/me/transactions, POST /wallet/me/retry. All Cognito-JWT."""

from fastapi import APIRouter, Depends, Query
from shared.handlers.auth import current_user_id

from domain.wallet_service import WalletService
from handlers.dependencies import get_wallet_service
from handlers.dto import serialize_transactions, success_envelope

router = APIRouter(tags=["wallet"])


@router.get("/wallet/me")
def get_wallet_me(
    user_id: str = Depends(current_user_id),
    service: WalletService = Depends(get_wallet_service),
):
    return success_envelope(service.get_wallet_me(user_id))


@router.get("/wallet/me/status")
def get_wallet_status(
    user_id: str = Depends(current_user_id),
    service: WalletService = Depends(get_wallet_service),
):
    # MA-1 body, unchanged: {walletId, status, balance (rupees), currency}.
    return success_envelope(service.get_wallet_status_legacy(user_id))


@router.get("/wallet/me/transactions")
def get_wallet_transactions(
    user_id: str = Depends(current_user_id),
    limit: int | None = Query(default=None, ge=1, le=100),
    cursor: str | None = Query(default=None),
    service: WalletService = Depends(get_wallet_service),
):
    page = service.list_transactions(user_id, limit, cursor)
    return success_envelope(serialize_transactions(page))


@router.post("/wallet/me/retry")
def retry_provision(
    user_id: str = Depends(current_user_id),
    service: WalletService = Depends(get_wallet_service),
):
    service.retry_provision(user_id)
    return success_envelope({"status": "queued"})
