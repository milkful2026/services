"""FastAPI HTTP surface — TestClient against the SQLite double."""

import jwt
import pytest
from fastapi.testclient import TestClient

from handlers.app import app
from handlers.dependencies import get_wallet_service
from tests.conftest import seed_wallet


@pytest.fixture
def client(service, engine):
    get_wallet_service.cache_clear()
    app.dependency_overrides[get_wallet_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def _bearer(sub="user-1"):
    return {"Authorization": "Bearer " + jwt.encode({"sub": sub}, "x", algorithm="HS256")}


def test_get_wallet_me_returns_fr1_body(client, engine):
    seed_wallet(engine, balance_paise=45000)
    r = client.get("/wallet/me", headers=_bearer())
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["balancePaise"] == 45000
    assert data["rechargeMinPaise"] == 10000
    assert data["rechargeMaxPaise"] == 10000000


def test_get_wallet_status_returns_ma1_rupee_body(client, engine):
    seed_wallet(engine, balance_paise=45000)
    r = client.get("/wallet/me/status", headers=_bearer())
    assert r.status_code == 200
    data = r.json()["data"]
    assert data == {"walletId": "wal_1", "status": "ACTIVE", "balance": 450, "currency": "INR"}
    assert "balancePaise" not in data


def test_me_and_status_bodies_differ(client, engine):
    seed_wallet(engine, balance_paise=12345)
    me = client.get("/wallet/me", headers=_bearer()).json()["data"]
    status = client.get("/wallet/me/status", headers=_bearer()).json()["data"]
    assert "balancePaise" in me and "balancePaise" not in status
    assert "balance" in status and "balance" not in me


def test_transactions_paged(client, engine, service):
    seed_wallet(engine)
    for i in range(3):
        service.credit_recharge(
            {
                "eventId": f"e{i}",
                "correlationId": "c",
                "paymentId": f"pay_{i}",
                "userId": "user-1",
                "purpose": "WALLET_RECHARGE",
                "amountPaise": 10000,
                "currency": "INR",
                "razorpayPaymentId": f"rzp_{i}",
                "razorpayOrderId": f"ord_{i}",
            }
        )
    r = client.get("/wallet/me/transactions?limit=2", headers=_bearer())
    body = r.json()["data"]
    assert len(body["items"]) == 2
    assert body["items"][0]["type"] == "RECHARGE"
    assert body["items"][0]["description"] == "Wallet top-up"
    assert body["nextCursor"] is not None


def test_transactions_bad_cursor_is_400(client, engine):
    seed_wallet(engine)
    r = client.get("/wallet/me/transactions?cursor=@@@", headers=_bearer())
    assert r.status_code == 400
    assert r.json()["data"]["errorCode"] == "INVALID_CURSOR"


def test_internal_limits_no_auth(client):
    r = client.get("/wallet/internal/limits")
    assert r.status_code == 200
    assert r.json()["data"] == {"rechargeMinPaise": 10000, "rechargeMaxPaise": 10000000}


def test_missing_bearer_is_401(client):
    assert client.get("/wallet/me").status_code == 401


def test_internal_debit_no_auth_debits_and_returns_200(client, engine):
    seed_wallet(engine, balance_paise=100000)
    r = client.post(
        "/wallet/internal/debit",
        json={"userId": "user-1", "orderId": "order-1", "amountPaise": 30000},
    )
    assert r.status_code == 200
    assert r.json()["data"] == {"status": "DEBITED", "balanceAfterPaise": 70000}


def test_internal_debit_insufficient_balance_is_200_not_4xx(client, engine):
    seed_wallet(engine, balance_paise=100)
    r = client.post(
        "/wallet/internal/debit",
        json={"userId": "user-1", "orderId": "order-1", "amountPaise": 30000},
    )
    assert r.status_code == 200
    assert r.json()["data"] == {
        "status": "INSUFFICIENT_BALANCE",
        "balancePaise": 100,
        "requiredPaise": 30000,
    }


def test_internal_debit_wallet_not_active_is_200_not_4xx(client, engine):
    seed_wallet(engine, balance_paise=100000, status="FAILED")
    r = client.post(
        "/wallet/internal/debit",
        json={"userId": "user-1", "orderId": "order-1", "amountPaise": 30000},
    )
    assert r.status_code == 200
    assert r.json()["data"] == {"status": "WALLET_NOT_ACTIVE"}


def test_internal_debit_no_wallet_row_is_503_not_200(client):
    # Regression: the provisioning race must surface as a retryable 503,
    # never the 200 WALLET_NOT_ACTIVE shape.
    r = client.post(
        "/wallet/internal/debit",
        json={"userId": "ghost", "orderId": "order-1", "amountPaise": 30000},
    )
    assert r.status_code == 503
    assert r.json()["data"]["errorCode"] == "WALLET_PROVISIONING_PENDING"


def test_internal_debit_non_positive_amount_is_422(client, engine):
    seed_wallet(engine, balance_paise=100000)
    r = client.post(
        "/wallet/internal/debit",
        json={"userId": "user-1", "orderId": "order-1", "amountPaise": 0},
    )
    assert r.status_code == 422  # pydantic Field(gt=0) rejection


def test_internal_balance_no_auth(client, engine):
    seed_wallet(engine, balance_paise=45000)
    r = client.get("/wallet/internal/balance", params={"userId": "user-1"})
    assert r.status_code == 200
    assert r.json()["data"] == {"balancePaise": 45000, "status": "ACTIVE"}


def test_internal_balance_no_wallet_is_creating_zero(client):
    r = client.get("/wallet/internal/balance", params={"userId": "ghost"})
    assert r.status_code == 200
    assert r.json()["data"] == {"balancePaise": 0, "status": "CREATING"}


# --- MA-142: GET /wallet/internal/debits/{orderId} ---


def test_internal_debit_lookup_after_debit_is_200(client, engine):
    seed_wallet(engine, balance_paise=100000)
    client.post(
        "/wallet/internal/debit",
        json={"userId": "user-1", "orderId": "ord_x", "amountPaise": 30000},
    )
    r = client.get("/wallet/internal/debits/ord_x")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "DEBITED"
    assert data["amountPaise"] == 30000
    assert data["balanceAfterPaise"] == 70000
    # Ledger rows are immutable: a repeated call returns the same data
    # (only the envelope's per-request requestId differs).
    assert client.get("/wallet/internal/debits/ord_x").json()["data"] == data


def test_internal_debit_lookup_unknown_is_404(client, engine):
    seed_wallet(engine)
    r = client.get("/wallet/internal/debits/ord_unknown")
    assert r.status_code == 404
    assert r.json()["data"]["errorCode"] == "DEBIT_NOT_FOUND"


@pytest.mark.parametrize("bad", ["bad%20id", "a" * 65, "ord.x"])
def test_internal_debit_lookup_invalid_id_is_400(client, bad):
    r = client.get(f"/wallet/internal/debits/{bad}")
    assert r.status_code == 400
    assert r.json()["data"]["errorCode"] == "VALIDATION_ERROR"


def test_internal_debit_routes_are_only_internal():
    # openapi() flattens included routers (app.routes nests them).
    routes = sorted(p for p in app.openapi()["paths"] if "/debits/" in p)
    assert routes == [
        "/wallet/internal/debits/{orderId}",
        "/wallet/internal/debits/{orderId}/void",
    ]


# --- MA-142: POST /wallet/internal/debits/{orderId}/void ---


def test_void_after_debit_is_409_already_debited_with_the_debit(client, engine):
    seed_wallet(engine, balance_paise=100000)
    client.post(
        "/wallet/internal/debit",
        json={"userId": "user-1", "orderId": "ord_x", "amountPaise": 30000},
    )
    r = client.post("/wallet/internal/debits/ord_x/void", json={"userId": "user-1"})
    assert r.status_code == 409
    body = r.json()["data"]
    # The shared error envelope flattens `details` into `data`.
    assert body["errorCode"] == "ALREADY_DEBITED"
    assert body["status"] == "DEBITED"
    assert body["amountPaise"] == 30000
    assert body["balanceAfterPaise"] == 70000
    assert body["debitedAt"]


def test_void_then_debit_is_409_debit_voided(client, engine):
    seed_wallet(engine, balance_paise=100000)
    r = client.post("/wallet/internal/debits/ord_x/void", json={"userId": "user-1"})
    assert r.status_code == 200
    voided = r.json()["data"]
    assert voided["orderId"] == "ord_x"
    assert voided["status"] == "VOIDED"
    assert voided["voidedAt"]

    r = client.post(
        "/wallet/internal/debit",
        json={"userId": "user-1", "orderId": "ord_x", "amountPaise": 30000},
    )
    assert r.status_code == 409
    body = r.json()["data"]
    assert body["errorCode"] == "DEBIT_VOIDED"
    assert body["orderId"] == "ord_x"
    assert body["voidedAt"] == voided["voidedAt"]
    balance = client.get("/wallet/internal/balance", params={"userId": "user-1"})
    assert balance.json()["data"]["balancePaise"] == 100000


def test_void_repeated_returns_the_same_200(client, engine):
    seed_wallet(engine)
    first = client.post("/wallet/internal/debits/ord_x/void", json={"userId": "user-1"})
    second = client.post("/wallet/internal/debits/ord_x/void", json={"userId": "user-1"})
    assert second.status_code == 200
    assert second.json()["data"] == first.json()["data"]


def test_lookup_of_voided_order_is_200_voided(client, engine):
    seed_wallet(engine)
    voided = client.post(
        "/wallet/internal/debits/ord_x/void", json={"userId": "user-1"}
    ).json()["data"]
    r = client.get("/wallet/internal/debits/ord_x")
    assert r.status_code == 200
    assert r.json()["data"] == voided


@pytest.mark.parametrize("bad", ["bad%20id", "a" * 65, "ord.x"])
def test_void_invalid_id_is_400(client, bad):
    r = client.post(f"/wallet/internal/debits/{bad}/void", json={"userId": "user-1"})
    assert r.status_code == 400
    assert r.json()["data"]["errorCode"] == "VALIDATION_ERROR"

