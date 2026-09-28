"""FastAPI dependency wiring — the composition root. `lru_cache` gives a
per-process singleton; tests override via `app.dependency_overrides`."""

import os
import socket
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from adapters.cart_client_adapter import HttpCartClient
from adapters.logging_metrics import LoggingMetricsRecorder
from adapters.order_repository import SqlAlchemyOrderRepository
from adapters.pricing_client_adapter import HttpPricingClient
from adapters.subscription_client_adapter import HttpSubscriptionClient
from adapters.user_client_adapter import HttpUserClient
from adapters.wallet_client_adapter import HttpWalletClient
from config.env import get_settings
from domain.checkout_service import CheckoutService
from domain.order_service import OrderService
from domain.sweep_service import SweepService


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
    return OrderService(
        repository,
        user_client,
        pricing_client,
        wallet_client,
        lease_seconds=settings.sweep_lease_seconds,
    )


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
        lease_seconds=settings.sweep_lease_seconds,
    )


@lru_cache
def get_sweep_service() -> SweepService:
    settings = get_settings()
    return SweepService(
        SqlAlchemyOrderRepository(_engine()),
        get_order_service(),
        LoggingMetricsRecorder(),
        # Unique per task, so a lease names the process that holds it.
        owner=f"sweep:{socket.gethostname()}:{os.getpid()}",
        cutoff_hour_ist=settings.checkout_cutoff_hour_ist,
        subscription_order_stale_seconds=settings.subscription_order_stale_seconds,
        max_attempts=settings.sweep_max_attempts,
        lease_seconds=settings.sweep_lease_seconds,
        batch_size=settings.sweep_batch_size,
        checkout_service=get_checkout_service(),
        checkout_stale_seconds=settings.checkout_stale_seconds,
    )
