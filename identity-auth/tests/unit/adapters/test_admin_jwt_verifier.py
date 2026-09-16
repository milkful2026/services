"""Same JWKS-mocking pattern as test_social_jwks_adapter.py, adapted for
Cognito's own JWKS endpoint and access-token claim shape (token_use,
client_id — no `aud`)."""

import json as _json
import time

import jwt
import pytest
import responses
from cryptography.hazmat.primitives.asymmetric import rsa

from adapters.admin_jwt_verifier import AdminJwtVerifierAdapter
from domain.admin_exceptions import AdminAuthenticationError
from domain.exceptions import ExternalServiceUnavailableError

POOL_ID = "ap-south-1_admintestpool"
REGION = "ap-south-1"
CLIENT_ID = "test-admin-client-id"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
KID = "test-key-1"


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks_document(rsa_key):
    jwk = _json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key()))
    jwk["kid"] = KID
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return {"keys": [jwk]}


def _sign_token(rsa_key, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "admin-cognito-sub-1",
        "token_use": "access",
        "client_id": CLIENT_ID,
        "iat": now,
        "exp": now + 900,
    }
    claims.update(overrides)
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": KID})


@pytest.fixture
def adapter():
    return AdminJwtVerifierAdapter(
        user_pool_id=POOL_ID, client_id=CLIENT_ID, region_name=REGION, cache_ttl_seconds=3600
    )


@responses.activate
def test_verify_valid_access_token_returns_claims(adapter, rsa_key, jwks_document):
    responses.get(JWKS_URL, json=jwks_document)
    token = _sign_token(rsa_key)

    claims = adapter.verify_access_token(token)

    assert claims["sub"] == "admin-cognito-sub-1"
    assert claims["token_use"] == "access"


@responses.activate
def test_verify_caches_jwks_across_calls(adapter, rsa_key, jwks_document):
    responses.get(JWKS_URL, json=jwks_document)
    token = _sign_token(rsa_key)

    adapter.verify_access_token(token)
    adapter.verify_access_token(token)

    assert len(responses.calls) == 1


@responses.activate
def test_verify_rejects_id_token_use(adapter, rsa_key, jwks_document):
    responses.get(JWKS_URL, json=jwks_document)
    token = _sign_token(rsa_key, token_use="id")

    with pytest.raises(AdminAuthenticationError):
        adapter.verify_access_token(token)


@responses.activate
def test_verify_rejects_wrong_client_id(adapter, rsa_key, jwks_document):
    responses.get(JWKS_URL, json=jwks_document)
    token = _sign_token(rsa_key, client_id="some-other-client")

    with pytest.raises(AdminAuthenticationError):
        adapter.verify_access_token(token)


@responses.activate
def test_verify_rejects_wrong_issuer(adapter, rsa_key, jwks_document):
    responses.get(JWKS_URL, json=jwks_document)
    token = _sign_token(rsa_key, iss="https://evil.example.com")

    with pytest.raises(AdminAuthenticationError):
        adapter.verify_access_token(token)


@responses.activate
def test_verify_rejects_expired_token(adapter, rsa_key, jwks_document):
    responses.get(JWKS_URL, json=jwks_document)
    token = _sign_token(rsa_key, iat=int(time.time()) - 7200, exp=int(time.time()) - 3600)

    with pytest.raises(AdminAuthenticationError):
        adapter.verify_access_token(token)


@responses.activate
def test_verify_jwks_fetch_failure_raises_unavailable(adapter, rsa_key):
    responses.get(JWKS_URL, status=500)
    token = _sign_token(rsa_key)  # well-formed JWT — the JWKS fetch itself is what fails

    with pytest.raises(ExternalServiceUnavailableError):
        adapter.verify_access_token(token)


@responses.activate
def test_verify_forces_refresh_when_kid_missing_from_cache(adapter, rsa_key, jwks_document):
    responses.get(JWKS_URL, json={"keys": []})
    responses.get(JWKS_URL, json=jwks_document)
    token = _sign_token(rsa_key)

    claims = adapter.verify_access_token(token)

    assert claims["sub"] == "admin-cognito-sub-1"
    assert len(responses.calls) == 2


def test_verify_malformed_token_raises(adapter):
    with pytest.raises(AdminAuthenticationError):
        adapter.verify_access_token("not-a-jwt-at-all")
