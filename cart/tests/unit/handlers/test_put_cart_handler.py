import json

import pytest

import handlers.put_cart_handler as put_cart_handler
from domain.exceptions import CartVersionMismatchError
from domain.models import Cart, Frequency, LineItem


class FakeCartService:
    def __init__(self, cart=None, raises=None):
        self.cart = cart or Cart()
        self.raises = raises
        self.calls: list[dict] = []
        self.correlation_id = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def replace_cart(self, user_id, items, if_version):
        self.calls.append({"user_id": user_id, "items": items, "if_version": if_version})
        if self.raises:
            raise self.raises
        return self.cart


@pytest.fixture(autouse=True)
def _reset_deps():
    put_cart_handler._deps = None
    yield
    put_cart_handler._deps = None


def _inject(cart=None, raises=None):
    service = FakeCartService(cart=cart, raises=raises)
    put_cart_handler._deps = {"cart_service": service}
    return service


def _event(body: dict, sub: str | None = "sub-123") -> dict:
    return {
        "headers": {"x-request-id": "corr-1"},
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub} if sub else {}}}},
        "body": json.dumps(body),
    }


def test_happy_path_returns_updated_cart():
    cart = Cart(
        line_items=[
            LineItem(id="item-1", product_id="cow-milk", quantity=1, frequency=Frequency.ONE_TIME,
                      start_date=None, added_at="2026-08-28T00:00:00Z")
        ],
        cart_version=1,
    )
    service = _inject(cart=cart)

    response = put_cart_handler.handler(
        _event({"items": [{"productId": "cow-milk", "quantity": 1, "frequency": "ONE_TIME"}],
                "ifVersion": 0}),
        None,
    )

    assert response["statusCode"] == 200
    data = json.loads(response["body"])["data"]
    assert data["cartVersion"] == 1
    assert service.calls[0]["if_version"] == 0


def test_stale_version_returns_409():
    _inject(raises=CartVersionMismatchError("stale"))

    response = put_cart_handler.handler(
        _event({"items": [], "ifVersion": 0}), None
    )

    assert response["statusCode"] == 409
    assert json.loads(response["body"])["data"]["errorCode"] == "CART_VERSION_MISMATCH"


def test_missing_if_version_returns_400():
    _inject()

    response = put_cart_handler.handler(_event({"items": []}), None)

    assert response["statusCode"] == 400


def test_missing_jwt_claims_returns_400():
    _inject()

    response = put_cart_handler.handler(_event({"items": [], "ifVersion": 0}, sub=None), None)

    assert response["statusCode"] == 400
