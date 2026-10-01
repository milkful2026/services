"""Shared composition root for the admin customer-status handlers
(list/detail/suspend/deactivate/reactivate/bulk) — all six wire the same
dependency graph, so it's built once here rather than copy-pasted six
times (same reasoning as services/user's own handlers/composition.py and
identity-auth's handlers/admin_users/composition.py)."""

from sqlalchemy import create_engine

from adapters.cognito_attribute_adapter import CognitoAttributeAdapter
from adapters.user_repository import SqlAlchemyUserRepository
from config.env import Settings
from domain.customer_status_service import CustomerStatusService


def build_customer_status_service(settings: Settings) -> CustomerStatusService:
    engine = create_engine(settings.database_url)
    repository = SqlAlchemyUserRepository(engine)
    cognito_attributes = CognitoAttributeAdapter(settings.cognito_user_pool_id, settings.aws_region)
    return CustomerStatusService(repository, cognito_attributes)
