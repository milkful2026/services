"""Container entrypoint. Runs the StockChanged consumer in a background
thread and the FastAPI app (via uvicorn) in the main thread — mirrors
inventory/src/main.py's own single-deployable-does-both structure
exactly."""

import logging
import os
import threading
from pathlib import Path


def _load_local_env_file() -> None:
    # Local dev only — see inventory/src/main.py's identical helper for
    # the full reasoning (must run before `from handlers.app import app`,
    # since that import triggers the CORS-toggle env read at module-import
    # time). Duplicated, not imported, since local-dev/ isn't shipped in
    # the production image.
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

from adapters.stock_changed_consumer import StockChangedConsumer  # noqa: E402
from config.env import get_settings  # noqa: E402
from handlers.app import app  # noqa: E402
from handlers.dependencies import get_catalog_service  # noqa: E402
from handlers.health import consumer_health  # noqa: E402

logger = logging.getLogger(__name__)


def _run_consumer() -> None:
    try:
        settings = get_settings()
        consumer = StockChangedConsumer(
            queue_url=settings.stock_changed_queue_url,
            catalog_service=get_catalog_service(),
            region_name=settings.aws_region,
        )
        consumer.run_forever()
    except Exception:
        # run_forever() never returns in normal operation — reaching here
        # means the consumer thread is dead. The HTTP server in the main
        # thread would otherwise keep the ALB health check green forever
        # with no signal that StockChanged events have stopped being
        # consumed, so flip /healthz unhealthy instead.
        logger.critical(
            "stock_changed_consumer thread died — no longer consuming StockChanged events"
        )
        consumer_health.alive = False
        raise


def main() -> None:
    consumer_thread = threading.Thread(
        target=_run_consumer, daemon=True, name="stock-changed-consumer"
    )
    consumer_thread.start()
    uvicorn.run(app, host="0.0.0.0", port=8003)  # noqa: S104 — Fargate task, not exposed directly


if __name__ == "__main__":
    main()
