import json

import pytest

import handlers.internal_address_state_handler as internal_address_state_handler
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
            default_address_state="Karnataka",
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
    internal_address_state_handler._deps = None
    yield
    internal_address_state_handler._deps = None


def _inject(profile=None, raises=None):
    service = FakeRegistrationService(profile=profile, raises=raises)
    internal_address_state_handler._deps = {"registration_service": service}
    return service


def _event(cognito_sub: str | None = "sub-123") -> dict:
    return {
        "headers": {"x-request-id": "corr-1"},
        "queryStringParameters": {"cognitoSub": cognito_sub} if cognito_sub else {},
    }


def test_success_returns_default_address_state():
    service = _inject()

    response = internal_address_state_handler.handler(_event(), None)

    assert response["statusCode"] == 200
    data = json.loads(response["body"])["data"]
    assert data == {"defaultAddressState": "Karnataka"}
    assert service.calls == ["sub-123"]


def test_no_default_address_returns_null_state():
    profile = UserProfile(
        user_id="user-2",
        name="Amit Rao",
        mobile="+919812345670",
        account_type="B2C",
        default_address_id="",
        default_address_state=None,
    )
    _inject(profile=profile)

    response = internal_address_state_handler.handler(_event(), None)

    data = json.loads(response["body"])["data"]
    assert data == {"defaultAddressState": None}


def test_missing_cognito_sub_query_param_returns_400():
    _inject()

    response = internal_address_state_handler.handler(_event(cognito_sub=None), None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["data"]["errorCode"] == "VALIDATION_ERROR"


def test_no_matching_user_returns_404():
    _inject(raises=UserNotFoundError("No profile found for this account"))

    response = internal_address_state_handler.handler(_event(), None)

    assert response["statusCode"] == 404
    assert json.loads(response["body"])["data"]["errorCode"] == "USER_NOT_FOUND"


def test_db_unavailable_returns_503():
    _inject(raises=ExternalServiceUnavailableError("db down"))

    response = internal_address_state_handler.handler(_event(), None)

    assert response["statusCode"] == 503


def test_unexpected_exception_returns_500():
    _inject(raises=RuntimeError("boom"))

    response = internal_address_state_handler.handler(_event(), None)

    assert response["statusCode"] == 500
