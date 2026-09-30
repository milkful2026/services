"""Environment configuration, validated at import time (cold start).

Per services/README.md §3: config is read at the composition root only —
never deep inside domain modules — and every value comes from env vars /
Secrets Manager, never hardcoded. Local dev loads
services/local-dev/order/.env.local into os.environ at the process
entrypoint (see src/main.py).
"""

from pydantic import field_validator, model_validator
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

    # --- MA-136: cart checkout ---
    cart_internal_base_url: str = "http://localhost:8004"
    subscription_internal_base_url: str = "http://localhost:8008"
    # Must equal Subscription Service's own cutoff_hour_ist (8 PM IST,
    # confirmed by Product 2026-09-25 for one-time deliveries too).
    checkout_cutoff_hour_ist: int = 20
    # Must equal Cart's wallet_minimum_balance (₹500), in paise.
    subscription_min_balance_paise: int = 50_000

    # --- MA-143: reconciliation sweep (FR-7) ---
    sweep_enabled: bool = True
    sweep_interval_seconds: float = 300
    subscription_order_stale_seconds: float = 900
    # MA-143 D-5 (amended 2026-09-29): a subscription order is created by the
    # Daily Run at the 20:00 cut-off, so it can't also be its deadline. It
    # may still be charged until this hour IST on the day before delivery —
    # the latest a charged order still makes the dispatch list.
    subscription_charge_deadline_hour_ist: int = 23
    checkout_stale_seconds: float = 600  # MA-144
    sweep_max_attempts: int = 6
    # Must outlast one dependency call (≈12 s with retries); the holder
    # renews before each call, so this doesn't bound a whole record run.
    sweep_lease_seconds: float = 120
    sweep_batch_size: int = 50

    @field_validator("sweep_lease_seconds")
    @classmethod
    def _lease_at_least_a_minute(cls, value: float) -> float:
        if value < 60:
            raise ValueError("ORDER_SWEEP_LEASE_SECONDS must be at least 60")
        return value

    @model_validator(mode="after")
    def _deadline_after_cutoff(self) -> "Settings":
        if not self.checkout_cutoff_hour_ist < self.subscription_charge_deadline_hour_ist <= 23:
            raise ValueError(
                "ORDER_SUBSCRIPTION_CHARGE_DEADLINE_HOUR_IST must be later than the "
                f"{self.checkout_cutoff_hour_ist}:00 cut-off and at most 23"
            )
        return self


def get_settings() -> Settings:
    """Instantiated lazily so tests can inject env vars before first access."""
    return Settings()
