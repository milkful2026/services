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
    # Plain process liveness only -- code-review fix (finding #6):
    # previously this also failed whenever the user_status_changed
    # consumer thread (MA-140) died, which would make an orchestrator
    # restart/recycle the WHOLE subscription REST API over an issue in
    # a secondary, add-on consumer -- subscription's core job is the
    # REST API (create/pause/resume/stop/skip/edit, Daily Run trigger),
    # not event consumption, unlike wallet (whose /healthz intentionally
    # stays consumer-aware, since consuming IS wallet's core job). The
    # consumer's own health is now exposed separately at
    # /healthz/consumer below, so an orchestrator can be configured to
    # restart only on the check that actually matters to it.
    return JSONResponse(status_code=200, content={"status": "ok"})


@app.get("/healthz/consumer")
def healthz_consumer() -> JSONResponse:
    """Reflects only the user_status_changed_consumer background
    thread's health (MA-140) -- deliberately separate from /healthz
    (the core REST API's own liveness, see its docstring above) so an
    orchestrator/alarm that only cares about the consumer can point at
    this path specifically instead of recycling the whole service."""
    if not consumer_health.alive:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": "user_status_changed_consumer stopped"},
        )
    return JSONResponse(status_code=200, content={"status": "ok"})
