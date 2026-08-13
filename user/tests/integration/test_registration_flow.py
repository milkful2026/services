"""Handler -> domain -> datastore integration tests, real handler
functions wired against SQLite (Aurora double), moto (Cognito/
EventBridge), and `responses`-mocked Inventory HTTP calls."""

import json

import pytest
import responses as responses_lib

import handlers.delivery_slots_handler as delivery_slots_handler
import handlers.get_me_handler as get_me_handler
import handlers.outbox_publisher_handler as outbox_publisher_handler
import handlers.register_handler as register_handler
from adapters.cognito_attribute_adapter import CognitoAttributeAdapter
from adapters.inventory_client_adapter import HttpInventoryClient
from adapters.outbox_event_publisher import EventBridgeOutboxPublisher
from adapters.user_repository import SqlAlchemyUserRepository, zone_slots_table
from domain.registration_service import RegistrationService

INVENTORY_CHECK_URL = "http://inventory.internal.test/v1/internal/serviceability/check"


@pytest.fixture(autouse=True)
def _reset_handler_deps():
    register_handler._deps = None
    delivery_slots_handler._deps = None
    outbox_publisher_handler._deps = None
    get_me_handler._deps = None
    yield
    register_handler._deps = None
    delivery_slots_handler._deps = None
    outbox_publisher_handler._deps = None
    get_me_handler._deps = None


@pytest.fixture
def wired_env(sqlite_engine, cognito_user_pool, event_bus):
    repository = SqlAlchemyUserRepository(engine=sqlite_engine)
    inventory_client = HttpInventoryClient(
        base_url="http://inventory.internal.test", timeout_seconds=1.0
    )
    cognito_attributes = CognitoAttributeAdapter(
        user_pool_id=cognito_user_pool["pool_id"], region_name="ap-south-1"
    )
    registration_service = RegistrationService(repository, inventory_client, cognito_attributes)
    register_handler._deps = {"registration_service": registration_service}
    delivery_slots_handler._deps = {"registration_service": registration_service}
    get_me_handler._deps = {"registration_service": registration_service}

    publisher = EventBridgeOutboxPublisher(
        event_bus_name="default", event_source="user", region_name="ap-south-1"
    )
    outbox_publisher_handler._deps = {
        "repository": repository,
        "publisher": publisher,
        "settings": type("S", (), {"outbox_batch_size": 25})(),
    }
    return repository


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
    "consents": [
        {"type": "TERMS", "version": "2026-01", "acceptedAt": "2026-07-20T10:00:00Z"},
        {"type": "PRIVACY", "version": "2026-01", "acceptedAt": "2026-07-20T10:00:00Z"},
    ],
}


def _event(body: dict, sub="sub-1", mobile="+919876543210") -> dict:
    return {
        "body": json.dumps(body),
        "headers": {"x-request-id": "corr-1"},
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub, "phone_number": mobile}}}},
    }


def _mock_inventory_serviceable(serviceable: bool = True) -> None:
    responses_lib.get(
        INVENTORY_CHECK_URL,
        json={"requestId": "r1", "status": "success", "data": {"serviceable": serviceable}},
        status=200,
    )


@responses_lib.activate
def test_full_registration_happy_path_and_outbox_publish(wired_env):
    _mock_inventory_serviceable(True)

    response = register_handler.handler(_event(_VALID_BODY), None)

    assert response["statusCode"] == 201
    data = json.loads(response["body"])["data"]
    assert data["userId"]
    assert data["walletStatus"] == "PENDING"

    # Outbox row should exist, unpublished, until the publisher runs.
    unpublished = wired_env.get_unpublished_outbox_events(limit=10)
    assert len(unpublished) == 1

    publish_result = outbox_publisher_handler.handler({}, None)
    assert publish_result == {"publishedCount": 1, "failedCount": 0}
    assert wired_env.get_unpublished_outbox_events(limit=10) == []


@responses_lib.activate
def test_duplicate_registration_is_idempotent(wired_env):
    _mock_inventory_serviceable(True)
    _mock_inventory_serviceable(True)

    first = register_handler.handler(_event(_VALID_BODY), None)
    second = register_handler.handler(_event(_VALID_BODY), None)

    assert first["statusCode"] == 201
    assert second["statusCode"] == 200
    first_user_id = json.loads(first["body"])["data"]["userId"]
    second_user_id = json.loads(second["body"])["data"]["userId"]
    assert first_user_id == second_user_id

    # Still only one outbox row despite two register calls.
    unpublished = wired_env.get_unpublished_outbox_events(limit=10)
    assert len(unpublished) == 1


@responses_lib.activate
def test_non_serviceable_address_returns_422_and_writes_nothing(wired_env):
    _mock_inventory_serviceable(False)

    response = register_handler.handler(_event(_VALID_BODY), None)

    assert response["statusCode"] == 422
    assert wired_env.get_unpublished_outbox_events(limit=10) == []


def test_delivery_slots_reads_seeded_zone_slots(wired_env, sqlite_engine):
    with sqlite_engine.begin() as conn:
        conn.execute(
            zone_slots_table.insert().values(
                zone_id="blr-central", slot_id="morning-6-8", label="Morning 6-8 AM", active=True
            )
        )

    response = delivery_slots_handler.handler(
        {"queryStringParameters": {"zoneId": "blr-central"}, "headers": {}}, None
    )

    assert response["statusCode"] == 200
    data = json.loads(response["body"])["data"]
    assert data == [{"id": "morning-6-8", "label": "Morning 6-8 AM", "available": True}]


def _get_me_event(sub: str) -> dict:
    return {
        "headers": {"x-request-id": "corr-2"},
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub}}}},
    }


@responses_lib.activate
def test_get_me_after_registration_returns_b2c_profile(wired_env):
    _mock_inventory_serviceable(True)
    register_response = register_handler.handler(_event(_VALID_BODY, sub="sub-me-1"), None)
    user_id = json.loads(register_response["body"])["data"]["userId"]

    response = get_me_handler.handler(_get_me_event("sub-me-1"), None)

    assert response["statusCode"] == 200
    data = json.loads(response["body"])["data"]
    assert data["userId"] == user_id
    assert data["name"] == "Priya Sharma"
    assert data["mobile"] == "+919876543210"
    assert data["accountType"] == "B2C"
    assert data["defaultAddressId"]


def test_get_me_for_unregistered_sub_returns_404(wired_env):
    response = get_me_handler.handler(_get_me_event("never-registered-sub"), None)

    assert response["statusCode"] == 404
    assert json.loads(response["body"])["data"]["errorCode"] == "USER_NOT_FOUND"
