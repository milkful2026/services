"""Runs this service's Lambda handlers locally over plain HTTP, for the
docker-compose-based local dev environment (see services/local-dev/).
Not used in any deployed environment — Lambda invokes handler(event,
context) directly there, via API Gateway's own integration, not this
file. (outbox_publisher_handler is a scheduled Lambda, not an HTTP
route, so it isn't served here — see services/local-dev/README.md for
how to run it manually in the local flow.)

    python run_local.py

Requires services/local-dev/bootstrap.py and apply_migrations.py to have
already run (creates .env.local, and the Postgres schema this service
reads/writes).
"""

import sys
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SERVICE_DIR / "src"))
sys.path.insert(0, str(_SERVICE_DIR.parent / "local-dev"))

from _env_file import load_env_file  # noqa: E402
from _lambda_local_server import serve  # noqa: E402

# Before importing any handler module — populates real env vars
# (including the standard AWS_ENDPOINT_URL boto3 already reads
# natively) from bootstrap.py's generated .env.local.
load_env_file(_SERVICE_DIR / ".env.local")

import handlers.delivery_slots_handler as delivery_slots_handler  # noqa: E402
import handlers.get_me_handler as get_me_handler  # noqa: E402
import handlers.internal_address_state_handler as internal_address_state_handler  # noqa: E402
import handlers.register_handler as register_handler  # noqa: E402

ROUTES = {
    ("POST", "/users/register"): register_handler.handler,
    ("GET", "/delivery/slots"): delivery_slots_handler.handler,
    ("GET", "/users/me"): get_me_handler.handler,
    ("GET", "/v1/internal/users/address-state"): internal_address_state_handler.handler,
}

if __name__ == "__main__":
    serve(ROUTES, port=8002)
