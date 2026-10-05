"""Abstract adapter interfaces (Protocols). Domain code depends on these
only, never on SQLAlchemy/redis/boto3 directly."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from domain.models import (
    AuditLogEntry,
    Page,
    Reservation,
    Stock,
    StockBatch,
    StockState,
    Zone,
)


class ZoneRepositoryPort(Protocol):
    def get_active_zones(self) -> list[Zone]:
        """Raises ServiceUnavailableError on any DB failure — fail closed,
        per spec NFR (never silently return an empty/stale list)."""
        ...


class ZoneCachePort(Protocol):
    def get(self, pincode: str) -> dict | None: ...
    def set(self, pincode: str, result: dict, ttl_seconds: int) -> None: ...
    def invalidate(self, pincode: str) -> None: ...
    def invalidate_by_prefix(self, prefix: str) -> None:
        """Busts every cached pincode result starting with `prefix` — used
        by the ZoneUpdated consumer, which only knows the affected zone's
        pincode prefixes, not every individual pincode ever cached under
        them."""
        ...


class StockRepositoryPort(Protocol):
    """MA-118/MA-119/MA-150's single repository port — one `stock` row's
    lock is the serialization point every write below shares (mirrors
    wallet/src/adapters/wallet_repository.py's `debit_for_order` shape).
    Raises the domain's own `ServiceUnavailableError` on any DB failure;
    raises `ProductNotFoundError`/`ReservationNotFoundError` for the
    specific not-found cases each spec calls out."""

    def get_stock_with_next_batch(
        self, product_id: str
    ) -> tuple[Stock, StockBatch | None] | None: ...

    def provision_stock_if_absent(self, product_id: str) -> bool:
        """FR-8 — idempotent `INSERT ... ON CONFLICT DO NOTHING`."""
        ...

    def reserve(
        self, product_id: str, order_ref: str, quantity: int, ttl_seconds: int
    ) -> tuple[Reservation, bool]:
        """Returns (reservation, created). `created=False` means FR-2's
        idempotent replay path (an existing RESERVED/COMMITTED reservation
        for this key) — `available` was not re-decremented."""
        ...

    def commit_reservation(self, product_id: str, order_ref: str) -> tuple[Reservation, bool]:
        """Returns (reservation, changed). `changed=False` means the
        reservation was already COMMITTED/RELEASED (idempotent replay) —
        on_hand/reserved/batches were not touched again. Reported directly
        by the repository (observed under the same lock that performs the
        write) rather than inferred by the caller via a separate,
        unlocked before/after on_hand read, which a concurrent adjust()/
        receive_stock() on the same product could otherwise corrupt."""
        ...

    def release_reservation(self, product_id: str, order_ref: str) -> Reservation: ...

    def release_by_order_ref(self, order_ref: str) -> list[Reservation]:
        """FR-5 (`OrderCancelled`) — releases every active reservation for
        an order across every product it touched."""
        ...

    def sweep_expired_reservations(self, limit: int = 100) -> list[Reservation]:
        """FR-2's TTL auto-release. Returns the reservations released by
        this call (for StockChanged publication) — safe to run
        concurrently with itself (SKIP LOCKED)."""
        ...

    def adjust(
        self, product_id: str, admin_id: str, adjustment: int, reason: str | None
    ) -> tuple[Stock, AuditLogEntry]:
        """MA-119 FR-1 — raises OnHandFloorViolationError /
        AvailableFloorViolationError per the two distinct floor checks."""
        ...

    def receive_stock(
        self,
        product_id: str,
        quantity: int,
        expiry_date: date,
        admin_id: str,
        reason: str | None,
        available_from: date | None = None,
    ) -> tuple[StockBatch, Stock, AuditLogEntry]:
        """MA-150 FR-1. `available_from=None` (default) means the batch is
        available immediately — today's only previously-reachable
        behavior, since no caller could set this before. A non-None value
        schedules a future-available batch (MA-118's AVAILABLE_FROM
        stockState); `on_hand` still increases immediately either way —
        excluding a not-yet-available batch's quantity from `available`
        until its date arrives is a separate, not-yet-built follow-up."""
        ...

    def get_batches(self, product_id: str) -> list[StockBatch]:
        """MA-150 FR-2 — oldest-expiry-first. Raises ProductNotFoundError
        if `product_id` is unknown."""
        ...

    def list_stock(
        self, status_filter: StockState | None, page: int, page_size: int
    ) -> Page: ...

    def get_audit_log(self, product_id: str, page: int, page_size: int) -> Page:
        """MA-150 FR-4 — newest-first. Raises ProductNotFoundError if
        `product_id` is unknown."""
        ...


class StockEventPublisherPort(Protocol):
    """FR-6 — `StockChanged`/`LowStock`. Implementations publish directly
    (no table-backed outbox — see domain/inventory_stock_service.py's
    module docstring for why) and must not raise on a transient publish
    failure reaching the caller's own write transaction, which has
    already committed by the time this is invoked."""

    def publish_stock_changed(self, summary) -> None: ...

    def publish_low_stock(self, summary) -> None: ...
