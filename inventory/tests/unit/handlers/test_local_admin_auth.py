"""LocalAdminAuthMiddleware — verifies the Authorization Bearer -> X-Admin-*
header translation end-to-end through a real FastAPI app, since the bug
this exists to fix (every admin route 401ing for a real browser session)
only shows up once a request actually reaches admin_context.py's
get_caller_admin() through the full ASGI stack, not from testing the
middleware's claims-decoding in isolation."""

import base64
import json

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from domain.exceptions import InventoryError
from handlers.admin_context import get_caller_admin
from handlers.dto import error_envelope
from handlers.local_admin_auth import LocalAdminAuthMiddleware


def _fake_jwt(claims: dict) -> str:
    # No signature needed — the middleware never verifies one, matching
    # _lambda_local_server.py's documented local-dev trust model.
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _build_app() -> FastAPI:
    # Registers the same InventoryError -> JSONResponse exception handler
    # app.py itself registers — without it, get_caller_admin()'s
    # AdminAuthenticationError would propagate as an unhandled 500 in
    # this standalone test app instead of the real app's actual 401.
    app = FastAPI()
    app.add_middleware(LocalAdminAuthMiddleware)

    @app.exception_handler(InventoryError)
    async def _inventory_error_handler(request: Request, exc: InventoryError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=error_envelope(exc.error_code, exc.message, exc.details),
        )

    @app.get("/whoami")
    def whoami(admin: dict = Depends(get_caller_admin)):
        return admin

    return app


def test_valid_bearer_token_is_translated_into_admin_headers():
    client = TestClient(_build_app())
    token = _fake_jwt({"sub": "admin-1", "cognito:groups": ["Ops"], "email": "ops@milkful.test"})

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"adminId": "admin-1", "email": "ops@milkful.test", "role": "Ops"}


def test_token_without_email_claim_omits_the_header_not_crashes():
    client = TestClient(_build_app())
    token = _fake_jwt({"sub": "admin-1", "cognito:groups": ["Ops"]})

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["email"] is None


def test_token_with_no_groups_claim_leaves_caller_unauthenticated():
    client = TestClient(_build_app())
    token = _fake_jwt({"sub": "admin-1"})

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_missing_authorization_header_leaves_caller_unauthenticated():
    client = TestClient(_build_app())

    response = client.get("/whoami")

    assert response.status_code == 401


def test_malformed_bearer_token_does_not_crash_the_request():
    client = TestClient(_build_app())

    response = client.get("/whoami", headers={"Authorization": "Bearer not-a-jwt"})

    assert response.status_code == 401


def test_non_bearer_authorization_header_is_ignored():
    client = TestClient(_build_app())

    response = client.get("/whoami", headers={"Authorization": "Basic dXNlcjpwYXNz"})

    assert response.status_code == 401


def test_non_string_claims_do_not_crash_fall_through_to_401():
    # Regression: admin_id.encode()/role.encode() previously assumed
    # every claim decodes to a string — a hand-crafted/malformed local
    # token with e.g. a numeric sub raised AttributeError instead of the
    # documented 401.
    client = TestClient(_build_app())
    token = _fake_jwt({"sub": 12345, "cognito:groups": ["Ops"]})

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_caller_supplied_admin_headers_cannot_override_the_token_derived_role():
    # Regression: appending the derived X-Admin-* headers without
    # stripping any pre-existing ones let a caller-supplied X-Admin-Role
    # win, since Starlette's Headers.get() returns the first match —
    # a Support-role token plus a spoofed X-Admin-Role: Ops header must
    # still resolve to Support, not Ops.
    client = TestClient(_build_app())
    token = _fake_jwt({"sub": "admin-1", "cognito:groups": ["Support"]})

    response = client.get(
        "/whoami",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Admin-Id": "spoofed",
            "X-Admin-Role": "Ops",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"adminId": "admin-1", "email": None, "role": "Support"}
