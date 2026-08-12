"""POST /v1/auth/token/refresh — thin Lambda entrypoint (FR-4)."""

import json
import logging
import uuid

from pydantic import ValidationError as PydanticValidationError

from adapters.cognito_adapter import CognitoAdapter
from config.env import get_settings
from domain.exceptions import IdentityAuthError
from domain.token_service import TokenService
from handlers.dto import TokenRefreshRequest, error_response, success_response, validation_error_response

logger = logging.getLogger(__name__)

_deps: dict | None = None


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps

    settings = get_settings()
    cognito = CognitoAdapter(
        settings.cognito_user_pool_id, settings.cognito_client_id, settings.aws_region
    )
    _deps = {"token_service": TokenService(cognito)}
    return _deps


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = (event.get("headers") or {}).get("x-request-id", str(uuid.uuid4()))

    try:
        body = json.loads(event.get("body") or "{}")
        request = TokenRefreshRequest.model_validate(body)
    except (json.JSONDecodeError, PydanticValidationError) as exc:
        return validation_error_response(str(exc))

    try:
        tokens = deps["token_service"].refresh(request.refresh_token)
        return success_response(
            {
                "accessToken": tokens.access_token,
                "refreshToken": tokens.refresh_token,
                "expiresIn": tokens.expires_in,
            }
        )
    except IdentityAuthError as exc:
        logger.info(
            "token_refresh rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
