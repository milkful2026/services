"""Shared pytest fixtures. SQLite in-memory stands in for Aurora Postgres
(documented fidelity gap, same as every other service here). No real
AWS, DB, or network.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from adapters.cart_client_adapter import CartSnapshot
from adapters.order_repository import SqlAlchemyOrderRepository, create_schema
from config.env import Settings
from domain.checkout_service import CheckoutService
from domain.exceptions import (
    AddressLookupUnavailableError,
    CartUnavailableError,
    CartVersionConflictError,
    PricingUnavailableError,
    ProductPricingUnknownError,
    SubscriptionRejectedError,
    SubscriptionUnavailableError,
    WalletBalanceUnavailableError,
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
        return self.quote_items([(product_id, quantity)], delivery_state)

    def quote_items(self, items, delivery_state: str) -> Quote:
        self.quote_calls = getattr(self, "quote_calls", []) + [list(items)]
        if self.raise_product_unknown:
            raise ProductPricingUnknownError(
                f"no such product {items[0][0]!r}",
                details={"productId": getattr(self, "unknown_product_id", items[0][0])},
            )
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
        # MA-136 — balance read (paise) for checkout's pre-check.
        self.balance_paise = 100_000
        self.raise_balance_unavailable = False

    def debit(self, user_id: str, order_id: str, amount_paise: int, correlation_id: str):
        self.calls.append((user_id, order_id, amount_paise))
        if self.raise_unavailable:
            raise WalletUnavailableError("fake unavailable")
        if self.result_status == "DEBITED":
            return DebitResult(
                status="DEBITED", balance_after_paise=self.balance_paise - amount_paise
            )
        return DebitResult(status=self.result_status, balance_after_paise=self.balance_paise)

    def get_balance(self, user_id: str) -> int:
        if self.raise_balance_unavailable:
            raise WalletBalanceUnavailableError("fake unavailable")
        return self.balance_paise


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


# --- MA-136: checkout fakes ---------------------------------------------------


class FakeCartClient:
    """Cart Service's internal read/clear, with just enough version
    semantics to exercise conflicts and replays."""

    def __init__(self):
        self.items: list[dict] = []
        self.cart_version = 3
        self.raise_unavailable = False
        self.remove_unavailable = False
        self.conflict_once = False
        self.remove_calls: list[tuple[list[str], int, str]] = []

    def get_cart(self, user_id: str) -> CartSnapshot:
        if self.raise_unavailable:
            raise CartUnavailableError("fake unavailable")
        return CartSnapshot(items=[dict(i) for i in self.items], cart_version=self.cart_version)

    def remove_items(self, user_id, item_ids, if_version, checkout_id) -> CartSnapshot:
        self.remove_calls.append((list(item_ids), if_version, checkout_id))
        if self.remove_unavailable:
            raise CartUnavailableError("fake unavailable")
        if self.conflict_once:
            self.conflict_once = False
            self.cart_version += 1  # someone edited the cart elsewhere
            raise CartVersionConflictError("moved on")
        present = [i for i in self.items if i["id"] in item_ids]
        if not present and self.cart_version > if_version:
            return self.get_cart(user_id)  # retried clear that already happened
        if if_version != self.cart_version:
            raise CartVersionConflictError("moved on")
        self.items = [i for i in self.items if i["id"] not in item_ids]
        self.cart_version += 1
        return self.get_cart(user_id)


class FakeSubscriptionClient:
    def __init__(self):
        self.calls: list[dict] = []
        self.reject: dict[str, str] = {}  # product_id -> errorCode
        self.unavailable_for: set[str] = set()  # product_ids
        self._by_key: dict[str, dict] = {}

    def create(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        product_id = kwargs["product_id"]
        if product_id in self.unavailable_for:
            raise SubscriptionUnavailableError("fake unavailable")
        if product_id in self.reject:
            raise SubscriptionRejectedError(self.reject[product_id])
        key = kwargs["idempotency_key"]
        if key not in self._by_key:
            self._by_key[key] = {
                "subscriptionId": f"sub_{len(self._by_key) + 1}",
                "nextDeliveryDate": kwargs["start_date"].isoformat(),
            }
        return self._by_key[key]


@pytest.fixture
def cart_client():
    return FakeCartClient()


@pytest.fixture
def subscription_client():
    return FakeSubscriptionClient()


@pytest.fixture
def checkout_service(
    repo, cart_client, user_client, pricing_client, wallet_client, subscription_client
):
    return CheckoutService(
        repo,
        cart_client,
        user_client,
        pricing_client,
        wallet_client,
        subscription_client,
        cutoff_hour_ist=20,
        subscription_min_balance_paise=50_000,
    )
