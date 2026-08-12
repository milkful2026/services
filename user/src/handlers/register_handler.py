"""POST /users/register — thin Lambda entrypoint (FR-1).

`sub` and `mobile` are extracted from the API Gateway HTTP API JWT
authorizer's verified claims (`requestContext.authorizer.jwt.claims`),
never trusted from the request body — per spec §5b and the spec's own
explicit requirement that the registering user's identity comes from the
verified JWT claim.
"""

import json
import logging
import uuid

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import create_engine

from adapters.cognito_attribute_adapter import CognitoAttributeAdapter
from adapters.inventory_client_adapter import HttpInventoryClient
from adapters.user_repository import SqlAlchemyUserRepository
from config.env import get_settings
from domain.exceptions import UserServiceError
from domain.registration_service import RegistrationService
from handlers.dto import (
    RegisterRequestDto,
    error_response,
    serialize_registration_result,
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
    engine = create_engine(settings.database_url)
    repository = SqlAlchemyUserRepository(engine)
    inventory_client = HttpInventoryClient(
        settings.inventory_internal_base_url, settings.inventory_request_timeout_seconds
    )
    cognito_attributes = CognitoAttributeAdapter(settings.cognito_user_pool_id, settings.aws_region)
    registration_service = RegistrationService(repository, inventory_client, cognito_attributes)

    _deps = {"registration_service": registration_service}
    return _deps


def _extract_claims(event: dict) -> dict:
    return event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = (event.get("headers") or {}).get("x-request-id", str(uuid.uuid4()))

    claims = _extract_claims(event)
    cognito_sub = claims.get("sub")
    mobile = claims.get("phone_number")
    if not cognito_sub or not mobile:
        return validation_error_response("Missing or invalid JWT claims")

    try:
        body = json.loads(event.get("body") or "{}")
        request_dto = RegisterRequestDto.model_validate(body)
    except (json.JSONDecodeError, PydanticValidationError) as exc:
        return validation_error_response(str(exc))

    domain_request = request_dto.to_domain(cognito_sub, mobile)

    try:
        result = deps["registration_service"].register(domain_request)
        status_code = 201 if result.is_new_user else 200
        return success_response(serialize_registration_result(result), status_code=status_code)
    except UserServiceError as exc:
        logger.info(
            "register rejected", extra={"correlationId": correlation_id, "errorCode": exc.error_code}
        )
        return error_response(exc)
