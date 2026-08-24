"""Environment configuration, validated at import time (cold start).

Per services/README.md §3: config is read at the composition root only —
never deep inside domain modules — and every value comes from env vars /
Secrets Manager, never hardcoded.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Local dev populates real env vars (including the standard
    # AWS_ENDPOINT_URL botocore already reads natively) by loading
    # services/local-dev/cart/.env.local at the process entrypoint — see
    # run_local.py — not via any local-dev-specific wiring here. This
    # class is identical in every environment.
    model_config = SettingsConfigDict(env_prefix="CART_")

    # DynamoDB
    cart_table_name: str = "cart"
    aws_region: str = "ap-south-1"

    # EventBridge
    event_bus_name: str = "default"
    event_source: str = "cart"

    # Cross-service HTTP clients — all internal, non-JWT endpoints
    # (services/README.md §3.7's adapter pattern)
    catalog_internal_base_url: str
    user_internal_base_url: str
    pricing_internal_base_url: str
    wallet_internal_base_url: str = ""  # unset: MA-100 doesn't exist yet, see README Known Gaps
    request_timeout_seconds: float = 3.0

    # Business rules
    cart_ttl_days: int = 30
    idempotency_key_ttl_hours: int = 24
    wallet_minimum_balance: int = 500


def get_settings() -> Settings:
    """Instantiated lazily so tests can inject env vars before first access."""
    return Settings()
