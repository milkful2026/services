import json

import pytest

import handlers.register_handler as register_handler
from domain.exceptions import ExternalServiceUnavailableError, NotServiceableError, ValidationError
from domain.models import RegistrationResult


class FakeRegistrationService:
    def __init__(self, result=None, raises=None, mobile="+919876543210", resolve_mobile_raises=None):
        self.result = result or RegistrationResult(
            user_id="user-1", default_address_id="addr-1", is_new_user=True
        )
        self.raises = raises
        self.mobile = mobile
        self.resolve_mobile_raises = resolve_mobile_raises
        self.calls = []
        self.resolve_mobile_calls = []
        self.correlation_id = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def resolve_mobile(self, cognito_sub: str) -> str:
        self.resolve_mobile_calls.append(cognito_sub)
        if self.resolve_mobile_raises:
            raise self.resolve_mobile_raises
        return self.mobile

    def register(self, request):
        self.calls.append(request)
        if self.raises:
            raise self.raises
        return self.result


@pytest.fixture(autouse=True)
def _reset_deps():
    register_handler._deps = None
    yield
    register_handler._deps = None


def _inject(result=None, raises=None, mobile="+919876543210", resolve_mobile_raises=None):
    service = FakeRegistrationService(
        result=result, raises=raises, mobile=mobile, resolve_mobile_raises=resolve_mobile_raises
    )
    register_handler._deps = {"registration_service": service}
    return service


_VALID_BODY = {
    "name": "Priya Sharma",
    "addresses": [
        {
            "lines": ["12 MG Road"],
            "city": "Bangalore",
            "state": "Karnataka",
            "pincode": "560001",
            "lat": 12.9716,
            "lng": 77.5946,
            "isDefault": True,
        }
    ],
    "preferredSlotId": "morning-6-8",
    "consents": [
        {"type": "TERMS", "version": "2026-01", "acceptedAt": "2026-07-20T10:00:00Z"},
        {"type": "PRIVACY", "version": "2026-01", "acceptedAt": "2026-07-20T10:00:00Z"},
    ],
}


def _event(body: dict, sub: str = "sub-1") -> dict:
    return {
        "body": json.dumps(body),
        "headers": {"x-request-id": "corr-1"},
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub}}}},
    }


def test_register_new_user_returns_201():
    service = _inject()

    response = register_handler.handler(_event(_VALID_BODY), None)

    assert response["statusCode"] == 201
    body = json.loads(response["body"])
    assert body["data"]["userId"] == "user-1"
    assert len(service.calls) == 1
    assert service.calls[0].cognito_sub == "sub-1"
    assert service.calls[0].mobile == "+919876543210"


def test_register_existing_user_returns_200_not_201():
    _inject(
        result=RegistrationResult(user_id="user-1", default_address_id="addr-1", is_new_user=False)
    )

    response = register_handler.handler(_event(_VALID_BODY), None)

    assert response["statusCode"] == 200


def test_register_missing_jwt_claims_returns_400():
    _inject()
    event = _event(_VALID_BODY)
    event["requestContext"]["authorizer"]["jwt"]["claims"] = {}

    response = register_handler.handler(event, None)

    assert response["statusCode"] == 400


def test_register_malformed_body_returns_400():
    _inject()

    response = register_handler.handler(
        {
            "body": "not json",
            "headers": {},
            "requestContext": {"authorizer": {"jwt": {"claims": {"sub": "s"}}}},
        },
        None,
    )

    assert response["statusCode"] == 400


def test_register_mobile_resolution_failure_returns_503():
    _inject(resolve_mobile_raises=ExternalServiceUnavailableError("cognito down"))

    response = register_handler.handler(_event(_VALID_BODY), None)

    assert response["statusCode"] == 503


def test_register_resolves_mobile_via_cognito_before_calling_register():
    service = _inject(mobile="+919876543210")

    register_handler.handler(_event(_VALID_BODY, sub="sub-42"), None)

    assert service.resolve_mobile_calls == ["sub-42"]
    assert service.calls[0].mobile == "+919876543210"


def test_register_not_serviceable_returns_422():
    _inject(raises=NotServiceableError("not serviceable"))

    response = register_handler.handler(_event(_VALID_BODY), None)

    assert response["statusCode"] == 422


def test_register_validation_error_from_service_returns_400():
    _inject(raises=ValidationError("missing consent"))

    response = register_handler.handler(_event(_VALID_BODY), None)

    assert response["statusCode"] == 400


def test_register_sets_correlation_id_from_header_on_service():
    service = _inject()

    register_handler.handler(_event(_VALID_BODY), None)

    assert service.correlation_id == "corr-1"


def test_register_unexpected_exception_returns_500_not_raw_traceback():
    _inject(raises=RuntimeError("boom"))

    response = register_handler.handler(_event(_VALID_BODY), None)

    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert body["status"] == "error"
