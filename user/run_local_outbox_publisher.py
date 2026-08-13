"""Runs outbox_publisher_handler on a loop locally, standing in for the
EventBridge Scheduler rule (`rate(1 minute)`) that triggers it in real
deployments — see services/local-dev/README.md.

    python run_local_outbox_publisher.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

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
