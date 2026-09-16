"""Container entrypoint. Runs the reconciliation sweep in a background
thread and the FastAPI app (uvicorn) in the main thread — mirrors
catalog/inventory/wallet's single-deployable-does-both structure."""

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

from handlers import reconcile  # noqa: E402
from handlers.app import app  # noqa: E402
from handlers.health import consumer_health  # noqa: E402

logger = logging.getLogger(__name__)


def _run_reconcile_loop() -> None:
    try:
        reconcile.run_forever()
    except Exception:
        logger.critical("reconcile loop thread died — stale payments will not be swept")
        consumer_health.alive = False
        raise


def main() -> None:
    threading.Thread(target=_run_reconcile_loop, daemon=True, name="reconcile-sweep").start()
    uvicorn.run(app, host="0.0.0.0", port=8007)  # noqa: S104 — Fargate task, not exposed directly


if __name__ == "__main__":
    main()
