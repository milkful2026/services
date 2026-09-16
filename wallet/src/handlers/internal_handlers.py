"""GET /wallet/internal/limits — service-to-service (SigV4/mTLS in prod,
VPC-only; not exposed via the public API Gateway). Payment Service
(MA-126) reads the recharge bounds from here."""

from fastapi import APIRouter, Depends

from domain.wallet_service import WalletService
from handlers.dependencies import get_wallet_service
from handlers.dto import success_envelope

router = APIRouter(tags=["internal"])


@router.get("/wallet/internal/limits")
def get_internal_limits(
    service: WalletService = Depends(get_wallet_service),
):
    return success_envelope(service.get_internal_limits())
