"""Container entrypoint. Runs the ZoneUpdated consumer in a background
thread and the FastAPI app (via uvicorn) in the main thread — a single
Fargate deployable handles both, since nothing in this spec (unlike
Wallet's) calls for separating them into two artifacts."""

import logging
import threading

import uvicorn

from adapters.zone_cache_adapter import RedisZoneCacheAdapter, build_redis_client
from adapters.zone_update_consumer import ZoneUpdateConsumer
from config.env import get_settings
from handlers.app import app

logger = logging.getLogger(__name__)


def _run_consumer() -> None:
    settings = get_settings()
    zone_cache = RedisZoneCacheAdapter(
        build_redis_client(settings.redis_host, settings.redis_port, settings.redis_use_tls)
    )
    consumer = ZoneUpdateConsumer(
        queue_url=settings.zone_updated_queue_url,
        zone_cache=zone_cache,
        region_name=settings.aws_region,
    )
    consumer.run_forever()


def main() -> None:
    consumer_thread = threading.Thread(target=_run_consumer, daemon=True, name="zone-update-consumer")
    consumer_thread.start()
    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104 — Fargate task, not exposed directly


if __name__ == "__main__":
    main()
