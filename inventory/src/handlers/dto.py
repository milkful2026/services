"""Response envelope + serialization helpers. Fixed envelope shape per
services/README.md §5."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field
from shared.handlers.dto import error_envelope, success_envelope  # noqa: F401

from domain.models import (
    AuditLogEntry,
    Page,
    Reservation,
    ServiceabilityResult,
    Stock,
    StockBatch,
    StockSummary,
)
from domain.serviceability_service import result_to_dict


def serialize_result(result: ServiceabilityResult) -> dict[str, Any]:
    return result_to_dict(result)


# --- MA-118/MA-119/MA-150 request bodies ----------------------------------


class ReserveRequest(BaseModel):
    productId: str  # noqa: N815 — wire contract casing
    quantity: int = Field(gt=0)
    orderId: str  # noqa: N815
    ttlSeconds: int | None = Field(default=None, gt=0)  # noqa: N815


class OrderRefRequest(BaseModel):
    """Shared body shape for commit/release — both key off the same
    (productId, orderId) correlation pair reserve() created (FR-3/FR-4)."""

    productId: str  # noqa: N815
    orderId: str  # noqa: N815


class AdjustRequest(BaseModel):
    productId: str  # noqa: N815
    adjustment: int
    reason: str | None = None


class ReceiveRequest(BaseModel):
    productId: str  # noqa: N815
    quantity: int
    expiryDate: date  # noqa: N815
    reason: str | None = None


# --- MA-118/MA-119/MA-150 response serializers -----------------------------


def serialize_summary(summary: StockSummary) -> dict[str, Any]:
    return {
        "productId": summary.product_id,
        "onHand": summary.on_hand,
        "reserved": summary.reserved,
        "available": summary.available,
        "lowStockThreshold": summary.low_stock_threshold,
        "stockState": summary.stock_state.value,
        "availableFrom": summary.available_from.isoformat() if summary.available_from else None,
    }


def serialize_reservation(reservation: Reservation) -> dict[str, Any]:
    return {
        "reservationId": reservation.id,
        "productId": reservation.product_id,
        "orderId": reservation.order_ref,
        "quantity": reservation.quantity,
        "status": reservation.status.value,
        "createdAt": _iso(reservation.created_at),
        "expiresAt": _iso(reservation.expires_at),
    }


def serialize_stock(stock: Stock) -> dict[str, Any]:
    return {
        "productId": stock.product_id,
        "onHand": stock.on_hand,
        "reserved": stock.reserved,
        "available": stock.available,
        "lowStockThreshold": stock.low_stock_threshold,
    }


def serialize_batch(batch: StockBatch) -> dict[str, Any]:
    return {
        "batchId": batch.id,
        "productId": batch.product_id,
        "quantity": batch.quantity,
        "expiryDate": batch.expiry_date.isoformat() if batch.expiry_date else None,
        "availableFrom": batch.available_from.isoformat() if batch.available_from else None,
        "receivedAt": _iso(batch.received_at),
    }


def serialize_audit_entry(entry: AuditLogEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "productId": entry.product_id,
        "adminId": entry.admin_id,
        "previousQuantity": entry.previous_quantity,
        "newQuantity": entry.new_quantity,
        "adjustment": entry.adjustment,
        "reason": entry.reason,
        "createdAt": _iso(entry.created_at),
    }


def serialize_page(page: Page, item_serializer) -> dict[str, Any]:
    return {
        "items": [item_serializer(item) for item in page.items],
        "total": page.total,
        "page": page.page,
        "pageSize": page.page_size,
    }


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None
