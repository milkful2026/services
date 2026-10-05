"""PATCH /v1/inventory (MA-119 FR-1), POST /v1/inventory/receive (MA-150
FR-1), GET /v1/inventory/{productId}/batches (MA-150 FR-2),
GET /v1/inventory (MA-150 FR-3), GET /v1/inventory/{productId}/audit-log
(MA-150 FR-4).

All five routes require the Ops role (MA-119 §5 / MA-150 §5 D4), enforced
by `require_ops_role` — see admin_context.py for how the admin
authorizer's context reaches this handler. `adminId` for the audit row
always comes from that context, never the request body (MA-119 FR-1 /
MA-150 §5's anti-spoofing rule).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from domain.exceptions import ValidationError
from domain.inventory_stock_service import InventoryStockService
from domain.models import StockState
from handlers.admin_context import require_ops_role
from handlers.dependencies import get_inventory_stock_service
from handlers.dto import (
    AdjustRequest,
    ReceiveRequest,
    serialize_audit_entry,
    serialize_batch,
    serialize_page,
    serialize_stock,
    serialize_summary,
    success_envelope,
)

router = APIRouter(prefix="/v1", tags=["inventory-admin"])


@router.patch("/inventory")
def adjust_inventory(
    body: AdjustRequest,
    admin: dict = Depends(require_ops_role),
    service: InventoryStockService = Depends(get_inventory_stock_service),
):
    stock, audit_entry = service.adjust(
        body.productId, admin["adminId"], body.adjustment, body.reason
    )
    return success_envelope(
        {"stock": serialize_stock(stock), "auditEntry": serialize_audit_entry(audit_entry)}
    )


@router.post("/inventory/receive", status_code=201)
def receive_inventory(
    body: ReceiveRequest,
    admin: dict = Depends(require_ops_role),
    service: InventoryStockService = Depends(get_inventory_stock_service),
):
    batch, stock, audit_entry = service.receive(
        body.productId,
        body.quantity,
        body.expiryDate,
        admin["adminId"],
        body.reason,
        body.availableFrom,
    )
    return success_envelope(
        {
            "batch": serialize_batch(batch),
            "stock": serialize_stock(stock),
            "auditEntry": serialize_audit_entry(audit_entry),
        }
    )


@router.get("/inventory/{product_id}/batches")
def get_batches(
    product_id: str,
    admin: dict = Depends(require_ops_role),
    service: InventoryStockService = Depends(get_inventory_stock_service),
):
    batches = service.get_batches(product_id)
    return success_envelope({"items": [serialize_batch(b) for b in batches]})


@router.get("/inventory")
def list_inventory(
    stockState: str | None = Query(default=None),  # noqa: N803 — wire contract casing
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=50, ge=1, le=500),  # noqa: N803
    admin: dict = Depends(require_ops_role),
    service: InventoryStockService = Depends(get_inventory_stock_service),
):
    status_filter = None
    if stockState is not None:
        try:
            status_filter = StockState(stockState)
        except ValueError as exc:
            raise ValidationError(f"Unknown stockState {stockState!r}") from exc
    result = service.list_stock(status_filter, page, pageSize)
    return success_envelope(serialize_page(result, serialize_summary))


@router.get("/inventory/{product_id}/audit-log")
def get_audit_log(
    product_id: str,
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=50, ge=1, le=500),  # noqa: N803
    admin: dict = Depends(require_ops_role),
    service: InventoryStockService = Depends(get_inventory_stock_service),
):
    result = service.get_audit_log(product_id, page, pageSize)
    return success_envelope(serialize_page(result, serialize_audit_entry))
