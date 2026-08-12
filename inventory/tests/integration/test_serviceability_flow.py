"""End-to-end: FastAPI TestClient -> real ServiceabilityService -> real
SqlAlchemyZoneRepository (SQLite) + real RedisZoneCacheAdapter (fakeredis)
+ real ZoneUpdateConsumer (moto SQS). No fakes/mocks of our own code —
only the DB/Redis/SQS backends themselves are test doubles."""

import json

import pytest
from fastapi.testclient import TestClient

from adapters.zone_cache_adapter import RedisZoneCacheAdapter
from adapters.zone_repository import SqlAlchemyZoneRepository
from adapters.zone_update_consumer import ZoneUpdateConsumer
from domain.serviceability_service import ServiceabilityService
from handlers.app import app
from handlers.dependencies import get_serviceability_service
from tests.conftest import seed_zone


@pytest.fixture
def wired_service(sqlite_engine, fake_redis):
    repository = SqlAlchemyZoneRepository(engine=sqlite_engine)
    cache = RedisZoneCacheAdapter(redis_client=fake_redis)
    service = ServiceabilityService(repository, cache, cache_ttl_seconds=900)
    app.dependency_overrides[get_serviceability_service] = lambda: service
    yield service, cache
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    return TestClient(app)


def test_serviceable_pincode_returns_zone_and_slots(sqlite_engine, wired_service, client):
    seed_zone(sqlite_engine)

    response = client.get("/v1/serviceability/check", params={"pincode": "560001"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["serviceable"] is True
    assert data["zoneId"] == "blr-central"
    assert data["slots"] == [{"id": "morning-6-8", "label": "Morning 6-8 AM"}]


def test_non_serviceable_pincode(sqlite_engine, wired_service, client):
    seed_zone(sqlite_engine)

    response = client.get("/v1/serviceability/check", params={"pincode": "110001"})

    assert response.status_code == 200
    assert response.json()["data"]["serviceable"] is False


def test_second_request_served_from_cache_not_repository(sqlite_engine, wired_service, client, monkeypatch):
    seed_zone(sqlite_engine)
    service, _ = wired_service

    client.get("/v1/serviceability/check", params={"pincode": "560001"})

    # Break the repository so a second DB hit would raise — if the second
    # request still succeeds, it proves the cache actually served it.
    def _raise(*args, **kwargs):
        raise Exception("should not be called")

    monkeypatch.setattr(service._zone_repository, "get_active_zones", _raise)

    response = client.get("/v1/serviceability/check", params={"pincode": "560001"})

    assert response.status_code == 200
    assert response.json()["data"]["zoneId"] == "blr-central"


def test_zone_update_event_invalidates_cache(sqlite_engine, wired_service, client, sqs_queue):
    seed_zone(sqlite_engine)
    service, cache = wired_service

    # First check populates the cache.
    first = client.get("/v1/serviceability/check", params={"pincode": "560001"})
    assert first.json()["data"]["zoneName"] == "Bangalore Central"

    # Zone gets renamed in the DB, and a ZoneUpdated event fires.
    from sqlalchemy import update

    from adapters.zone_repository import serviceability_zones_table

    with sqlite_engine.begin() as conn:
        conn.execute(
            update(serviceability_zones_table)
            .where(serviceability_zones_table.c.id == "blr-central")
            .values(name="Bangalore Central Renamed")
        )

    consumer = ZoneUpdateConsumer(
        queue_url=sqs_queue["queue_url"], zone_cache=cache, region_name="ap-south-1"
    )
    sqs_queue["client"].send_message(
        QueueUrl=sqs_queue["queue_url"],
        MessageBody=json.dumps(
            {
                "eventType": "inventory.zone.updated",
                "payload": {"zoneId": "blr-central", "pincodePrefixes": ["5600"]},
            }
        ),
    )
    consumer.poll_once(wait_time_seconds=0)

    # Without invalidation, this would still return the stale cached name.
    second = client.get("/v1/serviceability/check", params={"pincode": "560001"})
    assert second.json()["data"]["zoneName"] == "Bangalore Central Renamed"


def test_internal_route_returns_same_result_as_public(sqlite_engine, wired_service, client):
    seed_zone(sqlite_engine)

    public = client.get("/v1/serviceability/check", params={"pincode": "560001"})
    internal = client.get("/v1/internal/serviceability/check", params={"pincode": "560001"})

    assert public.json()["data"] == internal.json()["data"]
