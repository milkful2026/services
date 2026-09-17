import json

import pytest

import handlers.admin_auth.verify_2fa_handler as verify_2fa_handler
from domain.admin_exceptions import AdminAccountLockedError, ChallengeExpiredError, Invalid2faCodeError
from domain.admin_models import AdminTokenBundle


class FakeLoginService:
    def __init__(self, tokens=None, raise_error=None):
        self.tokens = tokens
        self.raise_error = raise_error
        self.calls = []

    def verify_2fa(self, challenge_token, code, correlation_id):
        self.calls.append((challenge_token, code, correlation_id))
        if self.raise_error:
            raise self.raise_error
        return self.tokens


@pytest.fixture(autouse=True)
def _reset_deps():
    verify_2fa_handler._deps = None
    yield
    verify_2fa_handler._deps = None


def _event(body: dict) -> dict:
    return {"body": json.dumps(body), "headers": {"x-request-id": "corr-1"}}


def test_verify_2fa_success_returns_tokens():
    tokens = AdminTokenBundle(access_token="a", refresh_token="r", id_token="i", expires_in=900)
    verify_2fa_handler._deps = {"login_service": FakeLoginService(tokens=tokens)}

    response = verify_2fa_handler.handler(_event({"challengeToken": "tok-1", "code": "123456"}), None)

    assert response["statusCode"] == 200
    data = json.loads(response["body"])["data"]
    assert data["accessToken"] == "a"
    assert data["refreshToken"] == "r"
    assert data["expiresIn"] == 900


def test_verify_2fa_wrong_code_returns_401():
    verify_2fa_handler._deps = {"login_service": FakeLoginService(raise_error=Invalid2faCodeError())}

    response = verify_2fa_handler.handler(_event({"challengeToken": "tok-1", "code": "000000"}), None)

    assert response["statusCode"] == 401
    assert json.loads(response["body"])["data"]["errorCode"] == "INVALID_2FA_CODE"


def test_verify_2fa_expired_challenge_returns_distinct_error_code():
    verify_2fa_handler._deps = {"login_service": FakeLoginService(raise_error=ChallengeExpiredError())}

    response = verify_2fa_handler.handler(_event({"challengeToken": "expired-tok", "code": "123456"}), None)

    assert response["statusCode"] == 401
    assert json.loads(response["body"])["data"]["errorCode"] == "CHALLENGE_EXPIRED"


def test_verify_2fa_locked_account_returns_401_with_lock_code():
    verify_2fa_handler._deps = {"login_service": FakeLoginService(raise_error=AdminAccountLockedError())}

    response = verify_2fa_handler.handler(_event({"challengeToken": "tok-1", "code": "000000"}), None)

    assert response["statusCode"] == 401
    assert json.loads(response["body"])["data"]["errorCode"] == "ADMIN_ACCOUNT_LOCKED"


def test_verify_2fa_missing_fields_returns_validation_error():
    verify_2fa_handler._deps = {"login_service": FakeLoginService()}

    response = verify_2fa_handler.handler(_event({"code": "123456"}), None)

    assert response["statusCode"] == 400
