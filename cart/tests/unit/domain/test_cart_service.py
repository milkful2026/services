import pytest

from domain.cart_service import CartService
from domain.exceptions import (
    DeliveryAddressRequiredError,
    OutOfStockError,
    ValidationError,
    WalletBalanceTooLowError,
    WalletCheckUnavailableError,
)
from domain.models import Cart, Frequency, LineItem, Quote


class FakeCartRepository:
    def __init__(self, cart: Cart | None = None):
        self.cart = cart or Cart()
        self.correlation_id = ""
        self.add_item_calls: list[dict] = []
        self.replace_cart_calls: list[dict] = []
        self.delete_item_calls: list[dict] = []

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def get_cart(self, user_id: str) -> Cart:
        return self.cart

    def add_item(self, user_id, product_id, quantity, frequency, start_date, idempotency_key):
        self.add_item_calls.append({
            "user_id": user_id, "product_id": product_id, "quantity": quantity,
            "frequency": frequency, "start_date": start_date, "idempotency_key": idempotency_key,
        })
        return LineItem(
            id="new-item",
            product_id=product_id,
            quantity=quantity,
            frequency=frequency,
            start_date=start_date,
            added_at="2026-08-28T00:00:00Z",
        )

    def replace_cart(self, user_id, items, if_version):
        self.replace_cart_calls.append({"user_id": user_id, "items": items, "if_version": if_version})
        return Cart(line_items=[], cart_version=if_version + 1)

    def delete_item(self, user_id, line_item_id):
        self.delete_item_calls.append({"user_id": user_id, "line_item_id": line_item_id})


class FakeCatalogClient:
    def __init__(self, available_quantity: int | None = 100):
        self.available_quantity = available_quantity
        self.correlation_id = ""
        self.calls: list[str] = []

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def get_available_quantity(self, product_id: str) -> int | None:
        self.calls.append(product_id)
        return self.available_quantity


class FakeUserClient:
    def __init__(self, delivery_state: str | None = "Karnataka"):
        self.delivery_state = delivery_state
        self.correlation_id = ""
        self.calls: list[str] = []

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def get_delivery_address_state(self, cognito_sub: str) -> str | None:
        self.calls.append(cognito_sub)
        return self.delivery_state


class FakePricingClient:
    def __init__(self, quote: Quote | None = None):
        self.quote_result = quote or Quote(
            base_price=100.0, tax_amount=5.0, tax_rate=5.0, delivery_fee=20.0, net_payable=125.0
        )
        self.correlation_id = ""
        self.calls: list[dict] = []

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def quote(self, items, delivery_state, offer_code=None):
        self.calls.append({"items": items, "delivery_state": delivery_state, "offer_code": offer_code})
        return self.quote_result


class FakeWalletClient:
    def __init__(self, balance: int | None = 1000, raises: Exception | None = None):
        self.balance = balance
        self.raises = raises
        self.correlation_id = ""
        self.calls: list[str] = []

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def get_balance(self, cognito_sub: str) -> int:
        self.calls.append(cognito_sub)
        if self.raises:
            raise self.raises
        return self.balance


def _service(
    cart=None, available_quantity=100, delivery_state="Karnataka", quote=None,
    wallet_balance=1000, wallet_raises=None, wallet_minimum_balance=500,
) -> tuple[CartService, dict]:
    repo = FakeCartRepository(cart)
    catalog = FakeCatalogClient(available_quantity)
    user = FakeUserClient(delivery_state)
    pricing = FakePricingClient(quote)
    wallet = FakeWalletClient(wallet_balance, wallet_raises)
    service = CartService(repo, catalog, user, pricing, wallet, wallet_minimum_balance)
    return service, {"repo": repo, "catalog": catalog, "user": user, "pricing": pricing, "wallet": wallet}


def _item(id="item-1", product_id="cow-milk", quantity=1, frequency=Frequency.ONE_TIME, start_date=None):
    return LineItem(id=id, product_id=product_id, quantity=quantity, frequency=frequency,
                     start_date=start_date, added_at="2026-08-28T00:00:00Z")


# -- get_cart ---------------------------------------------------------------


def test_get_cart_empty_skips_address_and_pricing_calls():
    service, fakes = _service(cart=Cart(line_items=[], cart_version=0))

    view = service.get_cart("user-1")

    assert view.cart.line_items == []
    assert view.quote is None
    assert fakes["user"].calls == []
    assert fakes["pricing"].calls == []


def test_get_cart_with_items_calls_address_then_pricing():
    cart = Cart(line_items=[_item()], cart_version=1)
    service, fakes = _service(cart=cart)

    view = service.get_cart("user-1")

    assert view.cart is cart
    assert view.quote is not None
    assert fakes["user"].calls == ["user-1"]
    assert fakes["pricing"].calls == [
        {"items": [{"product_id": "cow-milk", "quantity": 1, "frequency": "ONE_TIME"}],
         "delivery_state": "Karnataka", "offer_code": None}
    ]


def test_get_cart_no_default_address_raises():
    cart = Cart(line_items=[_item()], cart_version=1)
    service, _ = _service(cart=cart, delivery_state=None)

    with pytest.raises(DeliveryAddressRequiredError):
        service.get_cart("user-1")


# -- add_item -----------------------------------------------------------


def test_add_item_happy_path():
    service, fakes = _service()

    result = service.add_item("user-1", "cow-milk", 2, Frequency.ONE_TIME, None, None)

    assert result.product_id == "cow-milk"
    assert len(fakes["repo"].add_item_calls) == 1
    assert fakes["wallet"].calls == []  # no wallet gate for ONE_TIME


def test_add_item_quantity_below_one_raises_without_side_effects():
    service, fakes = _service()

    with pytest.raises(ValidationError):
        service.add_item("user-1", "cow-milk", 0, Frequency.ONE_TIME, None, None)

    assert fakes["catalog"].calls == []
    assert fakes["repo"].add_item_calls == []


def test_add_item_one_time_with_start_date_raises():
    service, _ = _service()

    with pytest.raises(ValidationError):
        service.add_item("user-1", "cow-milk", 1, Frequency.ONE_TIME, "2026-09-01", None)


def test_add_item_subscription_without_start_date_raises():
    service, _ = _service()

    with pytest.raises(ValidationError):
        service.add_item("user-1", "cow-milk", 1, Frequency.DAILY, None, None)


def test_add_item_out_of_stock_raises_without_repo_write():
    service, fakes = _service(available_quantity=1)

    with pytest.raises(OutOfStockError):
        service.add_item("user-1", "cow-milk", 5, Frequency.ONE_TIME, None, None)

    assert fakes["repo"].add_item_calls == []


def test_add_item_unknown_stock_never_blocks():
    # Catalog's own available_quantity addition isn't implemented yet —
    # None must never be treated as "out of stock" regardless of quantity.
    service, _ = _service(available_quantity=None)

    service.add_item("user-1", "cow-milk", 1_000_000, Frequency.ONE_TIME, None, None)  # must not raise


def test_add_item_subscription_triggers_wallet_gate_and_passes_when_sufficient():
    service, fakes = _service(wallet_balance=1000, wallet_minimum_balance=500)

    service.add_item("user-1", "cow-milk", 1, Frequency.DAILY, "2026-09-01", None)

    assert fakes["wallet"].calls == ["user-1"]


def test_add_item_subscription_wallet_balance_too_low():
    service, _ = _service(wallet_balance=100, wallet_minimum_balance=500)

    with pytest.raises(WalletBalanceTooLowError):
        service.add_item("user-1", "cow-milk", 1, Frequency.DAILY, "2026-09-01", None)


def test_add_item_subscription_wallet_unavailable_propagates():
    # Today's real, only case — MA-100 doesn't exist (HttpWalletClient
    # always raises this).
    service, _ = _service(wallet_raises=WalletCheckUnavailableError("no wallet service"))

    with pytest.raises(WalletCheckUnavailableError):
        service.add_item("user-1", "cow-milk", 1, Frequency.DAILY, "2026-09-01", None)


# -- replace_cart -----------------------------------------------------------


def test_replace_cart_unchanged_subscription_item_not_re_gated():
    existing = _item(id="sub-1", frequency=Frequency.DAILY, start_date="2026-09-01", quantity=2)
    service, fakes = _service(cart=Cart(line_items=[existing], cart_version=3))

    service.replace_cart(
        "user-1",
        items=[
            {"id": "sub-1", "product_id": "cow-milk", "quantity": 2, "frequency": Frequency.DAILY,
             "start_date": "2026-09-01"},
        ],
        if_version=3,
    )

    assert fakes["wallet"].calls == []  # unchanged — not re-gated per FR-6's scoping


def test_replace_cart_changed_subscription_item_is_re_gated():
    existing = _item(id="sub-1", frequency=Frequency.DAILY, start_date="2026-09-01", quantity=2)
    service, fakes = _service(cart=Cart(line_items=[existing], cart_version=3), wallet_balance=100,
                               wallet_minimum_balance=500)

    with pytest.raises(WalletBalanceTooLowError):
        service.replace_cart(
            "user-1",
            items=[
                {"id": "sub-1", "product_id": "cow-milk", "quantity": 5, "frequency": Frequency.DAILY,
                 "start_date": "2026-09-01"},  # quantity changed 2 -> 5
            ],
            if_version=3,
        )


def test_replace_cart_new_subscription_item_is_gated():
    service, fakes = _service(wallet_balance=1000, wallet_minimum_balance=500)

    service.replace_cart(
        "user-1",
        items=[
            {"product_id": "cow-milk", "quantity": 1, "frequency": Frequency.DAILY, "start_date": "2026-09-01"},
        ],
        if_version=0,
    )

    assert fakes["wallet"].calls == ["user-1"]


def test_replace_cart_stock_checked_for_every_item():
    service, fakes = _service(available_quantity=1)

    with pytest.raises(OutOfStockError):
        service.replace_cart(
            "user-1",
            items=[{"product_id": "cow-milk", "quantity": 10, "frequency": Frequency.ONE_TIME}],
            if_version=0,
        )

    assert fakes["repo"].replace_cart_calls == []  # rejected before the write


def test_replace_cart_delegates_to_repository_with_same_args():
    service, fakes = _service()

    service.replace_cart(
        "user-1",
        items=[{"product_id": "cow-milk", "quantity": 1, "frequency": Frequency.ONE_TIME}],
        if_version=2,
    )

    assert len(fakes["repo"].replace_cart_calls) == 1
    assert fakes["repo"].replace_cart_calls[0]["if_version"] == 2


# -- delete_item --------------------------------------------------------


def test_delete_item_delegates_to_repository():
    service, fakes = _service()

    service.delete_item("user-1", "item-1")

    assert fakes["repo"].delete_item_calls == [{"user_id": "user-1", "line_item_id": "item-1"}]


# -- set_correlation_id -------------------------------------------------


def test_set_correlation_id_propagates_to_every_dependency():
    service, fakes = _service()

    service.set_correlation_id("corr-1")

    assert fakes["repo"].correlation_id == "corr-1"
    assert fakes["catalog"].correlation_id == "corr-1"
    assert fakes["user"].correlation_id == "corr-1"
    assert fakes["pricing"].correlation_id == "corr-1"
    assert fakes["wallet"].correlation_id == "corr-1"
