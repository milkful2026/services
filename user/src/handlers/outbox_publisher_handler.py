"""Scheduled Lambda (EventBridge Scheduler, ~every 1 min): polls
`outbox_events` for unpublished rows and publishes them to EventBridge,
marking each published only after a successful publish. A publish
failure for one event is logged and left unpublished for the next run —
it does not stop the rest of the batch.
"""

import logging
import uuid

from sqlalchemy import create_engine

from adapters.outbox_event_publisher import EventBridgeOutboxPublisher
from adapters.user_repository import SqlAlchemyUserRepository
from config.env import get_settings
from domain.exceptions import UserServiceError

logger = logging.getLogger(__name__)

_deps: dict | None = None


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps

    settings = get_settings()
    engine = create_engine(settings.database_url)
    repository = SqlAlchemyUserRepository(engine)
    publisher = EventBridgeOutboxPublisher(
        settings.event_bus_name, settings.event_source, settings.aws_region
    )

    _deps = {"repository": repository, "publisher": publisher, "settings": settings}
    return _deps


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = str(uuid.uuid4())

    outbox_events = deps["repository"].get_unpublished_outbox_events(
        limit=deps["settings"].outbox_batch_size
    )

    published_count = 0
    failed_count = 0
    for outbox_event in outbox_events:
        try:
            deps["publisher"].publish(outbox_event["type"], outbox_event["payload"], correlation_id)
            deps["repository"].mark_outbox_published(outbox_event["id"])
            published_count += 1
        except UserServiceError:
            logger.error(
                "outbox_publisher_handler: failed to publish event, will retry next run",
                extra={"correlationId": correlation_id, "outboxEventId": outbox_event["id"]},
            )
            failed_count += 1

    return {"publishedCount": published_count, "failedCount": failed_count}
