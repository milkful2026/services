import json

import pytest

import handlers.get_cart_handler as get_cart_handler
from domain.exceptions import DeliveryAddressRequiredError
from domain.models import Cart, CartView, Frequency, LineItem, Quote


class FakeCartService:
    def __init__(self, view=None, raises=None):
        self.view = view or CartView(cart=Cart(), quote=None)
        self.raises = raises
        self.calls: list[str] = []
        self.correlation_id = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def get_cart(self, user_id: str):
        self.calls.append(user_id)
        if self.raises:
            raise self.raises
        return self.view


@pytest.fixture(autouse=True)
def _reset_deps():
    get_cart_handler._deps = None
    yield
    get_cart_handler._deps = None


def _inject(view=None, raises=None):
    service = FakeCartService(view=view, raises=raises)
    get_cart_handler._deps = {"cart_service": service}
    return service


def _event(sub: str | None = "sub-123") -> dict:
    return {
        "headers": {"x-request-id": "corr-1"},
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub} if sub else {}}}},
    }


def test_empty_cart_returns_200_with_null_quote():
    _inject()

    response = get_cart_handler.handler(_event(), None)

    assert response["statusCode"] == 200
    data = json.loads(response["body"])["data"]
    assert data == {"items": [], "cartVersion": 0, "quote": None}


def test_cart_with_items_serializes_quote():
    view = CartView(
        cart=Cart(
            line_items=[
                LineItem(
                    id="item-1", product_id="cow-milk", quantity=2, frequency=Frequency.ONE_TIME,
                    start_date=None, added_at="2026-08-28T00:00:00Z",
                )
            ],
            cart_version=1,
        ),
        quote=Quote(
            base_price=136.0, tax_amount=6.8, tax_rate=5.0, delivery_fee=20.0, net_payable=162.8
        ),
    )
    _inject(view=view)

    response = get_cart_handler.handler(_event(), None)

    data = json.loads(response["body"])["data"]
    assert data["cartVersion"] == 1
    assert data["items"][0]["productId"] == "cow-milk"
    assert data["quote"]["netPayable"] == 162.8


def test_missing_jwt_claims_returns_400():
    _inject()

    response = get_cart_handler.handler(_event(sub=None), None)

    assert response["statusCode"] == 400


def test_no_default_address_returns_422():
    _inject(raises=DeliveryAddressRequiredError("no address"))

    response = get_cart_handler.handler(_event(), None)

    assert response["statusCode"] == 422
    assert json.loads(response["body"])["data"]["errorCode"] == "DELIVERY_ADDRESS_REQUIRED"
