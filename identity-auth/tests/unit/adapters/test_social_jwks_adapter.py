import time

import jwt
import pytest
import responses
from cryptography.hazmat.primitives.asymmetric import rsa

from adapters.social_jwks_adapter import SocialJwksAdapter
from domain.exceptions import ExternalServiceUnavailableError, InvalidSocialTokenError

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
KID = "test-key-1"


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks_document(rsa_key):
    jwk_json = jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key())
    import json as _json

    jwk = _json.loads(jwk_json)
    jwk["kid"] = KID
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return {"keys": [jwk]}


def _sign_token(rsa_key, **claim_overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "test-google-client-id",
        "sub": "google-user-sub-123",
        "email": "user@example.com",
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(claim_overrides)
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": KID})


@pytest.fixture
def adapter():
    return SocialJwksAdapter(
        google_client_id="test-google-client-id",
        apple_client_id="test-apple-client-id",
        google_jwks_url=GOOGLE_JWKS_URL,
        apple_jwks_url=APPLE_JWKS_URL,
        cache_ttl_seconds=3600,
    )


@responses.activate
def test_verify_valid_google_token_returns_claims(adapter, rsa_key, jwks_document):
    responses.get(GOOGLE_JWKS_URL, json=jwks_document)
    token = _sign_token(rsa_key)

    claims = adapter.verify("google", token)

    assert claims["sub"] == "google-user-sub-123"
    assert claims["email"] == "user@example.com"


@responses.activate
def test_verify_caches_jwks_across_calls(adapter, rsa_key, jwks_document):
    responses.get(GOOGLE_JWKS_URL, json=jwks_document)
    token = _sign_token(rsa_key)

    adapter.verify("google", token)
    adapter.verify("google", token)

    assert len(responses.calls) == 1


@responses.activate
def test_verify_wrong_audience_raises(adapter, rsa_key, jwks_document):
    responses.get(GOOGLE_JWKS_URL, json=jwks_document)
    token = _sign_token(rsa_key, aud="someone-elses-client-id")

    with pytest.raises(InvalidSocialTokenError):
        adapter.verify("google", token)


@responses.activate
def test_verify_wrong_issuer_raises(adapter, rsa_key, jwks_document):
    responses.get(GOOGLE_JWKS_URL, json=jwks_document)
    token = _sign_token(rsa_key, iss="https://evil.example.com")

    with pytest.raises(InvalidSocialTokenError):
        adapter.verify("google", token)


@responses.activate
def test_verify_expired_token_raises(adapter, rsa_key, jwks_document):
    responses.get(GOOGLE_JWKS_URL, json=jwks_document)
    token = _sign_token(rsa_key, iat=int(time.time()) - 7200, exp=int(time.time()) - 3600)

    with pytest.raises(InvalidSocialTokenError):
        adapter.verify("google", token)


@responses.activate
def test_verify_unknown_kid_raises(adapter, rsa_key, jwks_document):
    responses.get(GOOGLE_JWKS_URL, json=jwks_document)
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "https://accounts.google.com",
            "aud": "test-google-client-id",
            "sub": "x",
            "iat": now,
            "exp": now + 3600,
        },
        rsa_key,
        algorithm="RS256",
        headers={"kid": "some-other-kid"},
    )

    with pytest.raises(InvalidSocialTokenError):
        adapter.verify("google", token)


@responses.activate
def test_verify_jwks_fetch_failure_raises_unavailable(adapter):
    responses.get(GOOGLE_JWKS_URL, status=500)

    with pytest.raises(ExternalServiceUnavailableError):
        adapter.verify("google", "irrelevant-token")
