"""Container entrypoint. FastAPI (uvicorn) in the main thread; MA-140
additionally runs the `user.status.changed` SQS consumer in a background
thread when a queue URL is configured — mirrors wallet/payment/
inventory's own single-deployable-does-both structure (see
wallet/src/main.py). The Daily Run is still triggered externally via
POST /internal/run-daily, not a loop this process owns."""

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

from adapters.user_status_changed_consumer import UserStatusChangedConsumer  # noqa: E402
from config.env import get_settings  # noqa: E402
from handlers.app import app  # noqa: E402
from handlers.dependencies import get_subscription_service  # noqa: E402
from handlers.health import consumer_health  # noqa: E402

logger = logging.getLogger(__name__)


def _run_consumer() -> None:
    try:
        settings = get_settings()
        if not settings.user_status_events_queue_url:
            logger.warning(
                "SUBSCRIPTION_USER_STATUS_EVENTS_QUEUE_URL unset — consumer thread not started"
            )
            return
        consumer = UserStatusChangedConsumer(
            queue_url=settings.user_status_events_queue_url,
            subscription_service=get_subscription_service(),
            region_name=settings.aws_region,
        )
        consumer.run_forever()
    except Exception:
        logger.critical(
            "user_status_changed_consumer thread died — no longer consuming account status events"
        )
        consumer_health.alive = False
        raise


def main() -> None:
    threading.Thread(
        target=_run_consumer, daemon=True, name="user-status-changed-consumer"
    ).start()
    uvicorn.run(app, host="0.0.0.0", port=8008)  # noqa: S104 — Fargate task, not exposed directly


if __name__ == "__main__":
    main()
