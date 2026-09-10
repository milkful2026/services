"""Cognito identity for `/wallet/me/*`.

API Gateway's Cognito JWT authorizer verifies the token upstream; this
service trusts the verified claims. Locally there is no authorizer, so we
decode the bearer token unverified — the same dev-only posture
`services/local-dev/_lambda_local_server.py` documents. `sub` is the
user id.
"""

import jwt
from fastapi import Header, HTTPException


def current_user_id(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:  # noqa: F841
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Token has no sub")
    return str(sub)
