"""Shared pytest fixtures. SQLite in-memory stands in for Aurora Postgres
(documented fidelity gap, same as every other service here). No real
AWS, DB, or network.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from adapters.order_repository import SqlAlchemyOrderRepository, create_schema
from config.env import Settings
from domain.exceptions import (
    AddressLookupUnavailableError,
    PricingUnavailableError,
    ProductPricingUnknownError,
    WalletUnavailableError,
)
from domain.models import DebitResult, Quote
from domain.order_service import OrderService


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ORDER_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("ORDER_AWS_REGION", "ap-south-1")
    monkeypatch.setenv("ORDER_EVENT_BUS_NAME", "milkful-events")
    monkeypatch.setenv("ORDER_USER_INTERNAL_BASE_URL", "http://user.test")
    monkeypatch.setenv("ORDER_PRICING_BASE_URL", "http://pricing.test")
    monkeypatch.setenv("ORDER_WALLET_INTERNAL_BASE_URL", "http://wallet.test")
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
    return SqlAlchemyOrderRepository(engine)


class FakeUserClient:
    def __init__(self):
        self.address_states: dict[str, str | None] = {"user-1": "MH"}
        self.raise_unavailable = False

    def get_delivery_address_state(self, cognito_sub: str) -> str | None:
        if self.raise_unavailable:
            raise AddressLookupUnavailableError("fake unavailable")
        return self.address_states.get(cognito_sub)


class FakePricingClient:
    def __init__(self):
        # net_payable in rupees, tax/delivery-fee-inclusive by default so
        # amountPaise > catalogPrice * quantity * 100 in every test unless
        # a test explicitly wants a zero-tax quote.
        self.net_payable = 55.0
        self.raise_unavailable = False
        self.raise_product_unknown = False

    def quote(self, product_id: str, quantity: int, delivery_state: str) -> Quote:
        if self.raise_product_unknown:
            raise ProductPricingUnknownError(f"no such product {product_id!r}")
        if self.raise_unavailable:
            raise PricingUnavailableError("fake unavailable")
        return Quote(
            base_price=50.0,
            tax_amount=3.0,
            tax_rate=0.06,
            delivery_fee=2.0,
            net_payable=self.net_payable,
        )


class FakeWalletClient:
    def __init__(self):
        self.result_status = "DEBITED"
        self.raise_unavailable = False
        self.calls: list[tuple[str, str, int]] = []

    def debit(self, user_id: str, order_id: str, amount_paise: int, correlation_id: str):
        self.calls.append((user_id, order_id, amount_paise))
        if self.raise_unavailable:
            raise WalletUnavailableError("fake unavailable")
        return DebitResult(status=self.result_status, balance_after_paise=100000)


@pytest.fixture
def user_client():
    return FakeUserClient()


@pytest.fixture
def pricing_client():
    return FakePricingClient()


@pytest.fixture
def wallet_client():
    return FakeWalletClient()


@pytest.fixture
def service(repo, user_client, pricing_client, wallet_client):
    return OrderService(repo, user_client, pricing_client, wallet_client)
