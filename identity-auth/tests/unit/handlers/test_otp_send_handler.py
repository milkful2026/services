"""Handler-level tests: routing, DTO validation, exception->status mapping.
Domain/adapter internals are covered elsewhere — here we inject simple
fakes via the module's _deps cache to isolate handler behavior."""

import json

import pytest

import handlers.otp_send_handler as otp_send_handler
from domain.exceptions import NotificationPublishError, RateLimitExceededError
from domain.otp_service import OtpService


class FakeCognito:
    def __init__(self, verified_sub: str | None = None):
        self.verified_sub = verified_sub

    def find_verified_sub_by_phone(self, mobile: str) -> str | None:
        return self.verified_sub


class FakePublisher:
    def __init__(self, fail_times: int = 0):
        self.calls: list[tuple] = []
        self._fail_times = fail_times

    def publish_otp_requested(self, mobile: str, otp: str, correlation_id: str) -> None:
        self.calls.append((mobile, otp, correlation_id))
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
    otp_send_handler._deps = None
    yield
    otp_send_handler._deps = None


def _inject_deps(cognito=None, publisher=None, rate_limiter=None):
    cognito = cognito or FakeCognito()
    publisher = publisher or FakePublisher()
    otp_service = OtpService(
        otp_store=FakeOtpStore(),
        rate_limiter=rate_limiter or NoOpRateLimiter(),
    )
    otp_send_handler._deps = {
        "settings": FakeSettings(),
        "cognito": cognito,
        "publisher": publisher,
        "otp_service": otp_service,
    }
    return cognito, publisher, otp_service


def _event(body: dict) -> dict:
    return {"body": json.dumps(body), "headers": {"x-request-id": "test-corr-id"}}


def test_send_otp_success_publishes_and_returns_request_id():
    cognito, publisher, _ = _inject_deps()

    response = otp_send_handler.handler(_event({"mobile": "+919876543210"}), None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["status"] == "success"
    assert "requestId" in body["data"]
    assert body["data"]["expiresIn"] == 300
    assert body["data"]["resendAfter"] == 30
    assert len(publisher.calls) == 1
    assert publisher.calls[0][2] == "test-corr-id"


def test_send_otp_existing_verified_user_returns_409_with_redirect():
    _inject_deps(cognito=FakeCognito(verified_sub="cognito-sub-123"))

    response = otp_send_handler.handler(_event({"mobile": "+919876543210"}), None)

    assert response["statusCode"] == 409
    body = json.loads(response["body"])
    assert body["data"]["errorCode"] == "USER_EXISTS"
    assert body["data"]["redirect"] == "login"


def test_send_otp_invalid_mobile_returns_400():
    _inject_deps()

    response = otp_send_handler.handler(_event({"mobile": "9876543210"}), None)

    assert response["statusCode"] == 400


def test_send_otp_malformed_json_returns_400():
    _inject_deps()

    response = otp_send_handler.handler({"body": "{not json", "headers": {}}, None)

    assert response["statusCode"] == 400


def test_send_otp_missing_body_returns_400():
    _inject_deps()

    response = otp_send_handler.handler({"headers": {}}, None)

    assert response["statusCode"] == 400


def test_send_otp_rate_limited_returns_429():
    _inject_deps(rate_limiter=RaisingRateLimiter())

    response = otp_send_handler.handler(_event({"mobile": "+919876543210"}), None)

    assert response["statusCode"] == 429
    body = json.loads(response["body"])
    assert body["data"]["errorCode"] == "RATE_LIMIT_EXCEEDED"


def test_send_otp_resend_within_cooldown_does_not_republish():
    cognito, publisher, _ = _inject_deps()

    otp_send_handler.handler(_event({"mobile": "+919876543210"}), None)
    otp_send_handler.handler(_event({"mobile": "+919876543210"}), None)

    # Second call is a resend within cooldown — must not publish a new SMS.
    assert len(publisher.calls) == 1


def test_send_otp_publish_failure_returns_502_and_does_not_block_immediate_retry():
    publisher = FakePublisher(fail_times=1)
    _inject_deps(publisher=publisher)

    first = otp_send_handler.handler(_event({"mobile": "+919876543210"}), None)
    assert first["statusCode"] == 502
    body = json.loads(first["body"])
    assert body["data"]["errorCode"] == "NOTIFICATION_PUBLISH_FAILED"

    # An immediate retry (well within the resend cooldown) must actually
    # attempt to publish again, not be silently swallowed as a cooldown
    # resend for an OTP that was never delivered.
    second = otp_send_handler.handler(_event({"mobile": "+919876543210"}), None)
    assert second["statusCode"] == 200
    assert len(publisher.calls) == 2
