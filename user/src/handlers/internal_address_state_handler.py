"""GET /v1/internal/users/address-state?cognitoSub= — thin Lambda
entrypoint (MA-96 impl plan §4A).

Service-to-service call for Cart Service (and any future caller) to
resolve a user's default-address state without re-authenticating as
that user — not JWT-authenticated the way /users/me is; network
isolation (this route is never exposed outside the VPC) is the
boundary, matching Inventory's own public-vs-internal endpoint split
(serviceability_check_handler.py vs
internal_serviceability_check_handler.py). Reuses
RegistrationService.get_my_profile unchanged — this is a new transport
onto existing domain logic, not new domain logic.

Query parameter, not a path parameter, matching Inventory's own
internal endpoint's convention (`?pincode=&lat=&lng=`) and this
codebase's local Lambda shim, which only matches routes by exact
literal path (services/local-dev/_lambda_local_server.py).
"""

import logging
import uuid

from config.env import get_settings
from domain.exceptions import UserServiceError, ValidationError
from handlers.composition import build_registration_service
from handlers.dto import error_response, success_response

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

    cognito_sub = (event.get("queryStringParameters") or {}).get("cognitoSub")
    if not cognito_sub:
        return error_response(ValidationError("cognitoSub query parameter is required"))

    try:
        profile = deps["registration_service"].get_my_profile(cognito_sub)
        return success_response({"defaultAddressState": profile.default_address_state})
    except UserServiceError as exc:
        logger.info(
            "internal_address_state rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
    except Exception:
        logger.exception(
            "internal_address_state: unexpected error", extra={"correlationId": correlation_id}
        )
        return error_response(UserServiceError("An unexpected error occurred"))
