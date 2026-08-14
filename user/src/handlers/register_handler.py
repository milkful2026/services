"""POST /users/register — thin Lambda entrypoint (FR-1).

`sub` is extracted from the API Gateway HTTP API JWT authorizer's
verified claims (`requestContext.authorizer.jwt.claims`), never trusted
from the request body — per spec §5b and the spec's own explicit
requirement that the registering user's identity comes from the verified
JWT claim.

`mobile` is *not* read from a JWT claim (a `phone_number` claim, which
the original implementation relied on, since spec §FR-1 explicitly
authorizes this endpoint with a Cognito *access* token, and access tokens
never carry profile attributes — only ID tokens do). It's instead
resolved server-side from Cognito via `sub`
(`registration_service.resolve_mobile`) — see
adapters/cognito_attribute_adapter.py's module docstring for the full
story of why.
"""

import json
import logging
import uuid

from pydantic import ValidationError as PydanticValidationError

from config.env import get_settings
from domain.exceptions import UserServiceError
from handlers.composition import build_registration_service
from handlers.dto import (
    RegisterRequestDto,
    error_response,
    extract_jwt_claims,
    serialize_registration_result,
    success_response,
    validation_error_response,
)

logger = logging.getLogger(__name__)

_deps: dict | None = None


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps

    settings = get_settings()
    _deps = {"registration_service": build_registration_service(settings)}
    return _deps


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = (event.get("headers") or {}).get("x-request-id", str(uuid.uuid4()))
    deps["registration_service"].set_correlation_id(correlation_id)

    claims = extract_jwt_claims(event)
    cognito_sub = claims.get("sub")
    if not cognito_sub:
        return validation_error_response("Missing or invalid JWT claims")

    try:
        body = json.loads(event.get("body") or "{}")
        request_dto = RegisterRequestDto.model_validate(body)
    except (json.JSONDecodeError, PydanticValidationError) as exc:
        return validation_error_response(str(exc))

    try:
        mobile = deps["registration_service"].resolve_mobile(cognito_sub)
        domain_request = request_dto.to_domain(cognito_sub, mobile)
        result = deps["registration_service"].register(domain_request)
        status_code = 201 if result.is_new_user else 200
        return success_response(serialize_registration_result(result), status_code=status_code)
    except UserServiceError as exc:
        logger.info(
            "register rejected", extra={"correlationId": correlation_id, "errorCode": exc.error_code}
        )
        return error_response(exc)
    except Exception:
        logger.exception(
            "register: unexpected error", extra={"correlationId": correlation_id}
        )
        return error_response(UserServiceError("An unexpected error occurred"))
