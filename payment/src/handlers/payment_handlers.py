"""POST /payments, POST /payments/{id}/confirm, GET /payments/{id}.
All Cognito-JWT, owner-scoped."""

from fastapi import APIRouter, Depends, Header, HTTPException
from shared.handlers.auth import current_user_id

from config.env import Settings, get_settings
from domain.payment_service import PaymentService
from handlers.dependencies import correlation_id, get_payment_service
from handlers.dto import ConfirmPaymentRequest, CreatePaymentRequest, success_envelope

router = APIRouter(tags=["payments"])


@router.post("/payments")
def create_payment(
    body: CreatePaymentRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user_id: str = Depends(current_user_id),
    corr_id: str = Depends(correlation_id),
    service: PaymentService = Depends(get_payment_service),
    settings: Settings = Depends(get_settings),
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    result = service.create(
        user_id=user_id,
        purpose=body.purpose,
        amount_paise=body.amountPaise,
        currency=body.currency,
        method=body.method,
        idempotency_key=idempotency_key,
        correlation_id=corr_id,
    )
    result["razorpayKeyId"] = settings.razorpay_key_id
    return success_envelope(result)


@router.post("/payments/{payment_id}/confirm")
def confirm_payment(
    payment_id: str,
    body: ConfirmPaymentRequest,
    user_id: str = Depends(current_user_id),
    service: PaymentService = Depends(get_payment_service),
):
    result = service.confirm(
        payment_id=payment_id,
        user_id=user_id,
        razorpay_payment_id=body.razorpayPaymentId,
        razorpay_order_id=body.razorpayOrderId,
        razorpay_signature=body.razorpaySignature,
    )
    return success_envelope(result)


@router.get("/payments/{payment_id}")
def get_payment(
    payment_id: str,
    user_id: str = Depends(current_user_id),
    service: PaymentService = Depends(get_payment_service),
):
    result = service.get(payment_id, user_id)
    return success_envelope(result)
