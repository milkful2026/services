"""Environment configuration, validated at import time (cold start)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Local dev populates real env vars (including the standard
    # AWS_ENDPOINT_URL botocore already reads natively) by loading
    # services/local-dev/catalog/.env.local at the process entrypoint —
    # see src/main.py — not via any local-dev-specific wiring here. This
    # class is identical in every environment.
    model_config = SettingsConfigDict(env_prefix="CATALOG_")

    database_url: str  # postgresql+psycopg2://... in prod, sqlite:// in tests
    aws_region: str = "ap-south-1"

    stock_changed_queue_url: str = ""

    # Note: local-dev CORS support (CATALOG_CORS_ALLOW_ALL) is read
    # directly from os.environ in handlers/app.py, not through this
    # class — matches inventory/src/handlers/app.py's own documented
    # reasoning (Settings' eager validation of every required field at
    # construction time is unsafe to trigger at module-import time).


def get_settings() -> Settings:
    """Instantiated lazily so tests can inject env vars before first access."""
    return Settings()
