"""FastAPI app — thin: routing, exception translation, dependency wiring
only. Business rules live in domain/serviceability_service.py."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from domain.exceptions import InventoryError
from handlers.dto import error_envelope
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
