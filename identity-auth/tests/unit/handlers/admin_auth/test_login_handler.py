import json

import pytest

import handlers.admin_auth.login_handler as login_handler
from domain.admin_exceptions import AdminAccountPendingError, IncorrectAdminCredentialsError


class FakeLoginService:
    def __init__(self, token=None, raise_error=None):
        self.token = token
        self.raise_error = raise_error
        self.calls = []

    def login_password(self, email, password, correlation_id):
        self.calls.append((email, password, correlation_id))
        if self.raise_error:
            raise self.raise_error
        return self.token


class FakeSettings:
    admin_challenge_ttl_seconds = 300


@pytest.fixture(autouse=True)
def _reset_deps():
    login_handler._deps = None
    yield
    login_handler._deps = None


def _event(body: dict) -> dict:
    return {"body": json.dumps(body), "headers": {"x-request-id": "corr-1"}}


def test_login_success_returns_challenge_token():
    login_handler._deps = {"login_service": FakeLoginService(token="tok-abc"), "settings": FakeSettings()}

    response = login_handler.handler(_event({"email": "admin@milkful.test", "password": "pw"}), None)

    assert response["statusCode"] == 200
    data = json.loads(response["body"])["data"]
    assert data["challengeToken"] == "tok-abc"
    assert data["expiresIn"] == 300


def test_login_wrong_credentials_returns_generic_401():
    login_handler._deps = {
        "login_service": FakeLoginService(raise_error=IncorrectAdminCredentialsError()),
        "settings": FakeSettings(),
    }

    response = login_handler.handler(_event({"email": "admin@milkful.test", "password": "wrong"}), None)

    assert response["statusCode"] == 401
    body = json.loads(response["body"])
    assert body["data"]["errorCode"] == "INCORRECT_CREDENTIALS"
    assert body["data"]["message"] == "Incorrect email or password"


def test_login_pending_account_returns_403():
    login_handler._deps = {
        "login_service": FakeLoginService(raise_error=AdminAccountPendingError()),
        "settings": FakeSettings(),
    }

    response = login_handler.handler(_event({"email": "pending@milkful.test", "password": "pw"}), None)

    assert response["statusCode"] == 403
    assert json.loads(response["body"])["data"]["errorCode"] == "ADMIN_ACCOUNT_PENDING"


def test_login_missing_fields_returns_validation_error():
    login_handler._deps = {"login_service": FakeLoginService(), "settings": FakeSettings()}

    response = login_handler.handler(_event({"email": "admin@milkful.test"}), None)

    assert response["statusCode"] == 400
