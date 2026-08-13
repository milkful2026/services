"""Shared composition root for handlers that wire the same dependency
graph (register_handler, delivery_slots_handler) — keeps the graph from
drifting between copies. outbox_publisher_handler wires a different graph
(no Inventory/Cognito collaborators) and isn't a fit for this helper.
"""

from sqlalchemy import create_engine

from adapters.cognito_attribute_adapter import CognitoAttributeAdapter
from adapters.inventory_client_adapter import HttpInventoryClient
from adapters.user_repository import SqlAlchemyUserRepository
from config.env import Settings
from domain.registration_service import RegistrationService


def build_registration_service(settings: Settings) -> RegistrationService:
    engine = create_engine(settings.database_url)
    repository = SqlAlchemyUserRepository(engine)
    inventory_client = HttpInventoryClient(
        settings.inventory_internal_base_url, settings.inventory_request_timeout_seconds
    )
    cognito_attributes = CognitoAttributeAdapter(
        settings.cognito_user_pool_id, settings.aws_region, endpoint_url=settings.aws_endpoint_url
    )
    return RegistrationService(repository, inventory_client, cognito_attributes)
