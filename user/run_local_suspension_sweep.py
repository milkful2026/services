"""Local dev only: fires the FR-7 suspension sweep on demand, standing in
for the real EventBridge Scheduler `rate(1 day)` rule that triggers
suspension_sweep_handler in production (MA-139 §4 FR-7 / §12.2 — exact
schedule time is an implementation choice, not fixed here either).

Mirrors run_local_outbox_publisher.py's env-loading preamble, but invoked
once per run rather than looped — the same "manual trigger you run
yourself, once per simulated day" shape as Subscription Service's own
local-dev/run_daily_local.py, since this is this service's own daily
scheduled job, not a frequent poll.

    python run_local_suspension_sweep.py
"""

import os
import sys
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SERVICE_DIR / "src"))
sys.path.insert(0, str(_SERVICE_DIR.parent / "local-dev"))

from _env_file import load_env_file  # noqa: E402

# Before importing the handler module — populates real env vars
# (including the standard AWS_ENDPOINT_URL boto3 already reads
# natively) from bootstrap.py's generated .env.local. ENV_LOCAL_PATH
# support matches run_local.py's own fix.
load_env_file(Path(os.environ.get("ENV_LOCAL_PATH", str(_SERVICE_DIR / ".env.local"))))

import handlers.suspension_sweep_handler as suspension_sweep_handler  # noqa: E402

if __name__ == "__main__":
    result = suspension_sweep_handler.handler({}, None)
    lifted = result["liftedCount"]
    if not lifted:
        print("Suspension sweep complete — no expired suspensions to lift.")
    else:
        print(f"Suspension sweep complete — {lifted} account(s) auto-reactivated.")
