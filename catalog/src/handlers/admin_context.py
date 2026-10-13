"""FastAPI `Depends()` admin-authorizer context — near-direct port of
`services/inventory/src/handlers/admin_context.py` (MA-48), same
`{adminId, email, role}` contract and `X-Admin-Id`/`X-Admin-Email`/
`X-Admin-Role` header transport. See that module's own docstring for why
this service reads headers rather than a Lambda authorizer event dict:
Catalog, like Inventory, is FastAPI behind an internal ALB reached via an
API Gateway VPC Link, not Lambda-proxy.
"""

from __future__ import annotations

from fastapi import Request

from domain.exceptions import AdminAuthenticationError, AdminForbiddenError

ADMIN_ID_HEADER = "x-admin-id"
ADMIN_EMAIL_HEADER = "x-admin-email"
ADMIN_ROLE_HEADER = "x-admin-role"

# Same set confirmed for Inventory (MA-48/MA-150) — Ops is who normally
# operates Catalog day-to-day; SuperAdmin is let through too so a
# SuperAdmin never gets locked out of an operational screen they should
# be able to reach.
ALLOWED_ROLES = frozenset({"Ops", "SuperAdmin"})


def get_caller_admin(request: Request) -> dict:
    """Raises AdminAuthenticationError (401) if the expected headers are
    missing — should never happen for a request that actually passed the
    admin authorizer Lambda, but a handler reached without them must fail
    loudly, not silently trust an unauthenticated caller."""
    admin_id = request.headers.get(ADMIN_ID_HEADER)
    role = request.headers.get(ADMIN_ROLE_HEADER)
    if not admin_id or not role:
        raise AdminAuthenticationError("Missing admin identity in request context")
    return {
        "adminId": admin_id,
        "email": request.headers.get(ADMIN_EMAIL_HEADER),
        "role": role,
    }


def require_ops_role(request: Request) -> dict:
    """MA-156 FR-6: every admin route is gated to Ops and SuperAdmin — any
    other role is authenticated but still 403s, not silently allowed
    through."""
    admin = get_caller_admin(request)
    if admin["role"] not in ALLOWED_ROLES:
        raise AdminForbiddenError(f"role {admin['role']!r} is not permitted to call this route")
    return admin
