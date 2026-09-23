"""Runs outbox_publisher_handler on a loop locally, standing in for the
EventBridge Scheduler rule (`rate(1 minute)`) that triggers it in real
deployments — see services/local-dev/README.md.

    python run_local_outbox_publisher.py
"""

import os
import sys
import time
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SERVICE_DIR / "src"))
sys.path.insert(0, str(_SERVICE_DIR.parent / "local-dev"))

from _env_file import load_env_file  # noqa: E402

# Before importing the handler module — populates real env vars
# (including the standard AWS_ENDPOINT_URL boto3 already reads
# natively) from bootstrap.py's generated .env.local. ENV_LOCAL_PATH
# lets this run as its own docker-compose container, reading from the
# same shared volume run_local.py's container reads from, rather than a
# host path — see run_local.py's identical fix.
load_env_file(Path(os.environ.get("ENV_LOCAL_PATH", str(_SERVICE_DIR / ".env.local"))))

import handlers.outbox_publisher_handler as outbox_publisher_handler  # noqa: E402

POLL_INTERVAL_SECONDS = 5

if __name__ == "__main__":
    print(f"Polling outbox every {POLL_INTERVAL_SECONDS}s (Ctrl+C to stop)")
    try:
        while True:
            result = outbox_publisher_handler.handler({}, None)
            if result["publishedCount"] or result["failedCount"]:
                print(f"[outbox] {result}")
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nShutting down")
