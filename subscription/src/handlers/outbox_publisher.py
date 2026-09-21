"""Outbox publisher loop — drains unpublished `outbox` rows to
EventBridge. Run as a sidecar/loop (services/local-dev) or a scheduled
task in prod. Never invoked from the request path.
"""

import logging
import time

from shared.adapters.outbox_event_publisher import EventBridgeOutboxPublisher
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from adapters.subscription_repository import SqlAlchemySubscriptionRepository
from config.env import get_settings

logger = logging.getLogger(__name__)


def run_once(repo: SqlAlchemySubscriptionRepository, publisher: EventBridgeOutboxPublisher) -> int:
    rows = repo.fetch_unpublished()
    published = 0
    for row in rows:
        publisher.publish(row["event_type"], row["payload"])
        repo.mark_published(row["id"])
        published += 1
    return published


def run_forever(interval_seconds: float = 5.0, engine: Engine | None = None) -> None:
    # Built once, outside the loop — a fresh engine (and connection pool)
    # per tick would pay a blocking DB connection setup every
    # `interval_seconds` for the life of the process.
    settings = get_settings()
    engine = engine or create_engine(settings.database_url)
    repo = SqlAlchemySubscriptionRepository(engine)
    publisher = EventBridgeOutboxPublisher(
        event_bus_name=settings.event_bus_name,
        event_source=settings.event_source,
        region_name=settings.aws_region,
    )
    while True:
        try:
            n = run_once(repo, publisher)
            if n:
                logger.info("subscription outbox: published %d events", n)
        except Exception:  # noqa: BLE001 — keep the loop alive; retry next tick
            logger.exception("subscription outbox publish tick failed")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_forever()
