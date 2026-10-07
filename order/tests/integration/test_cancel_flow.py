"""MA-154 — POST /orders/{id}/cancel over HTTP, against the SQLite double and
the conftest fakes (real clock, so orders are delivered a few days out)."""

import threading
from datetime import UTC, datetime, timedelta

import jsonschema
import jwt
import pytest
from fastapi.testclient import TestClient
from shared.events import load_schema
from sqlalchemy import create_engine, event
from sqlalchemy.pool import NullPool

from adapters.order_repository import SqlAlchemyOrderRepository, create_schema
from domain.cutoff import IST
from domain.order_service import OrderService
from handlers.app import app
from handlers.dependencies import get_order_service


@pytest.fixture
def client(service):
    get_order_service.cache_clear()
    app.dependency_overrides[get_order_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def _bearer(sub="user-1"):
    return {"Authorization": "Bearer " + jwt.encode({"sub": sub}, "x", algorithm="HS256")}


def _confirmed(service, repo, days_out=3):
    delivery = datetime.now(UTC).astimezone(IST).date() + timedelta(days=days_out)
    service.materialize(
        subscription_id="sub-1",
        user_id="user-1",
        product_id="prod-1",
        quantity=2,
        delivery_date=delivery,
        correlation_id="c",
    )
    return repo.get_by_subscription_and_date("sub-1", delivery)


def test_cancel_then_get_shows_the_new_fields(client, service, repo, wallet_client):
    order = _confirmed(service, repo)
    before = client.get(f"/orders/{order.id}", headers=_bearer()).json()["data"]
    assert before["cancellableUntil"]
    assert before["refundState"] is None

    r = client.post(
        f"/orders/{order.id}/cancel",
        json={"reason": "NOT_HOME"},
        headers={**_bearer(), "X-Correlation-Id": "corr-http"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "success"
    data = r.json()["data"]
    assert data["status"] == "CANCELLED"
    assert data["refundState"] == "REFUNDED"

    detail = client.get(f"/orders/{order.id}", headers=_bearer()).json()["data"]
    assert detail["cancelReason"] == "NOT_HOME"
    assert detail["cancelledAt"]
    assert detail["refundState"] == "REFUNDED"
    assert detail["cancellableUntil"] is None
    assert detail["failureReason"] == "CUSTOMER_CANCELLED"

    [event] = [e for e in repo.fetch_unpublished() if e["event_type"] == "OrderCancelled"]
    jsonschema.validate(event["payload"], load_schema("OrderCancelled"))
    assert event["payload"]["correlationId"] == "corr-http"


@pytest.mark.parametrize("kwargs", [{}, {"json": {}}, {"json": {"reason": None}}])
def test_cancel_without_a_reason_is_200(client, service, repo, kwargs):
    order = _confirmed(service, repo)
    r = client.post(f"/orders/{order.id}/cancel", headers=_bearer(), **kwargs)
    assert r.status_code == 200
    assert r.json()["data"]["cancelReason"] is None


@pytest.mark.parametrize("reason", ["LOL", 5, ["NOT_HOME"]])
def test_unknown_reason_is_400_validation_error(client, service, repo, reason):
    order = _confirmed(service, repo)
    r = client.post(f"/orders/{order.id}/cancel", json={"reason": reason}, headers=_bearer())
    assert r.status_code == 400
    body = r.json()
    assert body["status"] == "error"
    assert body["data"]["errorCode"] == "VALIDATION_ERROR"


def test_someone_elses_order_is_404(client, service, repo):
    order = _confirmed(service, repo)
    r = client.post(f"/orders/{order.id}/cancel", headers=_bearer(sub="user-2"))
    assert r.status_code == 404
    assert r.json()["data"]["errorCode"] == "ORDER_NOT_FOUND"


def test_not_confirmed_is_409_with_status(client, service, repo, wallet_client):
    wallet_client.result_status = "INSUFFICIENT_BALANCE"
    order = _confirmed(service, repo)
    r = client.post(f"/orders/{order.id}/cancel", headers=_bearer())
    assert r.status_code == 409
    data = r.json()["data"]
    assert data["errorCode"] == "ORDER_NOT_CANCELLABLE"
    assert data["status"] == "PAYMENT_FAILED"


def test_past_the_cutoff_is_409_with_cancellable_until(client, service, repo):
    order = _confirmed(service, repo, days_out=0)  # today's delivery: cut-off was yesterday
    r = client.post(f"/orders/{order.id}/cancel", headers=_bearer())
    assert r.status_code == 409
    data = r.json()["data"]
    assert data["errorCode"] == "CUTOFF_PASSED"
    assert data["cancellableUntil"].endswith("+05:30")


def test_missing_bearer_is_401(client):
    assert client.post("/orders/ord_x/cancel").status_code == 401


@pytest.fixture
def isolated(tmp_path, user_client, pricing_client, wallet_client):
    """One connection per request (the suite's shared StaticPool connection
    can't hold two transactions at once); BEGIN IMMEDIATE serializes writers."""
    eng = create_engine(
        f"sqlite:///{tmp_path / 'order.db'}",
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
    repo = SqlAlchemyOrderRepository(eng)
    svc = OrderService(repo, user_client, pricing_client, wallet_client)
    get_order_service.cache_clear()
    app.dependency_overrides[get_order_service] = lambda: svc
    yield TestClient(app), svc, repo
    app.dependency_overrides.clear()
    eng.dispose()


def test_concurrent_cancels_cancel_once(isolated, wallet_client):
    client, service, repo = isolated
    order = _confirmed(service, repo)
    barrier = threading.Barrier(2)
    responses = []

    def cancel():
        barrier.wait()
        responses.append(client.post(f"/orders/{order.id}/cancel", headers=_bearer()))

    threads = [threading.Thread(target=cancel) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert [r.status_code for r in responses] == [200, 200]
    cancelled = [e for e in repo.fetch_unpublished() if e["event_type"] == "OrderCancelled"]
    assert len(cancelled) == 1
    assert repo.get(order.id).refund_state.value == "REFUNDED"
    # A replay landing while the winner is still PENDING runs its own
    # (idempotent) refund, so one or two calls; Wallet's ref keeps it one credit.
    assert 1 <= len(wallet_client.refund_calls) <= 2
