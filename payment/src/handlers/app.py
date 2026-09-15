"""FastAPI app — thin: routing, exception translation, dependency wiring
only. Business rules live in domain/payment_service.py."""

import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from domain.exceptions import PaymentError
from handlers.dto import error_envelope
from handlers.health import consumer_health
from handlers.payment_handlers import router as payments_router
from handlers.webhook_handler import router as webhook_router

app = FastAPI(title="Payment Service")
app.include_router(payments_router)
app.include_router(webhook_router)

if os.environ.get("PAYMENT_CORS_ALLOW_ALL", "").lower() == "true":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.exception_handler(PaymentError)
async def payment_error_handler(request: Request, exc: PaymentError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content=error_envelope(exc.error_code, exc.message, exc.details),
    )


@app.get("/healthz")
def healthz() -> JSONResponse:
    if not consumer_health.alive:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": "reconcile_loop stopped"},
        )
    return JSONResponse(status_code=200, content={"status": "ok"})
