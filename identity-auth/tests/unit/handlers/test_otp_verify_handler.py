import json

import pytest

import handlers.otp_verify_handler as otp_verify_handler
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

    def register_and_issue_tokens(self, mobile: str):
        self.calls.append(mobile)
        if self.raise_on_issue:
            raise self.raise_on_issue
        return (
            TokenBundle(access_token="access-tok", refresh_token="refresh-tok", id_token="id-tok", expires_in=900),
            True,
        )


@pytest.fixture(autouse=True)
def _reset_deps():
    otp_verify_handler._deps = None
    yield
    otp_verify_handler._deps = None


def _inject_deps(cognito=None):
    store = FakeOtpStore()
    otp_service = OtpService(otp_store=store, rate_limiter=NoOpRateLimiter())
    cognito = cognito or FakeCognito()
    otp_verify_handler._deps = {"cognito": cognito, "otp_service": otp_service}
    return store, otp_service, cognito


def _event(body: dict) -> dict:
    return {"body": json.dumps(body), "headers": {"x-request-id": "test-corr-id"}}


def test_verify_otp_success_returns_tokens():
    store, otp_service, cognito = _inject_deps()
    record, plaintext, _ = otp_service.request_otp("+919876543210")

    response = otp_verify_handler.handler(
        _event({"mobile": "+919876543210", "otp": plaintext, "requestId": record.request_id}), None
    )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["data"]["accessToken"] == "access-tok"
    assert body["data"]["refreshToken"] == "refresh-tok"
    assert body["data"]["expiresIn"] == 900
    assert body["data"]["isNewUser"] is True
    assert "idToken" not in body["data"]
    assert cognito.calls == ["+919876543210"]


def test_verify_otp_wrong_code_returns_401():
    _, otp_service, _ = _inject_deps()
    record, _, _ = otp_service.request_otp("+919876543210")

    response = otp_verify_handler.handler(
        _event({"mobile": "+919876543210", "otp": "000000", "requestId": record.request_id}), None
    )

    assert response["statusCode"] == 401
    body = json.loads(response["body"])
    assert body["data"]["errorCode"] == "OTP_INVALID"


def test_verify_otp_unknown_request_id_returns_400():
    _inject_deps()

    response = otp_verify_handler.handler(
        _event({"mobile": "+919876543210", "otp": "123456", "requestId": "nope"}), None
    )

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert body["data"]["errorCode"] == "OTP_REQUEST_NOT_FOUND"


def test_verify_otp_malformed_body_returns_400():
    _inject_deps()

    response = otp_verify_handler.handler({"body": "not json", "headers": {}}, None)

    assert response["statusCode"] == 400


def test_verify_otp_cognito_failure_propagates_as_503():
    store, otp_service, _ = _inject_deps(cognito=FakeCognito(raise_on_issue=ExternalServiceUnavailableError("down")))
    record, plaintext, _ = otp_service.request_otp("+919876543210")

    response = otp_verify_handler.handler(
        _event({"mobile": "+919876543210", "otp": plaintext, "requestId": record.request_id}), None
    )

    assert response["statusCode"] == 503


def test_verify_otp_cognito_failure_leaves_otp_retryable():
    # A transient Cognito failure must not burn the OTP — the record
    # must stay ACTIVE (not CONSUMED) so a retry with the same
    # requestId/otp can still succeed once Cognito recovers.
    failing_cognito = FakeCognito(raise_on_issue=ExternalServiceUnavailableError("down"))
    store, otp_service, _ = _inject_deps(cognito=failing_cognito)
    record, plaintext, _ = otp_service.request_otp("+919876543210")

    first = otp_verify_handler.handler(
        _event({"mobile": "+919876543210", "otp": plaintext, "requestId": record.request_id}), None
    )
    assert first["statusCode"] == 503
    assert store.records[record.request_id].status == OtpStatus.ACTIVE

    failing_cognito.raise_on_issue = None  # Cognito recovers
    second = otp_verify_handler.handler(
        _event({"mobile": "+919876543210", "otp": plaintext, "requestId": record.request_id}), None
    )
    assert second["statusCode"] == 200
    assert store.records[record.request_id].status == OtpStatus.CONSUMED
