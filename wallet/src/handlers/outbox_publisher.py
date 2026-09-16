"""Outbox publisher loop — drains unpublished `outbox` rows to
EventBridge. Run as a sidecar/loop (services/local-dev) or a scheduled
task in prod. Never invoked from the request or SQS-consumer path.
"""

import logging
import time

from shared.adapters.outbox_event_publisher import EventBridgeOutboxPublisher
from sqlalchemy import create_engine

from adapters.wallet_repository import SqlAlchemyWalletRepository
from config.env import get_settings

logger = logging.getLogger(__name__)


def run_once() -> int:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    repo = SqlAlchemyWalletRepository(engine)
    publisher = EventBridgeOutboxPublisher(
        event_bus_name=settings.event_bus_name,
        event_source=settings.event_source,
        region_name=settings.aws_region,
    )
    rows = repo.fetch_unpublished()
    published = 0
    for row in rows:
        publisher.publish(row["event_type"], row["payload"])
        repo.mark_published(row["id"])
        published += 1
    return published


def run_forever(interval_seconds: float = 5.0) -> None:
    while True:
        try:
            n = run_once()
            if n:
                logger.info("wallet outbox: published %d events", n)
        except Exception:  # noqa: BLE001 — keep the loop alive; retry next tick
            logger.exception("wallet outbox publish tick failed")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_forever()
