"""Container entrypoint. Runs the ZoneUpdated consumer in a background
thread and the FastAPI app (via uvicorn) in the main thread — a single
Fargate deployable handles both, since nothing in this spec (unlike
Wallet's) calls for separating them into two artifacts."""

import logging
import os
import threading
from pathlib import Path


def _load_local_env_file() -> None:
    # Local dev only: populates real env vars (including the standard
    # AWS_ENDPOINT_URL botocore reads natively, and INVENTORY_CORS_
    # ALLOW_ALL, which handlers/app.py reads at *import* time) from
    # bootstrap.py's generated .env.local. A no-op if absent, as in
    # every deployed environment. Duplicated (not imported) from
    # local-dev/_env_file.py deliberately — this file is inventory's
    # real container entrypoint, and local-dev/ isn't shipped in the
    # production image.
    #
    # Deliberately called here, before the imports below, not inside
    # main() — handlers/app.py's CORS setup runs at import time
    # (module-level, so a real CORSMiddleware can be installed once at
    # app-construction rather than checked per-request), so the env
    # file must be loaded before `from handlers.app import app` ever
    # executes, not after.
    #
    # ENV_LOCAL_PATH overrides the default path so the containerized
    # version of this service can read .env.local from a shared docker
    # volume (written by the "bootstrap" compose service) instead of
    # this file's own directory — unset, and this is unchanged from a
    # native/host run.
    path = Path(os.environ.get("ENV_LOCAL_PATH", str(Path(__file__).resolve().parents[1] / ".env.local")))
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_local_env_file()

import uvicorn  # noqa: E402

from adapters.zone_cache_adapter import RedisZoneCacheAdapter, build_redis_client  # noqa: E402
from adapters.zone_update_consumer import ZoneUpdateConsumer  # noqa: E402
from config.env import get_settings  # noqa: E402
from handlers.app import app  # noqa: E402
from handlers.health import consumer_health  # noqa: E402

logger = logging.getLogger(__name__)


def _run_consumer() -> None:
    try:
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
    except Exception:
        # run_forever() never returns in normal operation — reaching here
        # means the consumer thread is dead. The HTTP server in the main
        # thread would otherwise keep the ALB health check green forever
        # with no signal that ZoneUpdated events have stopped being
        # consumed, so flip /healthz unhealthy instead.
        logger.critical("zone_update_consumer thread died — no longer consuming ZoneUpdated events")
        consumer_health.alive = False
        raise


def main() -> None:
    consumer_thread = threading.Thread(target=_run_consumer, daemon=True, name="zone-update-consumer")
    consumer_thread.start()
    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104 — Fargate task, not exposed directly


if __name__ == "__main__":
    main()
