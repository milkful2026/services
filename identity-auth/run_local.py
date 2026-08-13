"""Runs this service's Lambda handlers locally over plain HTTP, for the
docker-compose-based local dev environment (see services/local-dev/).
Not used in any deployed environment — Lambda invokes handler(event,
context) directly there, via API Gateway's own integration, not this
file.

    python run_local.py

Requires services/local-dev/bootstrap.py to have already run (creates
this service's .env.local with the moto-backed Cognito pool/table IDs).
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

import handlers.login_otp_send_handler as login_otp_send_handler  # noqa: E402
import handlers.login_otp_verify_handler as login_otp_verify_handler  # noqa: E402
import handlers.logout_handler as logout_handler  # noqa: E402
import handlers.otp_send_handler as otp_send_handler  # noqa: E402
import handlers.otp_verify_handler as otp_verify_handler  # noqa: E402
import handlers.social_auth_handler as social_auth_handler  # noqa: E402
import handlers.token_refresh_handler as token_refresh_handler  # noqa: E402

ROUTES = {
    ("POST", "/v1/auth/otp/send"): otp_send_handler.handler,
    ("POST", "/v1/auth/otp/verify"): otp_verify_handler.handler,
    ("POST", "/v1/auth/social"): social_auth_handler.handler,
    ("POST", "/v1/auth/token/refresh"): token_refresh_handler.handler,
    ("POST", "/v1/auth/login/otp/send"): login_otp_send_handler.handler,
    ("POST", "/v1/auth/login/otp/verify"): login_otp_verify_handler.handler,
    ("POST", "/v1/auth/logout"): logout_handler.handler,
}

if __name__ == "__main__":
    serve(ROUTES, port=8001)
