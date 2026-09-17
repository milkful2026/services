"""Shared composition root for the admin-user-management handlers
(create/list/update/deactivate/reactivate) — all five wire the exact
same dependency graph, so it's built once here rather than copy-pasted
five times (same reasoning as services/user's own composition.py)."""

from sqlalchemy import create_engine

from adapters.admin_cognito_adapter import AdminCognitoAdapter
from adapters.admin_session_registry import AdminSessionRegistryAdapter
from adapters.admin_user_repository import SqlAlchemyAdminUserRepository
from adapters.notification_publisher import EventBridgeNotificationPublisher
from adapters.rate_limit_adapter import build_redis_client
from config.env import Settings
from domain.admin_users.user_service import AdminUserService


def build_admin_user_service(settings: Settings) -> AdminUserService:
    engine = create_engine(settings.admin_database_url)
    admin_repo = SqlAlchemyAdminUserRepository(engine)
    cognito = AdminCognitoAdapter(
        settings.admin_cognito_user_pool_id, settings.admin_cognito_client_id, settings.aws_region
    )
    redis_client = build_redis_client(settings.redis_host, settings.redis_port, settings.redis_use_tls)
    session_registry = AdminSessionRegistryAdapter(redis_client)
    publisher = EventBridgeNotificationPublisher(
        settings.event_bus_name, settings.event_source, settings.aws_region
    )
    return AdminUserService(admin_repo, cognito, session_registry, publisher)
