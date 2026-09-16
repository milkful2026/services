"""Process-wide liveness, shared between a service's own background
thread (a reconciliation sweep, an SQS consumer, ...) and its /healthz
endpoint served in the same process — mirrors catalog/inventory's
ConsumerHealth convention.

Was hand-duplicated in payment/wallet's own `handlers/health.py` — moved
here per services/README.md §2 (`shared/` holds cross-cutting libs with
no domain rules). Each service constructs its own `ConsumerHealth()`
instance — this is not shared *state*, just shared *code*.
"""


class ConsumerHealth:
    def __init__(self) -> None:
        self.alive = True
