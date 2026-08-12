"""FastAPI dependency wiring — the composition root. `lru_cache` gives a
per-process singleton (reused across requests in the same Fargate task),
while still letting tests cleanly override via `app.dependency_overrides`
without needing to reset any global state."""

from functools import lru_cache

from sqlalchemy import create_engine

from adapters.zone_cache_adapter import RedisZoneCacheAdapter, build_redis_client
from adapters.zone_repository import SqlAlchemyZoneRepository
from config.env import get_settings
from domain.serviceability_service import ServiceabilityService


@lru_cache
def get_serviceability_service() -> ServiceabilityService:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    repository = SqlAlchemyZoneRepository(engine)
    cache = RedisZoneCacheAdapter(
        build_redis_client(settings.redis_host, settings.redis_port, settings.redis_use_tls)
    )
    return ServiceabilityService(repository, cache, settings.cache_ttl_seconds)
