"""Metrics port. No service in this codebase has a CloudWatch client
today — every service relies on structured logging (a metric filter
turns a matching log line into a CloudWatch metric). This port follows
that established convention rather than introducing a new dependency;
`adapters/logging_metrics.py` is the only implementation, and tests
inject a recording fake.
"""

from typing import Protocol


class MetricsPort(Protocol):
    def emit(self, name: str, **dimensions: object) -> None:
        """Record one occurrence of `name` (a CloudWatch metric filter
        target), with `dimensions` as structured log fields."""
        ...
