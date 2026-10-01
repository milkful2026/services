"""POST /v1/admin/customers/{id}/suspend — thin Lambda entrypoint
(MA-139 §4 FR-3). Admin-authorized — see handlers/admin_context.py."""

import json
import logging
import uuid

from pydantic import ValidationError as PydanticValidationError

from config.env import get_settings
from domain.exceptions import UserServiceError, ValidationError
from handlers.admin_context import get_caller_admin
from handlers.admin_customers.composition import build_customer_status_service
from handlers.admin_customers.dto import SuspendRequest, serialize_customer_detail
from handlers.dto import error_response, success_response, validation_error_response

logger = logging.getLogger(__name__)

_deps: dict | None = None


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps
    settings = get_settings()
    _deps = {"service": build_customer_status_service(settings)}
    return _deps


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = (event.get("headers") or {}).get("x-request-id", str(uuid.uuid4()))
    deps["service"].set_correlation_id(correlation_id)

    try:
        caller = get_caller_admin(event)
    except UserServiceError as exc:
        return error_response(exc)

    customer_id = (event.get("pathParameters") or {}).get("id")
    if not customer_id:
        return error_response(ValidationError("Missing customer id in path"))

    try:
        body = json.loads(event.get("body") or "{}")
        request = SuspendRequest.model_validate(body)
    except (json.JSONDecodeError, PydanticValidationError) as exc:
        return validation_error_response(str(exc))

    try:
        account = deps["service"].suspend(
            customer_id, request.reason, request.until, caller["adminId"]
        )
        return success_response(serialize_customer_detail(account))
    except UserServiceError as exc:
        logger.info(
            "admin_customers.suspend rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
    except Exception:
        logger.exception(
            "admin_customers.suspend: unexpected error", extra={"correlationId": correlation_id}
        )
        return error_response(UserServiceError("An unexpected error occurred"))
