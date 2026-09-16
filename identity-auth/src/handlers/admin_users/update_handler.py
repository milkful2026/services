"""PATCH /v1/admin/users/{id} — thin Lambda entrypoint (spec FR-4).

Distinguishes "field omitted" from "field explicitly set to null/empty"
via pydantic's `model_fields_set` — a PATCH that omits `ipAllowlist`
entirely must leave it untouched, while one that sends `"ipAllowlist":
[]` must clear it. Same distinction applies to `maxConcurrentSessions`.
"""

import json
import logging
import uuid

from pydantic import ValidationError as PydanticValidationError

from config.env import get_settings
from domain.exceptions import IdentityAuthError, ValidationError
from handlers.admin_context import get_caller_admin
from handlers.admin_users.composition import build_admin_user_service
from handlers.admin_users.dto import AdminUpdateUserRequest, admin_to_dict
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

    target_id = (event.get("pathParameters") or {}).get("id")
    if not target_id:
        return error_response(ValidationError("Missing admin id in path"))

    try:
        body = json.loads(event.get("body") or "{}")
        request = AdminUpdateUserRequest.model_validate(body)
    except (json.JSONDecodeError, PydanticValidationError) as exc:
        return validation_error_response(str(exc))

    ip_allowlist_set = "ip_allowlist" in request.model_fields_set
    max_concurrent_sessions_set = "max_concurrent_sessions" in request.model_fields_set

    try:
        updated = deps["user_service"].update_admin(
            caller["role"],
            target_id,
            role=request.role,
            ip_allowlist=request.ip_allowlist,
            ip_allowlist_set=ip_allowlist_set,
            max_concurrent_sessions=request.max_concurrent_sessions,
            max_concurrent_sessions_set=max_concurrent_sessions_set,
            correlation_id=correlation_id,
        )
        return success_response(admin_to_dict(updated))
    except IdentityAuthError as exc:
        logger.info(
            "admin_update_user rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
