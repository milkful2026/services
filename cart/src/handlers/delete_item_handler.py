"""DELETE /cart/items/{id} — thin Lambda entrypoint (FR-4).

404 if the id doesn't exist or doesn't belong to the caller — never
distinguishing the two in the response, so a caller can't use this
endpoint to probe whether some other account has a given line-item id.
"""

import logging
import uuid

from config.env import get_settings
from domain.exceptions import CartServiceError, ValidationError
from handlers.composition import build_cart_service
from handlers.dto import (
    error_response,
    extract_jwt_claims,
    no_content_response,
    validation_error_response,
)

logger = logging.getLogger(__name__)

_deps: dict | None = None


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps

    settings = get_settings()
    _deps = {"cart_service": build_cart_service(settings)}
    return _deps


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = (event.get("headers") or {}).get("x-request-id", str(uuid.uuid4()))
    deps["cart_service"].set_correlation_id(correlation_id)

    cognito_sub = extract_jwt_claims(event).get("sub")
    if not cognito_sub:
        return validation_error_response("Missing or invalid JWT claims")

    line_item_id = (event.get("pathParameters") or {}).get("id")
    if not line_item_id:
        return error_response(ValidationError("Missing line item id"))

    try:
        deps["cart_service"].delete_item(cognito_sub, line_item_id)
        return no_content_response()
    except CartServiceError as exc:
        logger.info(
            "delete_item rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
    except Exception:
        logger.exception("delete_item: unexpected error", extra={"correlationId": correlation_id})
        return error_response(CartServiceError("An unexpected error occurred"))
