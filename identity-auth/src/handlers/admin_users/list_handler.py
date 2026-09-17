"""GET /v1/admin/users — thin Lambda entrypoint (spec FR-4). Paginated,
role/status filter + name/email search via query string parameters."""

import logging
import uuid

from config.env import get_settings
from domain.exceptions import IdentityAuthError
from handlers.admin_context import get_caller_admin
from handlers.admin_users.composition import build_admin_user_service
from handlers.admin_users.dto import admin_page_to_dict
from handlers.dto import error_response, success_response

logger = logging.getLogger(__name__)

_deps: dict | None = None


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps
    settings = get_settings()
    _deps = {"user_service": build_admin_user_service(settings), "settings": settings}
    return _deps


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = (event.get("headers") or {}).get("x-request-id", str(uuid.uuid4()))

    try:
        caller = get_caller_admin(event)
    except IdentityAuthError as exc:
        return error_response(exc)

    params = event.get("queryStringParameters") or {}
    role = params.get("role") or None
    status = params.get("status") or None
    search = params.get("search") or None
    try:
        page = int(params.get("page", "1"))
        page_size = int(params.get("pageSize", str(deps["settings"].admin_default_page_size)))
    except ValueError:
        page, page_size = 1, deps["settings"].admin_default_page_size

    try:
        result = deps["user_service"].list_admins(caller["role"], role, status, search, page, page_size)
        return success_response(admin_page_to_dict(result))
    except IdentityAuthError as exc:
        logger.info(
            "admin_list_users rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
