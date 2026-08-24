"""FastAPI app — thin: routing, exception translation, CORS only.
Business rules live in domain/pricing_service.py. Matches catalog's own
handlers/app.py structure — minus a consumer-health check, since this
build has no background consumer thread (see README's "Scope" section)."""

import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from domain.exceptions import PricingError
from handlers.dto import error_envelope
from handlers.quote_handler import router as quote_router

app = FastAPI(title="Pricing Service")
app.include_router(quote_router)

# Read directly from os.environ, not config.env.Settings — matches
# catalog/inventory's own documented reasoning (Settings' eager
# validation of every required field would otherwise turn a local-dev-only
# CORS toggle into a way to break import entirely).
if os.environ.get("PRICING_CORS_ALLOW_ALL", "").lower() == "true":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.exception_handler(PricingError)
async def pricing_error_handler(request: Request, exc: PricingError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content=error_envelope(exc.error_code, exc.message, exc.details),
    )


@app.get("/healthz")
def healthz() -> JSONResponse:
    # No background consumer/DB to reflect the health of in this build
    # (see README's "Scope" section) — always green if the process is up.
    return JSONResponse(status_code=200, content={"status": "ok"})
