"""Reconciliation sweep loop (MA-126 FR-5) — an in-service scheduled
task (EventBridge Scheduler invoking this, or an internal thread; see
main.py). Resolves CONFIRMING (or CREATED-with-an-order) payments that
never got a webhook, by polling Razorpay directly.
"""

import logging
import time

from sqlalchemy import create_engine

from adapters.logging_metrics import LoggingMetricsRecorder
from adapters.payment_repository import SqlAlchemyPaymentRepository
from adapters.razorpay_gateway import RazorpayGateway
from adapters.wallet_limits_client import HttpWalletLimitsClient
from config.env import get_settings
from domain.payment_service import PaymentService

logger = logging.getLogger(__name__)


def _build_service() -> PaymentService:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    repo = SqlAlchemyPaymentRepository(engine)
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
    return PaymentService(repo, gateway, wallet_limits, LoggingMetricsRecorder(), settings)


def run_once() -> list[str]:
    return _build_service().reconcile_once()


def run_forever() -> None:
    settings = get_settings()
    while True:
        try:
            outcomes = run_once()
            if outcomes:
                logger.info("reconcile: processed %d stale payment(s)", len(outcomes))
        except Exception:  # noqa: BLE001 — keep the loop alive; retry next tick
            logger.exception("reconcile tick failed")
        time.sleep(settings.reconcile_interval_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_forever()
