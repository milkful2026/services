"""Process-wide liveness singleton for this service — the class itself
lives in shared.handlers.health (services/README.md §2: `shared/` holds
cross-cutting libs with no domain rules); this instance is this
process's own, not shared state. Reflects the order-events consumer
thread's liveness for the ALB health check, same pattern wallet's own
`/healthz` uses for its consumer thread."""

from shared.handlers.health import ConsumerHealth

consumer_health = ConsumerHealth()
# MA-143: the reconciliation sweep thread's liveness, reported the same way.
sweep_health = ConsumerHealth()
