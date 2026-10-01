"""Process-wide liveness singleton for this service — the class itself
lives in shared.handlers.health (services/README.md §2: `shared/` holds
cross-cutting libs with no domain rules); this instance is this
process's own, not shared state. Added by MA-140 — before this, the
service had no background thread of its own to reflect here (see
main.py's own updated module docstring)."""

from shared.handlers.health import ConsumerHealth

consumer_health = ConsumerHealth()
