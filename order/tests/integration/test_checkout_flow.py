"""MA-136 end-to-end over HTTP: POST /orders/checkout -> DB + outbox
(schema-validated) -> GET /orders/me, with fakes standing in for Cart,
User, Pricing, Wallet and Subscription."""

import jsonschema
import jwt
import pytest
from fastapi.testclient import TestClient
from shared.events import load_schema

from handlers.app import app
from handlers.dependencies import get_checkout_service, get_order_service


@pytest.fixture
def client(service, checkout_service):
    get_order_service.cache_clear()
    get_checkout_service.cache_clear()
    app.dependency_overrides[get_order_service] = lambda: service
    app.dependency_overrides[get_checkout_service] = lambda: checkout_service
    yield TestClient(app)
    app.dependency_overrides.clear()


def _headers(key="key-00000001", sub="user-1"):
    token = jwt.encode({"sub": sub}, "x", algorithm="HS256")
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


@pytest.fixture
def mixed_cart(cart_client):
    cart_client.items = [
        {"id": "li-1", "productId": "buffalo-milk", "quantity": 1, "frequency": "ONE_TIME",
         "startDate": None, "slotId": None},
        {"id": "li-2", "productId": "cow-milk", "quantity": 2, "frequency": "DAILY",
         "startDate": "2099-01-01", "slotId": "slot-am"},
    ]
    return cart_client


def test_checkout_places_order_and_it_is_readable(client, repo, mixed_cart):
    response = client.post("/orders/checkout", json={"cartVersion": 3}, headers=_headers())

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == "COMPLETED"
    assert body["order"]["status"] == "CONFIRMED"
    assert [s["status"] for s in body["subscriptions"]] == ["CREATED"]

    [event] = [e for e in repo.fetch_unpublished() if e["event_type"] == "OrderConfirmed"]
    jsonschema.validate(event["payload"], load_schema("OrderConfirmed"))

    mine = client.get("/orders/me", headers=_headers()).json()["data"]["items"]
    assert mine[0]["orderId"] == body["order"]["orderId"]
    assert mine[0]["source"] == "CHECKOUT"
    assert mine[0]["subscriptionId"] is None
    assert mine[0]["items"] == [{"productId": "buffalo-milk", "quantity": 1}]


def test_same_key_replays_the_identical_body(client, wallet_client, mixed_cart):
    first = client.post("/orders/checkout", json={"cartVersion": 3}, headers=_headers())
    again = client.post("/orders/checkout", json={"cartVersion": 3}, headers=_headers())

    assert again.status_code == 200
    assert again.json()["data"] == first.json()["data"]
    assert len(wallet_client.calls) == 1


def test_missing_idempotency_key_is_400(client, mixed_cart):
    headers = _headers()
    headers.pop("Idempotency-Key")

    response = client.post("/orders/checkout", json={"cartVersion": 3}, headers=headers)

    assert response.status_code == 400
    assert response.json()["data"]["errorCode"] == "VALIDATION_ERROR"


def test_short_idempotency_key_is_400(client, mixed_cart):
    response = client.post(
        "/orders/checkout", json={"cartVersion": 3}, headers=_headers(key="short")
    )

    assert response.status_code == 400


def test_insufficient_balance_is_402_with_shortfall_in_data(client, wallet_client, mixed_cart):
    wallet_client.balance_paise = 1000

    response = client.post("/orders/checkout", json={"cartVersion": 3}, headers=_headers())

    assert response.status_code == 402
    data = response.json()["data"]
    assert data["errorCode"] == "INSUFFICIENT_BALANCE"
    assert data["shortfallPaise"] == 5500 + 50_000 - 1000


def test_declined_charge_payment_failed_event_validates(client, repo, wallet_client, mixed_cart):
    wallet_client.result_status = "INSUFFICIENT_BALANCE"

    response = client.post("/orders/checkout", json={"cartVersion": 3}, headers=_headers())

    assert response.status_code == 402
    [event] = [e for e in repo.fetch_unpublished() if e["event_type"] == "OrderPaymentFailed"]
    jsonschema.validate(event["payload"], load_schema("OrderPaymentFailed"))


def test_line_invalid_lists_the_bad_lines(client, cart_client):
    cart_client.items = [
        {"id": "li-9", "productId": "cow-milk", "quantity": 1, "frequency": "DAILY",
         "startDate": "2099-01-01", "slotId": None},
    ]

    response = client.post("/orders/checkout", json={"cartVersion": 3}, headers=_headers())

    assert response.status_code == 422
    assert response.json()["data"]["lines"] == [{"lineId": "li-9", "reason": "SLOT_MISSING"}]


def test_requires_a_bearer_token(client, mixed_cart):
    response = client.post(
        "/orders/checkout", json={"cartVersion": 3}, headers={"Idempotency-Key": "key-00000001"}
    )

    assert response.status_code == 401
