"""POST /v1/admin/auth/login — thin Lambda entrypoint (spec FR-1).

Pre-auth (no Cognito JWT authorizer) — this and the 2FA-verify endpoint
are the only two `/v1/admin/*` routes not behind admin_authorizer_handler.
"""

import json
import logging
import uuid

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import create_engine

from adapters.admin_challenge_store_adapter import AdminChallengeStoreAdapter
from adapters.admin_cognito_adapter import AdminCognitoAdapter
from adapters.admin_lockout_adapter import AdminLockoutAdapter
from adapters.admin_session_registry import AdminSessionRegistryAdapter
from adapters.admin_user_repository import SqlAlchemyAdminUserRepository
from adapters.notification_publisher import EventBridgeNotificationPublisher
from adapters.rate_limit_adapter import build_redis_client
from config.env import get_settings
from domain.admin_auth.login_service import AdminLoginService
from domain.exceptions import IdentityAuthError
from handlers.admin_auth.dto import AdminLoginRequest
from handlers.dto import error_response, success_response, validation_error_response

logger = logging.getLogger(__name__)

_deps: dict | None = None


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps

    settings = get_settings()
    engine = create_engine(settings.admin_database_url)
    admin_repo = SqlAlchemyAdminUserRepository(engine)
    cognito = AdminCognitoAdapter(
        settings.admin_cognito_user_pool_id, settings.admin_cognito_client_id, settings.aws_region
    )
    redis_client = build_redis_client(settings.redis_host, settings.redis_port, settings.redis_use_tls)
    challenge_store = AdminChallengeStoreAdapter(redis_client)
    lockout = AdminLockoutAdapter(redis_client)
    session_registry = AdminSessionRegistryAdapter(redis_client)
    publisher = EventBridgeNotificationPublisher(
        settings.event_bus_name, settings.event_source, settings.aws_region
    )

    login_service = AdminLoginService(
        admin_repo=admin_repo,
        cognito=cognito,
        challenge_store=challenge_store,
        lockout=lockout,
        session_registry=session_registry,
        event_publisher=publisher,
        challenge_ttl_seconds=settings.admin_challenge_ttl_seconds,
        lockout_max_attempts=settings.admin_lockout_max_attempts,
        lockout_window_seconds=settings.admin_lockout_window_seconds,
        lockout_duration_seconds=settings.admin_lockout_duration_seconds,
    )

    _deps = {"login_service": login_service, "settings": settings}
    return _deps


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = (event.get("headers") or {}).get("x-request-id", str(uuid.uuid4()))

    try:
        body = json.loads(event.get("body") or "{}")
        request = AdminLoginRequest.model_validate(body)
    except (json.JSONDecodeError, PydanticValidationError) as exc:
        return validation_error_response(str(exc))

    try:
        challenge_token = deps["login_service"].login_password(request.email, request.password, correlation_id)
        return success_response(
            {"challengeToken": challenge_token, "expiresIn": deps["settings"].admin_challenge_ttl_seconds}
        )
    except IdentityAuthError as exc:
        logger.info(
            "admin_login rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
