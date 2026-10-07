"""FastAPI HTTP surface — TestClient against the SQLite double."""

import threading

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import NullPool

from adapters.wallet_repository import SqlAlchemyWalletRepository, create_schema
from domain.wallet_service import WalletService
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


# --- MA-148: ?types= filter ---


def _seed_mixed(client, engine, service):
    seed_wallet(engine, balance_paise=1_000_000)
    for i in range(2):
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
                "razorpayOrderId": f"ro_{i}",
            }
        )
    for i in range(3):
        client.post(
            "/wallet/internal/debit",
            json={"userId": "user-1", "orderId": f"ord_{i}", "amountPaise": 1000},
        )


def test_transactions_types_filter(client, engine, service):
    _seed_mixed(client, engine, service)
    body = client.get("/wallet/me/transactions?types=RECHARGE", headers=_bearer()).json()["data"]
    assert [i["type"] for i in body["items"]] == ["RECHARGE", "RECHARGE"]
    assert body["nextCursor"] is None


def test_transactions_repeated_types_params_are_unioned(client, engine, service):
    _seed_mixed(client, engine, service)
    body = client.get(
        "/wallet/me/transactions?types=RECHARGE&types=OPENING", headers=_bearer()
    ).json()["data"]
    assert [i["type"] for i in body["items"]] == ["RECHARGE", "RECHARGE", "OPENING"]


def test_transactions_repeated_types_params_validate_each_value(client, engine):
    seed_wallet(engine)
    r = client.get("/wallet/me/transactions?types=RECHARGE&types=BOGUS", headers=_bearer())
    assert r.status_code == 400
    assert r.json()["data"]["invalid"] == ["BOGUS"]


def test_transactions_unknown_type_is_400(client, engine):
    seed_wallet(engine)
    r = client.get("/wallet/me/transactions?types=BOGUS", headers=_bearer())
    assert r.status_code == 400
    data = r.json()["data"]
    assert data["errorCode"] == "VALIDATION_ERROR"
    assert data["field"] == "types"
    assert data["invalid"] == ["BOGUS"]


def test_transactions_filtered_cursor_stays_within_the_filter(client, engine, service):
    _seed_mixed(client, engine, service)
    first = client.get(
        "/wallet/me/transactions?types=ORDER_DEBIT&limit=1", headers=_bearer()
    ).json()["data"]
    assert [i["ref"] for i in first["items"]] == ["order:ord_2"]
    second = client.get(
        f"/wallet/me/transactions?types=ORDER_DEBIT&limit=5&cursor={first['nextCursor']}",
        headers=_bearer(),
    ).json()["data"]
    assert [i["ref"] for i in second["items"]] == ["order:ord_1", "order:ord_0"]
    assert second["nextCursor"] is None


def test_transactions_without_types_include_every_type(client, engine, service):
    _seed_mixed(client, engine, service)
    body = client.get("/wallet/me/transactions?limit=50", headers=_bearer()).json()["data"]
    assert {i["type"] for i in body["items"]} == {"OPENING", "RECHARGE", "ORDER_DEBIT"}



# --- MA-153: POST /wallet/internal/refunds ---


def _debit(client, order_id="ord_x", amount=30000, user_id="user-1"):
    r = client.post(
        "/wallet/internal/debit",
        json={"userId": user_id, "orderId": order_id, "amountPaise": amount},
    )
    assert r.status_code == 200


def _refund_body(**overrides):
    body = {"userId": "user-1", "orderId": "ord_x", "refundId": "cancel", "amountPaise": 30000}
    body.update(overrides)
    return body


def test_internal_refund_after_debit_is_200_and_shows_in_history(client, engine):
    seed_wallet(engine, balance_paise=100000)
    _debit(client)
    r = client.post("/wallet/internal/refunds", json=_refund_body())
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "REFUNDED"
    assert data["orderId"] == "ord_x"
    assert data["refundId"] == "cancel"
    assert data["amountPaise"] == 30000
    assert data["balanceAfterPaise"] == 100000
    assert data["replayed"] is False
    assert data["refundedAt"]

    history = client.get("/wallet/me/transactions?types=REFUND", headers=_bearer())
    items = history.json()["data"]["items"]
    assert [(i["amountPaise"], i["ref"]) for i in items] == [(30000, "refund:ord_x:cancel")]
    assert items[0]["id"] == f"led_{data['ledgerEntryId']}"


def test_internal_refund_replay_is_200_replayed(client, engine):
    seed_wallet(engine, balance_paise=100000)
    _debit(client)
    first = client.post("/wallet/internal/refunds", json=_refund_body()).json()["data"]
    second = client.post("/wallet/internal/refunds", json=_refund_body()).json()["data"]
    assert second["replayed"] is True
    assert second["ledgerEntryId"] == first["ledgerEntryId"]


@pytest.fixture
def isolated_client(tmp_path, settings):
    """A file-backed SQLite engine with one connection per request, unlike the
    suite's shared StaticPool connection (whose transactions can't overlap).
    `BEGIN IMMEDIATE` serializes writers the way Postgres' FOR UPDATE does."""
    eng = create_engine(
        f"sqlite:///{tmp_path / 'wallet.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        poolclass=NullPool,
    )

    @event.listens_for(eng, "connect")
    def _no_pysqlite_begin(dbapi_conn, _):
        dbapi_conn.isolation_level = None

    @event.listens_for(eng, "begin")
    def _begin_immediate(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    create_schema(eng)
    svc = WalletService(SqlAlchemyWalletRepository(eng), settings)
    get_wallet_service.cache_clear()
    app.dependency_overrides[get_wallet_service] = lambda: svc
    yield TestClient(app), eng
    app.dependency_overrides.clear()
    eng.dispose()


def test_internal_refund_concurrent_identical_requests_write_one_entry(isolated_client):
    client, eng = isolated_client
    seed_wallet(eng, balance_paise=100000)
    _debit(client)
    barrier = threading.Barrier(2)
    results = []

    def post():
        barrier.wait()
        results.append(client.post("/wallet/internal/refunds", json=_refund_body()).json())

    threads = [threading.Thread(target=post) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [r["status"] for r in results] == ["success", "success"]
    assert sorted(r["data"]["replayed"] for r in results) == [False, True]
    history = client.get("/wallet/me/transactions?types=REFUND", headers=_bearer())
    assert len(history.json()["data"]["items"]) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"orderId": "bad id"},
        {"orderId": "a" * 65},
        {"orderId": None},
        {"refundId": "bad.id"},
        {"refundId": "a" * 33},
        {"refundId": None},
        {"userId": ""},
        {"userId": None},
        {"amountPaise": None},
    ],
)
def test_internal_refund_invalid_body_is_400_validation_error(client, engine, overrides):
    seed_wallet(engine, balance_paise=100000)
    r = client.post("/wallet/internal/refunds", json=_refund_body(**overrides))
    assert r.status_code == 400
    body = r.json()
    assert body["status"] == "error"
    assert body["data"]["errorCode"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("amount", [0, -5])
def test_internal_refund_non_positive_amount_is_400_invalid_amount(client, engine, amount):
    seed_wallet(engine, balance_paise=100000)
    _debit(client)
    r = client.post("/wallet/internal/refunds", json=_refund_body(amountPaise=amount))
    assert r.status_code == 400
    assert r.json()["data"]["errorCode"] == "INVALID_AMOUNT"


def test_internal_refund_no_wallet_is_404(client):
    r = client.post("/wallet/internal/refunds", json=_refund_body(userId="ghost"))
    assert r.status_code == 404
    assert r.json()["data"]["errorCode"] == "WALLET_NOT_FOUND"


def test_internal_refund_without_debit_is_409_debit_not_found(client, engine):
    seed_wallet(engine, balance_paise=100000)
    r = client.post("/wallet/internal/refunds", json=_refund_body())
    assert r.status_code == 409
    assert r.json()["data"]["errorCode"] == "DEBIT_NOT_FOUND"


def test_internal_refund_over_debit_is_409_with_details(client, engine):
    seed_wallet(engine, balance_paise=100000)
    _debit(client)
    client.post("/wallet/internal/refunds", json=_refund_body())
    r = client.post(
        "/wallet/internal/refunds", json=_refund_body(refundId="extra", amountPaise=1)
    )
    assert r.status_code == 409
    data = r.json()["data"]
    assert data["errorCode"] == "REFUND_EXCEEDS_DEBIT"
    assert data["debitedPaise"] == 30000
    assert data["alreadyRefundedPaise"] == 30000


def test_internal_refund_other_users_debit_is_409_mismatch(client, engine):
    seed_wallet(engine, balance_paise=100000)
    seed_wallet(engine, user_id="user-2", wallet_id="wal_2")
    _debit(client)
    r = client.post("/wallet/internal/refunds", json=_refund_body(userId="user-2"))
    assert r.status_code == 409
    assert r.json()["data"]["errorCode"] == "ORDER_USER_MISMATCH"


def test_internal_debit_lookup_is_still_404_for_unknown_orders(client, engine):
    # The refund route's 409 DEBIT_NOT_FOUND is a subclass; the MA-142 lookup keeps its 404.
    seed_wallet(engine)
    assert client.get("/wallet/internal/debits/ord_unknown").status_code == 404


def test_internal_refund_route_is_only_internal():
    assert "/wallet/internal/refunds" in app.openapi()["paths"]
    assert not [p for p in app.openapi()["paths"] if "refund" in p and "internal" not in p]
