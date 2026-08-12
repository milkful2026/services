import pytest
from fastapi.testclient import TestClient

from domain.exceptions import InvalidPincodeError, ServiceUnavailableError
from domain.models import ServiceabilityResult, Slot
from handlers.app import app
from handlers.dependencies import get_serviceability_service
from handlers.health import consumer_health


class FakeService:
    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls: list[tuple] = []

    def check(self, pincode, lat, lng):
        self.calls.append((pincode, lat, lng))
        if self.raises:
            raise self.raises
        return self.result


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _override(fake_service: FakeService) -> FakeService:
    app.dependency_overrides[get_serviceability_service] = lambda: fake_service
    return fake_service


@pytest.fixture
def client():
    return TestClient(app)


def test_public_check_serviceable(client):
    _override(
        FakeService(
            result=ServiceabilityResult(
                serviceable=True,
                zone_id="blr-central",
                zone_name="Bangalore Central",
                slots=[Slot(id="morning-6-8", label="Morning 6-8 AM")],
            )
        )
    )

    response = client.get("/v1/serviceability/check", params={"pincode": "560001"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["serviceable"] is True
    assert data["zoneId"] == "blr-central"
    assert data["slots"] == [{"id": "morning-6-8", "label": "Morning 6-8 AM"}]


def test_public_check_not_serviceable(client):
    _override(
        FakeService(
            result=ServiceabilityResult(
                serviceable=False, message="We don't deliver to this area yet"
            )
        )
    )

    response = client.get("/v1/serviceability/check", params={"pincode": "110001"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["serviceable"] is False
    assert data["zoneId"] is None
    assert data["message"] == "We don't deliver to this area yet"


def test_public_check_invalid_pincode_returns_400(client):
    _override(FakeService(raises=InvalidPincodeError("bad pincode")))

    response = client.get("/v1/serviceability/check", params={"pincode": "abc"})

    assert response.status_code == 400
    assert response.json()["data"]["errorCode"] == "INVALID_PINCODE"


def test_public_check_missing_pincode_returns_422(client):
    response = client.get("/v1/serviceability/check")

    assert response.status_code == 422


def test_public_check_passes_lat_lng_through(client):
    fake = _override(FakeService(result=ServiceabilityResult(serviceable=True, zone_id="z")))

    client.get("/v1/serviceability/check", params={"pincode": "560001", "lat": 12.97, "lng": 77.59})

    assert fake.calls == [("560001", 12.97, 77.59)]


def test_public_check_db_unavailable_returns_503(client):
    _override(FakeService(raises=ServiceUnavailableError("db down")))

    response = client.get("/v1/serviceability/check", params={"pincode": "560001"})

    assert response.status_code == 503
    assert response.json()["data"]["errorCode"] == "SERVICE_UNAVAILABLE"


def test_healthz_does_not_touch_serviceability_service(client):
    # Deliberately no dependency override — if /healthz called through to
    # ServiceabilityService it would blow up with no DB/cache configured.
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_reports_unhealthy_when_consumer_thread_has_died(client):
    consumer_health.alive = False
    try:
        response = client.get("/healthz")
    finally:
        consumer_health.alive = True

    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"


def test_internal_check_uses_same_service_and_response_shape(client):
    _override(
        FakeService(
            result=ServiceabilityResult(serviceable=True, zone_id="blr-central", zone_name="Bangalore Central")
        )
    )

    response = client.get("/v1/internal/serviceability/check", params={"pincode": "560001"})

    assert response.status_code == 200
    assert response.json()["data"]["zoneId"] == "blr-central"
