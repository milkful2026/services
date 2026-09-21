"""Environment configuration, validated at import time (cold start).

Per services/README.md §3: config is read at the composition root only —
never deep inside domain modules — and every value comes from env vars /
Secrets Manager, never hardcoded. This class is identical in every
environment; local dev loads services/local-dev/wallet/.env.local into
os.environ at the process entrypoint (see src/main.py).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WALLET_")

    database_url: str  # postgresql+psycopg2://... in prod, sqlite:// in tests
    aws_region: str = "ap-south-1"

    event_bus_name: str = "default"
    # Bare service-name source, matching this codebase's established
    # convention (user/cart/catalog publish "user"/"cart"/"catalog", not a
    # dotted "milkful.*" namespace).
    event_source: str = "wallet"

    # SQS queue this service consumes (UserRegistered + recharge PaymentConfirmed).
    events_queue_url: str = ""

    # Recharge limits — Wallet Service owns these numbers; Payment Service
    # (MA-126) reads them via GET /wallet/internal/limits and enforces the
    # range before creating a Razorpay order.
    recharge_min_paise: int = 10_000       # ₹100
    recharge_max_paise: int = 10_000_000   # ₹1,00,000

    # MA-130 §7 — below this post-debit (or post-refusal) balance,
    # debit_for_order enqueues WalletLowBalance for Notification/Reporting.
    # Env var: WALLET_LOW_BALANCE_THRESHOLD_PAISE (prefix + field name).
    low_balance_threshold_paise: int = 10_000   # ₹100


def get_settings() -> Settings:
    """Instantiated lazily so tests can inject env vars before first access."""
    return Settings()
