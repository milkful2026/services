"""POST /payments/webhook — public, Razorpay-signature-gated, no Cognito
JWT. The handler never trusts the parsed body until the signature check
passes (the domain does the actual HMAC verification on the raw bytes)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from domain.exceptions import PaymentError
from domain.payment_service import PaymentService
from handlers.dependencies import get_payment_service
from handlers.dto import error_envelope, success_envelope

router = APIRouter(tags=["webhook"])


@router.post("/payments/webhook")
async def razorpay_webhook(
    request: Request,
    service: PaymentService = Depends(get_payment_service),
):
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    try:
        result = service.apply_webhook(raw_body, signature)
    except PaymentError as exc:
        # Always 200 once past signature verification so Razorpay doesn't
        # retry-storm us over an application-level no-op (duplicate,
        # orphan, etc.) — but a bad signature is the one case worth a
        # non-200 so it's visible in Razorpay's own delivery log too.
        if exc.error_code == "SIGNATURE_INVALID":
            return JSONResponse(
                status_code=400, content=error_envelope(exc.error_code, exc.message)
            )
        raise
    return success_envelope(result)
