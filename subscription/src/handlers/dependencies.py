"""FastAPI dependency wiring — the composition root. `lru_cache` gives a
per-process singleton; tests override via `app.dependency_overrides`."""

import uuid
from functools import lru_cache

from fastapi import Header
from sqlalchemy import create_engine

from adapters.catalog_client_adapter import HttpCatalogClient
from adapters.subscription_repository import SqlAlchemySubscriptionRepository
from config.env import get_settings
from domain.subscription_service import SubscriptionService


@lru_cache
def get_subscription_service() -> SubscriptionService:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    repository = SqlAlchemySubscriptionRepository(engine)
    catalog_client = HttpCatalogClient(settings.catalog_base_url)
    return SubscriptionService(repository, catalog_client, settings)


def correlation_id(x_correlation_id: str | None = Header(default=None)) -> str:
    return x_correlation_id or str(uuid.uuid4())
