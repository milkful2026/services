"""FastAPI app — thin: routing, exception translation, dependency wiring
only. Business rules live in domain/order_service.py."""

import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from domain.exceptions import OrderError
from handlers.checkout_handlers import router as checkout_router
from handlers.dto import error_envelope
from handlers.health import consumer_health
from handlers.order_handlers import router as order_router

app = FastAPI(title="Order Service")
# Checkout first: POST /orders/checkout must never be shadowed by a
# future /orders/{id} write route.
app.include_router(checkout_router)
app.include_router(order_router)

if os.environ.get("ORDER_CORS_ALLOW_ALL", "").lower() == "true":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.exception_handler(OrderError)
async def order_error_handler(request: Request, exc: OrderError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content=error_envelope(exc.error_code, exc.message, exc.details),
    )


@app.get("/healthz")
def healthz() -> JSONResponse:
    if not consumer_health.alive:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": "order_events_consumer stopped"},
        )
    return JSONResponse(status_code=200, content={"status": "ok"})
