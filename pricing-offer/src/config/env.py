"""Environment configuration, validated at import time (cold start).

Deliberately smaller than every other service's Settings — this build has
no database and no AWS dependency at all (see README's "Scope" section),
so there's nothing here to source from a bootstrap-generated `.env.local`
the way catalog/user/inventory do. Every field has a real default, so a
bare `python src/main.py` with no environment configured at all still
runs correctly against a locally-running Catalog Service.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRICING_")

    catalog_base_url: str = "http://localhost:8003"
    catalog_timeout_seconds: float = 5.0

    # Deliberate placeholder — no per-product HSN/GST data exists in this
    # build (see README's "Scope" section); every line item is taxed at
    # this single flat rate rather than a real per-product rate resolved
    # from Catalog. Confirm a real value with the business/finance owner
    # before this is ever treated as more than a local-dev placeholder —
    # same caveat MA-122 §11 raised for PRICING_SELLER_STATE (unused here,
    # see README).
    default_tax_rate_percent: float = 5.0

    # Flat per-order delivery fee — MA-122 doesn't specify a real fee
    # schedule (distance/weight-based, free-above-threshold, etc.); this
    # is a placeholder of the same kind as the tax rate above.
    delivery_fee: float = 20.0


def get_settings() -> Settings:
    """Instantiated lazily so tests can inject env vars before first access."""
    return Settings()
