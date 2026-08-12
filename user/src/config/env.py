"""Environment configuration, validated at import time (cold start)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="USER_")

    database_url: str  # postgresql+psycopg2://... in prod, sqlite:// in tests
    aws_region: str = "ap-south-1"

    cognito_user_pool_id: str

    inventory_internal_base_url: str  # e.g. http://internal-alb.inventory.local
    inventory_request_timeout_seconds: float = 5.0

    event_bus_name: str = "default"
    event_source: str = "user"

    outbox_batch_size: int = 25


def get_settings() -> Settings:
    """Instantiated lazily so tests can inject env vars before first access."""
    return Settings()
