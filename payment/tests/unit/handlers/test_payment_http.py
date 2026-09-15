"""FastAPI HTTP surface — TestClient against the fake gateway + SQLite."""

import json

import jwt
import pytest
from fastapi.testclient import TestClient

from handlers.app import app
from handlers.dependencies import get_payment_service
from tests.conftest import FakeGateway, FakeMetrics, FakeWalletLimits


@pytest.fixture
def client(service, engine):
    get_payment_service.cache_clear()
    app.dependency_overrides[get_payment_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def _bearer(sub="user-1"):
    return {"Authorization": "Bearer " + jwt.encode({"sub": sub}, "x", algorithm="HS256")}


def test_create_requires_idempotency_key(client):
    r = client.post(
        "/payments", headers=_bearer(), json={"purpose": "WALLET_RECHARGE", "amountPaise": 50000}
    )
    assert r.status_code == 400


def test_create_returns_order_and_key_id(client, settings):
    r = client.post(
        "/payments",
        headers={**_bearer(), "Idempotency-Key": "k1"},
        json={"purpose": "WALLET_RECHARGE", "amountPaise": 50000, "method": "UPI"},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["razorpayOrderId"] == "order_1"
    assert data["razorpayKeyId"] == settings.razorpay_key_id


def test_create_missing_auth_is_401(client):
    r = client.post(
        "/payments",
        headers={"Idempotency-Key": "k1"},
        json={"purpose": "WALLET_RECHARGE", "amountPaise": 50000},
    )
    assert r.status_code == 401


def test_confirm_then_get_reflects_confirming(client, gateway):
    created = client.post(
        "/payments", headers={**_bearer(), "Idempotency-Key": "k1"},
        json={"purpose": "WALLET_RECHARGE", "amountPaise": 50000},
    ).json()["data"]

    r = client.post(
        f"/payments/{created['paymentId']}/confirm",
        headers=_bearer(),
        json={
            "razorpayPaymentId": "rzp_pay_1",
            "razorpayOrderId": created["razorpayOrderId"],
            "razorpaySignature": "sig",
        },
    )
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "CONFIRMING"

    got = client.get(f"/payments/{created['paymentId']}", headers=_bearer())
    assert got.json()["data"]["status"] == "CONFIRMING"


def test_get_wrong_owner_is_404(client):
    created = client.post(
        "/payments", headers={**_bearer(), "Idempotency-Key": "k1"},
        json={"purpose": "WALLET_RECHARGE", "amountPaise": 50000},
    ).json()["data"]
    r = client.get(f"/payments/{created['paymentId']}", headers=_bearer(sub="someone-else"))
    assert r.status_code == 404


def test_webhook_no_auth_needed_but_signature_checked(engine, repo, settings):
    gw = FakeGateway()
    from domain.payment_service import PaymentService

    svc = PaymentService(repo, gw, FakeWalletLimits(), FakeMetrics(), settings)
    get_payment_service.cache_clear()
    app.dependency_overrides[get_payment_service] = lambda: svc
    client = TestClient(app)
    try:
        gw.webhook_signature_valid = False
        r = client.post("/payments/webhook", content=b"{}", headers={"X-Razorpay-Signature": "bad"})
        assert r.status_code == 400
        assert r.json()["data"]["errorCode"] == "SIGNATURE_INVALID"
    finally:
        app.dependency_overrides.clear()


def test_webhook_captured_confirms_payment(client, repo, gateway):
    created = client.post(
        "/payments", headers={**_bearer(), "Idempotency-Key": "k1"},
        json={"purpose": "WALLET_RECHARGE", "amountPaise": 50000},
    ).json()["data"]

    body = json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "rzp_pay_1",
                        "order_id": created["razorpayOrderId"],
                        "amount": 50000,
                        "method": "upi",
                    }
                }
            },
        }
    ).encode()
    r = client.post("/payments/webhook", content=body, headers={"X-Razorpay-Signature": "any"})
    assert r.status_code == 200
    assert repo.get(created["paymentId"]).status.value == "CONFIRMED"
