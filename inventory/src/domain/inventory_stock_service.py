"""`InventoryStockService` — MA-118 (reserve/commit/release, batch &
expiry, read API), MA-119 (admin manual adjustment & audit trail), and
MA-150 (admin receive, batch detail, stock list, audit-log read). One
service, extended by each spec in turn, per the impl-plan's own framing
("new domain method ... alongside MA-118's reserve/commit/release and
MA-119's adjust" — MA-150 §6).

Event publication (FR-6) is a direct, synchronous `StockEventPublisherPort`
call made *after* the repository's write transaction has already
committed — not a table-backed transactional outbox like
wallet/user/cart/payment use. This is a deliberate, documented departure
from those services' pattern, not an oversight: the impl-plan (§4) notes
Inventory has no outbound synchronous call to compensate for, and FR-6's
own consumer (Catalog) already tolerates at-least-once/best-effort
delivery (it dedupes on `eventId`, per FR-6's payload contract) — adding
an outbox table and a separate polling publisher would duplicate
mechanism for a guarantee this spec doesn't actually need. A publish
failure here is logged by the adapter and swallowed (see
adapters/stock_event_publisher.py) rather than rolling back or retrying
the already-committed DB write, consistent with NFR Reliability's own
framing (the write and the audit row must be atomic with each other; the
event is a secondary effect of an already-true fact, not a commit
gate).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from adapters.interfaces import StockEventPublisherPort, StockRepositoryPort
from domain.exceptions import ValidationError
from domain.models import (
    AuditLogEntry,
    Page,
    Reservation,
    Stock,
    StockBatch,
    StockState,
    StockSummary,
)

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 900  # 15 min, MA-118 §12 Q1's assumed default


class InventoryStockService:
    def __init__(
        self,
        repository: StockRepositoryPort,
        event_publisher: StockEventPublisherPort,
        default_ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._repository = repository
        self._event_publisher = event_publisher
        self._default_ttl_seconds = default_ttl_seconds

    # --- FR-1 ------------------------------------------------------------

    def get_summary(self, product_id: str) -> StockSummary:
        return self._load_summary(product_id)

    # --- FR-2/FR-3/FR-4: reserve/commit/release --------------------------

    def reserve(
        self,
        product_id: str,
        order_ref: str,
        quantity: int,
        ttl_seconds: int | None = None,
    ) -> Reservation:
        if quantity <= 0:
            raise ValidationError("quantity must be a positive integer")
        reservation, created = self._repository.reserve(
            product_id, order_ref, quantity, ttl_seconds or self._default_ttl_seconds
        )
        if created:
            # available decreased by `quantity`; on_hand unchanged.
            self._publish_after_change(product_id, available_delta=-quantity)
        return reservation

    def commit(self, product_id: str, order_ref: str) -> Reservation:
        # commit() only mutates on_hand when it actually transitions
        # RESERVED -> COMMITTED; the repository returns the reservation
        # unchanged for the idempotent-replay (already-terminal) case.
        # Reading on_hand before and after is how this method tells those
        # two cases apart without threading an extra "changed" boolean
        # through the repository port. commit() never changes
        # `available` (on_hand and reserved both move by the same
        # amount, per stock_repository.py's module docstring), so there
        # is never a LowStock crossing to check here.
        before = self._repository.get_stock_with_next_batch(product_id)
        before_on_hand = before[0].on_hand if before is not None else None

        reservation = self._repository.commit_reservation(product_id, order_ref)

        loaded = self._repository.get_stock_with_next_batch(product_id)
        if loaded is not None and loaded[0].on_hand != before_on_hand:
            self._event_publisher.publish_stock_changed(_summarize(*loaded))
        return reservation

    def release(self, product_id: str, order_ref: str) -> Reservation:
        reservation = self._repository.release_reservation(product_id, order_ref)
        if reservation.status.value == "RELEASED":
            # Same idempotency ambiguity as commit() above: the
            # repository returns the same object whether this call
            # actually transitioned the row or found it already
            # terminal. A second `available` read would be needed to
            # tell those apart precisely; since a no-op release leaves
            # `available` unchanged, publishing StockChanged redundantly
            # on a no-op is harmless (Catalog dedupes on `eventId`, FR-6)
            # — simpler than threading a `changed` flag through the port
            # for a case whose only cost is one extra, idempotent publish.
            self._publish_after_change(product_id, available_delta=0)
        return reservation

    def handle_order_cancelled(self, order_ref: str) -> list[Reservation]:
        released = self._repository.release_by_order_ref(order_ref)
        for reservation in released:
            self._publish_after_change(reservation.product_id, available_delta=0)
        return released

    def run_ttl_sweep(self, limit: int = 100) -> list[Reservation]:
        released = self._repository.sweep_expired_reservations(limit)
        for reservation in released:
            self._publish_after_change(reservation.product_id, available_delta=0)
        return released

    # --- MA-119 FR-1: admin adjustment ------------------------------------

    def adjust(
        self, product_id: str, admin_id: str, adjustment: int, reason: str | None
    ) -> tuple[Stock, AuditLogEntry]:
        if adjustment == 0:
            raise ValidationError("adjustment must be a non-zero integer")
        stock, audit_entry = self._repository.adjust(product_id, admin_id, adjustment, reason)
        self._publish_after_change(product_id, available_delta=adjustment)
        return stock, audit_entry

    # --- MA-150 FR-1: receive ---------------------------------------------

    def receive(
        self,
        product_id: str,
        quantity: int,
        expiry_date: date,
        admin_id: str,
        reason: str | None,
    ) -> tuple[StockBatch, Stock, AuditLogEntry]:
        if quantity <= 0:
            raise ValidationError("quantity must be a positive integer")
        if expiry_date < datetime.now(UTC).date():
            raise ValidationError("expiryDate must not be in the past")
        batch, stock, audit_entry = self._repository.receive_stock(
            product_id, quantity, expiry_date, admin_id, reason
        )
        self._publish_after_change(product_id, available_delta=quantity)
        return batch, stock, audit_entry

    # --- MA-150 FR-2/FR-3/FR-4: admin reads -------------------------------

    def get_batches(self, product_id: str) -> list[StockBatch]:
        return self._repository.get_batches(product_id)

    def list_stock(
        self, status_filter: StockState | None, page: int, page_size: int
    ) -> Page:
        return self._repository.list_stock(status_filter, page, page_size)

    def get_audit_log(self, product_id: str, page: int, page_size: int) -> Page:
        return self._repository.get_audit_log(product_id, page, page_size)

    # --- FR-8 --------------------------------------------------------------

    def provision_product(self, product_id: str) -> bool:
        return self._repository.provision_stock_if_absent(product_id)

    # --- internal ------------------------------------------------------

    def _load_summary(self, product_id: str) -> StockSummary:
        from domain.exceptions import ProductNotFoundError

        loaded = self._repository.get_stock_with_next_batch(product_id)
        if loaded is None:
            raise ProductNotFoundError(f"Unknown product {product_id!r}")
        stock, next_batch = loaded
        return _summarize(stock, next_batch)

    def _publish_after_change(self, product_id: str, available_delta: int) -> None:
        """Publishes StockChanged (always) and LowStock (only on a
        downward crossing of `low_stock_threshold`) for the product's
        *current* state, computing the pre-change `available` via simple
        arithmetic from the known delta rather than an extra locked read
        — see module docstring. `available_delta=0` is used by the
        idempotent-no-op/already-terminal paths above, where it's cheaper
        to publish a redundant (but harmless, per FR-6's `eventId`-dedupe
        contract) StockChanged than to thread a `changed` boolean through
        every repository method."""
        loaded = self._repository.get_stock_with_next_batch(product_id)
        if loaded is None:
            return
        stock, next_batch = loaded
        summary = _summarize(stock, next_batch)
        self._event_publisher.publish_stock_changed(summary)

        previous_available = summary.available - available_delta
        if (
            available_delta < 0
            and previous_available >= stock.low_stock_threshold
            and summary.available < stock.low_stock_threshold
        ):
            self._event_publisher.publish_low_stock(summary)


def _summarize(stock: Stock, next_batch) -> StockSummary:
    if stock.available > 0:
        state = StockState.IN_STOCK
        available_from = None
    elif next_batch is not None:
        state = StockState.AVAILABLE_FROM
        available_from = next_batch.available_from
    else:
        state = StockState.OUT_OF_STOCK
        available_from = None
    return StockSummary(
        product_id=stock.product_id,
        on_hand=stock.on_hand,
        reserved=stock.reserved,
        available=stock.available,
        low_stock_threshold=stock.low_stock_threshold,
        stock_state=state,
        available_from=available_from,
    )
