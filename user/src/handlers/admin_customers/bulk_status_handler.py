"""POST /v1/admin/customers/bulk-status — thin Lambda entrypoint
(MA-139 §4 FR-6). Admin-authorized — see handlers/admin_context.py.

Unlike every other handler here, a per-row failure does NOT produce an
error_response — FR-6's contract is a 200 with a per-id result array
(`{customerId, success, errorCode?}`); only a request-level failure
(missing/malformed body, an invalid `action`) is an error_response.
"""

import json
import logging
import uuid

from pydantic import ValidationError as PydanticValidationError

from config.env import get_settings
from domain.exceptions import UserServiceError
from handlers.admin_context import get_caller_admin
from handlers.admin_customers.composition import build_customer_status_service
from handlers.admin_customers.dto import BulkStatusRequest, serialize_bulk_results
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

    try:
        body = json.loads(event.get("body") or "{}")
        request = BulkStatusRequest.model_validate(body)
    except (json.JSONDecodeError, PydanticValidationError) as exc:
        return validation_error_response(str(exc))

    try:
        results = deps["service"].bulk_status_change(
            request.customer_ids, request.action, request.reason, request.until, caller["adminId"]
        )
        return success_response(serialize_bulk_results(results))
    except UserServiceError as exc:
        # Request-level rejection only (e.g. an invalid `action` value) —
        # per-row failures never reach here, they're captured in `results`.
        logger.info(
            "admin_customers.bulk_status rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
    except Exception:
        logger.exception(
            "admin_customers.bulk_status: unexpected error", extra={"correlationId": correlation_id}
        )
        return error_response(UserServiceError("An unexpected error occurred"))
