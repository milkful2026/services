"""POST /cart/internal/users/{userId}/remove-items — thin Lambda
entrypoint (MA-135 FR-4). Order Service's post-checkout clear.

IAM (SigV4)-authorized, same trust reasoning as
internal_get_cart_handler.py. A stale `ifVersion` is the existing 409
CART_VERSION_MISMATCH; a retried clear whose items are already gone is a
200 with the current cart (see CartRepositoryPort.remove_items).
"""

import json
import logging
import uuid

from pydantic import ValidationError as PydanticValidationError

from config.env import get_settings
from domain.exceptions import CartServiceError
from handlers.composition import build_cart_service
from handlers.dto import (
    InternalRemoveItemsRequestDto,
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
        body = json.loads(event.get("body") or "{}")
        request_dto = InternalRemoveItemsRequestDto.model_validate(body)
    except (json.JSONDecodeError, PydanticValidationError) as exc:
        return validation_error_response(str(exc))

    try:
        cart = deps["cart_service"].remove_items_internal(
            user_id, request_dto.item_ids, request_dto.if_version, request_dto.checkout_id
        )
        return success_response(serialize_cart(cart))
    except CartServiceError as exc:
        logger.info(
            "internal_remove_items rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
    except Exception:
        logger.exception(
            "internal_remove_items: unexpected error", extra={"correlationId": correlation_id}
        )
        return error_response(CartServiceError("An unexpected error occurred"))
