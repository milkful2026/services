"""Process-wide liveness singleton for this service — the class itself
lives in shared.handlers.health (services/README.md §2: `shared/` holds
cross-cutting libs with no domain rules); this instance is this
process's own, not shared state."""

from shared.handlers.health import ConsumerHealth

consumer_health = ConsumerHealth()
