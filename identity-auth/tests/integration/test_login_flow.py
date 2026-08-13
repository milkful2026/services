"""Handler -> domain -> datastore integration tests for the MA-21 login
flow, moto-backed end to end — same pattern as test_registration_flow.py.

`revoke_token` is monkeypatched at the boto3-client level for the logout
tests: moto does not implement the RevokeToken action at all (raises a
raw NotImplementedError, not even a ClientError — see
test_cognito_adapter.py's docstring for the same gap at unit level), so
exercising the real call through moto isn't possible here either.
"""

import json
from datetime import timedelta

import pytest
from freezegun import freeze_time

import handlers.login_otp_send_handler as login_otp_send_handler
import handlers.login_otp_verify_handler as login_otp_verify_handler
import handlers.logout_handler as logout_handler
import handlers.otp_send_handler as otp_send_handler
import handlers.otp_verify_handler as otp_verify_handler


@pytest.fixture(autouse=True)
def _reset_handler_deps():
    modules = [
        otp_send_handler,
        otp_verify_handler,
        login_otp_send_handler,
        login_otp_verify_handler,
        logout_handler,
    ]
    for m in modules:
        m._deps = None
    yield
    for m in modules:
        m._deps = None


@pytest.fixture
def wired_env(otp_table, cognito_user_pool, event_bus, fake_redis, monkeypatch):
    # Each handler module imported build_redis_client by reference at
    # module load time — same reasoning as test_registration_flow.py.
    monkeypatch.setattr(otp_send_handler, "build_redis_client", lambda *a, **kw: fake_redis)
    monkeypatch.setattr(login_otp_send_handler, "build_redis_client", lambda *a, **kw: fake_redis)

    captured_otps: dict[str, str] = {}

    def _capture_publish(self, mobile, otp, correlation_id, purpose="REGISTER"):
        captured_otps[mobile] = otp

    monkeypatch.setattr(
        "adapters.notification_publisher.EventBridgeNotificationPublisher.publish_otp_requested",
        _capture_publish,
    )
    return captured_otps


def _event(body: dict, headers: dict | None = None) -> dict:
    return {"body": json.dumps(body), "headers": headers or {"x-request-id": "corr-1"}}


def _register(captured_otps, mobile: str) -> None:
    send = otp_send_handler.handler(_event({"mobile": mobile}), None)
    request_id = json.loads(send["body"])["data"]["requestId"]
    otp_verify_handler.handler(
        _event({"mobile": mobile, "otp": captured_otps[mobile], "requestId": request_id}), None
    )
    otp_send_handler._deps = None  # simulate a fresh Lambda invocation for the next call


def test_full_login_happy_path(wired_env):
    captured_otps = wired_env
    mobile = "+919876543220"
    _register(captured_otps, mobile)

    send = login_otp_send_handler.handler(_event({"mobile": mobile}), None)
    assert send["statusCode"] == 200
    request_id = json.loads(send["body"])["data"]["requestId"]
    otp = captured_otps[mobile]

    verify = login_otp_verify_handler.handler(
        _event({"mobile": mobile, "otp": otp, "requestId": request_id}), None
    )

    assert verify["statusCode"] == 200
    data = json.loads(verify["body"])["data"]
    assert data["accessToken"]
    assert data["refreshToken"]
    assert "isNewUser" not in data


def test_login_otp_send_for_unregistered_mobile_returns_404(wired_env):
    response = login_otp_send_handler.handler(
        _event({"mobile": "+919876543221"}), None
    )

    assert response["statusCode"] == 404
    body = json.loads(response["body"])
    assert body["data"]["errorCode"] == "USER_NOT_FOUND"
    assert body["data"]["redirect"] == "signup"


def test_login_and_registration_otp_counters_are_independent_for_same_mobile(wired_env):
    # Full-stack regression guard for the purpose-scoping bug: register a
    # user, exhaust registration's rate limit for a *different*, not-yet-
    # registered mobile isn't relevant here — instead, confirm that after
    # registering this mobile, 3 login-OTP sends don't get blocked by (or
    # interfere with) anything registration-side, and vice versa isn't
    # reachable since the mobile is already verified.
    captured_otps = wired_env
    mobile = "+919876543222"
    _register(captured_otps, mobile)

    with freeze_time("2026-01-01 00:00:00") as frozen:
        for _ in range(3):
            response = login_otp_send_handler.handler(_event({"mobile": mobile}), None)
            assert response["statusCode"] == 200
            frozen.tick(delta=timedelta(seconds=31))

        exhausted = login_otp_send_handler.handler(_event({"mobile": mobile}), None)

    assert exhausted["statusCode"] == 429


def test_login_verify_wrong_otp_returns_401(wired_env):
    captured_otps = wired_env
    mobile = "+919876543223"
    _register(captured_otps, mobile)

    send = login_otp_send_handler.handler(_event({"mobile": mobile}), None)
    request_id = json.loads(send["body"])["data"]["requestId"]

    response = login_otp_verify_handler.handler(
        _event({"mobile": mobile, "otp": "000000", "requestId": request_id}), None
    )

    assert response["statusCode"] == 401
    assert json.loads(response["body"])["data"]["errorCode"] == "OTP_INVALID"


def test_logout_after_login_returns_204(wired_env, monkeypatch):
    captured_otps = wired_env
    mobile = "+919876543224"
    _register(captured_otps, mobile)

    send = login_otp_send_handler.handler(_event({"mobile": mobile}), None)
    request_id = json.loads(send["body"])["data"]["requestId"]
    verify = login_otp_verify_handler.handler(
        _event({"mobile": mobile, "otp": captured_otps[mobile], "requestId": request_id}), None
    )
    refresh_token = json.loads(verify["body"])["data"]["refreshToken"]

    deps = logout_handler._get_deps()
    monkeypatch.setattr(deps["cognito"]._client, "revoke_token", lambda **kwargs: None)

    response = logout_handler.handler(_event({"refreshToken": refresh_token}), None)

    assert response["statusCode"] == 204
