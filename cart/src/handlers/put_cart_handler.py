"""PUT /cart — thin Lambda entrypoint (FR-3).

Full-cart replace: the request's `items` list is the complete desired
state — any existing line item not present in it is removed. `ifVersion`
must match the caller's last-read `cartVersion` (optimistic concurrency)
or the write is rejected with 409 CART_VERSION_MISMATCH and no write
occurs.
"""

import json
import logging
import uuid

from pydantic import ValidationError as PydanticValidationError

from config.env import get_settings
from domain.exceptions import CartServiceError
from handlers.composition import build_cart_service
from handlers.dto import (
    ReplaceCartRequestDto,
    error_response,
    extract_jwt_claims,
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

    cognito_sub = extract_jwt_claims(event).get("sub")
    if not cognito_sub:
        return validation_error_response("Missing or invalid JWT claims")

    try:
        body = json.loads(event.get("body") or "{}")
        request_dto = ReplaceCartRequestDto.model_validate(body)
    except (json.JSONDecodeError, PydanticValidationError) as exc:
        return validation_error_response(str(exc))

    try:
        cart = deps["cart_service"].replace_cart(
            cognito_sub,
            [item.to_domain_dict() for item in request_dto.items],
            request_dto.if_version,
        )
        return success_response(serialize_cart(cart))
    except CartServiceError as exc:
        logger.info(
            "put_cart rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
    except Exception:
        logger.exception("put_cart: unexpected error", extra={"correlationId": correlation_id})
        return error_response(CartServiceError("An unexpected error occurred"))
