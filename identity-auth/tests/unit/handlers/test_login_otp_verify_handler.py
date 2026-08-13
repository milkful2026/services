import json

import pytest

import handlers.login_otp_verify_handler as login_otp_verify_handler
from domain.exceptions import ExternalServiceUnavailableError
from domain.models import OtpStatus, TokenBundle
from domain.otp_service import OtpService


class FakeOtpStore:
    def __init__(self):
        self.records = {}

    def put(self, record):
        self.records[record.request_id] = record

    def get(self, request_id):
        return self.records.get(request_id)

    def get_active_by_mobile(self, mobile, purpose):
        for r in self.records.values():
            if r.mobile == mobile and r.status == OtpStatus.ACTIVE and r.purpose == purpose:
                return r
        return None

    def increment_attempts(self, request_id):
        self.records[request_id].attempts += 1
        return self.records[request_id].attempts

    def mark_status(self, request_id, status):
        self.records[request_id].status = OtpStatus(status)


class NoOpRateLimiter:
    def check_and_increment(self, key, max_requests, window_seconds):
        pass


class FakeCognito:
    def __init__(self, raise_on_issue: Exception | None = None):
        self.raise_on_issue = raise_on_issue
        self.calls = []

    def issue_tokens(self, username: str):
        self.calls.append(username)
        if self.raise_on_issue:
            raise self.raise_on_issue
        return TokenBundle(
            access_token="access-tok", refresh_token="refresh-tok", id_token="id-tok", expires_in=900
        )


@pytest.fixture(autouse=True)
def _reset_deps():
    login_otp_verify_handler._deps = None
    yield
    login_otp_verify_handler._deps = None


def _inject_deps(cognito=None):
    store = FakeOtpStore()
    otp_service = OtpService(otp_store=store, rate_limiter=NoOpRateLimiter())
    cognito = cognito or FakeCognito()
    login_otp_verify_handler._deps = {"cognito": cognito, "otp_service": otp_service}
    return store, otp_service, cognito


def _event(body: dict) -> dict:
    return {"body": json.dumps(body), "headers": {"x-request-id": "test-corr-id"}}


def test_login_verify_success_returns_tokens_without_is_new_user_field():
    store, otp_service, cognito = _inject_deps()
    record, plaintext, _ = otp_service.request_otp("+919876543210", purpose="LOGIN")

    response = login_otp_verify_handler.handler(
        _event({"mobile": "+919876543210", "otp": plaintext, "requestId": record.request_id}), None
    )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["data"]["accessToken"] == "access-tok"
    assert body["data"]["refreshToken"] == "refresh-tok"
    assert body["data"]["expiresIn"] == 900
    assert "isNewUser" not in body["data"]
    assert "idToken" not in body["data"]
    assert cognito.calls == ["+919876543210"]


def test_login_verify_calls_issue_tokens_not_register(monkeypatch):
    # issue_tokens must be called directly — never register_and_issue_tokens,
    # which would AdminCreateUser a user that must already exist.
    store, otp_service, cognito = _inject_deps()
    assert not hasattr(cognito, "register_and_issue_tokens")

    record, plaintext, _ = otp_service.request_otp("+919876543210", purpose="LOGIN")
    login_otp_verify_handler.handler(
        _event({"mobile": "+919876543210", "otp": plaintext, "requestId": record.request_id}), None
    )

    assert cognito.calls == ["+919876543210"]


def test_login_verify_wrong_code_returns_401():
    _, otp_service, _ = _inject_deps()
    record, _, _ = otp_service.request_otp("+919876543210", purpose="LOGIN")

    response = login_otp_verify_handler.handler(
        _event({"mobile": "+919876543210", "otp": "000000", "requestId": record.request_id}), None
    )

    assert response["statusCode"] == 401
    assert json.loads(response["body"])["data"]["errorCode"] == "OTP_INVALID"


def test_login_verify_unknown_request_id_returns_400():
    _inject_deps()

    response = login_otp_verify_handler.handler(
        _event({"mobile": "+919876543210", "otp": "123456", "requestId": "nope"}), None
    )

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["data"]["errorCode"] == "OTP_REQUEST_NOT_FOUND"


def test_login_verify_malformed_body_returns_400():
    _inject_deps()

    response = login_otp_verify_handler.handler({"body": "not json", "headers": {}}, None)

    assert response["statusCode"] == 400


def test_login_verify_cognito_failure_propagates_as_503():
    store, otp_service, _ = _inject_deps(
        cognito=FakeCognito(raise_on_issue=ExternalServiceUnavailableError("down"))
    )
    record, plaintext, _ = otp_service.request_otp("+919876543210", purpose="LOGIN")

    response = login_otp_verify_handler.handler(
        _event({"mobile": "+919876543210", "otp": plaintext, "requestId": record.request_id}), None
    )

    assert response["statusCode"] == 503
