"""Runs this service's Lambda handlers locally over plain HTTP, for the
docker-compose-based local dev environment (see services/local-dev/).
Not used in any deployed environment — Lambda invokes handler(event,
context) directly there, via API Gateway's own integration, not this
file.

    python run_local.py

Requires services/local-dev/bootstrap.py to have already run (creates
this service's .env.local with the moto-backed DynamoDB table name).
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

import handlers.add_item_handler as add_item_handler  # noqa: E402
import handlers.delete_item_handler as delete_item_handler  # noqa: E402
import handlers.get_cart_handler as get_cart_handler  # noqa: E402
import handlers.put_cart_handler as put_cart_handler  # noqa: E402

ROUTES = {
    ("GET", "/cart"): get_cart_handler.handler,
    ("POST", "/cart/items"): add_item_handler.handler,
    ("PUT", "/cart"): put_cart_handler.handler,
    # {id} path-parameter support (first use of it in this shim) is
    # handled by _lambda_local_server.py's _match_parameterized_route
    # fallback — see that module for why an exact-match dict alone isn't
    # enough here, unlike every other service's route table so far.
    ("DELETE", "/cart/items/{id}"): delete_item_handler.handler,
}

if __name__ == "__main__":
    serve(ROUTES, port=8004)
