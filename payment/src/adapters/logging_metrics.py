"""Log-based metrics recorder. `metric` is a stable field name a
CloudWatch Logs metric filter matches on (MA-126 §5's named metrics: the
filter pattern is `{ $.metric = "recharge.created" }` etc., one per
name)."""

import logging

logger = logging.getLogger("payment.metrics")


class LoggingMetricsRecorder:
    def emit(self, name: str, **dimensions: object) -> None:
        logger.info(name, extra={"metric": name, **dimensions})
