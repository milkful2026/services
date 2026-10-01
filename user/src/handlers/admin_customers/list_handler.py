"""GET /v1/admin/customers — thin Lambda entrypoint (MA-139 §4 FR-1).
Paginated, status-filter + name/mobile/email free-text search via query
string parameters. Admin-authorized — see handlers/admin_context.py."""

import logging
import uuid

from config.env import get_settings
from domain.exceptions import UserServiceError
from handlers.admin_context import get_caller_admin
from handlers.admin_customers.composition import build_customer_status_service
from handlers.admin_customers.dto import serialize_customer_page
from handlers.dto import error_response, success_response

logger = logging.getLogger(__name__)

_deps: dict | None = None


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps
    settings = get_settings()
    _deps = {"service": build_customer_status_service(settings), "settings": settings}
    return _deps


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = (event.get("headers") or {}).get("x-request-id", str(uuid.uuid4()))
    deps["service"].set_correlation_id(correlation_id)

    try:
        get_caller_admin(event)
    except UserServiceError as exc:
        return error_response(exc)

    params = event.get("queryStringParameters") or {}
    status = params.get("status") or None
    search = params.get("search") or None
    try:
        page = int(params.get("page", "1"))
        page_size = int(params.get("pageSize", str(deps["settings"].admin_default_page_size)))
    except ValueError:
        page, page_size = 1, deps["settings"].admin_default_page_size

    try:
        result = deps["service"].list_customers(status, search, page, page_size)
        return success_response(serialize_customer_page(result))
    except UserServiceError as exc:
        logger.info(
            "admin_customers.list rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
    except Exception:
        logger.exception(
            "admin_customers.list: unexpected error", extra={"correlationId": correlation_id}
        )
        return error_response(UserServiceError("An unexpected error occurred"))
