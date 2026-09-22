"""Local dev only: fires Subscription Service's Daily Run
(POST /internal/run-daily) on demand, standing in for the real
EventBridge Scheduler `rate(1 day)` rule that hits this endpoint in
production near the IST cut-off hour (MA-131 §6/§11's documented
Scheduler-emulation gap — no local scheduler exists, so this is a
manual trigger you run yourself, once per simulated "day").

    python run_daily_local.py                       # http://localhost:8008
    python run_daily_local.py http://localhost:8008  # same, explicit

Requires the Subscription Service to already be running (Option A:
`docker compose up -d subscription`; Option B: `cd subscription && python
src/main.py`) with at least one ACTIVE subscription due today for
anything interesting to show up in `dueSubscriptionIds`.
"""

import sys

import requests

DEFAULT_BASE_URL = "http://localhost:8008"


def main() -> None:
    base_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE_URL
    response = requests.post(f"{base_url}/internal/run-daily", timeout=30)
    response.raise_for_status()
    data = response.json()["data"]
    due_ids = data["dueSubscriptionIds"]
    if not due_ids:
        print("Daily Run complete — no subscriptions due today.")
        return
    print(
        f"Daily Run complete — {len(due_ids)} subscription(s) due today, "
        "SubscriptionOrderDue emitted for:"
    )
    for sub_id in due_ids:
        print(f"  {sub_id}")
    print(
        "\nDrain subscription's outbox next (Option A: already running as the "
        "subscription-outbox container; Option B: `cd subscription && python "
        "src/handlers/outbox_publisher.py`), then check Order Service picked "
        "each one up: curl localhost:8009/orders/me -H \"Authorization: Bearer <token>\""
    )


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.ConnectionError:
        print(
            f"Could not reach {sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE_URL} — "
            "is Subscription Service running?",
            file=sys.stderr,
        )
        sys.exit(1)
