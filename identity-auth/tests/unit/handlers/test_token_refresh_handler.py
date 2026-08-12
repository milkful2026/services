import json

import pytest

import handlers.token_refresh_handler as token_refresh_handler
from domain.exceptions import InvalidRefreshTokenError
from domain.models import TokenBundle


class FakeTokenService:
    def __init__(self, raises=None):
        self.raises = raises
        self.calls = []

    def refresh(self, refresh_token: str) -> TokenBundle:
        self.calls.append(refresh_token)
        if self.raises:
            raise self.raises
        return TokenBundle(access_token="a", refresh_token="r2", id_token="i", expires_in=900)


@pytest.fixture(autouse=True)
def _reset_deps():
    token_refresh_handler._deps = None
    yield
    token_refresh_handler._deps = None


def _inject(raises=None):
    service = FakeTokenService(raises=raises)
    token_refresh_handler._deps = {"token_service": service}
    return service


def _event(body: dict) -> dict:
    return {"body": json.dumps(body), "headers": {"x-request-id": "corr"}}


def test_refresh_success_returns_new_tokens():
    service = _inject()

    response = token_refresh_handler.handler(_event({"refreshToken": "old-token"}), None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["data"]["accessToken"] == "a"
    assert body["data"]["refreshToken"] == "r2"
    assert service.calls == ["old-token"]


def test_refresh_invalid_token_returns_401():
    _inject(raises=InvalidRefreshTokenError("expired"))

    response = token_refresh_handler.handler(_event({"refreshToken": "old-token"}), None)

    assert response["statusCode"] == 401


def test_refresh_missing_field_returns_400():
    _inject()

    response = token_refresh_handler.handler(_event({}), None)

    assert response["statusCode"] == 400
