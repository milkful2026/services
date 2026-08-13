import json

import pytest

import handlers.logout_handler as logout_handler
from domain.exceptions import ValidationError


class FakeCognito:
    def __init__(self, raises: Exception | None = None):
        self.raises = raises
        self.calls: list[str] = []

    def revoke_token(self, refresh_token: str) -> None:
        self.calls.append(refresh_token)
        if self.raises:
            raise self.raises


@pytest.fixture(autouse=True)
def _reset_deps():
    logout_handler._deps = None
    yield
    logout_handler._deps = None


def _inject(cognito=None):
    cognito = cognito or FakeCognito()
    logout_handler._deps = {"cognito": cognito}
    return cognito


def _event(body: dict) -> dict:
    return {"body": json.dumps(body), "headers": {"x-request-id": "corr-1"}}


def test_logout_success_returns_204():
    cognito = _inject()

    response = logout_handler.handler(_event({"refreshToken": "some-refresh-token"}), None)

    assert response["statusCode"] == 204
    assert response["body"] == ""
    assert cognito.calls == ["some-refresh-token"]


def test_logout_already_revoked_token_still_returns_204():
    # cognito_adapter.revoke_token itself swallows non-malformed errors —
    # this test just confirms the handler doesn't add its own failure
    # path on top of that (idempotent per spec FR-3).
    cognito = _inject()

    first = logout_handler.handler(_event({"refreshToken": "tok"}), None)
    second = logout_handler.handler(_event({"refreshToken": "tok"}), None)

    assert first["statusCode"] == 204
    assert second["statusCode"] == 204


def test_logout_malformed_token_returns_400():
    _inject(cognito=FakeCognito(raises=ValidationError("Malformed refresh token")))

    response = logout_handler.handler(_event({"refreshToken": "garbage"}), None)

    assert response["statusCode"] == 400


def test_logout_missing_body_returns_400():
    _inject()

    response = logout_handler.handler({"headers": {}}, None)

    assert response["statusCode"] == 400


def test_logout_malformed_json_returns_400():
    _inject()

    response = logout_handler.handler({"body": "not json", "headers": {}}, None)

    assert response["statusCode"] == 400
