"""Reservation TTL sweep loop (MA-118 FR-2) — a periodic polling job, not
a per-reservation timer, mirroring (per the spec's own words) "this
platform's existing preference for simple polling over per-item
scheduled infrastructure" — same run_forever()/sleep(interval) shape as
payment/src/handlers/reconcile.py (itself an in-process background
thread, not a scheduled Lambda, since Inventory like Payment is Fargate/
FastAPI — see main.py).
"""

import logging
import time

from sqlalchemy import create_engine

from adapters.stock_event_publisher import EventBridgeStockEventPublisher
from adapters.stock_repository import SqlAlchemyStockRepository
from config.env import get_settings
from domain.inventory_stock_service import InventoryStockService

logger = logging.getLogger(__name__)


def _build_service() -> InventoryStockService:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    repository = SqlAlchemyStockRepository(engine)
    publisher = EventBridgeStockEventPublisher(
        settings.event_bus_name, settings.event_source, settings.aws_region
    )
    return InventoryStockService(repository, publisher, settings.reservation_ttl_seconds)


def run_once(service: InventoryStockService | None = None) -> int:
    service = service or _build_service()
    released = service.run_ttl_sweep()
    return len(released)


def run_forever() -> None:
    settings = get_settings()
    service = _build_service()
    while True:
        try:
            count = run_once(service)
            if count:
                logger.info("ttl_sweep: released %d expired reservation(s)", count)
        except Exception:  # noqa: BLE001 — keep the loop alive; retry next tick
            logger.exception("ttl_sweep tick failed")
        time.sleep(settings.ttl_sweep_interval_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_forever()
