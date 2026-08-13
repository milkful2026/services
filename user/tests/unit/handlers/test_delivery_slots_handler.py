import json

import pytest

import handlers.delivery_slots_handler as delivery_slots_handler
from domain.exceptions import ExternalServiceUnavailableError
from domain.models import DeliverySlot


class FakeRegistrationService:
    def __init__(self, slots=None, raises=None):
        self.slots = slots or []
        self.raises = raises
        self.calls = []
        self.correlation_id = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def get_delivery_slots(self, zone_id):
        self.calls.append(zone_id)
        if self.raises:
            raise self.raises
        return self.slots


@pytest.fixture(autouse=True)
def _reset_deps():
    delivery_slots_handler._deps = None
    yield
    delivery_slots_handler._deps = None


def _inject(slots=None, raises=None):
    service = FakeRegistrationService(slots=slots, raises=raises)
    delivery_slots_handler._deps = {"registration_service": service}
    return service


def test_get_slots_success():
    _inject(slots=[DeliverySlot(id="morning-6-8", label="Morning 6-8 AM")])

    response = delivery_slots_handler.handler(
        {"queryStringParameters": {"zoneId": "blr-central"}, "headers": {}}, None
    )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["data"] == [{"id": "morning-6-8", "label": "Morning 6-8 AM", "available": True}]


def test_get_slots_missing_zone_id_returns_400():
    _inject()

    response = delivery_slots_handler.handler({"queryStringParameters": {}, "headers": {}}, None)

    assert response["statusCode"] == 400


def test_get_slots_missing_query_params_key_returns_400():
    _inject()

    response = delivery_slots_handler.handler({"headers": {}}, None)

    assert response["statusCode"] == 400


def test_get_slots_service_unavailable_returns_503():
    _inject(raises=ExternalServiceUnavailableError("db down"))

    response = delivery_slots_handler.handler(
        {"queryStringParameters": {"zoneId": "blr-central"}, "headers": {}}, None
    )

    assert response["statusCode"] == 503


def test_get_slots_unexpected_exception_returns_500():
    _inject(raises=RuntimeError("boom"))

    response = delivery_slots_handler.handler(
        {"queryStringParameters": {"zoneId": "blr-central"}, "headers": {}}, None
    )

    assert response["statusCode"] == 500
