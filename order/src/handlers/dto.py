"""Response envelope helpers. Fixed `{requestId, status, data}` shape per
services/README.md §5 — the shape the mobile app's shared ApiClient
unwraps for every service. Request bodies are declared next to their
routes (checkout_handlers, order_handlers)."""

from shared.handlers.dto import error_envelope, success_envelope  # noqa: F401
