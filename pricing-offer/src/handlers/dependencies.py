"""FastAPI dependency wiring — the composition root. `lru_cache` gives a
per-process singleton, while still letting tests cleanly override via
`app.dependency_overrides` — matches catalog's own convention exactly."""

from functools import lru_cache

from adapters.catalog_client import HttpCatalogClient
from config.env import get_settings
from domain.pricing_service import PricingService


@lru_cache
def get_pricing_service() -> PricingService:
    settings = get_settings()
    catalog_client = HttpCatalogClient(
        base_url=settings.catalog_base_url,
        timeout_seconds=settings.catalog_timeout_seconds,
    )
    return PricingService(
        catalog_client=catalog_client,
        tax_rate_percent=settings.default_tax_rate_percent,
        delivery_fee=settings.delivery_fee,
    )
