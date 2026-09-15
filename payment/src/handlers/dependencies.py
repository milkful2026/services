"""FastAPI dependency wiring — the composition root. `lru_cache` gives a
per-process singleton; tests override via `app.dependency_overrides`."""

import uuid
from functools import lru_cache

from fastapi import Header
from sqlalchemy import create_engine

from adapters.logging_metrics import LoggingMetricsRecorder
from adapters.payment_repository import SqlAlchemyPaymentRepository
from adapters.razorpay_gateway import RazorpayGateway
from adapters.wallet_limits_client import HttpWalletLimitsClient
from config.env import get_settings
from domain.payment_service import PaymentService


@lru_cache
def get_payment_service() -> PaymentService:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    repository = SqlAlchemyPaymentRepository(engine)
    gateway = RazorpayGateway(
        key_id=settings.razorpay_key_id,
        key_secret=settings.razorpay_key_secret,
        webhook_secret=settings.razorpay_webhook_secret,
    )
    wallet_limits = HttpWalletLimitsClient(
        base_url=settings.wallet_internal_base_url,
        fallback_min_paise=settings.wallet_recharge_min_paise_fallback,
        fallback_max_paise=settings.wallet_recharge_max_paise_fallback,
    )
    metrics = LoggingMetricsRecorder()
    return PaymentService(repository, gateway, wallet_limits, metrics, settings)


def correlation_id(x_correlation_id: str | None = Header(default=None)) -> str:
    return x_correlation_id or str(uuid.uuid4())
