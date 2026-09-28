"""MA-144 end-to-end: the new 409 contracts over HTTP, a customer retry
racing the sweep, and a full sweep run_once over an abandoned checkout."""

import threading
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from adapters.order_repository import checkouts_table
from domain.checkout_models import CheckoutStatus
from domain.cutoff import IST
from domain.exceptions import CheckoutIncompleteError
from domain.sweep_service import SweepService
from handlers import sweep as sweep_handler
from handlers.app import app
from handlers.dependencies import get_checkout_service, get_order_service

LONG_AGO = datetime(2026, 1, 1, 10, 0, tzinfo=IST)  # delivery 2026-01-02, cut-off long passed


class _Metrics:
    def emit(self, name, **dimensions):
        pass


@pytest.fixture
def client(service, checkout_service):
    get_order_service.cache_clear()
    get_checkout_service.cache_clear()
    app.dependency_overrides[get_order_service] = lambda: service
    app.dependency_overrides[get_checkout_service] = lambda: checkout_service
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def sweep(repo, service, checkout_service):
    return SweepService(
        repo,
        service,
        _Metrics(),
        owner="sweep:test",
        cutoff_hour_ist=20,
        subscription_order_stale_seconds=900,
        max_attempts=3,
        lease_seconds=120,
        batch_size=50,
        checkout_service=checkout_service,
        checkout_stale_seconds=600,
    )


def _headers(key="key-00000001"):
    token = jwt.encode({"sub": "user-1"}, "x", algorithm="HS256")
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


def _one_time():
    return {"id": "li-1", "productId": "buffalo-milk", "quantity": 1, "frequency": "ONE_TIME",
            "startDate": None, "slotId": None}


def _daily():
    return {"id": "li-2", "productId": "cow-milk", "quantity": 2, "frequency": "DAILY",
            "startDate": "2099-01-01", "slotId": "slot-am"}


def _abandon(checkout_service, engine, now=None):
    with pytest.raises(CheckoutIncompleteError) as info:
        checkout_service.checkout(
            user_id="user-1",
            idempotency_key="key-00000001",
            cart_version=3,
            expected_pay_now_paise=None,
            correlation_id="corr",
            now=now,
        )
    with engine.begin() as conn:
        conn.execute(
            checkouts_table.update().values(
                updated_at=datetime.now(UTC) - timedelta(minutes=20)
            )
        )
    return info.value.details["checkoutId"]


def test_cancelled_checkout_replays_409_checkout_cancelled_envelope(
    client, checkout_service, engine, cart_client, wallet_client
):
    cart_client.items = [_one_time()]
    wallet_client.raise_unavailable = True
    _abandon(checkout_service, engine, now=LONG_AGO)
    wallet_client.raise_unavailable = False

    r = client.post("/orders/checkout", json={"cartVersion": 3}, headers=_headers())

    assert r.status_code == 409
    body = r.json()
    assert body["status"] == "error"
    assert body["data"]["errorCode"] == "CHECKOUT_CANCELLED"
    assert "weren't charged" in body["data"]["message"]
    assert wallet_client.debited == {}


def test_leased_checkout_returns_409_with_integer_retry_after(
    client, checkout_service, repo, engine, cart_client, wallet_client
):
    cart_client.items = [_one_time()]
    wallet_client.raise_unavailable = True
    checkout_id = _abandon(checkout_service, engine)
    wallet_client.raise_unavailable = False
    assert repo.claim_checkout(checkout_id, "sweep:x", 120)

    r = client.post("/orders/checkout", json={"cartVersion": 3}, headers=_headers())

    assert r.status_code == 409
    data = r.json()["data"]
    assert data["errorCode"] == "CHECKOUT_IN_PROGRESS"
    assert isinstance(data["retryAfterSeconds"], int) and data["retryAfterSeconds"] >= 1


def test_run_once_completes_an_abandoned_paid_checkout(
    sweep, checkout_service, repo, engine, cart_client, subscription_client, wallet_client
):
    cart_client.items = [_one_time(), _daily()]
    subscription_client.unavailable_for = {"cow-milk"}
    checkout_id = _abandon(checkout_service, engine)
    subscription_client.unavailable_for = set()

    counts = sweep_handler.run_once(service=sweep)

    assert counts["checkout.completed"] == 1
    assert repo.get_checkout_by_id(checkout_id).status == CheckoutStatus.COMPLETED
    assert len(wallet_client.debited) == 1
    assert cart_client.items == []


class _BlockingSubscriptions:
    """Creates subscriptions idempotently by key, but the first call waits
    until released, so a customer retry can race the in-flight sweep."""

    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self._by_key = {}

    def create(self, **kwargs):
        self.calls.append(kwargs["idempotency_key"])
        self.entered.set()
        assert self.release.wait(5)
        key = kwargs["idempotency_key"]
        self._by_key.setdefault(key, {"subscriptionId": f"sub_{len(self._by_key) + 1}",
                                      "nextDeliveryDate": "2099-01-01"})
        return self._by_key[key]


def test_customer_retry_racing_the_sweep_debits_and_subscribes_once(
    checkout_service, repo, engine, cart_client, wallet_client, subscription_client, sweep
):
    # Ticket AC 6 (checkout half).
    cart_client.items = [_one_time(), _daily()]
    subscription_client.unavailable_for = {"cow-milk"}
    checkout_id = _abandon(checkout_service, engine)

    blocking = _BlockingSubscriptions()
    checkout_service._subscriptions = blocking
    worker = threading.Thread(
        target=sweep.sweep_checkouts, args=("corr", datetime.now(UTC))
    )
    worker.start()
    assert blocking.entered.wait(5)

    from domain.exceptions import CheckoutInProgressError

    with pytest.raises(CheckoutInProgressError):
        checkout_service.checkout(
            user_id="user-1",
            idempotency_key="key-00000001",
            cart_version=3,
            expected_pay_now_paise=None,
            correlation_id="corr-customer",
        )
    blocking.release.set()
    worker.join(5)

    assert repo.get_checkout_by_id(checkout_id).status == CheckoutStatus.COMPLETED
    assert len(wallet_client.debited) == 1
    assert blocking.calls == [f"checkout:{checkout_id}:li-2"]
