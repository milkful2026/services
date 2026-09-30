"""FastAPI app — thin: routing, exception translation, dependency wiring
only. Business rules live in domain/subscription_service.py."""

import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from domain.exceptions import SubscriptionError
from handlers.dto import error_envelope
from handlers.health import consumer_health
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
    # MA-140 — mirrors wallet/inventory's consumer-aware /healthz now
    # that this service owns a background SQS consumer too (see
    # main.py). Stays a plain liveness check (no "was a queue URL even
    # configured" distinction) since an unconfigured queue is a valid,
    # intentional local-dev/test state, not a failure.
    if not consumer_health.alive:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": "user_status_changed_consumer stopped"},
        )
    return JSONResponse(status_code=200, content={"status": "ok"})
