"""Outbox publisher loop — drains unpublished `outbox` rows to
EventBridge. Run as a sidecar/loop (services/local-dev) or a scheduled
task in prod. Never invoked from the request path.
"""

import logging
import os
import time
from pathlib import Path

from shared.adapters.outbox_event_publisher import EventBridgeOutboxPublisher
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from adapters.subscription_repository import SqlAlchemySubscriptionRepository
from config.env import get_settings

logger = logging.getLogger(__name__)


def _load_local_env_file() -> None:
    # Same shim main.py carries — needed here too, since this runs as its
    # own process/container (services/local-dev's subscription-outbox
    # service), not imported by main.py, so main.py's own call to this
    # never runs for this entrypoint.
    path = Path(
        os.environ.get("ENV_LOCAL_PATH", str(Path(__file__).resolve().parents[2] / ".env.local"))
    )
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


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
    _load_local_env_file()
    logging.basicConfig(level=logging.INFO)
    run_forever()
