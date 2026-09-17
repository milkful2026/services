"""POST /v1/admin/users/{id}/deactivate — thin Lambda entrypoint (FR-4)."""

import logging
import uuid

from config.env import get_settings
from domain.exceptions import IdentityAuthError, ValidationError
from handlers.admin_context import get_caller_admin
from handlers.admin_users.composition import build_admin_user_service
from handlers.admin_users.dto import admin_to_dict
from handlers.dto import error_response, success_response

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

    target_id = (event.get("pathParameters") or {}).get("id")
    if not target_id:
        return error_response(ValidationError("Missing admin id in path"))

    try:
        updated = deps["user_service"].deactivate_admin(caller["role"], caller["adminId"], target_id, correlation_id)
        return success_response(admin_to_dict(updated))
    except IdentityAuthError as exc:
        logger.info(
            "admin_deactivate_user rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
