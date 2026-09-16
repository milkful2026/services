"""Environment configuration, validated at import time (cold start).

Per services/README.md SS3: config is read at the composition root only,
every value from env vars / Secrets Manager. Local dev loads
services/local-dev/payment/.env.local into os.environ at the process
entrypoint (see src/main.py).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAYMENT_")

    database_url: str  # postgresql+psycopg2://... in prod, sqlite:// in tests
    aws_region: str = "ap-south-1"

    event_bus_name: str = "default"
    # Bare service-name source, matching this codebase's established
    # convention (user/cart/catalog publish "user"/"cart"/"catalog", not a
    # dotted "milkful.*" namespace).
    event_source: str = "payment"

    # Razorpay — key_secret/webhook_secret are Secrets Manager values in
    # prod; a local .env.local carries rzp_test_* credentials.
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # Wallet Service — the recharge-limits source of truth (MA-127 FR-3).
    wallet_internal_base_url: str = ""
    wallet_recharge_min_paise_fallback: int = 10_000       # used only if the limits call fails
    wallet_recharge_max_paise_fallback: int = 10_000_000

    # Reconciliation sweep (MA-126 FR-5, round-2)
    reconcile_interval_seconds: float = 120.0
    reconcile_stale_seconds: float = 180.0
    reconcile_confirming_alert_seconds: float = 1800.0     # 30 min
    reconcile_hard_cap_seconds: float = 21600.0            # 6 h


def get_settings() -> Settings:
    """Instantiated lazily so tests can inject env vars before first access."""
    return Settings()
