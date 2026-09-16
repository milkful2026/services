"""POST /v1/admin/users — thin Lambda entrypoint (spec FR-3).

Behind admin_authorizer_handler.py's authorizer (SuperAdmin-only, enforced
both here at the domain layer via caller role from the request context —
never trusted from the client body — and again as defense-in-depth
inside AdminUserService itself, per services/README.md §5b)."""

import json
import logging
import uuid

from pydantic import ValidationError as PydanticValidationError

from config.env import get_settings
from domain.exceptions import IdentityAuthError
from handlers.admin_context import get_caller_admin
from handlers.admin_users.composition import build_admin_user_service
from handlers.admin_users.dto import AdminCreateUserRequest, admin_to_dict
from handlers.dto import error_response, success_response, validation_error_response

logger = logging.getLogger(__name__)

_deps: dict | None = None


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps
    settings = get_settings()
    _deps = {"user_service": build_admin_user_service(settings)}
    return _deps


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = (event.get("headers") or {}).get("x-request-id", str(uuid.uuid4()))

    try:
        caller = get_caller_admin(event)
    except IdentityAuthError as exc:
        return error_response(exc)

    try:
        body = json.loads(event.get("body") or "{}")
        request = AdminCreateUserRequest.model_validate(body)
    except (json.JSONDecodeError, PydanticValidationError) as exc:
        return validation_error_response(str(exc))

    try:
        created = deps["user_service"].create_admin(
            caller["role"], caller["adminId"], request.name, request.email, request.role, correlation_id
        )
        return success_response(admin_to_dict(created), status_code=201)
    except IdentityAuthError as exc:
        logger.info(
            "admin_create_user rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
