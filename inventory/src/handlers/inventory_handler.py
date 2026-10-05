"""GET /v1/inventory/{productId}, POST /v1/inventory/reserve,
POST /v1/inventory/commit, POST /v1/inventory/release (MA-118 FR-1..FR-4).

Service-to-service routes (Cart/Order, once built — MA-96/MA-97), not
customer-facing — MA-118 §5 NFR Security: "SigV4/mTLS applies per the
platform's service-to-service auth model", not the Cognito JWT authorizer
every admin/public route elsewhere in this codebase uses. No authorizer
dependency is wired here for the same reason internal_serviceability_
check_handler.py has none — see that file's own docstring and
inventory_stack.py's module docstring point 3: enforcement here is
network-level (this route is deliberately never put behind the public
HTTP API's Cognito authorizer in CDK, only reachable the way Cart/Order's
future calls are modeled), not an application-layer check this handler
itself performs.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from domain.inventory_stock_service import InventoryStockService
from handlers.dependencies import get_inventory_stock_service
from handlers.dto import (
    OrderRefRequest,
    ReserveRequest,
    serialize_reservation,
    serialize_summary,
    success_envelope,
)

router = APIRouter(prefix="/v1", tags=["inventory"])


@router.get("/inventory/{product_id}")
def get_inventory(
    product_id: str,
    service: InventoryStockService = Depends(get_inventory_stock_service),
):
    summary = service.get_summary(product_id)
    return success_envelope(serialize_summary(summary))


@router.post("/inventory/reserve")
def reserve(
    body: ReserveRequest,
    service: InventoryStockService = Depends(get_inventory_stock_service),
):
    reservation = service.reserve(
        body.productId, body.orderId, body.quantity, body.ttlSeconds
    )
    return success_envelope(serialize_reservation(reservation))


@router.post("/inventory/commit")
def commit(
    body: OrderRefRequest,
    service: InventoryStockService = Depends(get_inventory_stock_service),
):
    reservation = service.commit(body.productId, body.orderId)
    return success_envelope(serialize_reservation(reservation))


@router.post("/inventory/release")
def release(
    body: OrderRefRequest,
    service: InventoryStockService = Depends(get_inventory_stock_service),
):
    reservation = service.release(body.productId, body.orderId)
    return success_envelope(serialize_reservation(reservation))
