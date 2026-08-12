"""Environment configuration, validated at import time (cold start)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INVENTORY_")

    database_url: str  # postgresql+psycopg2://... in prod, sqlite:// in tests
    aws_region: str = "ap-south-1"

    redis_host: str
    redis_port: int = 6379
    # The CDK stack provisions a plain AWS::ElastiCache::CacheCluster, which
    # has no in-transit encryption support at all (only
    # AWS::ElastiCache::ReplicationGroup does) — this must stay False until
    # the infra is upgraded to a TLS-capable cluster type.
    redis_use_tls: bool = False
    cache_ttl_seconds: int = 900  # 15 min, per spec §6

    zone_updated_queue_url: str = ""


def get_settings() -> Settings:
    """Instantiated lazily so tests can inject env vars before first access."""
    return Settings()
