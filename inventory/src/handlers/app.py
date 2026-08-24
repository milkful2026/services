"""FastAPI app — thin: routing, exception translation, dependency wiring
only. Business rules live in domain/serviceability_service.py."""

import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from domain.exceptions import InventoryError
from handlers.dto import error_envelope
from handlers.health import consumer_health
from handlers.internal_serviceability_check_handler import router as internal_router
from handlers.serviceability_check_handler import router as public_router

app = FastAPI(title="Inventory Service")
app.include_router(public_router)
app.include_router(internal_router)

# Read directly from os.environ, not config.env.Settings — this runs at
# import time (app-setup, before any request is handled), and Settings()
# eagerly validates *every* required field (database_url, redis_host,
# ...) the moment it's constructed. Those aren't guaranteed to be set yet
# at this point (e.g. during test collection, which imports this module
# before any test's env-var fixture has run) — going through Settings
# here would turn a local-dev-only CORS toggle into a way to break
# import entirely. Local dev only either way; never true in a real
# deployment, where this defaults unset/False.
if os.environ.get("INVENTORY_CORS_ALLOW_ALL", "").lower() == "true":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.exception_handler(InventoryError)
async def inventory_error_handler(request: Request, exc: InventoryError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content=error_envelope(exc.error_code, exc.message, exc.details),
    )


@app.get("/healthz")
def healthz() -> JSONResponse:
    # Deliberately does not touch Aurora/Redis — see this stack's CDK
    # docstring for why the ALB target group must not point at the real
    # business endpoint. It does reflect the ZoneUpdated consumer thread's
    # liveness, since that thread has no other way to signal it died.
    if not consumer_health.alive:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": "zone_update_consumer stopped"},
        )
    return JSONResponse(status_code=200, content={"status": "ok"})
