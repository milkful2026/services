"""POST /v1/auth/logout — thin Lambda entrypoint (MA-21 FR-3).

Protected by the API Gateway Cognito JWT authorizer (unlike every other
endpoint in this service, which are all pre-auth) — the caller must be
an authenticated request. This handler itself doesn't inspect the JWT
claims; it revokes whichever refresh token is supplied in the body,
scoped to the calling device only (cognito_adapter.revoke_token).

Per spec: idempotent on an already-revoked/expired token (still 204,
not an error) — that's handled inside cognito_adapter.revoke_token
itself, which swallows every Cognito-side error except a genuinely
malformed token (ValidationError, mapped to 400 here like any other).
"""

import json
import logging
import uuid

from pydantic import ValidationError as PydanticValidationError

from adapters.cognito_adapter import CognitoAdapter
from config.env import get_settings
from domain.exceptions import IdentityAuthError
from handlers.dto import (
    LogoutRequest,
    error_response,
    no_content_response,
    validation_error_response,
)

logger = logging.getLogger(__name__)

_deps: dict | None = None


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps

    settings = get_settings()
    cognito = CognitoAdapter(
        settings.cognito_user_pool_id,
        settings.cognito_client_id,
        settings.aws_region,
        endpoint_url=settings.aws_endpoint_url,
    )
    _deps = {"cognito": cognito}
    return _deps


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = (event.get("headers") or {}).get("x-request-id", str(uuid.uuid4()))

    try:
        body = json.loads(event.get("body") or "{}")
        request = LogoutRequest.model_validate(body)
    except (json.JSONDecodeError, PydanticValidationError) as exc:
        return validation_error_response(str(exc))

    try:
        deps["cognito"].revoke_token(request.refresh_token)
        return no_content_response()
    except IdentityAuthError as exc:
        logger.info(
            "logout rejected", extra={"correlationId": correlation_id, "errorCode": exc.error_code}
        )
        return error_response(exc)
