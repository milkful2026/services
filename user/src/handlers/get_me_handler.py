"""GET /users/me — thin Lambda entrypoint (MA-107 FR-2).

Resolved via the API Gateway HTTP API JWT authorizer's verified `sub`
claim, never a client-supplied ID (services/README.md §5b) — same
pattern as register_handler.py. Read-only, no request body.
"""

import logging
import uuid

from config.env import get_settings
from domain.exceptions import UserServiceError
from handlers.composition import build_registration_service
from handlers.dto import error_response, serialize_user_profile, success_response, validation_error_response

logger = logging.getLogger(__name__)

_deps: dict | None = None


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps

    settings = get_settings()
    _deps = {"registration_service": build_registration_service(settings)}
    return _deps


def _extract_claims(event: dict) -> dict:
    return event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = (event.get("headers") or {}).get("x-request-id", str(uuid.uuid4()))
    deps["registration_service"].set_correlation_id(correlation_id)

    cognito_sub = _extract_claims(event).get("sub")
    if not cognito_sub:
        return validation_error_response("Missing or invalid JWT claims")

    try:
        profile = deps["registration_service"].get_my_profile(cognito_sub)
        return success_response(serialize_user_profile(profile))
    except UserServiceError as exc:
        logger.info(
            "get_me rejected", extra={"correlationId": correlation_id, "errorCode": exc.error_code}
        )
        return error_response(exc)
    except Exception:
        logger.exception("get_me: unexpected error", extra={"correlationId": correlation_id})
        return error_response(UserServiceError("An unexpected error occurred"))
