"""`StockEventPublisherPort` adapter — `StockChanged`/`LowStock` (FR-6),
published directly via EventBridge (see domain/inventory_stock_service.py's
module docstring for why this is not a table-backed outbox like
wallet/user/cart/payment use).

Wraps shared.adapters.outbox_event_publisher.EventBridgeOutboxPublisher
(built for those services' outbox-polling loops, but its `publish()`
method — retry, raise shared.errors.ServiceUnavailableError on
exhaustion — is exactly the behavior a direct publish needs too; no
reason to duplicate it) rather than hand-rolling a second boto3
`put_events` wrapper.

**Envelope shape, confirmed empirically while doing this story's own
mandated live-verification step (impl-plan §3 step 3)**: FR-6's spec
text shows its JSON example as a flat object (`eventId`, `productId`,
...). But every *actual* SQS consumer already in this codebase —
adapters/zone_update_consumer.py and, critically, Catalog's own
stock_changed_consumer.py, the one this event exists to feed — parses
`body["payload"][...]`, not the flat object directly. Publishing FR-6's
fields flat (as its own JSON example literally shows) reached Catalog's
real queue correctly (confirming the InputTransformer fix in
local-dev/bootstrap.py's `_wire_rule` actually works end-to-end) but
then failed inside Catalog's consumer with "failed to process message"
— confirmed live, not hypothesized, against the running local-dev
stack. Fixed here, not in Catalog: nesting FR-6's fields under
`{"payload": {...}, "correlationId": ...}` is this repo's own
established SQS envelope convention (used by every consumer that
predates this story), and matching it is a one-file, inventory-only
change versus editing Catalog's already-shipped, independently-owned
consumer and its tests. FR-6's semantic field list is unchanged by
this — only the wire-level wrapping the existing SQS consumers all
already require.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

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
        # eventId/correlationId: a fresh uuid per publish (FR-6 — "it's
        # what lets MA-116's consumer detect and ignore a redelivered
        # duplicate"), stamped here on `payload` — where every real
        # consumer actually looks, per module docstring — not left to
        # EventBridgeOutboxPublisher.publish()'s own `detail.setdefault(...)`.
        # That setdefault still runs (publish() is shared code, not
        # overridable from here), but `detail` is this method's `envelope`
        # dict, not `payload` — envelope has no top-level "eventId"/
        # "occurredAt" key of its own, so setdefault doesn't skip it; it
        # silently adds a SECOND, different eventId/occurredAt at the
        # envelope's top level, unrelated to payload["eventId"]. Setting
        # them explicitly on envelope too (mirroring payload's own values,
        # not a fresh uuid) makes that setdefault a true no-op — a
        # previous version of this method only guarded `payload`, so the
        # envelope-level fields silently diverged from the ones every
        # consumer/dedup check actually reads.
        payload.setdefault("eventId", str(uuid.uuid4()))
        payload.setdefault("occurredAt", datetime.now(UTC).isoformat())
        envelope = {
            "payload": payload,
            "correlationId": payload["eventId"],
            "eventId": payload["eventId"],
            "occurredAt": payload["occurredAt"],
        }
        try:
            self._publisher.publish(detail_type, envelope)
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
