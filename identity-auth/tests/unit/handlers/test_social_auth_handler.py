import json

import pytest

import handlers.social_auth_handler as social_auth_handler
from domain.exceptions import InvalidSocialTokenError
from domain.models import SocialAuthResult, TokenBundle


class FakeSocialLinkService:
    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    def authenticate(self, provider, id_token):
        self.calls.append((provider, id_token))
        if self.raises:
            raise self.raises
        return self.result


@pytest.fixture(autouse=True)
def _reset_deps():
    social_auth_handler._deps = None
    yield
    social_auth_handler._deps = None


def _inject(result=None, raises=None):
    service = FakeSocialLinkService(result=result, raises=raises)
    social_auth_handler._deps = {"social_link_service": service}
    return service


def _event(body: dict) -> dict:
    return {"body": json.dumps(body), "headers": {"x-request-id": "corr"}}


def test_social_auth_success_returns_tokens():
    _inject(
        result=SocialAuthResult(
            tokens=TokenBundle(access_token="a", refresh_token="r", id_token="i", expires_in=900),
            is_new_user=True,
        )
    )

    response = social_auth_handler.handler(
        _event({"provider": "google", "idToken": "tok"}), None
    )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["data"]["accessToken"] == "a"
    assert body["data"]["isNewUser"] is True


def test_social_auth_requires_mobile_verification():
    _inject(
        result=SocialAuthResult(requires_mobile_verification=True, partial_token="partial:sub-1")
    )

    response = social_auth_handler.handler(
        _event({"provider": "google", "idToken": "tok"}), None
    )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["data"]["requiresMobileVerification"] is True
    assert body["data"]["partialToken"] == "partial:sub-1"


def test_social_auth_invalid_token_returns_401():
    _inject(raises=InvalidSocialTokenError("bad token"))

    response = social_auth_handler.handler(
        _event({"provider": "google", "idToken": "tok"}), None
    )

    assert response["statusCode"] == 401


def test_social_auth_invalid_provider_returns_400():
    _inject()

    response = social_auth_handler.handler(
        _event({"provider": "facebook", "idToken": "tok"}), None
    )

    assert response["statusCode"] == 400


def test_social_auth_malformed_body_returns_400():
    _inject()

    response = social_auth_handler.handler({"body": "not json", "headers": {}}, None)

    assert response["statusCode"] == 400
