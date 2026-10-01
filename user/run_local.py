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

import os
import sys
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SERVICE_DIR / "src"))
sys.path.insert(0, str(_SERVICE_DIR.parent / "local-dev"))

from _env_file import load_env_file  # noqa: E402
from _lambda_local_server import serve  # noqa: E402

# Before importing any handler module — populates real env vars
# (including the standard AWS_ENDPOINT_URL boto3 already reads
# natively) from bootstrap.py's generated .env.local. ENV_LOCAL_PATH
# lets the containerized version of this service read its .env.local
# from a shared docker volume (written by the "bootstrap" compose
# service) instead of this file's own directory — unset, and this is
# unchanged from a native/host run.
load_env_file(Path(os.environ.get("ENV_LOCAL_PATH", str(_SERVICE_DIR / ".env.local"))))

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

# --- MA-139 Admin Customer Status --------------------------------------
#
# In a real deployment, these routes sit behind Identity & Auth's own
# MA-129 admin authorizer Lambda, referenced cross-stack (see
# infra/user/user_stack.py's `admin_authorizer_fn_arn` parameter) — the
# same authorizer instance protects both services' `/v1/admin/*` routes,
# never a second copy of its Cognito/Aurora/IP-allowlist logic (see
# handlers/admin_context.py's own module docstring).
#
# Locally, that real authorizer can't be reused as-is: it resolves the
# caller's role from identity-auth's OWN Aurora `admin_user` table (see
# identity-auth/src/handlers/admin_authorizer_handler.py — role is a DB
# lookup by cognito_sub, never a JWT claim), which this process has no
# connection to, and importing identity-auth's `src/` here would be the
# first cross-service Python import in this codebase (every other
# service-to-service local-dev need goes through `shared/` or an HTTP
# call, never a sibling service's `src/` — see this file's own sys.path
# setup above). Rather than wire a fragile second DB connection purely
# for local-dev, `_local_admin_authorizer` below is a deliberately small
# stand-in: it only checks that a bearer token was presented (decoded
# unsigned by _lambda_local_server.py's shim, same trust model already
# documented there) and grants a fixed local-dev "SuperAdmin" role — it
# does not verify a real admin session or role the way production does.
# Flagged in the PR description as a local-dev-only simplification, not a
# security decision.
def _local_admin_authorizer(event: dict, context) -> dict:
    claims = (
        (event.get("requestContext") or {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    )
    admin_sub = claims.get("sub")
    if not admin_sub:
        return {"isAuthorized": False, "context": {}}
    return {
        "isAuthorized": True,
        "context": {
            "adminId": admin_sub,
            "email": claims.get("email", "local-admin@milkful.test"),
            "role": "SuperAdmin",
        },
    }


import handlers.admin_customers.bulk_status_handler as admin_bulk_status_handler  # noqa: E402
import handlers.admin_customers.deactivate_handler as admin_customer_deactivate_handler  # noqa: E402
import handlers.admin_customers.detail_handler as admin_customer_detail_handler  # noqa: E402
import handlers.admin_customers.list_handler as admin_customer_list_handler  # noqa: E402
import handlers.admin_customers.reactivate_handler as admin_customer_reactivate_handler  # noqa: E402
import handlers.admin_customers.suspend_handler as admin_customer_suspend_handler  # noqa: E402

ROUTES.update(
    {
        ("GET", "/v1/admin/customers"): (
            admin_customer_list_handler.handler,
            _local_admin_authorizer,
        ),
        ("GET", "/v1/admin/customers/{id}"): (
            admin_customer_detail_handler.handler,
            _local_admin_authorizer,
        ),
        ("POST", "/v1/admin/customers/{id}/suspend"): (
            admin_customer_suspend_handler.handler,
            _local_admin_authorizer,
        ),
        ("POST", "/v1/admin/customers/{id}/deactivate"): (
            admin_customer_deactivate_handler.handler,
            _local_admin_authorizer,
        ),
        ("POST", "/v1/admin/customers/{id}/reactivate"): (
            admin_customer_reactivate_handler.handler,
            _local_admin_authorizer,
        ),
        ("POST", "/v1/admin/customers/bulk-status"): (
            admin_bulk_status_handler.handler,
            _local_admin_authorizer,
        ),
    }
)

if __name__ == "__main__":
    serve(ROUTES, port=8002)
