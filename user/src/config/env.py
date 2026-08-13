"""Environment configuration, validated at import time (cold start)."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Optional — only present in local dev, written by
# services/local-dev/bootstrap.py. Silently ignored by pydantic-settings
# when absent, so this has no effect on tests or real deployments.
_LOCAL_ENV_FILE = Path(__file__).resolve().parents[2] / ".env.local"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="USER_", env_file=_LOCAL_ENV_FILE, env_file_encoding="utf-8"
    )

    database_url: str  # postgresql+psycopg2://... in prod, sqlite:// in tests
    aws_region: str = "ap-south-1"
    # Local dev only: points boto3 at moto_server instead of real AWS.
    # None in every deployed environment, so behavior there is unchanged.
    aws_endpoint_url: str | None = None

    cognito_user_pool_id: str

    inventory_internal_base_url: str  # e.g. http://internal-alb.inventory.local
    inventory_request_timeout_seconds: float = 5.0

    event_bus_name: str = "default"
    event_source: str = "user"

    outbox_batch_size: int = 25


def get_settings() -> Settings:
    """Instantiated lazily so tests can inject env vars before first access."""
    return Settings()
