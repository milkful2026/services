"""Environment configuration, validated at import time (cold start).

Per services/README.md §3: config is read at the composition root only —
never deep inside domain modules — and every value comes from env vars /
Secrets Manager, never hardcoded. Local dev loads
services/local-dev/order/.env.local into os.environ at the process
entrypoint (see src/main.py).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORDER_")

    database_url: str  # postgresql+psycopg2://... in prod, sqlite:// in tests
    aws_region: str = "ap-south-1"

    event_bus_name: str = "default"
    # Bare service-name source, matching this codebase's established
    # convention (user/cart/catalog/wallet/subscription publish their own
    # bare name, not a dotted "milkful.*" namespace).
    event_source: str = "order"

    # SQS queue this service consumes (SubscriptionOrderDue) — owned by
    # this service's own infra/ CDK stack, not Subscription's.
    events_queue_url: str = ""

    user_internal_base_url: str = "http://localhost:8002"
    pricing_base_url: str = "http://localhost:8005"
    wallet_internal_base_url: str = "http://localhost:8006"


def get_settings() -> Settings:
    """Instantiated lazily so tests can inject env vars before first access."""
    return Settings()
