import pytest

from domain.exceptions import InvalidSocialTokenError, ValidationError
from domain.models import TokenBundle
from domain.social_link_service import SocialLinkService


class FakeTokenVerifier:
    def __init__(self, claims: dict | None = None, raises: Exception | None = None):
        self.claims = claims
        self.raises = raises

    def verify(self, provider: str, id_token: str) -> dict:
        if self.raises:
            raise self.raises
        return self.claims


class FakeCognito:
    def __init__(self, sub="sub-1", mobile=None, is_new_user=True, mobile_verified=False):
        self.sub = sub
        self.mobile = mobile
        self.is_new_user = is_new_user
        self.mobile_verified = mobile_verified
        self.issue_tokens_calls = []

    def find_or_create_federated_user(self, provider, provider_sub, email):
        return self.sub, self.mobile, self.is_new_user, self.mobile_verified

    def issue_tokens(self, username):
        self.issue_tokens_calls.append(username)
        return TokenBundle(access_token="a", refresh_token="r", id_token="i", expires_in=900)


def _claims(**overrides):
    base = {"sub": "google-sub-123", "email": "user@example.com"}
    base.update(overrides)
    return base


def test_new_social_user_without_mobile_requires_verification():
    verifier = FakeTokenVerifier(claims=_claims())
    cognito = FakeCognito(mobile_verified=False)
    service = SocialLinkService(verifier, cognito)

    result = service.authenticate("google", "id-token")

    assert result.requires_mobile_verification is True
    assert result.partial_token is not None
    assert result.tokens is None
    assert result.is_new_user is True


def test_existing_mobile_verified_user_gets_tokens():
    verifier = FakeTokenVerifier(claims=_claims())
    cognito = FakeCognito(mobile="+919876543210", is_new_user=False, mobile_verified=True)
    service = SocialLinkService(verifier, cognito)

    result = service.authenticate("google", "id-token")

    assert result.requires_mobile_verification is False
    assert result.tokens is not None
    assert result.tokens.access_token == "a"
    assert cognito.issue_tokens_calls == ["+919876543210"]


def test_missing_email_in_claims_raises_validation_error():
    verifier = FakeTokenVerifier(claims=_claims(email=None))
    cognito = FakeCognito()
    service = SocialLinkService(verifier, cognito)

    with pytest.raises(ValidationError):
        service.authenticate("google", "id-token")


def test_invalid_token_propagates_from_verifier():
    verifier = FakeTokenVerifier(raises=InvalidSocialTokenError("bad token"))
    cognito = FakeCognito()
    service = SocialLinkService(verifier, cognito)

    with pytest.raises(InvalidSocialTokenError):
        service.authenticate("google", "bad-token")
