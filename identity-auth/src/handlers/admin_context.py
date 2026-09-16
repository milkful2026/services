"""Shared helper for reading the caller's admin identity out of the
API Gateway authorizer context (services/README.md §5b: "Claims attached
to request context — not passed ad hoc through every function
signature"). Used by every `/v1/admin/*` handler that sits behind
admin_authorizer_handler.py's Lambda authorizer.
"""

from domain.admin_exceptions import AdminAuthenticationError


def get_caller_admin(event: dict) -> dict:
    """Returns {"adminId", "email", "role"} as set by
    admin_authorizer_handler._allow(). Raising here (rather than
    returning None) means a handler reached without a valid authorizer
    context — which should never happen in production traffic, but must
    fail loudly rather than silently trusting a client-supplied header —
    surfaces as a 401, not a 500."""
    authorizer = ((event.get("requestContext") or {}).get("authorizer") or {}).get("lambda") or {}
    admin_id = authorizer.get("adminId")
    role = authorizer.get("role")
    email = authorizer.get("email")
    if not admin_id or not role:
        raise AdminAuthenticationError("Missing admin identity in request context")
    return {"adminId": admin_id, "email": email, "role": role}
