"""Runs this service's Lambda handlers locally over plain HTTP, for the
docker-compose-based local dev environment (see services/local-dev/).
Not used in any deployed environment — Lambda invokes handler(event,
context) directly there, via API Gateway's own integration, not this
file.

    python run_local.py

Requires services/local-dev/bootstrap.py to have already run (creates
this service's .env.local with the moto-backed Cognito pool/table IDs).
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

# --- MA-129 Admin RBAC ------------------------------------------------
#
# admin_users/*_handler.py (create/list/update/deactivate/reactivate)
# and admin_authorizer_handler.py's repo/publisher all work unmodified
# against moto+local Postgres — no Cognito MFA or JWT-signature
# verification involved in those. Only two pieces genuinely can't run
# as production code does locally (see _admin_local_dev.py's module
# docstring for why) and are substituted here, nowhere else:
#
#   1. The admin login/2FA flow's Cognito adapter (real MFA challenge)
#   2. The admin authorizer's JWT verifier (real JWKS signature check)
import handlers.admin_auth.login_handler as admin_login_handler  # noqa: E402
import handlers.admin_auth.verify_2fa_handler as admin_verify_2fa_handler  # noqa: E402
import handlers.admin_authorizer_handler as admin_authorizer_handler  # noqa: E402
import handlers.admin_users.create_handler as admin_create_handler  # noqa: E402
import handlers.admin_users.deactivate_handler as admin_deactivate_handler  # noqa: E402
import handlers.admin_users.list_handler as admin_list_handler  # noqa: E402
import handlers.admin_users.reactivate_handler as admin_reactivate_handler  # noqa: E402
import handlers.admin_users.update_handler as admin_update_handler  # noqa: E402
from adapters.admin_challenge_store_adapter import AdminChallengeStoreAdapter  # noqa: E402
from adapters.admin_cognito_adapter import AdminCognitoAdapter  # noqa: E402
from adapters.admin_lockout_adapter import AdminLockoutAdapter  # noqa: E402
from adapters.admin_session_registry import AdminSessionRegistryAdapter  # noqa: E402
from adapters.admin_user_repository import SqlAlchemyAdminUserRepository  # noqa: E402
from adapters.notification_publisher import EventBridgeNotificationPublisher  # noqa: E402
from adapters.rate_limit_adapter import build_redis_client  # noqa: E402
from config.env import get_settings  # noqa: E402
from domain.admin_auth.login_service import AdminLoginService  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from _admin_local_dev import LocalDevAdminCognitoAdapter, LocalDevUnsignedJwtVerifier  # noqa: E402

_settings = get_settings()
_admin_engine = create_engine(_settings.admin_database_url)
_admin_repo = SqlAlchemyAdminUserRepository(_admin_engine)
_admin_redis = build_redis_client(_settings.redis_host, _settings.redis_port, _settings.redis_use_tls)
_admin_publisher = EventBridgeNotificationPublisher(
    _settings.event_bus_name, _settings.event_source, _settings.aws_region
)

# Pre-populate login_handler.py / verify_2fa_handler.py's own private
# `_deps` module caches with a service built on the fake-MFA adapter —
# each handler's real `_get_deps()` sees `_deps is not None` and returns
# this instead of constructing its own (which would use the real,
# always-fails-against-moto adapter). Real handler/domain code runs
# completely unmodified from here on.
_local_admin_login_service = AdminLoginService(
    admin_repo=_admin_repo,
    cognito=LocalDevAdminCognitoAdapter(
        AdminCognitoAdapter(
            _settings.admin_cognito_user_pool_id, _settings.admin_cognito_client_id, _settings.aws_region
        )
    ),
    challenge_store=AdminChallengeStoreAdapter(_admin_redis),
    lockout=AdminLockoutAdapter(_admin_redis),
    session_registry=AdminSessionRegistryAdapter(_admin_redis),
    event_publisher=_admin_publisher,
    challenge_ttl_seconds=_settings.admin_challenge_ttl_seconds,
    lockout_max_attempts=_settings.admin_lockout_max_attempts,
    lockout_window_seconds=_settings.admin_lockout_window_seconds,
    lockout_duration_seconds=_settings.admin_lockout_duration_seconds,
)
admin_login_handler._deps = {"login_service": _local_admin_login_service, "settings": _settings}
admin_verify_2fa_handler._deps = {"login_service": _local_admin_login_service}

# Same pre-populate trick for the authorizer — real repo/publisher, only
# the JWKS-dependent verifier is swapped.
admin_authorizer_handler._deps = {
    "verifier": LocalDevUnsignedJwtVerifier(),
    "repo": _admin_repo,
    "publisher": _admin_publisher,
}

ROUTES.update(
    {
        ("POST", "/v1/admin/auth/login"): admin_login_handler.handler,
        ("POST", "/v1/admin/auth/2fa/verify"): admin_verify_2fa_handler.handler,
        ("POST", "/v1/admin/users"): (admin_create_handler.handler, admin_authorizer_handler.handler),
        ("GET", "/v1/admin/users"): (admin_list_handler.handler, admin_authorizer_handler.handler),
        ("PATCH", "/v1/admin/users/{id}"): (admin_update_handler.handler, admin_authorizer_handler.handler),
        ("POST", "/v1/admin/users/{id}/deactivate"): (
            admin_deactivate_handler.handler,
            admin_authorizer_handler.handler,
        ),
        ("POST", "/v1/admin/users/{id}/reactivate"): (
            admin_reactivate_handler.handler,
            admin_authorizer_handler.handler,
        ),
    }
)

if __name__ == "__main__":
    serve(ROUTES, port=8001)
