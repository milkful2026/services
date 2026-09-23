"""Response envelope helpers. Fixed `{requestId, status, data}` shape per
services/README.md §5 — the shape the mobile app's shared ApiClient
unwraps for every service. No request DTOs — this service has no public
write endpoints (FR-3 is read-only; every order is consumer-created)."""

from shared.handlers.dto import error_envelope, success_envelope  # noqa: F401
