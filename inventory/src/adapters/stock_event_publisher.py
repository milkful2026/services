"""`StockEventPublisherPort` adapter — `StockChanged`/`LowStock` (FR-6),
published directly via EventBridge (see domain/inventory_stock_service.py's
module docstring for why this is not a table-backed outbox like
wallet/user/cart/payment use).

Wraps shared.adapters.outbox_event_publisher.EventBridgeOutboxPublisher
(built for those services' outbox-polling loops, but its `publish()`
method — stamp eventId/occurredAt if missing, retry, raise
shared.errors.ServiceUnavailableError on exhaustion — is exactly the
behavior a direct publish needs too; no reason to duplicate it) rather
than hand-rolling a second boto3 `put_events` wrapper.
"""

from __future__ import annotations

import logging

from shared.adapters.outbox_event_publisher import EventBridgeOutboxPublisher
from shared.errors import ServiceUnavailableError

from domain.models import StockSummary

logger = logging.getLogger(__name__)

STOCK_CHANGED_DETAIL_TYPE = "inventory.stock.changed"
LOW_STOCK_DETAIL_TYPE = "inventory.stock.low_stock"


class EventBridgeStockEventPublisher:
    def __init__(self, event_bus_name: str, event_source: str, region_name: str) -> None:
        self._publisher = EventBridgeOutboxPublisher(event_bus_name, event_source, region_name)

    def publish_stock_changed(self, summary: StockSummary) -> None:
        self._publish(STOCK_CHANGED_DETAIL_TYPE, _stock_changed_payload(summary))

    def publish_low_stock(self, summary: StockSummary) -> None:
        self._publish(
            LOW_STOCK_DETAIL_TYPE,
            {
                "productId": summary.product_id,
                "availableQuantity": summary.available,
                "lowStockThreshold": summary.low_stock_threshold,
            },
        )

    def _publish(self, detail_type: str, payload: dict) -> None:
        try:
            self._publisher.publish(detail_type, payload)
        except ServiceUnavailableError as exc:
            # A publish failure here is a secondary-effect failure (see
            # domain/inventory_stock_service.py's module docstring): the
            # DB write this event describes has already committed. Logged
            # and swallowed rather than propagated — propagating would
            # turn "Catalog's cache is briefly stale" into "the caller's
            # otherwise-successful reserve/commit/release/adjust/receive
            # call fails", which is strictly worse.
            logger.error(
                "stock_event_publisher failed to publish %s — event dropped",
                detail_type,
                extra={"error": str(exc), "productId": payload.get("productId")},
            )


def _stock_changed_payload(summary: StockSummary) -> dict:
    return {
        "productId": summary.product_id,
        "availableQuantity": summary.available,
        "stockState": summary.stock_state.value,
        "availableFrom": summary.available_from.isoformat() if summary.available_from else None,
    }
