"""GET /cart — thin Lambda entrypoint (FR-1).

No cart yet for this caller -> 200 with an empty line-item list and
cartVersion: 0 (CartService.get_cart already returns that shape via the
repository's own "no rows found" case) — never a 404, since "no cart"
and "empty cart" are the same state from the caller's perspective.
"""

import logging
import uuid

from config.env import get_settings
from domain.exceptions import CartServiceError
from handlers.composition import build_cart_service
from handlers.dto import (
    error_response,
    extract_jwt_claims,
    serialize_cart_view,
    success_response,
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

    try:
        view = deps["cart_service"].get_cart(cognito_sub)
        return success_response(serialize_cart_view(view))
    except CartServiceError as exc:
        logger.info(
            "get_cart rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
    except Exception:
        logger.exception("get_cart: unexpected error", extra={"correlationId": correlation_id})
        return error_response(CartServiceError("An unexpected error occurred"))
