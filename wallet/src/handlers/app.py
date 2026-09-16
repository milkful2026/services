"""FastAPI app — thin: routing, exception translation, dependency wiring
only. Business rules live in domain/wallet_service.py."""

import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from domain.exceptions import WalletError
from handlers.dto import error_envelope
from handlers.health import consumer_health
from handlers.internal_handlers import router as internal_router
from handlers.wallet_handlers import router as wallet_router

app = FastAPI(title="Wallet Service")
app.include_router(wallet_router)
app.include_router(internal_router)

if os.environ.get("WALLET_CORS_ALLOW_ALL", "").lower() == "true":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.exception_handler(WalletError)
async def wallet_error_handler(request: Request, exc: WalletError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content=error_envelope(exc.error_code, exc.message, exc.details),
    )


@app.get("/healthz")
def healthz() -> JSONResponse:
    if not consumer_health.alive:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": "wallet_events_consumer stopped"},
        )
    return JSONResponse(status_code=200, content={"status": "ok"})
