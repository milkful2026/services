"""GET /cart/internal/users/{userId} — thin Lambda entrypoint (MA-135
FR-3). Order Service's checkout read.

IAM (SigV4)-authorized at API Gateway, not the Cognito JWT authorizer
every public route uses — the `userId` path parameter is trusted *only*
because API Gateway already verified the caller is Order Service's own
execution role (see cart_stack.py). Returns `{items, cartVersion}` with no
quote: Order prices the one-time lines itself at checkout time.
"""

import logging
import uuid

from config.env import get_settings
from domain.exceptions import CartServiceError
from handlers.composition import build_cart_service
from handlers.dto import (
    error_response,
    serialize_cart,
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

    user_id = (event.get("pathParameters") or {}).get("userId")
    if not user_id:
        return validation_error_response("Missing userId path parameter")

    try:
        cart = deps["cart_service"].get_cart_internal(user_id)
        return success_response(serialize_cart(cart))
    except CartServiceError as exc:
        logger.info(
            "internal_get_cart rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
    except Exception:
        logger.exception(
            "internal_get_cart: unexpected error", extra={"correlationId": correlation_id}
        )
        return error_response(CartServiceError("An unexpected error occurred"))
