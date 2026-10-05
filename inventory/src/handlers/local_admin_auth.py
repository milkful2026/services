"""Local-dev-only ASGI middleware that forwards the browser's own
Authorization Bearer token into the X-Admin-Id/-Email/-Role request
headers admin_context.py reads (see that module's own docstring for why
it reads headers, not an authorizer context dict: Inventory is FastAPI
behind an ALB/VPC-Link, not Lambda).

In a real deployment, API Gateway's Lambda REQUEST authorizer verifies
the caller's Cognito token, resolves their role, and API Gateway's own
parameter mapping copies the authorizer's output into these three
headers before the request ever reaches this service — this service
never sees or decodes a token itself. Locally, nothing stands in for
that authorizer + parameter-mapping hop for this service (unlike the
Lambda-shaped services, where _lambda_local_server.py's `(handler,
authorizer)` tuple route convention already covers this — see its own
docstring), so without this, every admin_inventory_handler.py route
401s unconditionally for a real browser session: the frontend sends
`Authorization: Bearer <accessToken>`, not X-Admin-* headers, and
get_caller_admin() only reads the latter.

Same trust model _lambda_local_server.py already documents: the JWT
payload is decoded as-is, signature unchecked (moto's own Cognito tokens
aren't properly signed either). Implemented as a raw base64url+json
decode rather than taking a PyJWT dependency, since this runs inside the
same container image real traffic does (Dockerfile installs
requirements.txt only, not requirements-dev.txt) and no other prod code
path in this service needs a JWT library.

Gated behind INVENTORY_LOCAL_ADMIN_AUTH (bootstrap.py-written, local-dev
only — see handlers/app.py's INVENTORY_CORS_ALLOW_ALL for the identical
pattern) so this never runs against real traffic, where the real headers
already arrive from API Gateway and must not be overwritten by a
caller-supplied Authorization header instead.
"""

from __future__ import annotations

import base64
import binascii
import json

from starlette.types import ASGIApp, Receive, Scope, Send

ADMIN_ID_HEADER = b"x-admin-id"
ADMIN_EMAIL_HEADER = b"x-admin-email"
ADMIN_ROLE_HEADER = b"x-admin-role"


def _decode_jwt_claims_unsafe(token: str) -> dict:
    """Decodes a JWT's payload segment without verifying its signature.
    Returns {} for anything malformed — a bad/absent token must fall
    through to admin_context.py's own 401, not raise here."""
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError):
        return {}


class LocalAdminAuthMiddleware:
    """Raw ASGI middleware (not BaseHTTPMiddleware) so it can mutate
    `scope["headers"]` directly — the request object(s) built downstream
    read headers from this same scope dict, so an in-place edit here is
    visible to admin_context.py's `request.headers.get(...)` without
    needing to intercept/replay the request body."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        if auth_header.lower().startswith("bearer "):
            claims = _decode_jwt_claims_unsafe(auth_header[7:])
            admin_id = claims.get("sub")
            groups = claims.get("cognito:groups") or []
            role = groups[0] if groups else None
            if admin_id and role:
                new_headers = list(scope["headers"])
                new_headers.append((ADMIN_ID_HEADER, admin_id.encode("utf-8")))
                new_headers.append((ADMIN_ROLE_HEADER, role.encode("utf-8")))
                email = claims.get("email")
                if email:
                    new_headers.append((ADMIN_EMAIL_HEADER, email.encode("utf-8")))
                scope["headers"] = new_headers

        await self.app(scope, receive, send)
