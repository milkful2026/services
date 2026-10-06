"""FastAPI `Depends()` equivalent of user/src/handlers/admin_context.py's
`get_caller_admin` — same `{adminId, email, role}` contract, a different
transport.

User Service's admin routes are Lambda + HTTP API: the Lambda authorizer
context arrives as `event.requestContext.authorizer.lambda`, directly
inside the handler's own event dict. Inventory's admin routes are FastAPI
behind an internal ALB, reached via an API Gateway VPC Link (HttpAlbIntegration
— see infra/inventory/inventory_stack.py), so there is no Lambda `event`
for this handler to read at all. HTTP API's `HttpLambdaAuthorizer`
context is only visible by the time it reaches an ALB-integration target
through API Gateway's own *parameter mapping*: the CDK stack maps
`$context.authorizer.lambda.adminId`/`.email`/`.role` into
`X-Admin-Id`/`X-Admin-Email`/`X-Admin-Role` request headers on the way
out (see inventory_stack.py's `_build_admin_http_api`) — this module
reads those three headers back, not an event dict.

This is this story's own small addition to the "placeholder cross-stack
ARN" gap MA-139/MA-150 §11 point 3 both already flag: header-based
authorizer-context forwarding across an HttpApi -> VPC Link -> ALB hop is
a real, working API Gateway feature (not a placeholder), but it is new
to this codebase — every other admin route so far has been Lambda-proxy,
where the authorizer context arrives for free in the event. Flagged here,
not silently assumed, since a header name typo on either side (CDK's
parameter mapping vs. this file) would silently degrade to "every admin
call 401s" rather than failing loudly at deploy time.
"""

from __future__ import annotations

from fastapi import Request

from domain.exceptions import AdminAuthenticationError, AdminForbiddenError

ADMIN_ID_HEADER = "x-admin-id"
ADMIN_EMAIL_HEADER = "x-admin-email"
ADMIN_ROLE_HEADER = "x-admin-role"

# Ops is who normally operates Inventory day-to-day; SuperAdmin is let
# through too (confirmed as a real business requirement, not a
# convenience default) so a SuperAdmin never gets locked out of an
# operational screen they should be able to reach.
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
    """MA-119 FR-3 / MA-150 §5: all five admin routes are gated to Ops
    (D4) and SuperAdmin — any other role is authenticated but still
    403s, not silently allowed through."""
    admin = get_caller_admin(request)
    if admin["role"] not in ALLOWED_ROLES:
        raise AdminForbiddenError(f"role {admin['role']!r} is not permitted to call this route")
    return admin
