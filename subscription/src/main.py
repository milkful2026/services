"""Container entrypoint. FastAPI (uvicorn) only — no background thread:
this service consumes nothing (unlike wallet/payment/inventory's
consumer threads). The Daily Run is triggered externally via
POST /internal/run-daily, not a loop this process owns."""

import os
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

from handlers.app import app  # noqa: E402


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8008)  # noqa: S104 — Fargate task, not exposed directly


if __name__ == "__main__":
    main()
