"""FastAPI app — thin: routing, exception translation, dependency wiring
only. Business rules live in domain/serviceability_service.py."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from domain.exceptions import InventoryError
from handlers.dto import error_envelope
from handlers.health import consumer_health
from handlers.internal_serviceability_check_handler import router as internal_router
from handlers.serviceability_check_handler import router as public_router

app = FastAPI(title="Inventory Service")
app.include_router(public_router)
app.include_router(internal_router)


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
