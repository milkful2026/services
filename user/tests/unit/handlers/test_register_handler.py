import json

import pytest

import handlers.register_handler as register_handler
from domain.exceptions import NotServiceableError, ValidationError
from domain.models import RegistrationResult


class FakeRegistrationService:
    def __init__(self, result=None, raises=None):
        self.result = result or RegistrationResult(
            user_id="user-1", default_address_id="addr-1", is_new_user=True
        )
        self.raises = raises
        self.calls = []

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


def _inject(result=None, raises=None):
    service = FakeRegistrationService(result=result, raises=raises)
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


def _event(body: dict, sub: str = "sub-1", mobile: str = "+919876543210") -> dict:
    return {
        "body": json.dumps(body),
        "headers": {"x-request-id": "corr-1"},
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub, "phone_number": mobile}}}},
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
    _inject(result=RegistrationResult(user_id="user-1", default_address_id="addr-1", is_new_user=False))

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
            "requestContext": {"authorizer": {"jwt": {"claims": {"sub": "s", "phone_number": "+91"}}}},
        },
        None,
    )

    assert response["statusCode"] == 400


def test_register_not_serviceable_returns_422():
    _inject(raises=NotServiceableError("not serviceable"))

    response = register_handler.handler(_event(_VALID_BODY), None)

    assert response["statusCode"] == 422


def test_register_validation_error_from_service_returns_400():
    _inject(raises=ValidationError("missing consent"))

    response = register_handler.handler(_event(_VALID_BODY), None)

    assert response["statusCode"] == 400
