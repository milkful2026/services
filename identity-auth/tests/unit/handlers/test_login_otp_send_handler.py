"""Handler-level tests: routing, DTO validation, exception->status
mapping. Mirrors test_otp_send_handler.py's pattern, inverted: the
mobile must already be a verified Cognito user."""

import json

import pytest

import handlers.login_otp_send_handler as login_otp_send_handler
from domain.exceptions import NotificationPublishError, RateLimitExceededError
from domain.otp_service import OtpService


class FakeCognito:
    def __init__(self, verified_sub: str | None = "cognito-sub-123"):
        self.verified_sub = verified_sub

    def find_verified_sub_by_phone(self, mobile: str) -> str | None:
        return self.verified_sub


class FakePublisher:
    def __init__(self, fail_times: int = 0):
        self.calls: list[tuple] = []
        self._fail_times = fail_times

    def publish_otp_requested(self, mobile, otp, correlation_id, purpose="REGISTER"):
        self.calls.append((mobile, otp, correlation_id, purpose))
        if self._fail_times > 0:
            self._fail_times -= 1
            raise NotificationPublishError("boom")


class RaisingRateLimiter:
    def check_and_increment(self, key, max_requests, window_seconds):
        raise RateLimitExceededError("Too many requests")


class FakeOtpStore:
    def __init__(self):
        self.records = {}

    def put(self, record):
        self.records[record.request_id] = record

    def get(self, request_id):
        return self.records.get(request_id)

    def get_active_by_mobile(self, mobile, purpose):
        for r in self.records.values():
            if r.mobile == mobile and r.status.value == "ACTIVE" and r.purpose == purpose:
                return r
        return None

    def increment_attempts(self, request_id):
        self.records[request_id].attempts += 1
        return self.records[request_id].attempts

    def mark_status(self, request_id, status):
        from domain.models import OtpStatus

        self.records[request_id].status = OtpStatus(status)


class NoOpRateLimiter:
    def check_and_increment(self, key, max_requests, window_seconds):
        pass


class FakeSettings:
    otp_ttl_seconds = 300
    otp_resend_after_seconds = 30


@pytest.fixture(autouse=True)
def _reset_deps():
    login_otp_send_handler._deps = None
    yield
    login_otp_send_handler._deps = None


def _inject_deps(cognito=None, publisher=None, rate_limiter=None):
    cognito = cognito or FakeCognito()
    publisher = publisher or FakePublisher()
    otp_service = OtpService(otp_store=FakeOtpStore(), rate_limiter=rate_limiter or NoOpRateLimiter())
    login_otp_send_handler._deps = {
        "settings": FakeSettings(),
        "cognito": cognito,
        "publisher": publisher,
        "otp_service": otp_service,
    }
    return cognito, publisher, otp_service


def _event(body: dict) -> dict:
    return {"body": json.dumps(body), "headers": {"x-request-id": "test-corr-id"}}


def test_login_otp_send_success_publishes_with_login_purpose():
    cognito, publisher, _ = _inject_deps()

    response = login_otp_send_handler.handler(_event({"mobile": "+919876543210"}), None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert "requestId" in body["data"]
    assert len(publisher.calls) == 1
    assert publisher.calls[0][3] == "LOGIN"


def test_login_otp_send_unregistered_mobile_returns_404_with_redirect():
    _inject_deps(cognito=FakeCognito(verified_sub=None))

    response = login_otp_send_handler.handler(_event({"mobile": "+919876543210"}), None)

    assert response["statusCode"] == 404
    body = json.loads(response["body"])
    assert body["data"]["errorCode"] == "USER_NOT_FOUND"
    assert body["data"]["redirect"] == "signup"


def test_login_otp_send_invalid_mobile_returns_400():
    _inject_deps()

    response = login_otp_send_handler.handler(_event({"mobile": "9876543210"}), None)

    assert response["statusCode"] == 400


def test_login_otp_send_malformed_json_returns_400():
    _inject_deps()

    response = login_otp_send_handler.handler({"body": "{not json", "headers": {}}, None)

    assert response["statusCode"] == 400


def test_login_otp_send_rate_limited_returns_429():
    _inject_deps(rate_limiter=RaisingRateLimiter())

    response = login_otp_send_handler.handler(_event({"mobile": "+919876543210"}), None)

    assert response["statusCode"] == 429


def test_login_otp_send_resend_within_cooldown_does_not_republish():
    _, publisher, _ = _inject_deps()

    login_otp_send_handler.handler(_event({"mobile": "+919876543210"}), None)
    login_otp_send_handler.handler(_event({"mobile": "+919876543210"}), None)

    assert len(publisher.calls) == 1


def test_login_otp_send_does_not_collide_with_a_registration_otp_for_same_mobile():
    # Regression guard for the purpose-scoping bug fixed alongside this
    # handler: seed an ACTIVE REGISTER-purpose record for this mobile in
    # the shared fake store, then confirm the LOGIN send still generates
    # its own fresh OTP rather than being swallowed as a cross-purpose
    # "duplicate".
    cognito, publisher, otp_service = _inject_deps()
    otp_service.request_otp("+919876543210", purpose="REGISTER")

    response = login_otp_send_handler.handler(_event({"mobile": "+919876543210"}), None)

    assert response["statusCode"] == 200
    assert len(publisher.calls) == 1
    assert publisher.calls[0][3] == "LOGIN"


def test_login_otp_send_publish_failure_returns_502():
    publisher = FakePublisher(fail_times=1)
    _inject_deps(publisher=publisher)

    response = login_otp_send_handler.handler(_event({"mobile": "+919876543210"}), None)

    assert response["statusCode"] == 502
    body = json.loads(response["body"])
    assert body["data"]["errorCode"] == "NOTIFICATION_PUBLISH_FAILED"
