"""Handler -> domain -> datastore integration tests, moto-backed end to
end. Exercises the real handler functions (not fakes) wired against
moto's DynamoDB/Cognito/EventBridge and a fakeredis rate limiter — the
only thing not real here is Redis (no local server in this environment)
and the EventBridge publish (captured instead of asserted, since that
adapter has its own dedicated tests)."""

import json
from datetime import timedelta

import pytest
from freezegun import freeze_time

import handlers.otp_send_handler as otp_send_handler
import handlers.otp_verify_handler as otp_verify_handler


@pytest.fixture(autouse=True)
def _reset_handler_deps():
    otp_send_handler._deps = None
    otp_verify_handler._deps = None
    yield
    otp_send_handler._deps = None
    otp_verify_handler._deps = None


@pytest.fixture
def wired_env(otp_table, cognito_user_pool, event_bus, fake_redis, monkeypatch):
    # otp_send_handler imported build_redis_client by reference at module
    # load time, so the origin module's attribute must be patched via the
    # name as it's bound in the *consuming* module, not the source module.
    monkeypatch.setattr(otp_send_handler, "build_redis_client", lambda *a, **kw: fake_redis)
    captured_otps: dict[str, str] = {}

    def _capture_publish(self, mobile, otp, correlation_id):
        captured_otps[mobile] = otp

    monkeypatch.setattr(
        "adapters.notification_publisher.EventBridgeNotificationPublisher.publish_otp_requested",
        _capture_publish,
    )
    return captured_otps


def _event(body: dict) -> dict:
    return {"body": json.dumps(body), "headers": {"x-request-id": "corr-1"}}


def test_full_registration_happy_path(wired_env):
    captured_otps = wired_env
    mobile = "+919876543210"

    send_response = otp_send_handler.handler(_event({"mobile": mobile}), None)
    assert send_response["statusCode"] == 200
    request_id = json.loads(send_response["body"])["data"]["requestId"]
    otp = captured_otps[mobile]

    verify_response = otp_verify_handler.handler(
        _event({"mobile": mobile, "otp": otp, "requestId": request_id}), None
    )

    assert verify_response["statusCode"] == 200
    data = json.loads(verify_response["body"])["data"]
    assert data["isNewUser"] is True
    assert data["accessToken"]
    assert data["refreshToken"]


def test_registration_rejects_wrong_otp(wired_env):
    mobile = "+919876543211"
    send_response = otp_send_handler.handler(_event({"mobile": mobile}), None)
    request_id = json.loads(send_response["body"])["data"]["requestId"]

    verify_response = otp_verify_handler.handler(
        _event({"mobile": mobile, "otp": "000000", "requestId": request_id}), None
    )

    assert verify_response["statusCode"] == 401
    assert json.loads(verify_response["body"])["data"]["errorCode"] == "OTP_INVALID"


def test_registration_rate_limited_after_max_sends(wired_env):
    mobile = "+919876543212"
    with freeze_time("2026-01-01 00:00:00") as frozen:
        for _ in range(3):
            response = otp_send_handler.handler(_event({"mobile": mobile}), None)
            assert response["statusCode"] == 200
            frozen.tick(delta=timedelta(seconds=31))  # past resend cooldown, still within 15-min window

        response = otp_send_handler.handler(_event({"mobile": mobile}), None)

    assert response["statusCode"] == 429
    assert json.loads(response["body"])["data"]["errorCode"] == "RATE_LIMIT_EXCEEDED"


def test_send_otp_for_already_verified_mobile_returns_409(wired_env):
    captured_otps = wired_env
    mobile = "+919876543213"

    send_response = otp_send_handler.handler(_event({"mobile": mobile}), None)
    request_id = json.loads(send_response["body"])["data"]["requestId"]
    otp_verify_handler.handler(
        _event({"mobile": mobile, "otp": captured_otps[mobile], "requestId": request_id}), None
    )

    # Simulate a fresh Lambda invocation against the same backing resources.
    otp_send_handler._deps = None
    second_send = otp_send_handler.handler(_event({"mobile": mobile}), None)

    assert second_send["statusCode"] == 409
    body = json.loads(second_send["body"])
    assert body["data"]["errorCode"] == "USER_EXISTS"
    assert body["data"]["redirect"] == "login"
