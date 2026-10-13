"""Local-dev-only ASGI middleware that forwards the browser's own
Authorization Bearer token into the X-Admin-Id/-Email/-Role request
headers admin_context.py reads. Direct port of
`services/inventory/src/handlers/local_admin_auth.py` (MA-48) — see that
module's own docstring for the full reasoning (API Gateway's real Lambda
REQUEST authorizer + parameter-mapping hop has no local-dev stand-in for
a FastAPI/ALB service, unlike the Lambda-shaped services, where
_lambda_local_server.py's `(handler, authorizer)` tuple route convention
already covers this).

Same trust model: the JWT payload is decoded as-is, signature unchecked
(moto's own Cognito tokens aren't properly signed either).

Gated behind CATALOG_LOCAL_ADMIN_AUTH (bootstrap.py-written, local-dev
only — mirrors handlers/app.py's CATALOG_CORS_ALLOW_ALL pattern) so this
never runs against real traffic, where the real headers already arrive
from API Gateway and must not be overwritten by a caller-supplied
Authorization header instead.
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
            # A hand-crafted local token's `cognito:groups` could be a
            # bare string rather than a list (every other claim here is
            # scalar, an easy mistake) — `groups[0]` on a string silently
            # yields its first character (e.g. "Ops" -> "O"), a
            # plausible-looking role that fails role-gating with a
            # confusing 403 instead of this falling through to
            # admin_context.py's own 401 for malformed claims, as
            # documented above.
            role = groups[0] if isinstance(groups, list) and groups else None
            email = claims.get("email")
            # Claims come from an unverified token payload, not a typed
            # schema — a hand-crafted/malformed local JWT could carry a
            # non-string sub/group/email, which would otherwise raise
            # AttributeError out of .encode() below instead of falling
            # through to admin_context.py's own 401 as documented.
            if not isinstance(admin_id, str):
                admin_id = None
            if not isinstance(role, str):
                role = None
            if not isinstance(email, str):
                email = None
            if admin_id and role:
                # Strip any caller-supplied X-Admin-* headers first —
                # Starlette's Headers.get() returns the FIRST match, so
                # appending without stripping would let a client-sent
                # X-Admin-Role silently win over the token-derived one.
                admin_header_names = {ADMIN_ID_HEADER, ADMIN_ROLE_HEADER, ADMIN_EMAIL_HEADER}
                new_headers = [
                    (name, value) for name, value in scope["headers"] if name not in admin_header_names
                ]
                new_headers.append((ADMIN_ID_HEADER, admin_id.encode("utf-8")))
                new_headers.append((ADMIN_ROLE_HEADER, role.encode("utf-8")))
                if email:
                    new_headers.append((ADMIN_EMAIL_HEADER, email.encode("utf-8")))
                scope["headers"] = new_headers

        await self.app(scope, receive, send)
