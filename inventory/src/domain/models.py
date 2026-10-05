"""Domain models. Plain dataclasses only — no SQLAlchemy/FastAPI types."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class Slot:
    id: str
    label: str


@dataclass
class Zone:
    id: str
    name: str
    active: bool
    pincode_prefixes: list[str]
    polygon: list[tuple[float, float]] | None  # [(lat, lng), ...], closed ring; None if unset
    slots: list[Slot] = field(default_factory=list)


@dataclass
class ServiceabilityResult:
    serviceable: bool
    zone_id: str | None = None
    zone_name: str | None = None
    slots: list[Slot] = field(default_factory=list)
    message: str | None = None
    waitlist_available: bool = False  # always False — waitlist is phase 2 (spec G2)


# --- MA-118 (reserve/commit/release, batch & expiry, read API) ------------


class StockState(enum.Enum):
    """MA-118 FR-1's derived state. A plain `str, enum.Enum` (not IntEnum)
    so `.value` round-trips directly into the StockChanged payload (FR-6)
    and the GET /inventory response body without a separate mapping —
    mirrors catalog/src/domain/models.py's own `StockState` enum values
    exactly, since both sides of the StockChanged contract must agree on
    the literal strings."""

    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    AVAILABLE_FROM = "AVAILABLE_FROM"


class ReservationStatus(enum.Enum):
    RESERVED = "RESERVED"
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"


@dataclass
class Stock:
    product_id: str
    on_hand: int
    reserved: int
    low_stock_threshold: int
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


@dataclass
class StockBatch:
    id: str
    product_id: str
    quantity: int
    expiry_date: date | None
    available_from: date | None  # None = available now; set = scheduled future batch
    received_at: datetime | None = None


@dataclass
class Reservation:
    id: str
    product_id: str
    order_ref: str
    quantity: int
    status: ReservationStatus
    created_at: datetime | None
    expires_at: datetime


@dataclass
class StockSummary:
    """FR-1's derived read shape — `Stock` plus the fields that require
    looking at `stock_batches` too (`stockState`/`availableFrom`), kept
    separate from `Stock` itself so the repository's plain row reads
    don't have to always join batches just to return a bare `Stock`."""

    product_id: str
    on_hand: int
    reserved: int
    available: int
    low_stock_threshold: int
    stock_state: StockState
    available_from: date | None = None


# --- MA-119 (admin manual adjustment & audit trail) / MA-150 (receive) ----


@dataclass
class AuditLogEntry:
    id: str
    product_id: str
    admin_id: str
    previous_quantity: int
    new_quantity: int
    adjustment: int
    reason: str | None
    created_at: datetime | None = None


@dataclass
class Page:
    """Generic pagination envelope shared by MA-150's three list-shaped
    reads (`GET /inventory`, `GET /inventory/{productId}/batches` doesn't
    paginate per FR-2, `GET /inventory/{productId}/audit-log`)."""

    items: list
    total: int
    page: int
    page_size: int
