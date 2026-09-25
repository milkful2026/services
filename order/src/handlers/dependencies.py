"""FastAPI dependency wiring — the composition root. `lru_cache` gives a
per-process singleton; tests override via `app.dependency_overrides`."""

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from adapters.cart_client_adapter import HttpCartClient
from adapters.order_repository import SqlAlchemyOrderRepository
from adapters.pricing_client_adapter import HttpPricingClient
from adapters.subscription_client_adapter import HttpSubscriptionClient
from adapters.user_client_adapter import HttpUserClient
from adapters.wallet_client_adapter import HttpWalletClient
from config.env import get_settings
from domain.checkout_service import CheckoutService
from domain.order_service import OrderService


@lru_cache
def _engine() -> Engine:
    # One engine (and connection pool) per process, shared by both
    # services below.
    return create_engine(get_settings().database_url)


@lru_cache
def get_order_service() -> OrderService:
    settings = get_settings()
    repository = SqlAlchemyOrderRepository(_engine())
    user_client = HttpUserClient(settings.user_internal_base_url, settings.aws_region)
    pricing_client = HttpPricingClient(settings.pricing_base_url)
    wallet_client = HttpWalletClient(settings.wallet_internal_base_url)
    return OrderService(repository, user_client, pricing_client, wallet_client)


@lru_cache
def get_checkout_service() -> CheckoutService:
    settings = get_settings()
    return CheckoutService(
        SqlAlchemyOrderRepository(_engine()),
        HttpCartClient(settings.cart_internal_base_url, settings.aws_region),
        HttpUserClient(settings.user_internal_base_url, settings.aws_region),
        HttpPricingClient(settings.pricing_base_url),
        HttpWalletClient(settings.wallet_internal_base_url),
        HttpSubscriptionClient(settings.subscription_internal_base_url),
        cutoff_hour_ist=settings.checkout_cutoff_hour_ist,
        subscription_min_balance_paise=settings.subscription_min_balance_paise,
    )
