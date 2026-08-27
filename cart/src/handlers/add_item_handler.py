"""POST /cart/items — thin Lambda entrypoint (FR-2).

Idempotency-Key is an optional request header (FR-2) — a repeated
request with the same key within the bounded window (24h,
cart_repository.py's own TTL) returns the original result rather than
creating a duplicate line item, matching services/README.md §5's
documented idempotent-writes convention.
"""

import json
import logging
import uuid

from pydantic import ValidationError as PydanticValidationError

from config.env import get_settings
from domain.exceptions import CartServiceError
from handlers.composition import build_cart_service
from handlers.dto import (
    AddItemRequestDto,
    error_response,
    extract_jwt_claims,
    serialize_line_item,
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
        request_dto = AddItemRequestDto.model_validate(body)
    except (json.JSONDecodeError, PydanticValidationError) as exc:
        return validation_error_response(str(exc))

    idempotency_key = (event.get("headers") or {}).get("idempotency-key")

    try:
        line_item = deps["cart_service"].add_item(
            cognito_sub,
            request_dto.product_id,
            request_dto.quantity,
            request_dto.to_frequency(),
            request_dto.start_date,
            idempotency_key,
        )
        return success_response(serialize_line_item(line_item), status_code=201)
    except CartServiceError as exc:
        logger.info(
            "add_item rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
    except Exception:
        logger.exception("add_item: unexpected error", extra={"correlationId": correlation_id})
        return error_response(CartServiceError("An unexpected error occurred"))
