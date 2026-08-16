"""FastAPI app — thin: routing, exception translation, dependency wiring
only. Business rules live in domain/catalog_service.py."""

import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from domain.exceptions import CatalogError
from handlers.categories_handler import router as categories_router
from handlers.dto import error_envelope
from handlers.health import consumer_health
from handlers.products_handler import router as products_router
from handlers.search_handler import router as search_router

app = FastAPI(title="Catalog Service")
app.include_router(categories_router)
app.include_router(products_router)
app.include_router(search_router)

# Read directly from os.environ, not config.env.Settings — matches
# inventory/src/handlers/app.py's own documented reasoning exactly
# (Settings' eager validation of every required field would otherwise
# turn a local-dev-only CORS toggle into a way to break import entirely).
if os.environ.get("CATALOG_CORS_ALLOW_ALL", "").lower() == "true":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.exception_handler(CatalogError)
async def catalog_error_handler(request: Request, exc: CatalogError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content=error_envelope(exc.error_code, exc.message, exc.details),
    )


@app.get("/healthz")
def healthz() -> JSONResponse:
    # Deliberately does not touch Aurora — see inventory's own /healthz
    # for why the ALB target group must not point at the real business
    # endpoint. Does reflect the StockChanged consumer thread's liveness,
    # since that thread has no other way to signal it died.
    if not consumer_health.alive:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": "stock_changed_consumer stopped"},
        )
    return JSONResponse(status_code=200, content={"status": "ok"})
