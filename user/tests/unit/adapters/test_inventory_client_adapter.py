import pytest
import responses as responses_lib

from adapters.inventory_client_adapter import HttpInventoryClient
from domain.exceptions import ExternalServiceUnavailableError, ValidationError

BASE_URL = "http://inventory.internal.test"
CHECK_URL = f"{BASE_URL}/v1/internal/serviceability/check"


@pytest.fixture
def client():
    return HttpInventoryClient(base_url=BASE_URL, timeout_seconds=1.0, max_retries=2, backoff_base_seconds=0.01)


@responses_lib.activate
def test_check_serviceability_true(client):
    responses_lib.get(
        CHECK_URL,
        json={"requestId": "r1", "status": "success", "data": {"serviceable": True, "zoneId": "blr-central"}},
        status=200,
    )

    assert client.check_serviceability("560001", 12.97, 77.59) is True


@responses_lib.activate
def test_check_serviceability_false(client):
    responses_lib.get(
        CHECK_URL,
        json={"requestId": "r1", "status": "success", "data": {"serviceable": False}},
        status=200,
    )

    assert client.check_serviceability("110001", 28.6, 77.2) is False


@responses_lib.activate
def test_check_serviceability_400_raises_validation_error(client):
    responses_lib.get(
        CHECK_URL,
        json={"requestId": "r1", "status": "error", "data": {"errorCode": "INVALID_PINCODE"}},
        status=400,
    )

    with pytest.raises(ValidationError):
        client.check_serviceability("abc", 0, 0)


@responses_lib.activate
def test_check_serviceability_retries_then_raises_on_persistent_500(client):
    responses_lib.get(CHECK_URL, status=500)
    responses_lib.get(CHECK_URL, status=500)
    responses_lib.get(CHECK_URL, status=500)

    with pytest.raises(ExternalServiceUnavailableError):
        client.check_serviceability("560001", 12.97, 77.59)

    assert len(responses_lib.calls) == 3


@responses_lib.activate
def test_check_serviceability_succeeds_after_transient_500(client):
    responses_lib.get(CHECK_URL, status=500)
    responses_lib.get(
        CHECK_URL,
        json={"requestId": "r1", "status": "success", "data": {"serviceable": True}},
        status=200,
    )

    assert client.check_serviceability("560001", 12.97, 77.59) is True
