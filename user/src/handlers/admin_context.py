"""Shared helper for reading the caller's admin identity out of the API
Gateway authorizer context — used by every `/v1/admin/customers*`
handler (MA-139).

Per spec §6 point 2, these routes sit "behind the existing admin JWT
authorizer path (MA-129's authorizer, already role-aware — extended, not
duplicated)": in a real deployment, User Service's HTTP API references
Identity & Auth's own `admin_authorizer_handler.py` Lambda as a
cross-stack Lambda REQUEST authorizer (see infra/user/user_stack.py's
`admin_authorizer_fn_arn` parameter) rather than this service owning a
second copy of that authorizer's Cognito/Aurora/IP-allowlist logic. What
IS duplicated here is only the *contract* — the exact
`{adminId, email, role}` shape identity-auth's
`admin_authorizer_handler._allow()` already populates into
`requestContext.authorizer.lambda` — mirroring
identity-auth/src/handlers/admin_context.py's own `get_caller_admin`
byte-for-byte so a handler here can trust the same shape a handler there
does, without importing identity-auth's code (a different service's
`src/` is never imported cross-service in this codebase — see
run_local.py's own docstring for the local-dev equivalent of this same
split).
"""

from domain.exceptions import UserServiceError


class AdminAuthenticationError(UserServiceError):
    """Missing/malformed caller identity where one is required (e.g. no
    authorizer context reached the handler) — mirrors identity-auth's
    own AdminAuthenticationError error code/status exactly, since both
    are the same failure mode against the same authorizer contract."""

    error_code = "UNAUTHENTICATED"
    http_status = 401


def get_caller_admin(event: dict) -> dict:
    """Returns {"adminId", "email", "role"} as set by MA-129's admin
    authorizer. Raising here (rather than returning None) means a
    handler was reached with no valid authorizer context — should never
    happen in production traffic, but must fail loudly rather than
    silently trusting a client-supplied header — surfaces as a 401, not
    a 500."""
    authorizer = ((event.get("requestContext") or {}).get("authorizer") or {}).get("lambda") or {}
    admin_id = authorizer.get("adminId")
    role = authorizer.get("role")
    email = authorizer.get("email")
    if not admin_id or not role:
        raise AdminAuthenticationError("Missing admin identity in request context")
    return {"adminId": admin_id, "email": email, "role": role}
