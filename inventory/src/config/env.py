"""Environment configuration, validated at import time (cold start)."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Optional — only present in local dev, written by
# services/local-dev/bootstrap.py. Silently ignored by pydantic-settings
# when absent, so this has no effect on tests or real deployments.
_LOCAL_ENV_FILE = Path(__file__).resolve().parents[2] / ".env.local"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INVENTORY_", env_file=_LOCAL_ENV_FILE, env_file_encoding="utf-8"
    )

    database_url: str  # postgresql+psycopg2://... in prod, sqlite:// in tests
    aws_region: str = "ap-south-1"
    # Local dev only: points boto3 at moto_server instead of real AWS.
    # None in every deployed environment, so behavior there is unchanged.
    aws_endpoint_url: str | None = None

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
