"""End-to-end: SQS message -> consumer -> OrderService -> DB + outbox,
schema-validated, then the FastAPI read surface over the result."""

import json
from datetime import date

import jsonschema
import jwt
import pytest
from fastapi.testclient import TestClient
from shared.events import load_schema

from adapters.order_events_consumer import OrderEventsConsumer
from handlers.app import app
from handlers.dependencies import get_order_service

DELIVERY_DATE = date(2026, 2, 1)


class _FakeSqs:
    def __init__(self, messages):
        self._messages = messages
        self.deleted = []

    def receive_message(self, **_):
        msgs, self._messages = self._messages, []
        return {"Messages": msgs}

    def delete_message(self, QueueUrl, ReceiptHandle):  # noqa: N803
        self.deleted.append(ReceiptHandle)


def _msg(detail):
    return {
        "MessageId": "m1",
        "ReceiptHandle": "rh-1",
        "Body": json.dumps({"detail-type": "SubscriptionOrderDue", "detail": detail}),
    }


def _sod(**overrides):
    detail = {
        "eventId": "e1",
        "occurredAt": "2026-02-01T02:00:00+05:30",
        "subscriptionId": "sub-1",
        "userId": "user-1",
        "productId": "prod-1",
        "quantity": 2,
        "deliveryDate": DELIVERY_DATE.isoformat(),
        "slotId": "slot-1",
        "correlationId": "corr-1",
    }
    detail.update(overrides)
    return detail


def _consume(service, detail):
    c = OrderEventsConsumer.__new__(OrderEventsConsumer)
    c._sqs = _FakeSqs([_msg(detail)])
    c._queue_url = "q"
    c._order_service = service
    c.poll_once()
    return c


@pytest.fixture
def client(service):
    get_order_service.cache_clear()
    app.dependency_overrides[get_order_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def _bearer(sub="user-1"):
    return {"Authorization": "Bearer " + jwt.encode({"sub": sub}, "x", algorithm="HS256")}


def test_confirmed_flow_schema_validated_and_readable(client, service, repo):
    c = _consume(service, _sod())
    assert c._sqs.deleted == ["rh-1"]

    order = repo.get_by_subscription_and_date("sub-1", DELIVERY_DATE)
    assert order.status.value == "CONFIRMED"

    unpub = repo.fetch_unpublished()
    confirmed = [e for e in unpub if e["event_type"] == "OrderConfirmed"]
    assert len(confirmed) == 1
    jsonschema.validate(confirmed[0]["payload"], load_schema("OrderConfirmed"))

    detail = client.get(f"/orders/{order.id}", headers=_bearer()).json()["data"]
    assert detail["status"] == "CONFIRMED"
    assert detail["amountPaise"] == order.amount_paise

    listed = client.get("/orders/me", headers=_bearer()).json()["data"]
    assert len(listed["items"]) == 1
    assert listed["items"][0]["orderId"] == order.id


def test_insufficient_balance_flow_schema_validated(client, service, repo, wallet_client):
    wallet_client.result_status = "INSUFFICIENT_BALANCE"
    _consume(service, _sod())

    order = repo.get_by_subscription_and_date("sub-1", DELIVERY_DATE)
    assert order.status.value == "PAYMENT_FAILED"
    assert order.failure_reason == "INSUFFICIENT_BALANCE"

    unpub = repo.fetch_unpublished()
    failed = [e for e in unpub if e["event_type"] == "OrderPaymentFailed"]
    assert len(failed) == 1
    jsonschema.validate(failed[0]["payload"], load_schema("OrderPaymentFailed"))

    detail = client.get(f"/orders/{order.id}", headers=_bearer()).json()["data"]
    assert detail["status"] == "PAYMENT_FAILED"
    assert detail["failureReason"] == "INSUFFICIENT_BALANCE"


def test_get_someone_elses_order_is_404(client, service):
    _consume(service, _sod())
    r = client.get("/orders/me", headers=_bearer())
    order_id = r.json()["data"]["items"][0]["orderId"]
    forbidden = client.get(f"/orders/{order_id}", headers=_bearer(sub="user-2"))
    assert forbidden.status_code == 404
    assert forbidden.json()["data"]["errorCode"] == "ORDER_NOT_FOUND"


def test_missing_bearer_is_401(client):
    assert client.get("/orders/me").status_code == 401


def test_healthz_ok(client):
    assert client.get("/healthz").status_code == 200
