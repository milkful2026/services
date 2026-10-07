"""MA-143 FR-1 — the reconciliation sweep loop, run as a daemon thread by
main.py (same shape as Payment's handlers/reconcile.py). One correlation
id per run; a failing run is logged and the loop carries on."""

import logging
import threading
import time
import uuid
from collections import Counter
from datetime import UTC, datetime

from adapters.logging_metrics import LoggingMetricsRecorder
from config.env import get_settings
from handlers.dependencies import get_sweep_service

logger = logging.getLogger(__name__)
_metrics = LoggingMetricsRecorder()


def run_once(service=None, now: datetime | None = None) -> Counter:
    service = service or get_sweep_service()
    correlation_id = str(uuid.uuid4())
    now = now or datetime.now(UTC)
    started = time.monotonic()
    counts = Counter()
    for flow, run_pass in (
        ("subscription_order", service.sweep_subscription_orders),
        ("checkout", service.sweep_checkouts),
        ("settle", service.settle_unknown_charges),  # FR-4b: after the other passes
        ("refund", service.finish_pending_refunds),  # MA-154 FR-5
    ):
        counts.update({f"{flow}.{k}": v for k, v in run_pass(correlation_id, now).items()})
    duration_ms = int((time.monotonic() - started) * 1000)
    _metrics.emit("sweep.run_duration_ms", value=duration_ms)
    if counts:
        logger.info(
            "sweep: run complete",
            extra={"correlationId": correlation_id, "counts": dict(counts)},
        )
    return counts


def run_forever(stop: threading.Event | None = None) -> None:
    settings = get_settings()
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            run_once()
        except Exception:  # noqa: BLE001 — keep the loop alive; retry next tick
            logger.exception("sweep: run failed")
            _metrics.emit("sweep.run_failed")
        stop.wait(settings.sweep_interval_seconds)
