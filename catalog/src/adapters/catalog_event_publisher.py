"""`CatalogEventPublisherPort` adapter — `CatalogUpdated` (MA-156 FR-1),
published directly via EventBridge, mirroring
`inventory/src/adapters/stock_event_publisher.py`'s own shape: wraps
`shared.adapters.outbox_event_publisher.EventBridgeOutboxPublisher` for a
*direct* publish (not a polling-outbox table) — this is this service's
first-ever event publish, un-orphaning Inventory's existing, already-
implemented `catalog_updated_consumer.py` (MA-118 FR-8).

Envelope shape is pinned by that consumer, not redefined here: `{"payload":
{"productId": "..."}}` — the EventBridge rule filter
(`source: ["catalog"]`, `detail-type: ["CatalogUpdated"]`) is already
wired in `local-dev/bootstrap.py`'s `bootstrap_catalog_updated_queue()`.
"""

from __future__ import annotations

import logging

from shared.adapters.outbox_event_publisher import EventBridgeOutboxPublisher
from shared.errors import ServiceUnavailableError

logger = logging.getLogger(__name__)

CATALOG_UPDATED_DETAIL_TYPE = "CatalogUpdated"


class EventBridgeCatalogEventPublisher:
    def __init__(self, event_bus_name: str, event_source: str, region_name: str) -> None:
        self._publisher = EventBridgeOutboxPublisher(event_bus_name, event_source, region_name)

    def publish_catalog_updated(self, product_id: str) -> None:
        try:
            self._publisher.publish(
                CATALOG_UPDATED_DETAIL_TYPE, {"payload": {"productId": product_id}}
            )
        except ServiceUnavailableError as exc:
            # §11 Risk 1: the product row is already committed by the
            # time this runs — logged and swallowed, never raised, same
            # posture stock_event_publisher.py already established for
            # Inventory. Propagating would turn "Inventory's stock row
            # provisioning is briefly delayed" into "an otherwise-
            # successful product creation fails", which is strictly worse.
            logger.error(
                "catalog_event_publisher failed to publish CatalogUpdated — event dropped",
                extra={"error": str(exc), "productId": product_id},
            )
