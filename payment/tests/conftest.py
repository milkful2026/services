"""Shared pytest fixtures. SQLite in-memory stands in for Aurora Postgres
(documented fidelity gap, same as catalog/wallet). A FakeGateway stands
in for Razorpay — no real network/credentials needed for unit tests.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from adapters.payment_repository import SqlAlchemyPaymentRepository, create_schema
from config.env import Settings
from domain.payment_service import PaymentService


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PAYMENT_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("PAYMENT_AWS_REGION", "ap-south-1")
    monkeypatch.setenv("PAYMENT_EVENT_BUS_NAME", "milkful-events")
    monkeypatch.setenv("PAYMENT_RAZORPAY_KEY_ID", "rzp_test_fake")
    monkeypatch.setenv("PAYMENT_RAZORPAY_KEY_SECRET", "fake_key_secret")
    monkeypatch.setenv("PAYMENT_RAZORPAY_WEBHOOK_SECRET", "fake_webhook_secret")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    yield


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def repo(engine):
    return SqlAlchemyPaymentRepository(engine)


class FakeGateway:
    """Deterministic stand-in for RazorpayGateway. `orders_create` mints
    sequential order ids; `verify_*` are toggled per test; `fetch_order_payments`
    returns a script the test primes."""

    def __init__(self):
        self.orders_created: list[dict] = []
        self._order_seq = 0
        self.next_order_id: str | None = None
        self.raise_on_create: Exception | None = None
        self.client_signature_valid = True
        self.webhook_signature_valid = True
        self.order_payments: dict[str, list[dict]] = {}

    def orders_create(self, *, amount_paise, receipt, notes):
        if self.raise_on_create:
            raise self.raise_on_create
        self._order_seq += 1
        order_id = self.next_order_id or f"order_{self._order_seq}"
        self.orders_created.append(
            {"amount_paise": amount_paise, "receipt": receipt, "notes": notes}
        )
        return order_id

    def verify_webhook_signature(self, raw_body, signature_header):
        return self.webhook_signature_valid

    def verify_client_signature(self, razorpay_order_id, razorpay_payment_id, signature):
        return self.client_signature_valid

    def fetch_order_payments(self, razorpay_order_id):
        return self.order_payments.get(razorpay_order_id, [])


class FakeWalletLimits:
    def __init__(self, min_paise=10_000, max_paise=10_000_000):
        self._limits = (min_paise, max_paise)

    def get_limits(self):
        return self._limits


class FakeMetrics:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def emit(self, name, **dimensions):
        self.calls.append((name, dimensions))

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


@pytest.fixture
def gateway():
    return FakeGateway()


@pytest.fixture
def wallet_limits():
    return FakeWalletLimits()


@pytest.fixture
def metrics():
    return FakeMetrics()


@pytest.fixture
def service(repo, gateway, wallet_limits, metrics, settings):
    return PaymentService(repo, gateway, wallet_limits, metrics, settings)
