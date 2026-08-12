"""GET /delivery/slots?zoneId= — thin Lambda entrypoint (FR-2).

Routes through RegistrationService (domain), not the repository
directly, to respect the fixed Handler -> Domain -> Adapters dependency
direction even though this read path is simple.
"""

import logging
import uuid

from sqlalchemy import create_engine

from adapters.cognito_attribute_adapter import CognitoAttributeAdapter
from adapters.inventory_client_adapter import HttpInventoryClient
from adapters.user_repository import SqlAlchemyUserRepository
from config.env import get_settings
from domain.exceptions import UserServiceError, ValidationError
from domain.registration_service import RegistrationService
from handlers.dto import error_response, serialize_delivery_slots, success_response

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


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = (event.get("headers") or {}).get("x-request-id", str(uuid.uuid4()))

    zone_id = (event.get("queryStringParameters") or {}).get("zoneId")
    if not zone_id:
        return error_response(ValidationError("zoneId query parameter is required"))

    try:
        slots = deps["registration_service"].get_delivery_slots(zone_id)
        return success_response(serialize_delivery_slots(slots))
    except UserServiceError as exc:
        logger.info(
            "delivery_slots rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
