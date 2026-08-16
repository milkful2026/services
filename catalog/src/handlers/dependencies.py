"""FastAPI dependency wiring — the composition root. `lru_cache` gives a
per-process singleton (reused across requests in the same Fargate task),
while still letting tests cleanly override via `app.dependency_overrides`
without needing to reset any global state."""

from functools import lru_cache

from sqlalchemy import create_engine

from adapters.product_repository import SqlAlchemyProductRepository
from config.env import get_settings
from domain.catalog_service import CatalogService


@lru_cache
def get_catalog_service() -> CatalogService:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    repository = SqlAlchemyProductRepository(engine)
    return CatalogService(repository)
