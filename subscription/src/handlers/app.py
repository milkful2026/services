"""FastAPI app — thin: routing, exception translation, dependency wiring
only. Business rules live in domain/subscription_service.py."""

import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from domain.exceptions import SubscriptionError
from handlers.dto import error_envelope
from handlers.internal_run_daily_handler import router as internal_router
from handlers.subscription_handlers import router as subscription_router

app = FastAPI(title="Subscription Service")
app.include_router(subscription_router)
app.include_router(internal_router)

if os.environ.get("SUBSCRIPTION_CORS_ALLOW_ALL", "").lower() == "true":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.exception_handler(SubscriptionError)
async def subscription_error_handler(request: Request, exc: SubscriptionError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content=error_envelope(exc.error_code, exc.message, exc.details),
    )


@app.get("/healthz")
def healthz() -> JSONResponse:
    # No background consumer thread to reflect (this service owns no SQS
    # consumer — see main.py) — a plain liveness check is honest here,
    # unlike wallet/inventory's consumer-aware /healthz.
    return JSONResponse(status_code=200, content={"status": "ok"})
