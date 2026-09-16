"""Container entrypoint. Runs the wallet-events SQS consumer in a
background thread and the FastAPI app (uvicorn) in the main thread —
mirrors catalog/inventory's single-deployable-does-both structure."""

import logging
import os
import threading
from pathlib import Path


def _load_local_env_file() -> None:
    # Local dev only. Must run before `from handlers.app import app`
    # (that import triggers the CORS-toggle env read at import time).
    # ENV_LOCAL_PATH lets the containerized version read .env.local from
    # a shared docker volume written by the local-dev "bootstrap" service.
    path = Path(
        os.environ.get("ENV_LOCAL_PATH", str(Path(__file__).resolve().parents[1] / ".env.local"))
    )
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_local_env_file()

import uvicorn  # noqa: E402

from adapters.wallet_events_consumer import WalletEventsConsumer  # noqa: E402
from config.env import get_settings  # noqa: E402
from handlers.app import app  # noqa: E402
from handlers.dependencies import get_wallet_service  # noqa: E402
from handlers.health import consumer_health  # noqa: E402

logger = logging.getLogger(__name__)


def _run_consumer() -> None:
    try:
        settings = get_settings()
        if not settings.events_queue_url:
            logger.warning("WALLET_EVENTS_QUEUE_URL unset — consumer thread not started")
            return
        consumer = WalletEventsConsumer(
            queue_url=settings.events_queue_url,
            wallet_service=get_wallet_service(),
            region_name=settings.aws_region,
        )
        consumer.run_forever()
    except Exception:
        logger.critical("wallet_events_consumer thread died — no longer consuming wallet events")
        consumer_health.alive = False
        raise


def main() -> None:
    threading.Thread(target=_run_consumer, daemon=True, name="wallet-events-consumer").start()
    uvicorn.run(app, host="0.0.0.0", port=8006)  # noqa: S104 — Fargate task, not exposed directly


if __name__ == "__main__":
    main()
