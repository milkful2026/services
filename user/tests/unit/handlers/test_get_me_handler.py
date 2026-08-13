import json

import pytest

import handlers.get_me_handler as get_me_handler
from domain.exceptions import ExternalServiceUnavailableError, UserNotFoundError
from domain.models import UserProfile


class FakeRegistrationService:
    def __init__(self, profile=None, raises=None):
        self.profile = profile or UserProfile(
            user_id="user-1",
            name="Priya Sharma",
            mobile="+919876543210",
            account_type="B2C",
            default_address_id="addr-1",
        )
        self.raises = raises
        self.calls: list[str] = []
        self.correlation_id = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def get_my_profile(self, cognito_sub: str):
        self.calls.append(cognito_sub)
        if self.raises:
            raise self.raises
        return self.profile


@pytest.fixture(autouse=True)
def _reset_deps():
    get_me_handler._deps = None
    yield
    get_me_handler._deps = None


def _inject(profile=None, raises=None):
    service = FakeRegistrationService(profile=profile, raises=raises)
    get_me_handler._deps = {"registration_service": service}
    return service


def _event(sub: str | None = "sub-123") -> dict:
    claims = {"sub": sub} if sub else {}
    return {"headers": {"x-request-id": "corr-1"}, "requestContext": {"authorizer": {"jwt": {"claims": claims}}}}


def test_get_me_success_returns_profile():
    service = _inject()

    response = get_me_handler.handler(_event(), None)

    assert response["statusCode"] == 200
    data = json.loads(response["body"])["data"]
    assert data == {
        "userId": "user-1",
        "name": "Priya Sharma",
        "mobile": "+919876543210",
        "accountType": "B2C",
        "defaultAddressId": "addr-1",
    }
    assert service.calls == ["sub-123"]


def test_get_me_missing_jwt_claims_returns_400():
    _inject()

    response = get_me_handler.handler(_event(sub=None), None)

    assert response["statusCode"] == 400


def test_get_me_no_matching_user_returns_404():
    _inject(raises=UserNotFoundError("No profile found for this account"))

    response = get_me_handler.handler(_event(), None)

    assert response["statusCode"] == 404
    assert json.loads(response["body"])["data"]["errorCode"] == "USER_NOT_FOUND"


def test_get_me_db_unavailable_returns_503():
    _inject(raises=ExternalServiceUnavailableError("db down"))

    response = get_me_handler.handler(_event(), None)

    assert response["statusCode"] == 503


def test_get_me_unexpected_exception_returns_500():
    _inject(raises=RuntimeError("boom"))

    response = get_me_handler.handler(_event(), None)

    assert response["statusCode"] == 500
