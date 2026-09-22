"""FastAPI dependency wiring — the composition root. `lru_cache` gives a
per-process singleton; tests override via `app.dependency_overrides`."""

from functools import lru_cache

from sqlalchemy import create_engine

from adapters.order_repository import SqlAlchemyOrderRepository
from adapters.pricing_client_adapter import HttpPricingClient
from adapters.user_client_adapter import HttpUserClient
from adapters.wallet_client_adapter import HttpWalletClient
from config.env import get_settings
from domain.order_service import OrderService


@lru_cache
def get_order_service() -> OrderService:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    repository = SqlAlchemyOrderRepository(engine)
    user_client = HttpUserClient(settings.user_internal_base_url, settings.aws_region)
    pricing_client = HttpPricingClient(settings.pricing_base_url)
    wallet_client = HttpWalletClient(settings.wallet_internal_base_url)
    return OrderService(repository, user_client, pricing_client, wallet_client)
