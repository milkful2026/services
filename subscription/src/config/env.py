"""Environment configuration, validated at import time (cold start).

Per services/README.md §3: config is read at the composition root only —
never deep inside domain modules — and every value comes from env vars /
Secrets Manager, never hardcoded. Local dev loads
services/local-dev/subscription/.env.local into os.environ at the
process entrypoint (see src/main.py).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUBSCRIPTION_")

    database_url: str  # postgresql+psycopg2://... in prod, sqlite:// in tests
    aws_region: str = "ap-south-1"

    event_bus_name: str = "default"
    # Bare service-name source, matching this codebase's established
    # convention (user/cart/catalog/wallet publish their own bare name,
    # not a dotted "milkful.*" namespace).
    event_source: str = "subscription"

    catalog_base_url: str = "http://localhost:8003"

    # Local IST cut-off hour for same-day emission (create) and for
    # skip/edit's "before/after cut-off" rule (MA-131 §4 FR-1/FR-5/FR-6).
    cutoff_hour_ist: int = 20


def get_settings() -> Settings:
    """Instantiated lazily so tests can inject env vars before first access."""
    return Settings()
