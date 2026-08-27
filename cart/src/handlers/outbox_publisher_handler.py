"""Scheduled Lambda (EventBridge Scheduler, ~every 1 min): polls the
`cart` table for unpublished OUTBOX# rows and publishes them to
EventBridge, marking each published only after a successful publish. A
publish failure for one event is logged and left unpublished for the
next run — it does not stop the rest of the batch. Same shape as
services/user's own outbox_publisher_handler.py; DynamoDB's
transact_write_items stood in for Postgres's same-transaction outbox
insert at write time (cart_repository.py), this is the drain side.
"""

import logging
import uuid

from adapters.cart_repository import DynamoDbCartRepository
from adapters.outbox_event_publisher import EventBridgeOutboxPublisher
from config.env import get_settings
from domain.exceptions import CartServiceError

logger = logging.getLogger(__name__)

_deps: dict | None = None

_OUTBOX_BATCH_SIZE = 25


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps

    settings = get_settings()
    repository = DynamoDbCartRepository(
        settings.cart_table_name, settings.aws_region, settings.event_source
    )
    publisher = EventBridgeOutboxPublisher(
        settings.event_bus_name, settings.event_source, settings.aws_region
    )

    _deps = {"repository": repository, "publisher": publisher}
    return _deps


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = str(uuid.uuid4())
    deps["repository"].set_correlation_id(correlation_id)
    deps["publisher"].set_correlation_id(correlation_id)

    try:
        outbox_events = deps["repository"].get_unpublished_outbox_events(limit=_OUTBOX_BATCH_SIZE)
    except CartServiceError:
        logger.error(
            "outbox_publisher_handler: failed to read outbox, aborting run",
            extra={"correlationId": correlation_id},
        )
        return {"publishedCount": 0, "failedCount": 0}

    published_count = 0
    failed_count = 0
    for outbox_event in outbox_events:
        event_id = outbox_event.get("eventId")
        try:
            try:
                deps["publisher"].publish(
                    outbox_event["type"], outbox_event["payload"], correlation_id
                )
            except CartServiceError:
                logger.error(
                    "outbox_publisher_handler: failed to publish event, will retry next run",
                    extra={"correlationId": correlation_id, "outboxEventId": event_id},
                )
                failed_count += 1
                continue

            try:
                deps["repository"].mark_outbox_published(
                    outbox_event["userId"], outbox_event["eventId"]
                )
            except CartServiceError:
                # The event WAS published — unlike the branch above, this
                # will be redelivered next run since it's still unmarked.
                # Logged distinctly so the duplicate-delivery risk is
                # visible to an operator instead of reading identically
                # to "never published".
                logger.error(
                    "outbox_publisher_handler: event published but failed to mark —"
                    " will be redelivered next run, downstream consumers must dedupe by eventId",
                    extra={"correlationId": correlation_id, "outboxEventId": event_id},
                )
                failed_count += 1
                continue

            published_count += 1
        except Exception:
            logger.exception(
                "outbox_publisher_handler: unexpected error processing event, skipping",
                extra={"correlationId": correlation_id, "outboxEventId": event_id},
            )
            failed_count += 1

    return {"publishedCount": published_count, "failedCount": failed_count}
