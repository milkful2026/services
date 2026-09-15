"""Nightly balance-invariant sweep (MA-127 §5/§7/§11/§12) — an in-service
scheduled task, wired the same way as services/payment's reconcile task
(EventBridge Scheduler invoking this, or an internal loop thread; see
run_local.py-equivalent wiring in services/local-dev for the local cadence).

Never a correctness gate for a request — purely observability: any
mismatch between a wallet's stored balance and its own ledger sum is
logged (metric `wallet.balance_invariant_violations`) for investigation.
"""

import logging
import time

from sqlalchemy import create_engine

from adapters.wallet_repository import SqlAlchemyWalletRepository
from config.env import get_settings
from domain.wallet_service import WalletService

logger = logging.getLogger(__name__)


def run_once() -> list[str]:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    service = WalletService(SqlAlchemyWalletRepository(engine), settings)
    offending = service.check_balance_invariant()
    if offending:
        logger.error(
            "invariant_check: %d wallet(s) failed the balance invariant",
            len(offending),
            extra={"walletIds": offending},
        )
    else:
        logger.info("invariant_check: all wallets consistent")
    return offending


def run_forever(interval_seconds: float = 24 * 60 * 60) -> None:
    while True:
        try:
            run_once()
        except Exception:  # noqa: BLE001 — keep the loop alive; retry next tick
            logger.exception("invariant_check tick failed")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_forever()
