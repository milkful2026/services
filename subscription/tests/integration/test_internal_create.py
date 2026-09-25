"""MA-136 FR-11 — POST /internal/subscriptions, the route Order Service's
cart checkout creates each subscription line through."""

from datetime import date, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from handlers.app import app
from handlers.dependencies import get_subscription_service


@pytest.fixture
def client(service):
    get_subscription_service.cache_clear()
    app.dependency_overrides[get_subscription_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def _body(**overrides):
    body = {
        "userId": "user-1",
        "productId": "prod-1",
        "quantity": 2,
        "schedule": {"type": "DAILY"},
        # +3 days: see test_subscription_http.py's _create_body for why not +1.
        "startDate": (date.today() + timedelta(days=3)).isoformat(),
        "slotId": "slot-1",
        "idempotencyKey": "checkout:chk_1:li-1",
        "correlationId": "corr-1",
    }
    body.update(overrides)
    return body


def _bearer(sub):
    return {"Authorization": "Bearer " + jwt.encode({"sub": sub}, "x", algorithm="HS256")}


def test_creates_a_subscription_owned_by_the_body_user_without_a_jwt(client):
    response = client.post("/internal/subscriptions", json=_body())

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "ACTIVE"
    assert data["nextDeliveryDate"] is not None

    # Owned by the body's userId: visible to that user's own JWT read.
    detail = client.get(f"/subscriptions/{data['subscriptionId']}", headers=_bearer("user-1"))
    assert detail.status_code == 200
    assert detail.json()["data"]["slotId"] == "slot-1"
    assert detail.json()["data"]["quantity"] == 2


def test_same_idempotency_key_returns_the_same_subscription(client):
    first = client.post("/internal/subscriptions", json=_body()).json()["data"]
    second = client.post("/internal/subscriptions", json=_body()).json()["data"]

    assert first["subscriptionId"] == second["subscriptionId"]
    mine = client.get("/subscriptions/me", headers=_bearer("user-1")).json()["data"]
    assert len(mine["subscriptions"]) == 1


def test_matches_the_public_create_for_the_same_input(client):
    internal = client.post("/internal/subscriptions", json=_body()).json()["data"]
    public_body = _body(idempotencyKey="public-1")
    for key in ("userId", "correlationId"):
        public_body.pop(key)
    public = client.post(
        "/subscriptions", json=public_body, headers=_bearer("user-1")
    ).json()["data"]

    assert set(internal) == set(public)
    assert internal["nextDeliveryDate"] == public["nextDeliveryDate"]


def test_ineligible_product_is_422(client, catalog_client):
    catalog_client.seed("paneer", subscription_eligible=False)

    response = client.post("/internal/subscriptions", json=_body(productId="paneer"))

    assert response.status_code == 422
    assert response.json()["data"]["errorCode"] == "PRODUCT_NOT_ELIGIBLE"


def test_missing_user_id_is_rejected(client):
    body = _body()
    body.pop("userId")

    response = client.post("/internal/subscriptions", json=body)

    assert response.status_code in (400, 422)
