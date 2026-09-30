"""Log-based metrics recorder, same shape as Payment's. `metric` is a stable
field a CloudWatch Logs metric filter matches on (e.g.
`{ $.metric = "sweep.checkout.escalated" }`), one filter per name."""

import logging

logger = logging.getLogger("order.metrics")


class LoggingMetricsRecorder:
    def emit(self, name: str, **dimensions: object) -> None:
        logger.info(name, extra={"metric": name, **dimensions})
